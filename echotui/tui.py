"""Textual UI: courses → lectures → (video picker), plus the downloads library."""
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError

from PIL import Image as PILImage, ImageDraw, ImageEnhance, ImageOps  # comes with textual-image
from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Header, Input, OptionList, Static
from textual.worker import get_current_worker
from textual_image._terminal import get_cell_size
from textual_image.widget import Image  # import before the app starts: it probes the terminal

from . import api, model, player, store, worker

STATUS = {"upcoming": ("upcoming", "dim"), "processing": ("⏳ processing", "yellow"), "ready": ("ready", ""),
          "failed": ("✗ failed", "red"), "missing": ("– no recording", "dim"),
          "idle": ("∅ idle screen", "dim")}  # frame 10 min in shows a lectern idle screen, not a lecture
# hex, not named colours: Textual's theme remaps named greys/whites to one foreground colour
BAND = "#303030"  # background of every other week, so a week's lectures read as one group
GREY = "#626262"  # upcoming lectures and ones stuck on a lectern idle screen
WHITE = "#ffffff"  # grey/dim text on the cursor row: grey on the blue highlight is hard to read
RECENT_DAYS = 14  # how far back the launch prefetch looks for lectures to grab frames for
_REPO = Path(__file__).parents[1]  # ctrl+p → Todo appends to the repo's TODO.md in a source checkout, else DATA
TODO = (_REPO if (_REPO / "pyproject.toml").exists() else store.DATA) / "TODO.md"


class VimList(OptionList):
    BINDINGS = [Binding("j", "cursor_down", show=False), Binding("k", "cursor_up", show=False),
                Binding("l", "select", show=False), Binding("G", "last", show=False),
                Binding("ctrl+d", "half(1)", show=False), Binding("ctrl+u", "half(-1)", show=False)]
    DEFAULT_CSS = "VimList { text-wrap: nowrap; text-overflow: ellipsis; }"  # one row per item
    _last_g = 0.0

    def on_key(self, event):
        if event.key == "g":  # gg = top
            now = time.monotonic()
            if now - self._last_g < 0.5:
                self.action_first()
            self._last_g = now
            event.stop()

    def action_half(self, direction: int):
        if self.option_count:
            step = max(1, self.size.height // 2)
            self.highlighted = min(self.option_count - 1, max(0, (self.highlighted or 0) + direction * step))


class Base(Screen):
    BINDINGS = [Binding("h,escape", "back", "Back"), Binding("L", "library", "Library"),
                Binding("q", "app.quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Header(icon="")  # no ⭘ palette button: ctrl+p still opens it
        yield VimList()
        yield Footer()

    def action_back(self):
        if len(self.app.screen_stack) > 2:
            self.app.pop_screen()

    def action_library(self):
        self.app.push_screen(LibraryScreen())

    def error(self, msg: str):  # call from worker threads
        self.app.call_from_thread(self.notify, msg, severity="error")

    def fill(self, prompts: list):
        ol = self.query_one(VimList)
        ol.clear_options()
        ol.add_options(prompts)
        if prompts:
            ol.highlighted = 0
        ol.focus()


class CoursesScreen(Base):
    sections: list = []
    released: dict = {}  # section index → "Today 15:00", filled in by latest()

    def on_mount(self):
        self.title = "Courses"
        self.load()

    @work(thread=True, exclusive=True)
    def load(self):
        try:
            enr = self.app.client.get_json("/user/enrollments")[0]
        except api.AuthError as e:
            return self.error(str(e))
        except HTTPError as e:
            return self.error(f"echo360: {e}")
        except OSError:
            return self.app.call_from_thread(self.app.go_offline)
        except Exception as e:
            return self.error(f"load failed: {e}")
        self.app.call_from_thread(self.show, enr)

    def show(self, enr: dict):
        self.sections = model.order(model.current_sections(enr, date.today()), store.load()["opened"])
        self.released = {}
        self.fill([self.row(s, "") for s in self.sections])
        self.latest()
        if not self.app.prefetched:  # CoursesScreen has no 30s refresh; this guard is just a safety net
            self.app.prefetched = True
            self.app.prefetch(enr)

    def row(self, s: dict, released: str, cur: bool = False) -> Table:
        t = Table.grid(expand=True)
        t.add_column(no_wrap=True, overflow="ellipsis")
        t.add_column(justify="right", no_wrap=True)
        t.add_row(f"{s['courseCode']:<9} {s['courseName']}", Text(released, style=WHITE if cur else "dim"))
        return t

    @work(thread=True, exclusive=True, exit_on_error=False)
    def latest(self):
        """Fill in when each course's newest recording was released; quietly skip failures."""
        wk, now = get_current_worker(), datetime.now(timezone.utc)
        for i, s in enumerate(self.sections):
            if wk.is_cancelled:
                return
            try:
                syl = self.app.client.get_json(f"/section/{s['sectionId']}/syllabus")
                t = model.latest_released(model.lessons(syl, s["sectionName"], now))
            except Exception:
                continue
            if t:
                self.app.call_from_thread(self.set_released, i, model.when(t.astimezone(), now.astimezone()))

    def set_released(self, i: int, text: str):
        self.released[i] = text
        self.render_rows()

    def render_rows(self):
        ol = self.query_one(VimList)
        for i, s in enumerate(self.sections):
            ol.replace_option_prompt_at_index(i, self.row(s, self.released.get(i, ""), i == ol.highlighted))

    def on_option_list_option_highlighted(self, ev):
        self.render_rows()  # the released time turns white on the cursor row

    def on_option_list_option_selected(self, ev):
        s = self.sections[ev.option_index]
        with store.edit() as st:
            st["opened"][s["sectionId"]] = time.time()
        self.app.push_screen(LecturesScreen(s))


class LecturesScreen(Base):
    BINDINGS = [Binding("d", "queue(False)", "Download"), Binding("o", "queue(True)", "Download+open"),
                Binding("x", "delete", "Delete")]

    def __init__(self, section: dict):
        super().__init__()
        self.section, self.course, self.rows, self.idle = section, section["courseCode"], [], set()

    def on_mount(self):
        self.title = self.course
        self.load()
        self.set_interval(30, self.load)
        self.set_interval(1, self.render_rows)  # queue progress from the worker

    @work(thread=True, exclusive=True)
    def load(self):
        try:
            syl = self.app.client.get_json(f"/section/{self.section['sectionId']}/syllabus")
        except api.AuthError as e:
            return self.error(str(e))
        except OSError as e:
            return self.error(f"refresh failed: {e}")
        except Exception as e:
            return self.error(f"refresh failed: {e}")
        rows = model.lessons(syl, self.section["sectionName"], datetime.now(timezone.utc))
        idle = {r["id"] for r in rows if r["media"] and api.idle_lecture(r["media"])}
        self.app.call_from_thread(self.set_rows, *self.best(rows, idle))
        # rank duplicate recordings by size (one download-info each, cached), then re-pick
        slots = [(r["date"], r["label"], r["name"]) for r in rows]
        dups = [r for r, k in zip(rows, slots) if r["media"] and slots.count(k) > 1
                and not (store.CACHE / "frames" / f"{r['media']}.size").exists()]
        for r in dups:
            try:
                self.app.client.recording_size(r["media"])
            except Exception:
                pass
        if dups:
            self.app.call_from_thread(self.set_rows, *self.best(rows, idle))

    def best(self, rows: list, idle: set) -> tuple[list, set]:
        """One row per duplicated lecture: prefer the queued one, then not-idle, then the bigger recording."""
        queued = {q["lesson"] for q in store.load()["queue"]}

        def size(r):
            p = store.CACHE / "frames" / f"{r['media']}.size"
            return int(p.read_text()) if r["media"] and p.exists() else 0
        rows = model.collapse(rows, lambda r: (r["id"] in queued, r["id"] not in idle, size(r)))
        return model.visible(rows), idle

    def set_rows(self, rows: list, idle: set = frozenset()):
        self.rows, self.idle = rows, idle
        self.render_rows()

    def files(self, r: dict) -> list:
        return worker.lecture_files(self.course, r)

    def row_text(self, r: dict, q: dict | None, w: dict | None = None, week: int = 0, width: int = 0,
                 cur: bool = False) -> Text:
        if q and q["state"] == "downloading":
            st = (f"↓ {q.get('progress', 0):.0%}", "cyan")
        elif q and q["state"] == "failed":
            st = (f"✗ {q.get('error', 'failed')}", "red")
        elif q and q["state"] == "waiting":
            st = ("⌛ queued", "yellow")
        elif w and w.get("full"):
            st = ("● watched", "blue")
        elif w:
            st = ("◐ started", "blue")
        elif self.files(r):
            st = ("✓ downloaded", "green")
        else:
            st = STATUS["idle" if r["id"] in self.idle else r["status"]]
        grey = r["status"] == "upcoming" or r["id"] in self.idle
        muted = WHITE if cur else GREY
        dmy = "-".join(reversed(r["date"].split("-")))  # stored as yyyy-mm-dd (file names sort by it)
        t = Text(f"{dmy}  {model.short_label(r['label']):<6}  ", style=muted if grey else "")
        t.append(f"{st[0]:<16}", style=muted if grey else st[1])
        # names repeat "<section>-<label>-" and are mostly identical across lectures, so show only the rest, dimmed
        title = r["name"].removeprefix(f"{self.section['sectionName']}-").removeprefix(f"{r['label']}-")
        t.append(title, style=muted if grey else WHITE if cur else "dim")
        if week:
            t.pad_right(max(0, width - t.cell_len))  # band spans the whole row, not just the text
            t.stylize(f"on {BAND}")
        return t

    def render_rows(self):
        state = store.load()
        queue = {q["lesson"]: q for q in state["queue"]}
        watched = state.get("watched", {})
        ol = self.query_one(VimList)
        weeks = model.week_groups(self.rows)
        # cursor row: no band (a baked-in background would hide the highlight) and white instead of grey
        prompts = [self.row_text(r, queue.get(r["id"]), watched.get(f"{self.course}-{r['date']}-{r['label']}"),
                                 g and i != ol.highlighted, ol.scrollable_content_region.width, i == ol.highlighted)
                   for i, (r, g) in enumerate(zip(self.rows, weeks))]
        if ol.option_count == len(prompts):
            for i, p in enumerate(prompts):
                ol.replace_option_prompt_at_index(i, p)  # keeps the cursor where it is
        else:
            self.fill(prompts)

    def on_option_list_option_highlighted(self, ev):
        self.render_rows()  # move the band off the new cursor row and back onto the old one

    def current(self) -> dict | None:
        i = self.query_one(VimList).highlighted
        return self.rows[i] if i is not None and i < len(self.rows) else None

    def on_option_list_option_selected(self, ev):
        self.action_queue(True)

    def action_delete(self):
        """Unqueue the lecture (stopping a running download) and delete its downloaded files. No confirm:
        it can simply be downloaded again."""
        r = self.current()
        if r is None:
            return
        queued, files = any(q["lesson"] == r["id"] for q in store.load()["queue"]), self.files(r)
        if not (queued or files):
            return
        store.dequeue(r["id"])  # a running download notices on its next progress tick and stops
        for p in files:
            p.unlink(missing_ok=True)
            p.with_suffix(".srt").unlink(missing_ok=True)
        self.notify("deleted" if files else "unqueued")
        self.render_rows()

    def action_queue(self, open_after: bool):
        r = self.current()
        if r is None:
            return
        q = next((q for q in store.load()["queue"] if q["lesson"] == r["id"]), None)
        if q and q["state"] in ("waiting", "downloading"):
            return self.notify("already queued")
        if files := self.files(r):
            return player.play(files[0]) if open_after else self.notify("already downloaded")
        if r["status"] == "ready":
            self.pick(r, open_after)
        else:
            self.enqueue(r, "all", open_after)  # not ready: no download-info yet, so take every feed

    @work(thread=True, exclusive=True)
    def pick(self, r: dict, open_after: bool):
        c = self.app.client
        try:
            feeds = model.feeds(c.get_json(f"/api/ui/library/medias/{r['media']}/download-info"))
            if len(feeds) < 2:
                return self.app.call_from_thread(self.enqueue, r, model.default_choice(1, False), open_after)
            frames = c.frames(r["media"], r["id"], feeds)
            same, idle = api.same(frames), {n for n, p in enumerate(frames, 1) if api.idle_screen(p)}
        except (api.AuthError, OSError) as e:
            return self.error(str(e))
        except Exception as e:
            return self.error(f"pick failed: {e}")

        def chosen(choice):
            if choice:
                self.enqueue(r, choice, open_after)
        # build the screen on the UI thread, not in this worker thread
        self.app.call_from_thread(lambda: self.app.push_screen(
            PickerScreen(frames, model.default_choice(2, same, idle), same, idle), chosen))

    def enqueue(self, r: dict, feeds, open_after: bool):
        store.enqueue({"lesson": r["id"], "section": self.section["sectionId"],
                       "section_name": self.section["sectionName"], "course": self.course, "date": r["date"],
                       "label": r["label"], "feeds": feeds, "open": open_after, "state": "waiting"})
        worker.spawn()
        self.render_rows()


class PickerScreen(ModalScreen):
    BINDINGS = [Binding("h,left", "move(-1)", "Prev"), Binding("l,right", "move(1)", "Next"),
                Binding("space", "cycle", "Cycle"), Binding("enter", "done", "Download"),
                Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    PickerScreen { align: center middle; }
    #dialog { width: auto; height: auto; background: $surface; padding: 1 1 0 1; }
    #feeds { width: auto; height: auto; }
    .feed { width: auto; height: auto; border: round $panel; margin: 0 1; }
    .feed.cur { border: round $accent; }
    .feed.idle { opacity: 45%; }
    .preview { content-align: center middle; }
    #hint { width: 100%; content-align: center middle; color: $text-muted; margin-top: 1; }
    """
    MAX_WIDTH = 150  # columns for the whole dialog on very wide terminals
    CYCLE = {"full": "low", "low": "skip", "skip": "full"}

    def __init__(self, frames: list, choice: dict, same: bool, idle: set[int] = frozenset()):
        super().__init__()
        self.frames, self.same, self.idle, self.cur, self._shown = frames, same, idle, 0, {}
        self.q = [choice.get(str(i + 1), "skip") for i in range(len(frames))]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            with Horizontal(id="feeds"):
                for i, p in enumerate(self.frames):
                    with Vertical(classes="feed idle" if i + 1 in self.idle else "feed"):
                        yield Image(p, classes="preview") if p is not None else Static("no preview", classes="preview")
                        yield Static(id=f"lbl{i}")
            yield Static("h/l move · space full/low/skip · enter download · esc cancel", id="hint")

    def on_mount(self):
        self.fit()
        self.redraw()

    def on_resize(self):
        self.fit()

    def fit(self):
        """Size every preview to exactly 16:9 up front, so nothing reflows (or flashes) when the images load."""
        w, h = preview_size(self.app.size.width, self.app.size.height, len(self.frames), self.MAX_WIDTH)
        for p in self.query(".preview"):
            p.styles.width, p.styles.height = w, h

    def label(self, i: int) -> str:
        suffix = " (idle screen)" if i + 1 in self.idle else " (same as Video 1)" if self.same and i == 1 else ""
        return f"Video {i + 1}: {self.q[i]}{suffix}"

    def redraw(self):
        for i, box in enumerate(self.query(".feed")):
            box.set_class(i == self.cur, "cur")
            self.query_one(f"#lbl{i}", Static).update(Text(self.label(i), style="dim" if self.q[i] == "skip" else ""))
            if self.frames[i] is not None and self._shown.get(i) != self.q[i]:
                self._shown[i] = self.q[i]
                box.query_one(Image).image = look(self.frames[i], self.q[i])

    def action_move(self, d: int):
        self.cur = (self.cur + d) % len(self.frames)
        self.redraw()

    def action_cycle(self):
        self.q[self.cur] = self.CYCLE[self.q[self.cur]]
        self.redraw()

    def action_done(self):
        self.dismiss({str(i + 1): q for i, q in enumerate(self.q) if q != "skip"} or None)

    def action_cancel(self):
        self.dismiss(None)


def look(frame, q: str):
    """The preview as it should look for a choice: as-is for full, slightly pixelated for low,
    greyed and dimmed with a strike line (bottom left to top right) for skip."""
    im = PILImage.open(frame).convert("RGB")
    if q == "low":
        w, h = im.size
        return im.resize((w // 3, h // 3), PILImage.NEAREST).resize((w, h), PILImage.NEAREST)
    if q == "skip":
        im = ImageEnhance.Brightness(ImageOps.grayscale(im).convert("RGB")).enhance(0.55)
        w, h = im.size
        ImageDraw.Draw(im).line([(0, h), (w, 0)], fill=(215, 215, 215), width=max(2, w // 90))
    return im


def preview_size(cols: int, rows: int, n: int, max_width: int = 150) -> tuple[int, int]:
    """Cell size of one 16:9 preview so n of them side by side fit the terminal (with the dialog's chrome)."""
    try:
        cell = get_cell_size()
        ratio = cell.height / cell.width if cell.width and cell.height else 2.0
    except Exception:
        ratio = 2.0  # typical terminal cell: twice as tall as wide
    # per box: 2 border + 2 margin; dialog: 2 padding + 2 screen gap
    w = (min(cols, max_width) - 4) // n - 4
    h = round(w * 9 / 16 / ratio)
    cap = rows - 10  # labels, hint, borders
    if h > cap:
        h = max(3, cap)
        w = round(h * ratio * 16 / 9)
    return max(8, w), h


class ConfirmScreen(ModalScreen):
    BINDINGS = [Binding("y", "answer(True)", "Yes"), Binding("n,escape", "answer(False)", "No")]
    DEFAULT_CSS = "ConfirmScreen { align: center middle; } ConfirmScreen Static { width: auto; padding: 1 2; border: round $warning; }"

    def __init__(self, msg: str):
        super().__init__()
        self.msg = msg

    def compose(self) -> ComposeResult:
        yield Static(Text(f"{self.msg}  (y/n)"))

    def action_answer(self, ok: bool):
        self.dismiss(ok)


class TodoScreen(ModalScreen):
    """One-line box at the bottom: enter appends a todo to TODO.md, esc cancels."""
    BINDINGS = [Binding("escape", "dismiss", "Cancel")]
    DEFAULT_CSS = "TodoScreen { align: center bottom; } TodoScreen Input { dock: bottom; border: round $accent; }"

    def compose(self) -> ComposeResult:
        yield Input(placeholder="feature or fix — enter saves, esc cancels")

    def on_input_submitted(self, ev: Input.Submitted):
        if text := ev.value.strip():
            TODO.parent.mkdir(parents=True, exist_ok=True)
            with TODO.open("a") as f:
                f.write(f"- [ ] {text}\n")
            self.app.notify("saved to TODO.md")
        self.dismiss()


class LibraryScreen(Base):
    BINDINGS = [Binding("x", "delete", "Delete")]
    files: list = []

    def on_mount(self):
        self.title = "Library"
        self.reload()

    def action_library(self):
        pass

    def reload(self):
        newest = sorted(store.VIDEOS.glob("*/*.mp4"), key=lambda p: p.name, reverse=True)
        self.files = sorted(newest, key=lambda p: p.parent.name)  # stable: by course, newest first within
        self.fill([Text(f"{p.stem:<34} {p.stat().st_size / 1e6:>6.0f} MB  "
                        f"{'srt ✓' if p.with_suffix('.srt').exists() else ''}") for p in self.files])

    def on_option_list_option_selected(self, ev):
        player.play(self.files[ev.option_index])

    def action_delete(self):
        i = self.query_one(VimList).highlighted
        if i is None or i >= len(self.files):
            return
        p = self.files[i]

        def done(ok):
            if ok:
                p.unlink(missing_ok=True)
                p.with_suffix(".srt").unlink(missing_ok=True)
                self.reload()
        self.app.push_screen(ConfirmScreen(f"Delete {p.name}?"), done)


class EchoApp(App):
    TITLE = "echotui"
    prefetched = False  # set once CoursesScreen.show() has kicked off prefetch() for this app run

    def __init__(self):
        super().__init__()
        self.client = api.Client()

    def on_mount(self):
        player.cleanup()  # auto-delete watched/stale lectures whose time is up
        if any(q["state"] in ("waiting", "downloading") for q in store.load()["queue"]):
            worker.spawn()
        self.push_screen(CoursesScreen())

    def get_system_commands(self, screen):
        yield from super().get_system_commands(screen)
        yield SystemCommand("Todo", "Note a feature or fix for later", lambda: self.push_screen(TodoScreen()))

    def go_offline(self):
        self.notify("offline — showing downloads")
        self.switch_screen(LibraryScreen())

    @work(thread=True, exit_on_error=False)
    def prefetch(self, enr: dict):
        """Grab and check frames for recent lectures, so the picker opens instantly and lectures stuck
        on a lectern idle screen are tagged. Silent on any error.

        Takes enr from CoursesScreen (which already fetched it) instead of fetching enrollments
        itself, so an expired session doesn't trigger two concurrent re-logins. Checks
        is_cancelled between lectures/sections so quitting doesn't wait on a whole prefetch pass.
        """
        wk = get_current_worker()
        now = datetime.now(timezone.utc)
        for s in model.current_sections(enr, date.today()):
            if wk.is_cancelled:
                return
            try:
                syl = self.client.get_json(f"/section/{s['sectionId']}/syllabus")
            except Exception:
                continue
            for r in model.lessons(syl, s["sectionName"], now):
                if wk.is_cancelled:
                    return
                try:
                    if r["status"] != "ready":
                        continue
                    start = datetime.fromisoformat(r["start_utc"])
                    if now - start > timedelta(days=RECENT_DAYS):
                        continue
                    if worker.lecture_files(s["courseCode"], r):
                        continue  # already downloaded
                    if (store.CACHE / "frames" / f"{r['media']}-1.idle").exists():
                        continue  # grabbed and checked on an earlier launch
                    feeds = model.feeds(self.client.get_json(f"/api/ui/library/medias/{r['media']}/download-info"))
                    for p in self.client.frames(r["media"], r["id"], feeds):
                        api.idle_screen(p)
                except Exception:
                    continue


def run():
    EchoApp().run()

import asyncio
from datetime import datetime, timedelta, timezone

from textual.app import App

from echotui import model, store, tui


class ListApp(App):
    def compose(self):
        yield tui.VimList(*[str(i) for i in range(50)])


def test_vim_keys():
    async def go():
        app = ListApp()
        async with app.run_test(size=(80, 24)) as pilot:
            ol = app.query_one(tui.VimList)
            ol.focus()
            await pilot.press("j", "j")
            assert ol.highlighted == 2
            await pilot.press("G")
            assert ol.highlighted == 49
            await pilot.press("g", "g")
            assert ol.highlighted == 0
            await pilot.press("ctrl+d")
            assert ol.highlighted > 5
            await pilot.press("ctrl+u")
            assert ol.highlighted == 0
    asyncio.run(go())


class LibApp(App):
    def on_mount(self):
        self.push_screen(tui.LibraryScreen())


def test_library_delete_removes_video_and_srt():
    d = store.VIDEOS / "COSC264"
    d.mkdir(parents=True)
    (d / "COSC264-2026-09-28-LecA-s1-full.mp4").write_bytes(b"x")
    (d / "COSC264-2026-09-28-LecA-s1-full.srt").write_text("1")

    async def go():
        app = LibApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
    asyncio.run(go())
    assert list(d.iterdir()) == []


class PickerApp(App):
    def __init__(self, frames, choice, same):
        super().__init__()
        self.frames, self.choice, self.same, self.result = frames, choice, same, "unset"

    def on_mount(self):
        self.push_screen(tui.PickerScreen(self.frames, self.choice, self.same), self._set_result)

    def _set_result(self, r):
        self.result = r


def test_picker_cycles_full_low_skip_and_enter_returns_choice_after_skipping_feed_2():
    async def go():
        app = PickerApp([None, None], {"1": "full", "2": "low"}, False)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            assert screen.q == ["full", "low"]
            await pilot.press("space")
            assert screen.q[0] == "low"
            await pilot.press("space")
            assert screen.q[0] == "skip"
            await pilot.press("space")
            assert screen.q[0] == "full"
            await pilot.press("l")
            await pilot.press("space")  # feed 2: low -> skip
            assert screen.q[1] == "skip"
            await pilot.press("enter")
            await pilot.pause()
            assert app.result == {"1": "full"}
    asyncio.run(go())


def test_picker_same_starts_with_feed_2_skipped():
    async def go():
        app = PickerApp([None, None], model.default_choice(2, True), True)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.q == ["full", "skip"]
    asyncio.run(go())


class _FakeClient:
    def get_json(self, path):
        raise ValueError("bad json")


class LecturesApp(App):
    client = _FakeClient()

    def on_mount(self):
        self.push_screen(tui.LecturesScreen(
            {"sectionId": "s1", "courseCode": "COSC264", "sectionName": "COSC264-26S2"}))


def test_lectures_load_error_does_not_crash_app():
    async def go():
        app = LecturesApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.is_running
            assert isinstance(app.screen, tui.LecturesScreen)
    asyncio.run(go())


def test_queue_guard_notifies_instead_of_requeueing_a_downloading_lecture():
    store.enqueue({"lesson": "L1", "section": "s1", "section_name": "COSC264-26S2", "course": "COSC264",
                   "date": "2026-09-28", "label": "0900", "feeds": "all", "open": False, "state": "downloading"})

    async def go():
        app = LecturesApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            screen.set_rows([{"id": "L1", "name": "n", "date": "2026-09-28", "label": "0900",
                              "status": "ready", "media": "m1"}])
            notes = []
            screen.notify = lambda msg, **kw: notes.append(msg)
            await pilot.press("d")
            await pilot.pause()
            assert notes == ["already queued"]
    asyncio.run(go())
    q = store.load()["queue"]
    assert len(q) == 1 and q[0]["state"] == "downloading"  # untouched: not replaced or reopened


def _syllabus_row(start_utc: str) -> list:
    return [{"type": "SyllabusLessonType", "lesson": {
        "lesson": {"id": "L1", "name": "COSC264-26S2-LecA-Intro", "timing": {"start": "2026-09-28T15:00:00.000"}},
        "medias": [{"id": "m1", "mediaType": "Video", "isAvailable": True, "isProcessing": False,
                    "isFailed": False}],
        "isFuture": False, "startTimeUTC": start_utc, "endTimeUTC": start_utc}}]


class _FakePrefetchClient:
    def __init__(self, syl):
        self.syl, self.calls = syl, []

    def get_json(self, path):
        self.calls.append(path)
        return self.syl if path.endswith("/syllabus") else {}

    def frames(self, media, lesson, feeds):
        return [None, None]


class PrefetchApp(App):
    # Textual dispatches on_mount up the whole MRO (not just the most-derived one), so subclassing
    # EchoApp would still run its on_mount (worker.spawn(), push CoursesScreen) alongside ours.
    # Borrow the decorated method instead, onto a plain App that only has what prefetch() needs.
    prefetch = tui.EchoApp.__dict__["prefetch"]

    def __init__(self, client, enr):
        super().__init__()
        self.client, self._enr, self.worker_obj = client, enr, None

    def on_mount(self):
        self.worker_obj = self.prefetch(self._enr)


def test_prefetch_skips_download_info_when_frames_already_cached():
    enr = {"termsById": {"t1": {"name": "FY", "isActive": True}},
           "userSections": [{"sectionId": "s1", "sectionName": "COSC264-26S2", "courseCode": "COSC264",
                             "termId": "t1"}]}
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    frames_dir = store.CACHE / "frames"
    frames_dir.mkdir(parents=True)
    (frames_dir / "m1-1.idle").write_text("ok")  # grabbed and checked on an earlier launch
    c = _FakePrefetchClient(_syllabus_row(recent))

    async def go():
        app = PrefetchApp(c, enr)
        async with app.run_test():
            await app.worker_obj.wait()
    asyncio.run(go())
    assert c.calls == ["/section/s1/syllabus"]  # no download-info fetch: frames were already checked


class _FakeSingleFeedClient:
    def __init__(self, syl):
        self.syl, self.calls = syl, []

    def get_json(self, path):
        self.calls.append(path)
        if path.endswith("/syllabus"):
            return self.syl
        return {"primaryFiles": {"files": [{"fileName": "hd1.mp4", "label": "Full"}]}}

    def frames(self, media, lesson, feeds):
        assert len(feeds) == 1
        p = store.CACHE / "frames" / f"{media}-1.jpg"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"not a real jpg")  # ffmpeg can't compare it, so it's checked as "ok"
        return [p]


def test_prefetch_checks_single_feed_lectures_once():
    enr = {"termsById": {"t1": {"name": "FY", "isActive": True}},
           "userSections": [{"sectionId": "s1", "sectionName": "COSC264-26S2", "courseCode": "COSC264",
                             "termId": "t1"}]}
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    c = _FakeSingleFeedClient(_syllabus_row(recent))

    async def go():
        app = PrefetchApp(c, enr)
        async with app.run_test():
            await app.worker_obj.wait()
    asyncio.run(go())
    assert c.calls == ["/section/s1/syllabus", "/api/ui/library/medias/m1/download-info"]
    assert (store.CACHE / "frames" / "m1-1.idle").read_text() == "ok"

    c2 = _FakeSingleFeedClient(_syllabus_row(recent))

    async def go2():
        app = PrefetchApp(c2, enr)
        async with app.run_test():
            await app.worker_obj.wait()
    asyncio.run(go2())
    assert c2.calls == ["/section/s1/syllabus"]  # m1 was checked last launch: no re-fetch


class CoursesApp(App):
    prefetched = True  # CoursesScreen.show() skips starting the launch prefetch

    def __init__(self, client):
        super().__init__()
        self.client = client

    def on_mount(self):
        self.push_screen(tui.CoursesScreen())


def test_courses_show_last_released_right_aligned_without_lesson_count():
    start = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    enr = [{"termsById": {"t": {"name": "FY", "isActive": True}},
            "userSections": [{"sectionId": "s1", "sectionName": "COSC264-26S2", "courseCode": "COSC264",
                              "courseName": "Networks", "lessonCount": 29, "termId": "t"}]}]

    class Client(_FakePrefetchClient):
        def get_json(self, path):
            return enr if path == "/user/enrollments" else super().get_json(path)

    async def go():
        app = CoursesApp(Client(_syllabus_row(start)))
        async with app.run_test(size=(60, 10)) as pilot:
            for _ in range(20):
                await pilot.pause(0.05)
            row = app.screen.query_one(tui.VimList).render_line(0).text
            t = datetime.fromisoformat(start).astimezone()
            assert row.startswith("COSC264   Networks") and "(29)" not in row
            assert row.rstrip().endswith(model.when(t, datetime.now().astimezone()))
    asyncio.run(go())


def test_courses_tick_a_watched_newest_lecture_and_list_a_timeline():
    now = datetime.now(timezone.utc)

    def lecture(lid, label, t):
        x = _syllabus_row(t.strftime("%Y-%m-%dT%H:%M:%S.000Z"))[0]
        x["lesson"]["lesson"].update(id=lid, name=f"COSC264-26S2-{label}-x",
                                     timing={"start": t.astimezone().strftime("%Y-%m-%dT%H:%M:%S.000")})
        return x
    a = lecture("L1", "LecA", now - timedelta(minutes=1))
    nxt = lecture("L2", "LecB", now + timedelta(days=1))
    nxt["lesson"]["isFuture"], nxt["lesson"]["medias"] = True, []
    old = lecture("L0", "LecZ", now - timedelta(days=30))
    enr = [{"termsById": {"t": {"name": "FY", "isActive": True}},
            "userSections": [{"sectionId": "s1", "sectionName": "COSC264-26S2", "courseCode": "COSC264",
                              "courseName": "Networks", "termId": "t"}]}]
    with store.edit() as s:
        s["watched"] = {f"COSC264-{(now - timedelta(minutes=1)).astimezone():%Y-%m-%d}-LecA": {"last": 1, "full": True}}

    class Client(_FakePrefetchClient):
        def get_json(self, path):
            return enr if path == "/user/enrollments" else super().get_json(path)

    async def go():
        app = CoursesApp(Client([a, nxt, old]))
        async with app.run_test(size=(70, 10)) as pilot:
            for _ in range(20):
                await pilot.pause(0.05)
            ol = app.screen.query_one(tui.VimList)
            lines = [ol.render_line(i).text for i in range(4)]
            assert "✓ " in lines[0]  # newest released lecture (LecA) is watched
            assert lines[1].strip() == "" and lines[2].strip() == "Timeline"  # a gap above the timeline
            assert "LecA" in lines[3] and "● watched" in lines[3]
            assert ol.option_count == 4  # tomorrow's LecB and the month-old LecZ are left out
            await pilot.press("j")  # skips the gap and heading, onto the last row...
            await pilot.pause()
            assert ol.highlighted == 3
            assert ol.option_count == 5 and "LecZ" in ol.render_line(4).text  # ...which loads older lectures
            await pilot.press("enter")
            for _ in range(10):
                await pilot.pause(0.05)
            # LecA sits under the upcoming LecB on the lectures screen: the cursor jumped to it
            assert isinstance(app.screen, tui.LecturesScreen) and app.screen.current()["id"] == "L1"
    asyncio.run(go())


def test_lecture_rows_stay_on_one_line_without_repeated_prefix():
    long = "COSC264-26S2-LecA-Introduction to Computer Networks and the Internet, part one of many"
    syl = _syllabus_row("2026-09-28T02:00:00.000Z")
    syl[0]["lesson"]["lesson"]["name"] = long

    async def go():
        app = LecturesApp()
        app.client = _FakePrefetchClient(syl)
        async with app.run_test(size=(70, 10)) as pilot:
            for _ in range(10):
                await pilot.pause(0.05)
            ol = app.screen.query_one(tui.VimList)
            assert ol.option_count == 1
            row = ol.render_line(0).text
            assert "COSC264-26S2" not in row and "Introduction" in row
            assert "Networks" not in ol.render_line(1).text  # no wrapped continuation line
    asyncio.run(go())


def test_x_unqueues_a_queued_lecture():
    store.enqueue({"lesson": "L1", "state": "waiting"})

    async def go():
        app = LecturesApp()
        app.client = _FakePrefetchClient(_syllabus_row("2026-09-28T02:00:00.000Z"))
        async with app.run_test() as pilot:
            for _ in range(10):
                await pilot.pause(0.05)
            await pilot.press("x")
            await pilot.pause()
    asyncio.run(go())
    assert store.load()["queue"] == []


def test_x_deletes_downloaded_files_and_unqueues():
    d = store.VIDEOS / "COSC264"
    d.mkdir(parents=True)
    full, low = d / "COSC264-2026-09-28-LecA-s1-full.mp4", d / "COSC264-2026-09-28-LecA-s2-low.mp4"
    for p in (full, low):
        p.write_bytes(b"v")
    full.with_suffix(".srt").write_text("1")
    store.enqueue({"lesson": "L1", "state": "downloading"})

    async def go():
        app = LecturesApp()
        app.client = _FakePrefetchClient(_syllabus_row("2026-09-28T02:00:00.000Z"))
        async with app.run_test() as pilot:
            for _ in range(10):
                await pilot.pause(0.05)
            await pilot.press("x")
            await pilot.pause()
    asyncio.run(go())
    assert list(d.iterdir()) == [] and store.load()["queue"] == []


def test_lecture_on_idle_screen_is_greyed_and_tagged():
    frames = store.CACHE / "frames"
    frames.mkdir(parents=True)
    (frames / "m1-1.idle").write_text("idle")

    async def go():
        app = LecturesApp()
        app.client = _FakePrefetchClient(_syllabus_row("2026-09-28T02:00:00.000Z"))
        async with app.run_test(size=(90, 10)) as pilot:
            for _ in range(10):
                await pilot.pause(0.05)
            scr = app.screen
            text = scr.row_text(scr.rows[0], None)
            assert "∅ idle screen" in text.plain
            assert {str(span.style) for span in text.spans} | {str(text.style)} <= {tui.GREY, ""}
            cur = scr.row_text(scr.rows[0], None, cur=True)  # on the blue highlight: white, not grey
            assert {str(span.style) for span in cur.spans} | {str(cur.style)} <= {tui.WHITE, ""}
    asyncio.run(go())


def test_lecture_rows_show_weekday_and_date(monkeypatch):
    class Day(tui.date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 20)
    monkeypatch.setattr(tui, "date", Day)

    async def go():
        app = LecturesApp()
        app.client = _FakePrefetchClient(_syllabus_row("2026-09-28T02:00:00.000Z"))
        async with app.run_test(size=(90, 10)) as pilot:
            for _ in range(10):
                await pilot.pause(0.05)
            assert app.screen.query_one(tui.VimList).render_line(0).text.startswith("Mon 28-09  LecA")
    asyncio.run(go())


def _two_week_syllabus():
    rows = []
    for i, (d, lec) in enumerate([("2026-09-28", "LecA"), ("2026-09-25", "LecC"), ("2026-09-22", "LecB")]):
        r = _syllabus_row(f"{d}T02:00:00.000Z")[0]
        r["lesson"]["lesson"].update(id=f"L{i}", name=f"COSC264-26S2-{lec}-Intro", timing={"start": f"{d}T15:00:00.000"})
        r["lesson"]["medias"][0]["id"] = f"m{i}"
        rows.append(r)
    return rows


def test_lecture_rows_band_weeks_and_show_watched():
    with store.edit() as s:
        s["watched"] = {"COSC264-2026-09-28-LecA": {"last": 1, "full": True},
                        "COSC264-2026-09-25-LecC": {"last": 1, "full": False}}
    d = store.VIDEOS / "COSC264"
    d.mkdir(parents=True)
    for k in ("2026-09-28-LecA", "2026-09-25-LecC"):
        (d / f"COSC264-{k}-s1-full.mp4").touch()

    async def go():
        app = LecturesApp()
        app.client = _FakePrefetchClient(_two_week_syllabus())
        async with app.run_test(size=(90, 10)) as pilot:
            for _ in range(10):
                await pilot.pause(0.05)
            ol = app.screen.query_one(tui.VimList)
            ol.highlighted = 0  # cursor row takes highlight colours; compare the two rows of the other week
            await pilot.pause()
            week40, a, b = (ol.render_line(i) for i in range(3))  # no separator lines between weeks
            assert "● watched" in week40.text and "◐ started" in a.text and "ready" in b.text
            gone = app.screen.row_text(app.screen.rows[2], None, {"full": True})  # watched, then deleted
            assert "watched" in gone.plain and "●" not in gone.plain
            assert "blue" not in {str(sp.style) for sp in gone.spans}  # plain like ready

            def bg(strip):
                return {seg.style.bgcolor.name for seg in strip if seg.style and seg.style.bgcolor}
            assert bg(a) == bg(b) == {tui.BAND}  # 25 and 22 Sep: same ISO week, banded right across the row
            ol.highlighted = 2
            await pilot.pause()
            assert tui.BAND not in bg(ol.render_line(0))  # 28 Sep: the other week, not banded
            assert tui.BAND not in bg(ol.render_line(2))  # the cursor row keeps the highlight colour
    asyncio.run(go())



def test_duplicate_recordings_collapse_to_the_non_idle_then_bigger_one():
    def rec(i):
        r = _syllabus_row("2026-09-24T01:00:00.000Z")[0]
        r["lesson"]["lesson"].update(id=f"L{i}", name="ENCE260-26S2-LecB-Computer Systems",
                                     timing={"start": "2026-09-24T13:00:00.000"})
        r["lesson"]["medias"][0]["id"] = f"m{i}"
        return r
    frames = store.CACHE / "frames"
    frames.mkdir(parents=True)

    class Sized(_FakePrefetchClient):
        def recording_size(self, media):
            (frames / f"{media}.size").write_text({"m1": "100", "m2": "300", "m3": "200"}[media])

    async def rows_shown():
        app = LecturesApp()
        app.client = Sized([rec(1), rec(2), rec(3)])
        async with app.run_test(size=(90, 10)) as pilot:
            for _ in range(10):
                await pilot.pause(0.05)
            return [r["id"] for r in app.screen.rows]

    assert asyncio.run(rows_shown()) == ["L2"]  # biggest recording wins
    (frames / "m2-1.idle").write_text("idle")
    assert asyncio.run(rows_shown()) == ["L3"]  # an idle-screen duplicate loses to any real one


def test_preview_size_is_16_9_and_fits(monkeypatch):
    from textual_image._terminal import CellSize
    monkeypatch.setattr(tui, "get_cell_size", lambda: CellSize(10, 20))  # cells twice as tall as wide
    w, h = tui.preview_size(200, 60, 2)
    assert w == (150 - 4) // 2 - 4 and abs(w / (2 * h) - 16 / 9) < 0.1  # capped at the dialog max width
    w, h = tui.preview_size(200, 20, 2)
    assert h == 10 and abs(w / (2 * h) - 16 / 9) < 0.1  # short terminal: height-bound, still 16:9


def test_picker_previews_have_fixed_size_before_images_load():
    async def go():
        app = PickerApp([None, None], model.default_choice(2, False), False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            prev = list(app.screen.query(".preview"))
            assert len(prev) == 2 and all(p.size.height > 5 for p in prev)
            assert len({p.size for p in prev}) == 1
    asyncio.run(go())


def test_look_pixelates_low_and_greys_and_crosses_skip(tmp_path):
    from PIL import Image as PILImage
    src = tmp_path / "f.jpg"
    PILImage.radial_gradient("L").convert("RGB").resize((320, 180)).save(src)
    orig = PILImage.open(src).convert("RGB")
    full, low, skip = (tui.look(src, q) for q in ("full", "low", "skip"))
    assert full.tobytes() == orig.tobytes()
    assert low.size == orig.size and low.getpixel((3, 3)) == low.getpixel((5, 5)) and low != orig  # 3px blocks
    r, g, b = skip.getpixel((200, 20))
    assert r == g == b  # greyscale
    assert skip.getpixel((160, 90)) == (215, 215, 215)  # centre of the strike line
    assert skip.getpixel((40, 22)) != (215, 215, 215)  # top-left to bottom-right diagonal: no line


def test_todo_box_appends_on_enter_and_escape_cancels(tmp_path, monkeypatch):
    todo = tmp_path / "TODO.md"
    monkeypatch.setattr(tui, "TODO", todo)

    async def go():
        app = App()
        async with app.run_test() as pilot:
            app.push_screen(tui.TodoScreen())
            await pilot.pause()
            await pilot.press(*"fix x", "enter")
            assert todo.read_text() == "- [ ] fix x\n"
            app.push_screen(tui.TodoScreen())
            await pilot.pause()
            await pilot.press("y", "escape")
            await pilot.pause()
            assert todo.read_text() == "- [ ] fix x\n"
            assert not isinstance(app.screen, tui.TodoScreen)
    asyncio.run(go())


def test_palette_lists_todo_command():
    app = tui.EchoApp.__new__(tui.EchoApp)
    App.__init__(app)  # skip EchoApp.__init__: no api client needed to list commands
    assert "Todo" in [c.title for c in app.get_system_commands(tui.Screen())]


GRUVBOX = """system: "base16"
name: "Gruvbox dark, hard"
variant: "dark"
palette:
  base00: "#1d2021" # ----
  base01: "#3c3836"
  base02: "#504945"
  base03: "#665c54"
  base04: "#bdae93"
  base05: "#d5c4a1"
  base06: "#ebdbb2"
  base07: "#fbf1c7"
  base08: "#fb4934"
  base09: "#fe8019"
  base0A: "#fabd2f"
  base0B: "#b8bb26"
  base0C: "#8ec07c"
  base0D: "#83a598"
  base0E: "#d3869b"
  base0F: "#d65d0e"
"""


class TintyApp(App):
    follow_tinty = tui.EchoApp.follow_tinty
    _tinty = None


def test_app_follows_the_tinty_scheme(tmp_path, monkeypatch):
    for k in ("BAND", "GREY", "WHITE"):
        monkeypatch.setattr(tui, k, getattr(tui, k))  # restored after the test
    monkeypatch.setattr(tui, "TINTY", tmp_path)
    assert tui.tinty_palette() is None  # no tinty: keep the default theme
    (tmp_path / "repos/schemes/base16").mkdir(parents=True)
    (tmp_path / "repos/schemes/base16/gruvbox-dark-hard.yaml").write_text(GRUVBOX)
    (tmp_path / "current_scheme").write_text("base16-gruvbox-dark-hard")

    async def go():
        app = TintyApp()
        async with app.run_test():
            app.follow_tinty()
            assert app.theme == "base16-gruvbox-dark-hard" and app.current_theme.background == "#1d2021"
            assert app.ansi_theme.ansi_colors[4].hex == "#83a598"  # ANSI blue = base0D
            assert (tui.BAND, tui.GREY, tui.WHITE) == ("#3c3836", "#665c54", "#fbf1c7")
    asyncio.run(go())

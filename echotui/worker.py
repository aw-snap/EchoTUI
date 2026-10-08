"""Detached background process: wait for lectures, download them, pre-caption the first 5 minutes."""
import fcntl
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import api, model, player, store

POLL = 300  # seconds between re-checks of a lecture that is still processing
PREPASS = 300  # seconds of audio captioned ahead of time
MODEL = Path.home() / ".config/mpv/ggml-small.en.bin"
_bad: set[Path] = set()  # files whose pre-pass failed; not retried this run
_busy: set[Path] = set()  # files being captioned right now; one pre-pass per file at a time
_busy_lock = threading.Lock()


def video_path(course: str, row: dict, n: int, q: str) -> Path:
    return store.VIDEOS / course / model.video_name(course, row["date"], row["label"], n, q)


def lecture_files(course: str, row: dict) -> list[Path]:
    d = store.VIDEOS / course
    return sorted(d.glob(f"{course}-{row['date']}-{row['label']}-s*.mp4"))


def notify(msg: str):
    try:
        subprocess.run(["notify-send", "echo360", msg], check=False)
    except OSError:
        pass


def spawn():
    """Start a worker if none is running (a second one exits at once on the lock)."""
    store.DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = open(store.DATA / "worker.log", "a")
    subprocess.Popen([sys.executable, "-m", "echotui", "worker"], start_new_session=True, cwd=Path.home(),
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log)  # home: the launch dir may vanish


class Unqueued(Exception):
    """The user removed the item from the queue while it was downloading."""


def _progress(lesson: str, total: int, done: int):
    last = [-1]

    def cb(frac: float):
        pct = int((done + frac) / total * 100)
        if pct != last[0]:
            last[0] = pct
            if not any(q["lesson"] == lesson for q in store.load()["queue"]):
                raise Unqueued
            store.update(lesson, progress=pct / 100)
    return cb


def step(client, item: dict):
    syl = client.get_json(f"/section/{item['section']}/syllabus")
    now = datetime.now(timezone.utc)
    row = next((r for r in model.lessons(syl, item["section_name"], now) if r["id"] == item["lesson"]), None)
    when = f"{item['course']} {item['label']} {datetime.strptime(item['date'], '%Y-%m-%d'):%-d %b}"
    if row is None or row["status"] in ("failed", "missing"):
        error = "failed on echo360" if row and row["status"] == "failed" else "no recording"
        store.update(item["lesson"], state="failed", error=error)
        notify(f"{when}: {error}")
        return
    if row["status"] != "ready":
        return
    store.update(item["lesson"], state="downloading", progress=0)
    feeds = model.feeds(client.get_json(f"/api/ui/library/medias/{row['media']}/download-info"))
    choice = item["feeds"]
    if isinstance(choice, list):  # old tui.py wrote a list like [1, 2]; treat it as "all"
        choice = "all"
    if choice == "all":
        frames = client.frames(row["media"], row["id"], feeds)
        idle = {n for n, p in enumerate(frames, 1) if api.idle_screen(p)}
        choice = model.default_choice(len(feeds), api.same(frames), idle)
    by_n = {f["n"]: f for f in feeds}
    picks = [(n, q) for n, q in choice.items() if int(n) in by_n]
    if not picks:
        store.update(item["lesson"], state="failed", error="feed missing")
        notify(f"{when}: feed missing")
        return
    paths = []
    for n, q in picks:
        f = by_n[int(n)]
        dest = video_path(item["course"], row, int(n), q)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            if q == "full" and MODEL.exists() and not dest.with_suffix(".srt").exists():
                # the low file has the same audio with far less video to pull alongside the download
                caption_live(dest, client.signed_url(row["media"], model.pick_file(f, "low"), row["id"]))
            url = client.signed_url(row["media"], model.pick_file(f, q), row["id"])
            try:
                client.fetch(url, dest, progress=_progress(item["lesson"], len(picks), len(paths)))
            except Unqueued:
                dest.with_name(dest.name + ".part").unlink(missing_ok=True)
                dest.with_suffix(".srt").unlink(missing_ok=True)  # the live pre-pass may have finished first
                return
        paths.append(dest)
    store.dequeue(item["lesson"])
    notify(f"{when} downloaded")
    if item["open"] and paths:
        player.play(paths[0])


def _process(client, tried: dict, poll: float):
    now = time.monotonic()
    for item in store.load()["queue"]:
        if item["state"] != "waiting" or now - tried.get(item["lesson"], float("-inf")) < poll:
            continue
        tried[item["lesson"]] = now
        try:
            step(client, item)
        except api.AuthError:
            store.update(item["lesson"], state="failed", error="login failed")
            notify("login failed — run `echotui login`")
        except Exception as e:  # network, 5xx, odd JSON: keep waiting, retry next poll
            store.update(item["lesson"], state="waiting")
            print(f"{item['lesson']}: {e!r}", flush=True)
        if not any(q["lesson"] == item["lesson"] and q["state"] == "waiting" for q in store.load()["queue"]):
            tried.pop(item["lesson"])  # done, failed or unqueued: a re-queue must not wait out the poll


def run_pausable(cmd: list[str], tick: float = 1.0):
    """Run cmd, freezing it (SIGSTOP) while any echotui mpv is open and resuming (SIGCONT) after."""
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stopped = False
    while p.poll() is None:
        if player.active() != stopped:
            stopped = not stopped
            p.send_signal(signal.SIGSTOP if stopped else signal.SIGCONT)
        time.sleep(tick)
    if p.returncode:
        raise subprocess.CalledProcessError(p.returncode, cmd)


def pending_subs() -> list[Path]:
    return [p for p in sorted(store.VIDEOS.glob("*/*.mp4"))
            if not p.name.endswith("-low.mp4") and not p.with_suffix(".srt").exists()
            and p not in _bad and p not in _busy]


def _claim(mp4: Path) -> bool:
    with _busy_lock:
        if mp4 in _busy:
            return False
        _busy.add(mp4)
        return True


def make_srt(mp4: Path, src: str | None = None):
    """Caption the first PREPASS seconds of mp4, reading the audio from src (a URL) if given."""
    with tempfile.TemporaryDirectory() as d:
        wav = Path(d) / "a.wav"
        # not pausable: a URL read frozen for a whole lecture would time out and lose the pre-pass
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-rw_timeout", "30000000", "-i", src or str(mp4),
                        "-t", str(PREPASS), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        run_pausable(["whisper-cli", "-m", str(MODEL), "-f", str(wav), "-osrt", "-of", str(Path(d) / "a")])
        # deleted from the library or unqueued meanwhile → drop the result
        if mp4.exists() or mp4.with_name(mp4.name + ".part").exists():
            shutil.move(Path(d) / "a.srt", mp4.with_suffix(".srt"))  # only a finished srt ever lands


def caption_live(mp4: Path, url: str):
    """Pre-caption mp4 from its URL in a thread, so the srt is ready about when the download is."""
    if not _claim(mp4):
        return

    def run():
        try:
            make_srt(mp4, url)
        except (OSError, subprocess.CalledProcessError) as e:  # subtitle_pass retries from the local file
            print(f"live srt {mp4.name}: {e!r}", flush=True)
        finally:
            _busy.discard(mp4)
    threading.Thread(target=run).start()


def subtitle_pass():
    for mp4 in pending_subs():
        while player.active():
            time.sleep(1)
        if mp4.with_suffix(".srt").exists() or not _claim(mp4):  # done or started since the list was made
            continue
        try:
            make_srt(mp4)
        except (OSError, subprocess.CalledProcessError) as e:
            _bad.add(mp4)
            print(f"srt {mp4.name}: {e!r}", flush=True)
        finally:
            _busy.discard(mp4)


def _waiting() -> bool:
    return any(q["state"] == "waiting" for q in store.load()["queue"])


def run_until_idle(client, poll: float = POLL, tick: float = 1.0):
    tried: dict = {}
    subs = None
    while True:
        _process(client, tried, poll)
        if (subs is None or not subs.is_alive()) and pending_subs() and not player.active():
            subs = threading.Thread(target=subtitle_pass)  # own thread so a frozen whisper can't block downloads
            subs.start()
        if not (subs and subs.is_alive()) and not _busy and not pending_subs() and not _waiting():
            return
        time.sleep(tick)


def main():
    store.DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(store.DATA / "worker.lock", "w") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return  # another worker owns the queue
            with store.edit() as s:  # a killed worker leaves items mid-download
                for q in s["queue"]:
                    if q["state"] == "downloading":
                        q["state"] = "waiting"
            player.cleanup()  # also frees space before new downloads land
            run_until_idle(api.Client())
            fcntl.flock(lock, fcntl.LOCK_UN)
            # an item queued while we were exiting would find the lock held and its spawn would quit
            if not _waiting() and not pending_subs():
                return

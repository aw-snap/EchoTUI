"""Launch mpv detached, tell whether any echotui-launched mpv is still open, and track what was watched."""
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import store

_DEVNULL = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
FULL_MARGIN = 600  # closing mpv within the last 10 minutes counts as fully watched
FULL_TTL, PARTIAL_TTL, UNWATCHED_TTL = 3600, 4 * 3600, 14 * 86400  # auto-delete delays (seconds)


def _pids() -> Path:
    return store.DATA / "mpv"


def lecture_key(path: Path) -> str:
    """`COSC264-2026-09-28-LecA` for any feed/quality file of that lecture."""
    return re.sub(r"-s\d+-(full|low)$", "", path.stem)


def partner(path: Path) -> tuple[Path, Path | None]:
    prefix = lecture_key(path)
    siblings = sorted(path.parent.glob(f"{prefix}-s*-*.mp4"))
    fulls = [s for s in siblings if s.stem.endswith("-full")]
    lows = [s for s in siblings if s.stem.endswith("-low")]
    main = path if path.stem.endswith("-full") else (fulls[0] if fulls else path)
    low = next((s for s in lows if s != main), None)
    return main, low


def play(path: Path):
    """Open a lecture through the `echotui pip` helper, which syncs a low partner and records watching."""
    main, low = partner(path)
    # cwd=home: whisper-cli (run by the mpv script) aborts if it inherits a deleted working directory
    subprocess.Popen([sys.executable, "-m", "echotui", "pip", str(main)] + ([str(low)] if low else []),
                     start_new_session=True, cwd=Path.home(), **_DEVNULL)


def _record(key: str, **fields):
    with store.edit() as s:
        s.setdefault("watched", {}).setdefault(key, {}).update(fields)


def finish(key: str, pos: float | None, dur: float | None) -> bool:
    """Record that mpv closed at pos of dur seconds; returns whether that counts as fully watched."""
    full = bool(dur and pos is not None and pos >= dur - FULL_MARGIN)
    _record(key, pid=None, last=time.time(), pos=pos, dur=dur, full=full)
    return full


def cleanup(now: float | None = None):
    """Delete lectures 1 h after a full watch, 4 h after a partial one, or 2 weeks after download if unwatched."""
    now = time.time() if now is None else now
    watched = store.load().get("watched", {})
    for mp4 in store.VIDEOS.glob("*/*.mp4"):
        w = watched.get(lecture_key(mp4))
        try:
            got = mp4.stat().st_mtime  # download finished (.part renamed) or re-downloaded
        except OSError:
            continue
        if w and w.get("pid") and _alive(w["pid"]):
            continue  # open in mpv right now
        if w and w.get("last", 0) >= got:
            ttl, since = (FULL_TTL if w.get("full") else PARTIAL_TTL), w["last"]
        else:
            ttl, since = UNWATCHED_TTL, got
        if now - since > ttl:
            mp4.unlink(missing_ok=True)
            mp4.with_suffix(".srt").unlink(missing_ok=True)


def _ipc(sock: str, *command) -> Any | None:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect(sock)
            s.sendall(json.dumps({"command": list(command)}).encode() + b"\n")
            with s.makefile("rb") as f:
                for line in f:
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "error" in msg:
                        return msg.get("data")
    except OSError:
        return None
    return None


_CROP_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")


def _crop(ffmpeg_stderr: str) -> str | None:
    matches = _CROP_RE.findall(ffmpeg_stderr)
    if not matches:
        return None
    w, h, x, y = matches[-1]
    return f"{w}x{h}+{x}+{y}"


def pip(main: Path, low: Path | None = None):
    """Play main (with a synced, muted, cropped low partner if given), record how far it got, then
    stay around until its auto-delete time and run cleanup."""
    key, pos, dur = lecture_key(main), None, None
    tmp = tempfile.mkdtemp()
    a, b = f"{tmp}/a", f"{tmp}/b"
    with store.edit() as s:  # check and claim under one lock: a second open would run a second live whisper
        w = s.setdefault("watched", {}).setdefault(key, {})
        if w.get("pid") and _alive(w["pid"]):
            shutil.rmtree(tmp, ignore_errors=True)
            return  # already open
        full = subprocess.Popen(
            ["mpv", "--script-opts=autocaption-auto=yes", f"--input-ipc-server={a}", str(main)],
            start_new_session=True, **_DEVNULL)
        w.update(pid=full.pid, last=time.time())
    _pids().mkdir(parents=True, exist_ok=True, mode=0o700)
    pidfile = _pids() / str(full.pid)
    pidfile.touch()
    if low:
        try:
            detect = subprocess.run(
                ["ffmpeg", "-ss", "60", "-i", str(low), "-t", "2", "-vf", "cropdetect", "-f", "null", "-"],
                capture_output=True, text=True, errors="replace")
            crop = _crop(detect.stderr)
        except OSError:
            crop = None
        low_cmd = ["mpv", "--no-audio", "--wayland-app-id=mpv-low", f"--input-ipc-server={b}"]
        if crop:
            low_cmd.append(f"--video-crop={crop}")
        low_cmd += ["--autofit=560x560", str(low)]
        subprocess.Popen(low_cmd, start_new_session=True, **_DEVNULL)
    try:
        while _alive(full.pid):
            time.sleep(0.5)
            t = _ipc(a, "get_property", "time-pos")
            if t is not None:
                pos = t
                dur = _ipc(a, "get_property", "duration") or dur
            if not low:
                continue
            p = _ipc(a, "get_property", "pause")
            s = _ipc(a, "get_property", "speed")
            lt = _ipc(b, "get_property", "time-pos")
            if t is not None and lt is not None:
                _ipc(b, "set_property", "pause", p)
                _ipc(b, "set_property", "speed", s)
                if abs(t - lt) > 0.5:
                    _ipc(b, "seek", t, "absolute")
        if low:
            _ipc(b, "quit")
    finally:
        pidfile.unlink(missing_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)
        full_watch = finish(key, pos, dur)
    # ponytail: a sleeping helper per closed lecture; TUI launch and the worker also run cleanup() as a backstop
    time.sleep((FULL_TTL if full_watch else PARTIAL_TTL) + 5)
    cleanup()


def _alive(pid: int, name: str = "mpv") -> bool:
    # /proc rather than kill(0): an exited-but-unreaped mpv is a zombie and would look alive,
    # and checking the name guards against pid reuse.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return f"({name})" in stat and stat.rsplit(")", 1)[1].split()[0] != "Z"


def active() -> bool:
    alive = False
    for f in _pids().glob("*") if _pids().exists() else []:
        if f.name.isdigit() and _alive(int(f.name)):
            alive = True
        else:
            f.unlink(missing_ok=True)
    return alive

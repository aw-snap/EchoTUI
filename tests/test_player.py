import os
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

from echotui import player, store


CROPDETECT_STDERR = """\
[Parsed_cropdetect_0 @ 0x1] x1:0 x2:1279 y1:0 y2:527 w:1280 h:528 x:0 y:0 pts:100 t:1.000000 crop=1280:528:0:0
[Parsed_cropdetect_0 @ 0x1] x1:0 x2:1279 y1:2 y2:519 w:1280 h:520 x:0 y:4 pts:200 t:2.000000 crop=1280:520:0:4
"""


def test_crop_last_line_wins():
    assert player._crop(CROPDETECT_STDERR) == "1280x520+0+4"


def test_crop_none_when_no_crop_line():
    assert player._crop("nothing useful here\n") is None


def test_partner_full_and_low_siblings(tmp_path):
    full = tmp_path / "X-s1-full.mp4"
    low = tmp_path / "X-s2-low.mp4"
    full.touch()
    low.touch()
    assert player.partner(full) == (full, low)


def test_partner_opening_low_finds_full(tmp_path):
    full = tmp_path / "X-s1-full.mp4"
    low = tmp_path / "X-s2-low.mp4"
    full.touch()
    low.touch()
    assert player.partner(low) == (full, low)


def test_partner_full_alone(tmp_path):
    full = tmp_path / "X-s1-full.mp4"
    full.touch()
    assert player.partner(full) == (full, None)


def test_partner_low_alone(tmp_path):
    low = tmp_path / "X-s1-low.mp4"
    low.touch()
    assert player.partner(low) == (low, None)


def test_partner_both_full_keeps_requested_as_main(tmp_path):
    s1 = tmp_path / "X-s1-full.mp4"
    s2 = tmp_path / "X-s2-full.mp4"
    s1.touch()
    s2.touch()
    assert player.partner(s2) == (s2, None)
    assert player.partner(s1) == (s1, None)


def test_partner_unrecognized_name(tmp_path):
    foo = tmp_path / "foo.mp4"
    foo.touch()
    assert player.partner(foo) == (foo, None)


def test_ipc_returns_data_skipping_event_lines(tmp_path):
    sock_path = str(tmp_path / "mpv.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)

    def handle():
        conn, _ = server.accept()
        with conn:
            conn.recv(4096)
            conn.sendall(b'{"event":"idle"}\n')
            conn.sendall(b'{"data":12.5,"error":"success","request_id":0}\n')

    t = threading.Thread(target=handle, daemon=True)
    t.start()
    try:
        assert player._ipc(sock_path, "get_property", "time-pos") == 12.5
    finally:
        t.join(timeout=1)
        server.close()


def test_ipc_missing_socket_returns_none(tmp_path):
    assert player._ipc(str(tmp_path / "missing.sock"), "get_property", "time-pos") is None


def test_alive_matches_name_and_ignores_zombies():
    p = subprocess.Popen(["sleep", "5"])
    try:
        assert player._alive(p.pid, "sleep")
        assert not player._alive(p.pid, "mpv")
        p.send_signal(signal.SIGKILL)
        time.sleep(0.2)  # now a zombie: exited but not reaped
        assert not player._alive(p.pid, "sleep")
    finally:
        p.wait()


def test_active_prunes_dead_pid_files():
    d = store.DATA / "mpv"
    d.mkdir(parents=True)
    (d / "999999999").touch()
    assert player.active() is False
    assert not any(d.iterdir())


def test_finish_counts_closing_in_the_last_10_minutes_as_fully_watched():
    assert player.finish("C-2026-09-28-LecA", 3000.0, 3500.0) is True
    assert player.finish("C-2026-09-28-LecB", 2800.0, 3500.0) is False
    assert player.finish("C-2026-09-28-LecC", None, None) is False
    w = store.load()["watched"]
    assert w["C-2026-09-28-LecA"]["full"] and not w["C-2026-09-28-LecB"]["full"]
    assert w["C-2026-09-28-LecA"]["pid"] is None


def test_cleanup_deletes_by_watch_state_and_age(monkeypatch):
    d = store.VIDEOS / "C"
    d.mkdir(parents=True)
    now = 1_000_000_000.0
    day = 86400

    def video(name, downloaded):
        p = d / f"{name}-s1-full.mp4"
        p.write_bytes(b"v")
        p.with_suffix(".srt").write_text("1")
        os.utime(p, (downloaded, downloaded))
        return p

    fresh = video("C-new", now - 13 * day)            # unwatched, 13 days: keep
    stale = video("C-old", now - 15 * day)            # unwatched, 15 days: delete
    full_recent = video("C-fr", now - day)            # fully watched 30 min ago: keep
    full_old = video("C-fo", now - day)               # fully watched 2 h ago: delete
    part_recent = video("C-pr", now - day)            # partly watched 3 h ago: keep
    part_old = video("C-po", now - day)               # partly watched 5 h ago: delete
    redownloaded = video("C-rd", now - 3600)          # watched a week ago, downloaded again since: keep
    playing = video("C-pl", now - 30 * day)           # open in mpv right now: keep
    with store.edit() as s:
        s["watched"] = {
            "C-fr": {"last": now - 1800, "full": True}, "C-fo": {"last": now - 7200, "full": True},
            "C-pr": {"last": now - 3 * 3600, "full": False}, "C-po": {"last": now - 5 * 3600, "full": False},
            "C-rd": {"last": now - 7 * day, "full": True}, "C-pl": {"last": now - 30 * day, "pid": os.getpid()},
        }
    monkeypatch.setattr(player, "_alive", lambda pid, name="mpv": pid == os.getpid())
    player.cleanup(now)
    left = sorted(p.name for p in d.glob("*.mp4"))
    assert left == sorted(p.name for p in (fresh, full_recent, part_recent, redownloaded, playing))
    assert not stale.with_suffix(".srt").exists()


def test_play_starts_the_helper_from_home(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(player.subprocess, "Popen", lambda cmd, **kw: seen.update(cmd=cmd, **kw))
    f = tmp_path / "C-2026-09-28-LecA-s1-full.mp4"
    f.touch()
    player.play(f)
    assert seen["cwd"] == Path.home() and seen["cmd"][-2:] == ["pip", str(f)]


def test_pip_does_not_open_a_lecture_that_is_already_open(monkeypatch, tmp_path):
    with store.edit() as s:
        s["watched"] = {"C-2026-09-28-LecA": {"pid": 4242}}
    monkeypatch.setattr(player, "_alive", lambda pid, name="mpv": pid == 4242)
    started = []
    monkeypatch.setattr(player.subprocess, "Popen", lambda cmd, **kw: started.append(cmd))
    player.pip(tmp_path / "C-2026-09-28-LecA-s1-full.mp4")
    assert started == []  # a second mpv would start a second live whisper on the same video

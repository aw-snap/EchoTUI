import threading
import time

from echotui import player, store, worker

ITEM = {"lesson": "L1", "section": "S", "section_name": "COSC264-26S2", "course": "COSC264",
        "date": "2026-09-28", "label": "LecA", "feeds": "all", "open": False, "state": "waiting"}


def syllabus(**media):
    m = {"id": "m1", "mediaType": "Video", "isAvailable": False, "isProcessing": False, "isFailed": False, **media}
    return [{"type": "SyllabusLessonType", "lesson": {
        "lesson": {"id": "L1", "name": "COSC264-26S2-LecA-Intro", "timing": {"start": "2026-09-28T15:00:00.000"}},
        "medias": [m], "isFuture": False, "startTimeUTC": "2026-09-28T02:00:00.000Z",
        "endTimeUTC": "2026-09-28T02:55:00.000Z"}}]


INFO = {"primaryFiles": {"posterUrl": "p1", "files": [{"fileName": "hd1.mp4", "label": "Full"},
                                                       {"fileName": "sd1.mp4", "label": "Lower"}]},
        "secondaryFiles": {"posterUrl": "p2", "files": [{"fileName": "hd2.mp4", "label": "Full"},
                                                          {"fileName": "sd2.mp4", "label": "Lower"}]}}


class FakeClient:
    def __init__(self, syl, frame_paths=None):
        self.syl, self.urls, self.frame_paths = syl, [], frame_paths

    def get_json(self, path):
        return self.syl if path.endswith("/syllabus") else INFO

    def signed_url(self, media, name, lesson):
        return f"https://x/{name}"

    def fetch(self, url, dest, progress=None):
        self.urls.append(url)
        dest.write_bytes(b"v")
        if progress:
            progress(1.0)

    def frames(self, media, lesson, feeds):
        return self.frame_paths


def test_ready_lesson_downloads_all_feeds_and_dequeues(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(worker, "notify", sent.append)
    store.enqueue(dict(ITEM))
    f1, f2 = tmp_path / "f1.jpg", tmp_path / "f2.jpg"
    f1.write_bytes(b"a")
    f2.write_bytes(b"b")
    c = FakeClient(syllabus(isAvailable=True), frame_paths=[f1, f2])
    worker.step(c, dict(ITEM))
    d = store.VIDEOS / "COSC264"
    assert sorted(p.name for p in d.iterdir()) == [
        "COSC264-2026-09-28-LecA-s1-full.mp4", "COSC264-2026-09-28-LecA-s2-low.mp4"]
    assert c.urls == ["https://x/hd1.mp4", "https://x/sd2.mp4"]
    assert store.load()["queue"] == []
    assert sent == ["COSC264 LecA 28 Sep downloaded"]


def test_legacy_list_feeds_is_treated_as_all(tmp_path):
    # old tui.py wrote item["feeds"] as a list like [1, 2]; step must still resolve it
    # via default_choice rather than raise on choice.items().
    item = {**ITEM, "feeds": [1, 2]}
    store.enqueue(dict(item))
    f1, f2 = tmp_path / "f1.jpg", tmp_path / "f2.jpg"
    f1.write_bytes(b"a")
    f2.write_bytes(b"b")
    c = FakeClient(syllabus(isAvailable=True), frame_paths=[f1, f2])
    worker.step(c, item)
    d = store.VIDEOS / "COSC264"
    assert sorted(p.name for p in d.iterdir()) == [
        "COSC264-2026-09-28-LecA-s1-full.mp4", "COSC264-2026-09-28-LecA-s2-low.mp4"]


def test_identical_frames_leaves_out_the_duplicate_low_feed(tmp_path):
    store.enqueue(dict(ITEM))
    f1, f2 = tmp_path / "f1.jpg", tmp_path / "f2.jpg"
    f1.write_bytes(b"same")
    f2.write_bytes(b"same")
    c = FakeClient(syllabus(isAvailable=True), frame_paths=[f1, f2])
    worker.step(c, dict(ITEM))
    d = store.VIDEOS / "COSC264"
    assert sorted(p.name for p in d.iterdir()) == ["COSC264-2026-09-28-LecA-s1-full.mp4"]
    assert c.urls == ["https://x/hd1.mp4"]


def test_explicit_feeds_dict_downloads_only_requested_feed():
    item = {**ITEM, "feeds": {"2": "full"}}
    store.enqueue(dict(item))
    c = FakeClient(syllabus(isAvailable=True))
    worker.step(c, item)
    d = store.VIDEOS / "COSC264"
    assert sorted(p.name for p in d.iterdir()) == ["COSC264-2026-09-28-LecA-s2-full.mp4"]
    assert c.urls == ["https://x/hd2.mp4"]


def test_feeds_dict_matching_no_feed_marks_failed_without_dequeuing(monkeypatch):
    sent = []
    monkeypatch.setattr(worker, "notify", sent.append)
    item = {**ITEM, "feeds": {"5": "full"}}
    store.enqueue(dict(item))
    c = FakeClient(syllabus(isAvailable=True))
    worker.step(c, item)
    q = store.load()["queue"][0]
    assert q["state"] == "failed" and q["error"] == "feed missing"
    assert sent == ["COSC264 LecA 28 Sep: feed missing"]


def test_processing_lesson_stays_waiting():
    store.enqueue(dict(ITEM))
    worker.step(FakeClient(syllabus(isProcessing=True)), dict(ITEM))
    assert store.load()["queue"][0]["state"] == "waiting"


def test_failed_lesson_is_marked_failed(monkeypatch):
    monkeypatch.setattr(worker, "notify", lambda m: None)
    store.enqueue(dict(ITEM))
    worker.step(FakeClient(syllabus(isFailed=True)), dict(ITEM))
    assert store.load()["queue"][0]["state"] == "failed"


def test_run_pausable_stops_while_mpv_is_open(monkeypatch):
    answers = iter([True, True, True])
    monkeypatch.setattr(player, "active", lambda: next(answers, False))
    t = time.monotonic()
    worker.run_pausable(["sleep", "0.2"], tick=0.1)
    assert time.monotonic() - t >= 0.45  # ~0.3 s frozen + 0.2 s of real sleep


def test_pending_subs_skips_captioned_files():
    d = store.VIDEOS / "COSC264"
    d.mkdir(parents=True)
    (d / "a.mp4").touch()
    (d / "b.mp4").touch()
    (d / "b.srt").touch()
    assert [p.name for p in worker.pending_subs()] == ["a.mp4"]


def test_pending_subs_skips_low_feed():
    d = store.VIDEOS / "COSC264"
    d.mkdir(parents=True)
    (d / "a-s1-full.mp4").touch()
    (d / "a-s2-low.mp4").touch()
    assert [p.name for p in worker.pending_subs()] == ["a-s1-full.mp4"]


class UnqueuedMidDownload(FakeClient):
    def fetch(self, url, dest, progress=None):
        part = dest.with_name(dest.name + ".part")
        part.write_bytes(b"half")
        store.dequeue("L1")  # the user presses u in the TUI while this is downloading
        progress(0.5)
        dest.write_bytes(b"v")


def test_unqueue_mid_download_stops_and_removes_partial(monkeypatch):
    sent = []
    monkeypatch.setattr(worker, "notify", sent.append)
    store.enqueue(dict(ITEM, feeds={"1": "full"}))
    worker.step(UnqueuedMidDownload(syllabus(isAvailable=True)), dict(ITEM, feeds={"1": "full"}))
    assert list((store.VIDEOS / "COSC264").iterdir()) == []
    assert sent == [] and store.load()["queue"] == []


def test_full_feed_is_captioned_from_the_low_url_while_downloading(monkeypatch, tmp_path):
    (tmp_path / "model.bin").touch()
    monkeypatch.setattr(worker, "MODEL", tmp_path / "model.bin")
    calls = []

    def fake_srt(mp4, src=None):
        calls.append((mp4.name, src))
        mp4.with_suffix(".srt").write_text("1")
    monkeypatch.setattr(worker, "make_srt", fake_srt)
    item = {**ITEM, "feeds": {"1": "full", "2": "low"}}
    store.enqueue(dict(item))
    worker.step(FakeClient(syllabus(isAvailable=True)), item)
    for _ in range(100):
        if not worker._busy:
            break
        time.sleep(0.01)
    assert calls == [("COSC264-2026-09-28-LecA-s1-full.mp4", "https://x/sd1.mp4")]  # not for the low feed
    assert (store.VIDEOS / "COSC264" / "COSC264-2026-09-28-LecA-s1-full.srt").exists()


def test_a_file_is_only_ever_pre_captioned_once_at_a_time(monkeypatch):
    mp4 = store.VIDEOS / "COSC264" / "a-s1-full.mp4"
    mp4.parent.mkdir(parents=True)
    mp4.touch()
    gate, calls = threading.Event(), []

    def fake_srt(mp4, src=None):
        calls.append(src)
        gate.wait(2)
    monkeypatch.setattr(worker, "make_srt", fake_srt)
    worker.caption_live(mp4, "https://x/a")
    worker.caption_live(mp4, "https://x/b")  # e.g. a re-queue while the first pass still runs
    worker.subtitle_pass()
    gate.set()
    for _ in range(100):
        if not worker._busy:
            break
        time.sleep(0.01)
    assert calls == ["https://x/a"]

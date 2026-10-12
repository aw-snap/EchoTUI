import threading

from echotui import store


def test_load_defaults_when_missing():
    s = store.load()
    assert s["queue"] == [] and s["opened"] == {}


def test_queue_helpers():
    store.enqueue({"lesson": "a", "state": "waiting"})
    store.enqueue({"lesson": "a", "state": "waiting", "open": True})  # replaces, not duplicates
    store.update("a", state="downloading", progress=0.5)
    assert store.load()["queue"] == [{"lesson": "a", "state": "downloading", "open": True, "progress": 0.5}]
    store.dequeue("a")
    assert store.load()["queue"] == []


def _bump():
    with store.edit() as s:
        s["n"] = s.get("n", 0) + 1


def test_concurrent_edits_do_not_lose_writes():
    threads = [threading.Thread(target=_bump) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.load()["n"] == 20


def test_enqueue_drops_a_failed_try_at_another_copy_of_the_lecture():
    slot = {"course": "ENCE260", "date": "2026-10-09", "label": "LecC"}
    store.enqueue({"lesson": "a", "state": "failed", **slot})
    store.enqueue({"lesson": "c", "state": "failed", **slot, "label": "LecB"})
    store.enqueue({"lesson": "b", "state": "waiting", **slot})
    assert [q["lesson"] for q in store.load()["queue"]] == ["c", "b"]

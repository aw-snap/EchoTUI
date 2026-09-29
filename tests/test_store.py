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

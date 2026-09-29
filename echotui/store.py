"""Paths and the shared state file. The TUI and the worker both write it, always under flock."""
import copy
import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path

DATA = Path.home() / ".local/share/echotui"
CACHE = Path.home() / ".cache/echotui"
VIDEOS = Path.home() / "Videos/Echo360"
EMPTY = {"email": "", "opened": {}, "queue": []}


def load() -> dict:
    try:
        return {**copy.deepcopy(EMPTY), **json.loads((DATA / "state.json").read_text())}
    except FileNotFoundError:
        return copy.deepcopy(EMPTY)


@contextmanager
def edit():
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(DATA, 0o700)  # mkdir's mode is ignored for a dir that already existed with looser perms
    with open(DATA / "state.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        s = load()
        yield s
        tmp = DATA / "state.json.tmp"
        tmp.write_text(json.dumps(s, indent=1))
        tmp.replace(DATA / "state.json")  # atomic, so lock-free load() never sees half a file


def enqueue(item: dict):
    with edit() as s:
        s["queue"] = [q for q in s["queue"] if q["lesson"] != item["lesson"]] + [item]


def update(lesson: str, **fields):
    with edit() as s:
        for q in s["queue"]:
            if q["lesson"] == lesson:
                q.update(fields)


def dequeue(lesson: str):
    with edit() as s:
        s["queue"] = [q for q in s["queue"] if q["lesson"] != lesson]

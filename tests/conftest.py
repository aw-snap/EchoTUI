import pytest

from echotui import store, worker


@pytest.fixture(autouse=True)
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA", tmp_path / "data")
    monkeypatch.setattr(store, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(store, "VIDEOS", tmp_path / "videos")
    monkeypatch.setattr(worker, "notify", lambda msg: None)  # no real notify-send popups from tests
    monkeypatch.setattr(worker, "MODEL", tmp_path / "no-model.bin")  # no real whisper runs unless a test opts in
    return tmp_path

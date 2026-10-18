import shutil
import sys

import pytest

from echotui import __main__ as cli, player, store, worker


def test_missing_tools_print_the_install_command_and_the_no_subtitles_way_out(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda c: f"/usr/bin/{c}" if c in ("pacman", "yay", "ffmpeg") else None)
    monkeypatch.setattr(sys, "argv", ["echotui"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert str(e.value) == ("echotui needs mpv, whisper-cli. Install with:\n  yay -S --needed mpv whisper.cpp\n"
                            "or run `echotui --no-subtitles` to go without subtitles")


def test_no_subtitles_drops_whisper_and_turns_captions_off_for_the_helpers(monkeypatch):
    monkeypatch.setenv("ECHOTUI_SUBTITLES", "1")  # so monkeypatch restores it after main() sets "0"
    monkeypatch.setattr(shutil, "which", lambda c: None if c == "whisper-cli" else f"/usr/bin/{c}")
    monkeypatch.setattr(sys, "argv", ["echotui", "--no-subtitles", "nonsense"])
    with pytest.raises(SystemExit) as e:  # got past the tool check to the usage line
        cli.main()
    assert "usage" in str(e.value)
    mp4 = store.VIDEOS / "C" / "a-s1-full.mp4"
    mp4.parent.mkdir(parents=True)
    mp4.touch()
    assert not store.subtitles() and worker.pending_subs() == [] and player.mpv_captions() == []

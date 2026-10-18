import os
import shutil
import sys

WHISPER = "https://github.com/ggml-org/whisper.cpp#quick-start"


def install_hint(missing: list[str]) -> str:
    """The commands that install `missing` with this system's package manager."""
    system = [m for m in missing if m != "whisper-cli"]
    whisper = "whisper-cli" in missing
    if shutil.which("pacman"):  # extra/whisper-cpp ships whisper-cli
        return f"  sudo pacman -S --needed {' '.join(system + (['whisper-cpp'] if whisper else []))}"
    mgr = next((m for m in ("apt", "dnf", "zypper") if shutil.which(m)), None)
    lines = [f"  sudo {mgr} install {' '.join(system)}" if mgr else f"  {' '.join(system)} from your distro"] if system else []
    return "\n".join(lines + ([f"  whisper.cpp: build it from {WHISPER}"] if whisper else []))


def main():
    args = sys.argv[1:]
    if "--no-subtitles" in args:
        args.remove("--no-subtitles")
        os.environ["ECHOTUI_SUBTITLES"] = "0"  # inherited by the worker and mpv helper this run starts
    cmd = args[0] if args else ""
    from . import store
    need = ["mpv", "ffmpeg"] + (["whisper-cli"] if store.subtitles() else [])
    if cmd in ("", "login") and (missing := [c for c in need if not shutil.which(c)]):
        skip = "\nor run `echotui --no-subtitles` to go without subtitles" if "whisper-cli" in missing else ""
        sys.exit(f"echotui needs {', '.join(missing)}. Install with:\n{install_hint(missing)}{skip}")
    if cmd == "login":
        from . import api
        api.login_cli()
    elif cmd == "worker":
        from . import worker
        worker.main()
    elif cmd == "pip":
        from pathlib import Path

        from . import player
        player.pip(Path(args[1]), Path(args[2]) if len(args) > 2 else None)
    elif cmd:
        sys.exit("usage: echotui [--no-subtitles] [login]")
    else:
        from . import tui
        tui.run()


if __name__ == "__main__":
    main()

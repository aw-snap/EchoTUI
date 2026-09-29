import sys


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "login":
        from . import api
        api.login_cli()
    elif cmd == "worker":
        from . import worker
        worker.main()
    elif cmd == "pip":
        from pathlib import Path

        from . import player
        player.pip(Path(sys.argv[2]), Path(sys.argv[3]) if len(sys.argv) > 3 else None)
    elif cmd:
        sys.exit("usage: echotui [login]")
    else:
        from . import tui
        tui.run()


if __name__ == "__main__":
    main()

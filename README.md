# EchoTUI

A terminal UI for Echo360 lecture recordings. Browse your current courses, queue lectures for
download (including ones still processing), and watch them in mpv with subtitles that whisper.cpp
generates while you watch.

- Vim-style navigation (`j`/`k`, `h`/`l`, `gg`/`G`, `ctrl+d`/`ctrl+u`)
- Shows a preview frame of each feed (camera or screen), so you can pick which feeds to download and at
  what quality. Duplicate feeds and feeds that only show an idle lectern screen are skipped automatically.
- A background worker waits for lectures that are still processing, downloads them, and sends a
  desktop notification when they're ready.
- Two-feed lectures play as a main window plus a small synced picture-in-picture window, kept on top
  in the bottom-right corner. On Hyprland, EchoTUI floats and pins it itself; a window rule of yours
  that already floats it takes precedence.
- Watched lectures are deleted automatically: 1 h after you finish one, 4 h after a partial watch,
  or 2 weeks after download if you never open it.
- The courses page ticks courses whose newest lecture you've watched, and lists a timeline of every
  course's lectures, newest first: today plus the 3 days before it, with older ones loading as you scroll
  down. Pressing `enter` on one jumps straight to it.
- If you use [tinty](https://github.com/tinted-theming/tinty), EchoTUI uses your current base16/base24
  scheme and switches with it when you `tinty apply`.

## Requirements

Linux only. It uses `/proc`, `flock` and `notify-send`.

| Dependency | Needed for |
|---|---|
| Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/) (or pipx/pip) | installing |
| [ffmpeg](https://ffmpeg.org/) | feed previews and duplicate detection |
| [mpv](https://mpv.io/) | playback |
| a Secret Service keyring (GNOME Keyring, KWallet, KeePassXC…) | storing your password for automatic re-login |
| [whisper.cpp](https://github.com/ggml-org/whisper.cpp) `whisper-cli` | subtitles |
| `notify-send` (libnotify), optional | download notifications |

`echotui` checks that `mpv`, `ffmpeg` and `whisper-cli` are on your `PATH` when it starts and names any that are missing.
On Arch: `sudo pacman -S uv mpv ffmpeg` and `yay -S whisper.cpp`.

A terminal with image support (kitty, WezTerm, Ghostty, foot, or anything with sixel) gives you
real preview images. Other terminals fall back to coloured blocks.

Your Echo360 account needs an email and password login. SSO-only (Microsoft/Google) logins are not
supported.

## Install

```sh
uv tool install git+https://github.com/aw-snap/EchoTUI
```

Or, from a clone:

```sh
git clone https://github.com/aw-snap/EchoTUI && cd EchoTUI
uv tool install -e .
```

### Subtitles

Nothing to set up beyond installing whisper.cpp. EchoTUI downloads the whisper model (`ggml-small.en`,
about 470 MB) the first time it needs it and loads its own mpv script. It captions the first 5 minutes
of each lecture while it downloads, and mpv live-captions the rest while you watch into the `.srt` next
to the video. Closing and reopening a lecture picks up both playback and captions where they stopped.

To caption videos in your own mpv sessions too (press `ctrl+c`), copy
[`echotui/autocaption.lua`](echotui/autocaption.lua) to `~/.config/mpv/scripts/` and the model to
`~/.config/mpv/`. EchoTUI then uses that copy instead of its own.

## Setup

EchoTUI connects to `echo360.net.au` (Australia/NZ) by default. If your institution uses a
different region, set `ECHO360_HOST`, for example in your shell rc:

```sh
export ECHO360_HOST=echo360.org      # US; also echo360.org.uk, echo360.ca, echo360.net.au ...
```

It should match the address you see in your browser when you use Echo360.

Then log in once:

```sh
echotui login
```

Your password goes in the system keyring, so EchoTUI can log in again by itself when the session
expires.

## Usage

```sh
echotui
```

| Key | Where | Action |
|---|---|---|
| `j` / `k`, `l` / `enter`, `h` / `esc` | everywhere | move, open, back |
| `d` | lectures | download (opens the feed picker) |
| `o` | lectures | download, then open in mpv |
| `x` | lectures, library | delete the download |
| `L` | everywhere | library of downloaded lectures |
| `←` / `→`, `space`, `enter` | feed picker | choose a feed, cycle full / low / skip, confirm |
| `ctrl+p` | everywhere | command palette |
| `q` | everywhere | quit |

Queued lectures keep downloading after you quit, because a detached worker handles them.

### Where things go

| Path | Contents |
|---|---|
| `~/Videos/Echo360/<course>/` | downloaded videos and `.srt` subtitles |
| `~/.local/share/echotui/` | state, cookies (mode 0600), the whisper model and `worker.log` |
| `~/.cache/echotui/` | preview frames |

## Development

```sh
uv sync
uv run pytest
```

## License

[MIT](LICENSE)

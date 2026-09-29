"""Echo360 over plain HTTP: login replay, cookie jar, JSON endpoints, resumable downloads."""
import getpass
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import threading
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urljoin, urlparse

import keyring
import keyring.errors

from . import model, store

HOST = os.environ.get("ECHO360_HOST", "echo360.net.au")  # your region: echo360.org, echo360.org.uk, echo360.ca, ...
BASE = f"https://{HOST}"
LOGIN = f"https://login.{HOST}/login"
UA = "Mozilla/5.0 (X11; Linux x86_64) echotui/0.1"


class AuthError(Exception):
    pass


class _Forms(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.forms.append({"action": a.get("action") or "", "method": (a.get("method") or "get").lower(),
                               "fields": {}, "types": {}, "submit": {}})
            return
        if not self.forms or tag not in ("input", "button") or not a.get("name"):
            return
        f, kind = self.forms[-1], (a.get("type") or ("submit" if tag == "button" else "text")).lower()
        if kind in ("submit", "button", "image"):
            f["submit"] = f["submit"] or {a["name"]: a.get("value") or ""}
        else:
            f["fields"][a["name"]] = a.get("value") or ""
            f["types"][a["name"]] = kind


def parse_forms(html: str) -> list[dict]:
    p = _Forms()
    p.feed(html)
    return p.forms


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # surface the 3xx as HTTPError so we can read Location


def _wake_wallet():
    # KWallet only exposes org.freedesktop.secrets once kwalletd6 is running; DBus-activate it.
    try:
        subprocess.run(["busctl", "--user", "call", "org.kde.kwalletd6", "/modules/kwalletd6",
                        "org.kde.KWallet", "isEnabled"], capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _password(email: str) -> str | None:
    _wake_wallet()
    try:
        return keyring.get_password("echotui", email)
    except keyring.errors.KeyringError:
        return None


def _debug_page(body: str):
    # the saved page can hold one-time login tokens, so it gets the same 0600 treatment as the
    # cookie jar; state.json has no chmod of its own and relies on DATA being 0700 instead
    store.DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    p = store.DATA / "login-debug.html"
    fd = os.open(p, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(body)
    os.chmod(p, 0o600)  # in case the file pre-existed with looser perms


class Client:
    def __init__(self):
        self.jar = http.cookiejar.MozillaCookieJar(str(store.DATA / "cookies.txt"))
        if Path(self.jar.filename).exists():
            self.jar.load(ignore_discard=True, ignore_expires=True)
        cookies = urllib.request.HTTPCookieProcessor(self.jar)
        self.op = urllib.request.build_opener(cookies)
        self.noredir = urllib.request.build_opener(cookies, _NoRedirect)
        for o in (self.op, self.noredir):
            o.addheaders = [("User-Agent", UA)]
        self._lock = threading.RLock()  # RLock: relogin() holds it across a nested login()/_save()

    def _save(self):
        # ponytail: TUI and worker each save their own jar; last writer wins, the other re-logs in if needed
        with self._lock:
            p = Path(self.jar.filename)
            p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.close(os.open(p, os.O_CREAT | os.O_WRONLY, 0o600))  # create 0600 before writing secrets
            os.chmod(p, 0o600)  # O_CREAT above only fixes perms for a brand-new file
            self.jar.save(ignore_discard=True, ignore_expires=True)

    def get_json(self, path: str):
        for attempt in (0, 1):
            try:
                r = self.op.open(BASE + path, timeout=30)
                u = urlparse(r.url)
                if u.netloc == urlparse(BASE).netloc and not u.path.startswith("/login"):
                    data = json.load(r)["data"]
                    self._save()
                    return data
            except HTTPError as e:
                if e.code != 401:
                    raise
            if attempt:
                raise AuthError("session rejected after re-login — run `echotui login`")
            self.relogin()

    def relogin(self):
        with self._lock:  # so two threads sharing this Client can't both clear the jar mid-login
            email = store.load()["email"]
            pw = email and _password(email)
            if not pw:
                raise AuthError("not logged in — run `echotui login`")
            self.login(email, pw)

    def _submit(self, url: str, form: dict, fields: dict) -> tuple[str, str]:
        action = urljoin(url, form["action"] or url)
        if form["method"] == "get":
            req = urllib.request.Request(f"{action}{'&' if '?' in action else '?'}{urlencode(fields)}")
        else:
            req = urllib.request.Request(action, data=urlencode(fields).encode())
        try:
            r = self.op.open(req, timeout=30)
            return r.read().decode(errors="replace"), r.url
        except HTTPError as e:
            return e.read().decode(errors="replace"), e.url

    def login(self, email: str, password: str):
        self.jar.clear()
        r = self.op.open(LOGIN, timeout=30)
        body, url = r.read().decode(errors="replace"), r.url
        sent_email = sent_pw = False
        for _ in range(10):
            if urlparse(url).netloc == urlparse(BASE).netloc and any(
                    c.name == "PLAY_SESSION" and c.domain.lstrip(".") == urlparse(BASE).netloc for c in self.jar):
                self._save()
                return
            forms = parse_forms(body)
            form = (next((f for f in forms if "password" in f["types"].values()), None)
                    or next((f for f in forms if "email" in f["fields"]), None)
                    or (forms[0] if forms else None))
            if form is None:
                _debug_page(body)
                raise AuthError(f"login stuck at {url} (page saved to login-debug.html)")
            fields = {**form["fields"], **form["submit"]}
            pw_field = next((n for n, t in form["types"].items() if t == "password"), None)
            if pw_field:
                action = urljoin(url, form["action"] or url)
                if urlparse(action).scheme != "https":
                    raise AuthError("refusing to send password to a non-https form")
                if sent_pw:
                    raise AuthError("wrong password")
                fields[pw_field], sent_pw = password, True
                if "email" in fields and not fields["email"]:
                    fields["email"] = email
            elif "email" in fields:
                if sent_email:
                    raise AuthError("email not recognised by Echo360")
                fields["email"], sent_email = email, True
            body, url = self._submit(url, form, fields)
        _debug_page(body)
        raise AuthError(f"login did not finish (last page {url}, saved to login-debug.html)")

    def signed_url(self, media_id: str, file_name: str, lesson_id: str) -> str:
        """Resolve a download to its signed content URL; also sets the CloudFront cookies playback needs."""
        url = f"{BASE}/media/download/{media_id}/{file_name}?{urlencode({'lessonId': lesson_id})}"
        for attempt in (0, 1):
            try:
                self.noredir.open(url, timeout=30)
            except HTTPError as e:
                loc = e.headers.get("Location") or ""
                if e.code == 401 or "/login" in loc:
                    if attempt:
                        raise AuthError("session rejected after re-login — run `echotui login`")
                    self.relogin()
                    continue
                if 300 <= e.code < 400 and loc:
                    self._save()
                    return urljoin(url, loc)
                raise
            raise ValueError(f"expected a redirect from {url}")

    def fetch(self, url: str, dest: Path, progress=None):
        part = dest.with_name(dest.name + ".part")
        have = part.stat().st_size if part.exists() else 0
        req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
        try:
            r = self.op.open(req, timeout=60)
        except HTTPError as e:
            if e.code != 416:
                raise
            part.replace(dest)  # 416: the .part already holds the whole file
            return
        with r, open(part, "ab" if r.status == 206 else "wb") as f:
            done = have if r.status == 206 else 0
            total = done + int(r.headers.get("Content-Length") or 0)
            while chunk := r.read(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total)
        if total and done < total:
            raise OSError(f"download cut short at {done}/{total} bytes")  # keep .part for resume
        part.replace(dest)

    def recording_size(self, media_id: str) -> int:
        """Total bytes of the Full files; the bigger of two duplicate recordings is the better one. Cached."""
        p = store.CACHE / "frames" / f"{media_id}.size"
        if not p.exists():
            info = self.get_json(f"/api/ui/library/medias/{media_id}/download-info")
            n = sum(f.get("fileSize") or 0 for k in ("primaryFiles", "secondaryFiles")
                    for f in (info.get(k) or {}).get("files", []) if f.get("label") == "Full")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(str(n))
        return int(p.read_text())

    def frames(self, media_id: str, lesson_id: str, feeds: list[dict]) -> list[Path | None]:
        return [self._frame(media_id, lesson_id, f) for f in feeds]

    def _frame(self, media_id: str, lesson_id: str, feed: dict) -> Path | None:
        dest = store.CACHE / "frames" / f"{media_id}-{feed['n']}.jpg"
        if dest.exists():
            return dest
        # unique per call: two concurrent grabs of the same media (e.g. the TUI prefetch
        # thread and a worker process) must not clobber each other's tmp file.
        tmp = dest.with_name(f"{dest.stem}.{os.getpid()}.{threading.get_ident()}.tmp.jpg")
        try:
            url = self.signed_url(media_id, model.pick_file(feed, "low"), lesson_id)
            dest.parent.mkdir(parents=True, exist_ok=True)
            for ss in (600, 60):
                r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", str(ss), "-i", url,
                                    "-frames:v", "1", str(tmp)], timeout=60, capture_output=True, check=False)
                if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
                    tmp.replace(dest)
                    return dest
            return None
        except AuthError:
            raise
        except Exception:
            return None
        finally:
            tmp.unlink(missing_ok=True)  # a leftover half-written tmp from a timeout/kill is never left behind


# SSIM on small greyscale copies. 35 real feed pairs: look-alike >= 0.9997, different <= 0.30
SAME_SSIM = 0.9
IDLE_SCREENS = sorted(Path(__file__).with_name("idle_screens").glob("*.jpg"))  # lectern screens, no lecture
_SMALL = "scale=64:36,format=gray"


def _ssim(a: Path, b: Path) -> float | None:
    try:
        r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(a), "-i", str(b),
                            "-lavfi", f"[0]{_SMALL}[a];[1]{_SMALL}[b];[a][b]ssim", "-f", "null", "-"],
                           capture_output=True, text=True, errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    scores = re.findall(r"All:([\d.]+)", r.stderr)
    return float(scores[-1]) if scores else None


def same(paths: list) -> bool:
    """True when both feeds show the same picture (separately encoded copies differ in bytes, so use SSIM)."""
    if len(paths) != 2 or not all(paths):
        return False
    if paths[0].read_bytes() == paths[1].read_bytes():
        return True
    return (_ssim(paths[0], paths[1]) or 0) >= SAME_SSIM


def idle_screen(frame: Path | None) -> bool:
    """Whether a frame shows a lectern idle screen (sign-in page, "connect your device"). Cached in <frame>.idle."""
    if frame is None:
        return False
    mark = frame.with_suffix(".idle")
    if not mark.exists():
        idle = any((_ssim(frame, ref) or 0) >= SAME_SSIM for ref in IDLE_SCREENS)
        mark.write_text("idle" if idle else "ok")
    return mark.read_text() == "idle"


def idle_lecture(media_id: str) -> bool:
    """True when every checked frame of this lecture is an idle screen (nothing was presented)."""
    marks = [m.read_text() for m in (store.CACHE / "frames").glob(f"{media_id}-*.idle")]
    return bool(marks) and all(m == "idle" for m in marks)


def login_cli():
    saved = store.load()["email"]
    email = input(f"Echo360 email [{saved}]: ").strip() or saved
    pw = getpass.getpass("Password: ")
    c = Client()
    try:
        c.login(email, pw)
        n = len(c.get_json("/user/enrollments")[0]["userSections"])
    except (AuthError, OSError, ValueError, KeyError) as e:
        sys.exit(f"login failed: {e}")
    with store.edit() as s:
        s["email"] = email
    _wake_wallet()
    try:
        keyring.set_password("echotui", email, pw)
    except keyring.errors.KeyringError as e:
        print(f"warning: password not saved to keyring ({e}); re-run `echotui login` when the session expires")
    print(f"logged in — {n} enrolled sections")

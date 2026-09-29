import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from echotui import api, store

BODY = bytes(range(256)) * 400

LOGIN_HTML = """<form action="/login/institutions" method="POST" id="login-form">
<input name="email" type="email" value=""><input type="hidden" name="appId" value="app1">
<button type="submit" name="go" value="1">Next</button><button type="submit" name="no" value="0">No</button>
</form>"""


def test_parse_forms():
    f = api.parse_forms(LOGIN_HTML)[0]
    assert f["action"] == "/login/institutions" and f["method"] == "post"
    assert f["fields"] == {"email": "", "appId": "app1"}
    assert f["types"]["email"] == "email"
    assert f["submit"] == {"go": "1"}  # first submit button only, like a click


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/user/enrollments"):
            self.send_response(303)
            self.send_header("Location", "/login?afterLoginUrl=x")
            self.end_headers()
            return
        if self.path.startswith("/login"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html></html>")
            return
        start = int(self.headers.get("Range", "bytes=0-")[6:].split("-")[0])
        body = BODY[start:]
        self.send_response(206 if start else 200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    s = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_port}"
    s.shutdown()


def test_fetch_fresh(server, tmp_path):
    dest = tmp_path / "v.mp4"
    api.Client().fetch(server + "/file", dest)
    assert dest.read_bytes() == BODY


def test_fetch_resumes_part(server, tmp_path):
    dest = tmp_path / "v.mp4"
    (tmp_path / "v.mp4.part").write_bytes(BODY[:1000])
    seen = []
    api.Client().fetch(server + "/file", dest, progress=seen.append)
    assert dest.read_bytes() == BODY
    assert not (tmp_path / "v.mp4.part").exists()
    assert seen[-1] == 1.0


def test_login_redirect_without_credentials_raises_auth_error(server, monkeypatch):
    monkeypatch.setattr(api, "BASE", server)
    with pytest.raises(api.AuthError):
        api.Client().get_json("/user/enrollments")


class NoFormH(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><body>no form here</body></html>")

    def log_message(self, *a):
        pass


@pytest.fixture
def noform_server():
    s = ThreadingHTTPServer(("127.0.0.1", 0), NoFormH)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_port}/login"
    s.shutdown()


def test_login_stuck_writes_debug_page_0600_without_preexisting_store(noform_server, monkeypatch):
    from echotui import store

    monkeypatch.setattr(api, "LOGIN", noform_server)
    assert not store.DATA.exists()  # first-ever login: nothing has created store.DATA yet
    with pytest.raises(api.AuthError):
        api.Client().login("a@b", "pw")
    debug = store.DATA / "login-debug.html"
    assert debug.exists()
    assert oct(debug.stat().st_mode & 0o777) == oct(0o600)


class PwHttpFormH(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b'<form action="http://example.invalid/submit" method="POST">'
                         b'<input name="email" type="email" value="">'
                         b'<input name="password" type="password">'
                         b'<button type="submit" name="go" value="1">Go</button></form>')

    def log_message(self, *a):
        pass


@pytest.fixture
def pw_http_server():
    s = ThreadingHTTPServer(("127.0.0.1", 0), PwHttpFormH)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_port}/login"
    s.shutdown()


def test_login_refuses_password_to_non_https_form(pw_http_server, monkeypatch):
    monkeypatch.setattr(api, "LOGIN", pw_http_server)
    with pytest.raises(api.AuthError, match="non-https"):
        api.Client().login("a@b", "pw")


def _make_handler(video_path):
    # supports byte-Range requests: ffmpeg seeks into the mp4 (moov atom is at the end
    # for a non-faststart file), which it does over HTTP as Range requests.
    class VideoH(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/video.mp4":
                self.send_response(404)
                self.end_headers()
                return
            body = video_path.read_bytes()
            start = int(self.headers.get("Range", "bytes=0-")[6:].split("-")[0])
            chunk = body[start:]
            self.send_response(206 if start else 200)
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Accept-Ranges", "bytes")
            if start:
                self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
            self.end_headers()
            self.wfile.write(chunk)

        def log_message(self, *a):
            pass
    return VideoH


@pytest.fixture
def frame_server(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    mp4 = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-f", "lavfi", "-i", "color=c=red:s=64x64:d=700",
                    "-c:v", "libx264", "-t", "700", "-pix_fmt", "yuv420p", "-y", str(mp4)],
                   check=True, capture_output=True, timeout=60)
    s = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(mp4))
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_port}"
    s.shutdown()


def test_frames_grabs_a_jpg_caches_it_and_gives_none_on_404(frame_server, monkeypatch):
    c = api.Client()
    monkeypatch.setattr(c, "signed_url", lambda media, name, lesson: frame_server + "/video.mp4")
    feed = {"n": 1, "files": {"Lower": "sd1.mp4", "Full": "hd1.mp4"}}
    paths = c.frames("m1", "L1", [feed])
    assert paths == [store.CACHE / "frames" / "m1-1.jpg"]
    assert paths[0].exists() and paths[0].stat().st_size > 0

    # cached: an existing cache file is used as-is, no re-grab
    monkeypatch.setattr(c, "signed_url", lambda media, name, lesson: (_ for _ in ()).throw(AssertionError("re-grabbed")))
    assert c.frames("m1", "L1", [feed]) == paths

    monkeypatch.setattr(c, "signed_url", lambda media, name, lesson: frame_server + "/missing.mp4")
    feed2 = {"n": 2, "files": {"Lower": "sd2.mp4", "Full": "hd2.mp4"}}
    assert c.frames("m1", "L2", [feed2]) == [None]


def test_frames_ignores_a_leftover_stale_empty_tmp_file(frame_server, monkeypatch):
    # a pre-existing empty tmp file (e.g. from a killed/timed-out earlier grab, using the
    # old fixed tmp name) must not be mistaken for a freshly-grabbed frame.
    c = api.Client()
    monkeypatch.setattr(c, "signed_url", lambda media, name, lesson: frame_server + "/missing.mp4")
    feed = {"n": 3, "files": {"Lower": "sd3.mp4", "Full": "hd3.mp4"}}
    dest = store.CACHE / "frames" / "m3-3.jpg"
    stale = dest.with_name(f"{dest.stem}-tmp.jpg")
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"")
    assert c.frames("m3", "L3", [feed]) == [None]
    assert not dest.exists()


def test_same_true_only_for_two_equal_non_none_paths(tmp_path):
    a, b, c = tmp_path / "a.jpg", tmp_path / "b.jpg", tmp_path / "c.jpg"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    c.write_bytes(b"y")
    assert api.same([a, b]) is True
    assert api.same([a, c]) is False
    assert api.same([a, None]) is False
    assert api.same([a]) is False
    assert api.same([]) is False


def test_same_matches_look_alike_frames_encoded_differently(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")

    def jpg(name, src, q):
        p = tmp_path / name
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, "-frames:v", "1", "-q:v", str(q), str(p)],
                       check=True)
        return p
    slide = "testsrc2=s=640x360:d=1"
    a, b = jpg("a.jpg", slide, 2), jpg("b.jpg", slide, 9)  # same picture, different encode
    other = jpg("c.jpg", "mandelbrot=s=640x360", 2)
    assert a.read_bytes() != b.read_bytes()
    assert api.same([a, b]) is True
    assert api.same([a, other]) is False


def test_idle_screen_detects_each_reference_screen_and_caches_the_verdict(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    assert {p.name for p in api.IDLE_SCREENS} >= {"login.jpg", "connect_device.jpg"}
    frames = store.CACHE / "frames"
    frames.mkdir(parents=True)
    for i, ref in enumerate(api.IDLE_SCREENS, 1):  # a re-encoded copy of each reference still matches
        f = frames / f"m{i}-1.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(ref), "-q:v", "9", str(f)], check=True)
        assert api.idle_screen(f) is True and (frames / f"m{i}-1.idle").read_text() == "idle"
    other = frames / "m1-2.jpg"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360", "-frames:v", "1",
                    str(other)], check=True)
    assert api.idle_screen(other) is False and api.idle_screen(None) is False
    assert api.idle_lecture("m1") is False  # feed 2 has content
    assert api.idle_lecture("m2") is True and api.idle_lecture("nothing") is False
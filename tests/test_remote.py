"""Remote (HTTP range) reading gives exactly the same result as reading the downloaded file."""

import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest
import requests

from sarcompare.aoi import AOI
from sarcompare.demo import DEMO_BBOX
from sarcompare.products import NISAR, SENTINEL1
from sarcompare.readers import read_product
from sarcompare.download import _check_not_login_page, make_session
from sarcompare.remote import AccessError, is_streamable, open_remote, probe
from sarcompare.search import scan_local


class RangeHandler(SimpleHTTPRequestHandler):
    """Static files with HTTP Range support; /redirect/<f> answers 302 -> /<f> (like ASF -> signed S3 URL);
    HEAD is refused (signed S3 URLs usually reject it)."""

    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self.send_error(403)

    def do_GET(self):
        if self.path.startswith("/needs-login/"):  # like ASF when the token is not accepted
            self.send_response(302)
            self.send_header("Location", "/login-page")
            self.end_headers()
            return
        if self.path == "/login-page":
            body = b"<!DOCTYPE html><html><body>Earthdata Login</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/forbidden/"):
            self.send_error(403)
            return
        if self.path.startswith("/redirect/"):
            self.send_response(302)
            self.send_header("Location", self.path[len("/redirect"):] + "?X-Amz-Signature=abc%2Fdef")
            self.end_headers()
            return
        path = Path(self.translate_path(self.path.split("?")[0]))
        if not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            start_s, end_s = rng[6:].split("-")
            start = int(start_s)
            end = min(int(end_s) if end_s else len(data) - 1, len(data) - 1)
            chunk = data[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        else:
            chunk = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        self.wfile.write(chunk)


@pytest.fixture()
def server(demo_cfg):
    root = Path(demo_cfg.nisar_dir).parent
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(RangeHandler, directory=str(root)))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", root
    httpd.shutdown()


def test_is_streamable():
    assert is_streamable("https://x/y/NISAR_L2_GUNW.h5") and is_streamable("https://x/a.nc?sig=1")
    assert not is_streamable("https://x/a.zip") and not is_streamable(None)


@pytest.mark.parametrize("sensor,folder", [(NISAR, "nisar"), (SENTINEL1, "sentinel1")])
def test_remote_read_matches_local(server, sensor, folder):
    base, root = server
    aoi = AOI.from_bbox(*DEMO_BBOX)
    local = scan_local(root / folder, sensor)[0]
    expected = read_product(local, aoi, nisar_apply_ionosphere=True)

    remote = scan_local(root / folder, sensor)[0]
    remote.url = f"{base}/redirect/{folder}/{local.local_path.name}"
    remote.local_path = None
    session = requests.Session()
    final, size = probe(remote.url, session)
    assert "X-Amz-Signature" in final and size == local.local_path.stat().st_size
    with open_remote(remote.url, session, block_size=256 * 1024) as f:
        got = read_product(remote, aoi, nisar_apply_ionosphere=True, fileobj=f)
    assert (got.date1, got.date2) == (expected.date1, expected.date2)
    np.testing.assert_array_equal(got.los.values, expected.los.values)
    np.testing.assert_array_equal(got.coherence.values, expected.coherence.values)


def test_probe_reports_http_errors(server):
    base, _ = server
    with pytest.raises(RuntimeError):
        probe(f"{base}/missing.h5", requests.Session())


def test_probe_explains_login_and_permission_problems(server):
    base, _ = server
    with pytest.raises(AccessError, match="login page.*Authorized Apps"):
        probe(f"{base}/needs-login/NISAR_L2_GUNW.h5", requests.Session())
    with pytest.raises(AccessError, match="HTTP 403"):
        probe(f"{base}/forbidden/NISAR_L2_GUNW.h5", requests.Session())
    # a refused login must stop the run, not be skipped like a bad file (see Pipeline.load_and_stack)
    assert not issubclass(AccessError, (ValueError, KeyError, OSError, RuntimeError))


def test_saved_login_page_is_caught(tmp_path):
    page = tmp_path / "NISAR_L2_GUNW.h5"
    page.write_bytes(b"\n  <!DOCTYPE html><html>Earthdata Login</html>")
    with pytest.raises(AccessError):
        _check_not_login_page(page)
    assert not page.exists()
    good = tmp_path / "ok.h5"
    good.write_bytes(b"\x89HDF\r\n\x1a\n" + b"\0" * 100)
    _check_not_login_page(good)


def test_token_used_even_when_precheck_fails(monkeypatch):
    monkeypatch.setattr("sarcompare.download.check_token", lambda session, token: (False, "HTTP 401"))
    s = make_session(' "Bearer abc.def.ghi" ')
    assert s.headers["Authorization"] == "Bearer abc.def.ghi"
    assert s.token_check == (False, "HTTP 401")
    assert "earthdatacloud.nasa.gov" in s.auth_domains


def test_file_refused_by_storage_after_login_is_skipped_not_fatal(server):
    from sarcompare.remote import UnavailableError

    base, _ = server
    with pytest.raises(UnavailableError):  # redirect (login fine) -> file missing in storage
        probe(f"{base}/redirect/nisar/NOT_THERE.h5", requests.Session())

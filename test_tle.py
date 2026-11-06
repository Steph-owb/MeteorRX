"""TLE download robustness: fallback sources, error diagnosis, cache, import."""
import http.server
import shutil
import json
import os
import socket
import ssl
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from meteorrx import orbit  # noqa: E402

L1 = "1 25544U 98067A   19343.69339541  .00001764  00000-0  38792-4 0  9991"
L2 = "2 25544  51.6439 211.2001 0007417  17.6667  85.6398 15.50103472202482"


def sats():
    return [orbit.Satellite("ISS", 25544, [437.8])]


class _Handler(http.server.BaseHTTPRequestHandler):
    routes = {}

    def do_GET(self):
        code, ctype, body = self.routes.get(self.path.split("?")[0], (404, "text/plain", b"nope"))
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_checksum_and_parse():
    assert orbit.valid_tle(L1, L2, 25544)
    assert not orbit.valid_tle(L1[:-1] + "0", L2)
    txt = f"ISS (ZARYA)\r\n{L1}\r\n{L2}\r\nGARBAGE\n1 bad\n2 bad\n"
    assert orbit.parse_tle_text(txt) == {25544: (L1, L2)}


def test_fallback_to_second_source_json(server, tmp_path):
    _Handler.routes = {"/celestrak": (403, "text/plain", b"Forbidden"),
                       "/satnogs": (200, "application/json",
                                    json.dumps([{"tle0": "ISS", "tle1": L1, "tle2": L2}]).encode())}
    logs = []
    s = sats()
    ok = orbit.update_tles(s, str(tmp_path / "tle.txt"), 12, log=logs.append,
                           urls=[server + "/celestrak?CATNR={norad}", server + "/satnogs?id={norad}"])
    assert ok and s[0].sat is not None
    assert any("downloaded from 127.0.0.1" in x for x in logs)
    assert orbit.read_tle_file(str(tmp_path / "tle.txt")) == {25544: (L1, L2)}


def test_html_answer_rejected_and_reported(server, tmp_path):
    _Handler.routes = {"/x": (200, "text/html", b"<html>maintenance</html>")}
    logs = []
    ok = orbit.update_tles(sats(), str(tmp_path / "tle.txt"), 12, log=logs.append, urls=[server + "/x"])
    assert not ok
    assert any("no valid TLE in the answer" in x for x in logs)
    assert any("Import TLE file" in x for x in logs)


def test_offline_is_detected_quickly(tmp_path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing listens there -> connection refused
    import time
    logs = []
    t0 = time.monotonic()
    two = sats() + [orbit.Satellite("OTHER", 99999, [137.9])]
    orbit.update_tles(two, str(tmp_path / "tle.txt"), 12, log=logs.append,
                      urls=[f"http://127.0.0.1:{port}/a", f"http://127.0.0.1:{port}/b"])
    fails = [x for x in logs if "download failed" in x]
    assert len(fails) == 2 and all("connection refused" in x for x in fails)
    assert time.monotonic() - t0 < 3.0


def test_cache_used_when_download_fails(server, tmp_path):
    cache = tmp_path / "tle.txt"
    cache.write_text(f"ISS\n{L1}\n{L2}\n")
    os.utime(cache, (0, 0))  # very old -> refresh attempted
    _Handler.routes = {}
    s = sats()
    logs = []
    ok = orbit.update_tles(s, str(cache), 12, log=logs.append, urls=[server + "/missing"])
    assert ok and s[0].sat is not None
    assert any("HTTP 404" in x for x in logs)
    assert any("days old" in x for x in logs)  # 2019 TLE -> stale warning


def test_ssl_untrusted_certificate_reported(tmp_path):
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    if not shutil.which("openssl"):
        pytest.skip("openssl not available")
    r = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                        "-out", str(crt), "-days", "1", "-subj", "/CN=localhost"], capture_output=True)
    if r.returncode:
        pytest.skip("openssl not available")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        orbit.USE_QT_NETWORK = False
        with pytest.raises(orbit.TleError) as ei:
            orbit.fetch_tle(25544, f"https://localhost:{srv.server_address[1]}/x", timeout=5)
        orbit.USE_QT_NETWORK = True
        assert "SSL certificate not trusted" in str(ei.value) and not ei.value.offline
    finally:
        srv.shutdown()


def test_import_file(tmp_path):
    src = tmp_path / "weather.txt"
    src.write_text(f"NOAA X\n1 bad\n2 bad\nISS (ZARYA)\n{L1}\n{L2}\n")
    s = sats()
    n = orbit.import_tle_file(str(src), s, str(tmp_path / "data" / "tle.txt"), log=lambda m: None)
    assert n == 1 and s[0].sat is not None
    assert orbit.read_tle_file(str(tmp_path / "data" / "tle.txt")) == {25544: (L1, L2)}


def test_ssl_context_has_certifi_roots():
    ctx = orbit._ssl_context()
    assert ctx.cert_store_stats()["x509_ca"] > 100


def test_qt_network_fallback(server, monkeypatch):
    """Python stack fails -> the Qt (Windows Schannel) stack is used, from a worker thread."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])  # noqa: F841
    _Handler.routes = {"/tle": (200, "text/plain", f"ISS\n{L1}\n{L2}\n".encode())}

    def broken(*a, **k):
        raise ssl.SSLCertVerificationError("unable to get local issuer certificate")
    monkeypatch.setattr(orbit, "_http_get", broken)
    res = {}
    th = threading.Thread(target=lambda: res.update(r=orbit.fetch_tle(25544, server + "/tle", timeout=5)))
    th.start()
    th.join(15)
    assert res["r"] == (L1, L2, "system")


# --- not knocking on the servers' door more than we need ----------------------------------------
#
# CelesTrak asks programs to fetch only what they need. MeteorRX used to mark the whole satellite
# set as needing a refresh when a single one was missing from the cache, and a station that never
# gets that one retried every fifteen minutes for as long as it was left running - re-downloading
# every satellite each time. These pin the rate limiting that replaced that.

L1B = "1 59051U 24039A   26309.50000000  .00000000  00000+0  10000-4 0  9990"
L2B = "2 59051  98.7000 264.6000 0001000  90.0000 270.0000 14.22000000100007"


def two_sats():
    return [orbit.Satellite("ONE", 25544, [137.9]), orbit.Satellite("TWO", 59051, [137.9])]


@pytest.fixture
def counted(monkeypatch):
    """Serve the first satellite, never the second - the situation in a real tester's log."""
    calls = []

    def fetch(norad, url, proxy=""):
        calls.append(norad)
        if norad == 25544:
            return L1, L2, "x"
        raise orbit.TleError("not found")

    monkeypatch.setattr(orbit, "fetch_tle", fetch)
    return calls


def run(tmp_path, calls, **kw):
    before = len(calls)
    orbit.update_tles(two_sats(), str(tmp_path / "tle.txt"), 12, log=lambda m: None, **kw)
    return len(calls) - before


def age_the_state(tmp_path, hours=0.0, ok_days=None):
    """Pretend the last attempt (and optionally the last success) happened a while ago."""
    import time
    state = tmp_path / "tle.txt.state"
    st = json.loads(state.read_text())
    st["last_attempt"] -= hours * 3600
    if ok_days is not None:
        st["last_ok"] = st["last_attempt"] = time.time() - ok_days * 86400
    state.write_text(json.dumps(st))


def test_a_missing_satellite_does_not_re_download_the_others(tmp_path, counted):
    """One satellite missing used to mark the whole set as needing a refresh, so every retry
    fetched all of them again - the behaviour CelesTrak asks programs not to have."""
    run(tmp_path, counted, retry_h=1.0)
    assert 25544 in counted, "the first pass should fetch the one it can get"
    counted.clear()
    age_the_state(tmp_path, hours=2)                    # the retry window has passed
    run(tmp_path, counted, retry_h=1.0)
    assert counted, "it should try again for the one it is missing"
    assert set(counted) == {59051}, f"only the missing satellite, got {sorted(set(counted))}"


def test_a_failed_download_is_not_retried_immediately(tmp_path, counted):
    """A station with no internet used to ask again every quarter of an hour, all day."""
    first = run(tmp_path, counted, retry_h=1.0)
    assert first > 0
    assert [run(tmp_path, counted, retry_h=1.0) for _ in range(4)] == [0, 0, 0, 0]


def test_refresh_days_puts_a_floor_on_how_often_the_servers_are_asked(tmp_path, counted):
    run(tmp_path, counted, retry_h=1.0)
    age_the_state(tmp_path, ok_days=2)
    assert run(tmp_path, counted, refresh_days=7) == 0, "two days in, a seven-day setting must wait"
    assert run(tmp_path, counted, refresh_days=1) > 0, "two days in, a one-day setting must go"


def test_update_now_always_downloads(tmp_path, counted):
    """Whatever the settings say, an explicit request from the operator must act."""
    run(tmp_path, counted, retry_h=1.0)
    assert run(tmp_path, counted, refresh_days=7, retry_h=24.0, force=True) > 0


def test_the_floor_survives_a_restart(tmp_path, counted):
    """The state is a file, not something held in memory: restarting must not reset the limit."""
    run(tmp_path, counted, retry_h=1.0)
    assert (tmp_path / "tle.txt.state").is_file()
    assert run(tmp_path, counted, retry_h=1.0) == 0


def test_a_read_only_data_folder_still_gets_its_tles(tmp_path, counted, monkeypatch):
    """Losing the rate limiting is acceptable; failing to start is not."""
    real_open = open

    def no_write(path, mode="r", *a, **k):
        if "w" in mode and str(path).endswith(".state"):
            raise OSError("read-only")
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr("builtins.open", no_write)
    assert run(tmp_path, counted, retry_h=1.0) > 0

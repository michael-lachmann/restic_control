"""Tests for resticcontrol.backrest (no Backrest or AppKit needed)."""
import http.server
import plistlib
import socket
import threading

import pytest

from resticcontrol import backrest as br


def test_normalize_url():
    n = br.normalize_url
    assert n("") == "http://127.0.0.1:9898/"
    assert n("9900") == "http://127.0.0.1:9900/"
    assert n("nas") == "http://nas:9898/"
    assert n("nas.local:9000") == "http://nas.local:9000/"
    assert n("https://backrest.example.com") == "https://backrest.example.com/"
    assert n("http://10.0.0.5:9898/") == "http://10.0.0.5:9898/"


def test_url_for_bind_and_local():
    assert br.url_for_bind("127.0.0.1:9898") == "http://127.0.0.1:9898/"
    assert br.url_for_bind(":9898") == "http://127.0.0.1:9898/"
    assert br.url_for_bind("0.0.0.0:9999") == "http://127.0.0.1:9999/"
    assert br.url_for_bind("[::1]:9898") == "http://[::1]:9898/"
    assert br.is_local("http://127.0.0.1:9898/") and br.is_local("http://localhost:1/")
    assert not br.is_local("http://nas.example.com:9898/")


def test_parse_lsof():
    out = """COMMAND   PID    USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME
backrest 4711 michael   12u  IPv4 0x1234567890abcdef      0t0  TCP 127.0.0.1:9898 (LISTEN)
backrest 4711 michael   13u  IPv6 0x1234567890abcdee      0t0  TCP [::1]:9898 (LISTEN)
backrest 4711 michael   14u  IPv4 0x1234567890abcded      0t0  TCP *:9999 (LISTEN)
"""
    assert br.parse_lsof(out) == ["127.0.0.1:9898", "[::1]:9898", "*:9999"]


def test_configured_url(tmp_path):
    def plist(**kw):
        p = tmp_path / "x.plist"
        p.write_bytes(plistlib.dumps({"Label": "com.backrest", **kw}))
        return str(p)
    assert br.configured_url(plist(ProgramArguments=["/usr/local/bin/backrest"])) \
        == "http://127.0.0.1:9898/"
    assert br.configured_url(plist(ProgramArguments=["b"],
                                   EnvironmentVariables={"BACKREST_PORT": "0.0.0.0:9911"})) \
        == "http://127.0.0.1:9911/"
    assert br.configured_url(plist(ProgramArguments=["b", "--bind-address", "127.0.0.1:9922"])) \
        == "http://127.0.0.1:9922/"
    assert br.configured_url(str(tmp_path / "missing.plist")) is None


def _serve(body, status=200):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/"


def test_probe_backrest_other_and_closed():
    srv, url = _serve("<html><head><title>Backrest</title></head></html>")
    p = br.probe(url)
    assert p.reachable and p.is_backrest
    srv.shutdown()

    srv, url = _serve("<html><title>Some other thing</title></html>")
    p = br.probe(url)
    assert p.reachable and not p.is_backrest and "doesn't look like Backrest" in p.message
    srv.shutdown()

    srv, url = _serve("nope", status=404)
    p = br.probe(url)
    assert p.reachable and not p.is_backrest and "404" in p.message
    srv.shutdown()

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                    # nothing listens there now
    p = br.probe(f"http://127.0.0.1:{port}/", timeout=1)
    assert not p.reachable and "refused" in p.message


def test_probe_unknown_host():
    p = br.probe("http://no-such-host.invalid:9898/", timeout=2)
    assert not p.reachable


@pytest.mark.parametrize("delay", [0.5])
def test_wait_until_up(delay):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<title>Backrest</title>")

        def log_message(self, *a):
            pass

    def later():
        import time
        time.sleep(delay)
        srv = http.server.HTTPServer(("127.0.0.1", port), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=later, daemon=True).start()
    p = br.wait_until_up(f"http://127.0.0.1:{port}/", seconds=5, interval=0.2)
    assert p.is_backrest

"""Network access: the open service, the access code with login page and cookie, the desktop app's host and origin
checks and its network listener."""

import json
import sys
import urllib.error
import urllib.request
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from vinylcheck import network
from vinylcheck.config import Config
from vinylcheck.web import create_app

LAN_CLIENT = "192.168.1.50"
LOCAL_URL, LAN_URL = "http://127.0.0.1:8123", "http://192.168.1.10:8080"


def _cfg(tmp_path, **kw):
    return replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state", min_free_gb=0.0, **kw)


def _from(app, host):
    """The app as seen from client address `host` (the test client itself always reports "testclient")."""
    async def wrapped(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = dict(scope, client=(host, 50000))
        await app(scope, receive, send)
    return wrapped


def _clients(app):
    """(local, lan): the local client runs the app's start-up and shut-down, the LAN client only sends requests."""
    local = TestClient(_from(app, "127.0.0.1"), base_url=LOCAL_URL)
    lan = TestClient(_from(app, LAN_CLIENT), base_url=LAN_URL, follow_redirects=False)
    return local, lan


def test_helpers():
    code = network.new_code()
    assert len(code) == 9 and code[4] == "-" and set(code.replace("-", "")) <= set(network.CODE_ALPHABET)
    assert network.new_code() != code
    assert network.normalize_code(" k7qm-2xpa ") == "K7QM2XPA"
    assert network.is_loopback("127.0.0.1") and network.is_loopback("::1") and network.is_loopback("::ffff:127.0.0.1")
    assert not network.is_loopback(LAN_CLIENT) and not network.is_loopback("testclient") and not network.is_loopback(None)
    for ok in ("127.0.0.1:8123", "localhost:80", "[::1]:8080", "192.168.1.10:8080", "my-mac.local:8080", None):
        assert network.host_ok(ok), ok
    for bad in ("evil.example:8080", "127.0.0.1.nip.example", "[bad"):
        assert not network.host_ok(bad), bad
    assert network.host_ok("vinyl.example.net:443", ("Vinyl.Example.net.",))      # a configured proxy name
    assert not network.host_ok("vinyl.example.net", ("other.example.net",))
    assert network.same_origin("http://127.0.0.1:8123", "127.0.0.1:8123")
    assert not network.same_origin("http://evil.example", "127.0.0.1:8123") and not network.same_origin("null", "x:1")
    for a in network.lan_addresses(timeout=0.5):
        assert not a.startswith("127.")


def test_qr_svg():
    pytest.importorskip("segno")
    svg = network.qr_svg("http://192.0.2.1:8080/access?code=ABCD-EFGH")
    assert svg.startswith("<svg") and "viewBox" in svg and "#000" in svg


def test_service_stays_open(tmp_path):
    """A service without access_code behaves as before: open to the local network, no login."""
    app = create_app(_cfg(tmp_path))
    local, lan = _clients(app)
    with local:
        assert lan.get("/api/status").status_code == 200
        assert lan.get("/").status_code == 200
        assert lan.get("/access").status_code == 303                   # nothing to sign in to
        n = lan.get("/api/network").json()
        assert n["desktop"] is False and n["required"] is False and n["code"] is None
        assert local.put("/api/network", json={"lan_enabled": True}).status_code == 409
        r = lan.get("/api/status", headers={"Origin": "http://elsewhere.example"})
        assert r.status_code == 200                                    # reading cross-origin is guarded by CORS


def test_service_host_and_origin_checks(tmp_path):
    """A service checks Host and Origin like the desktop app: a web page open in a browser in the local network
    cannot change anything on the station or read its WebSocket, and a DNS name that points to the station is
    refused unless it is configured (a reverse proxy)."""
    app = create_app(_cfg(tmp_path, trusted_hosts=("Vinyl.Example.net",)))
    local, lan = _clients(app)
    with local:
        assert lan.get("/api/health", headers={"Host": "rebind.example"}).status_code == 400
        for host in ("192.168.1.10:8080", "pi.local", "vinyl.example.net", "localhost:8080"):
            assert lan.get("/api/health", headers={"Host": host}).status_code == 200, host
        foreign = {"Origin": "http://evil.example"}
        assert lan.post("/api/retention/run", headers=foreign).status_code == 403
        assert lan.delete("/api/records/1", headers=foreign).status_code == 403
        assert lan.put("/api/vinylmind", json={"contribute": True}, headers=foreign).status_code == 403
        assert lan.post("/api/records", json={"title": "x"}, headers={"Origin": "null"}).status_code == 403
        assert lan.get("/api/status", headers=foreign).status_code == 200
        same = {"Origin": LAN_URL}
        assert lan.post("/api/records", json={"title": "x"}, headers=same).status_code == 200
        assert lan.post("/api/records", json={"title": "y"}).status_code == 200    # scripts send no Origin
        # behind a reverse proxy with TLS: Origin and Host both carry the proxy's name
        proxied = {"Host": "vinyl.example.net", "Origin": "https://vinyl.example.net"}
        assert lan.post("/api/records", json={"title": "z"}, headers=proxied).status_code == 200
        ws_lan = LAN_URL.replace("http", "ws") + "/api/ws"
        with pytest.raises(Exception):
            with lan.websocket_connect(ws_lan, headers=foreign):
                pass
        with lan.websocket_connect(ws_lan, headers=same) as ws:
            assert json.loads(ws.receive_text())["version"]


def test_service_with_configured_code(tmp_path):
    app = create_app(_cfg(tmp_path, access_code="Turn-Table 42"))
    local, lan = _clients(app)
    with local:
        assert local.get("/api/status").status_code == 200              # the computer itself never needs the code
        r = lan.get("/api/status")
        assert r.status_code == 401 and "Access code required" in r.json()["detail"]
        r = lan.get("/")
        assert r.status_code == 303 and r.headers["location"] == "/access"
        page = lan.get("/access")
        assert page.status_code == 200 and 'name="code"' in page.text and "Access code" in page.text
        assert "Zugangscode" in lan.get("/access", params={"lang": "de"}).text
        wrong = lan.post("/access", data={"code": "nope"})
        assert wrong.status_code == 401 and "not correct" in wrong.text
        ok = lan.post("/access", data={"code": "turn table 42"})       # case, spaces and dashes do not matter
        assert ok.status_code == 303 and ok.headers["location"] == "/"
        cookie = ok.headers["set-cookie"].lower()
        assert "vc_access=" in cookie and "httponly" in cookie and "samesite=lax" in cookie
        assert "turn" not in cookie                                     # the cookie does not reveal the code
        assert lan.get("/api/status").status_code == 200
        assert lan.get("/api/network").json()["code"] is None           # the code is never sent to other devices


def test_desktop_code_qr_and_websocket(tmp_path):
    app = create_app(_cfg(tmp_path), desktop=True)
    local, lan = _clients(app)
    with local:
        n = local.get("/api/network").json()
        assert n["desktop"] and n["required"] and n["local"] and n["lan_enabled"] is False and not n["running"]
        code = n["code"]
        assert len(network.normalize_code(code)) == network.CODE_LEN
        assert local.get("/api/network").json()["code"] == code             # stable until a new one is made
        assert lan.get("/api/status").status_code == 401
        ws_lan = LAN_URL.replace("http", "ws") + "/api/ws"
        with pytest.raises(Exception):
            with lan.websocket_connect(ws_lan):
                pass
        r = lan.get(f"/access?code={code.lower()}")                    # the link in the QR code signs in directly
        assert r.status_code == 303 and "vc_access=" in r.headers["set-cookie"]
        assert lan.get("/api/status").status_code == 200
        with lan.websocket_connect(ws_lan, headers={"Cookie": f"vc_access={lan.cookies['vc_access']}"}) as ws:
            assert json.loads(ws.receive_text())["version"]
        n = lan.get("/api/network").json()
        assert n["local"] is False and n["code"] is None


def test_new_code_signs_devices_out(tmp_path):
    app = create_app(_cfg(tmp_path), desktop=True)
    local, lan = _clients(app)
    with local:
        old = local.get("/api/network").json()["code"]
        assert lan.post("/access", data={"code": old}).status_code == 303
        assert lan.get("/api/status").status_code == 200
        assert lan.post("/api/network/code").status_code == 403         # only on the computer itself
        assert lan.put("/api/network", json={"lan_enabled": False}).status_code == 403
        new = local.post("/api/network/code").json()["code"]
        assert new != old
        assert lan.get("/api/status").status_code == 401                # the old cookie is no longer valid
        assert lan.post("/access", data={"code": old}).status_code == 401
        assert lan.post("/access", data={"code": new}).status_code == 303


def test_wrong_codes_pause(tmp_path, monkeypatch):
    app = create_app(_cfg(tmp_path), desktop=True)
    local, lan = _clients(app)
    with local:
        code = local.get("/api/network").json()["code"]
        for _ in range(network.MAX_FAILURES):
            assert lan.post("/access", data={"code": "WRONG-CODE"}).status_code == 401
        r = lan.post("/access", data={"code": code})
        assert r.status_code == 429 and "wait a minute" in r.text            # even the right code waits
        assert local.get("/api/status").status_code == 200
        monkeypatch.setattr(network, "LOCK_S", 0.0)                          # the pause is over
        assert lan.post("/access", data={"code": code}).status_code == 303


def test_desktop_host_and_origin_checks(tmp_path):
    app = create_app(_cfg(tmp_path), desktop=True)
    local, _ = _clients(app)
    with local:
        assert local.get("/api/health", headers={"Host": "rebind.example:8123"}).status_code == 400
        assert local.get("/api/health", headers={"Host": "studio-mac.local:8123"}).status_code == 200
        foreign = {"Origin": "http://evil.example"}
        assert local.post("/api/records", json={"title": "x"}, headers=foreign).status_code == 403
        assert local.get("/api/health", headers=foreign).status_code == 200        # reading is guarded by CORS
        same = {"Origin": LOCAL_URL}
        assert local.post("/api/records", json={"title": "x"}, headers=same).status_code == 200
        ws_local = LOCAL_URL.replace("http", "ws") + "/api/ws"
        with pytest.raises(Exception):
            with local.websocket_connect(ws_local, headers=foreign):
                pass
        with local.websocket_connect(ws_local, headers=same) as ws:
            assert json.loads(ws.receive_text())["version"]


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None


def test_lan_listener(tmp_path, monkeypatch):
    """The setting starts a second server for the same app, keeps its port and is restored at the next start."""
    monkeypatch.setattr(network, "LAN_HOST", "127.0.0.1")                  # no listener on the real network in tests
    cfg = _cfg(tmp_path, port=0)
    app = create_app(cfg, desktop=True)
    local, _ = _clients(app)
    with local:
        n = local.put("/api/network", json={"lan_enabled": True}).json()
        assert n["lan_enabled"] and n["running"] and n["port"] and n["error"] is None
        port = n["port"]
        assert all(u["url"].endswith(f":{port}/") for u in n["urls"])
        if n["urls"] and n["urls"][0]["qr"] is not None:
            assert n["urls"][0]["qr"].startswith("<svg")
        st, body = _get(f"http://127.0.0.1:{port}/api/health")             # served without a second start-up
        assert st == 200 and body["ok"]
        n = local.put("/api/network", json={"lan_enabled": False}).json()
        assert not n["running"] and not n["lan_enabled"]
        with pytest.raises(OSError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2)
        local.put("/api/network", json={"lan_enabled": True})
    app.state.network.stop_lan()
    # next start: the setting is kept, the listener comes back on the same port
    app2 = create_app(cfg, desktop=True)
    app2.state.network.start_lan()
    try:
        new_port = app2.state.network.lan.port
        if sys.platform != "win32":       # Windows may hold the port for a moment; then any free port is taken
            assert new_port == port
        assert _get(f"http://127.0.0.1:{new_port}/api/health")[0] == 200
    finally:
        app2.state.network.stop_lan()

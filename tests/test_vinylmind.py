"""Vinyl-Mind, station side: identity and request signing, contributions, the signed model, switches, packages.

The fake hub below implements docs/vinyl-mind-protocol.md (draft v0) on its own, without the station's helpers, so a
mistake in the station's canonical strings or signatures shows up as a rejected request instead of agreeing with
itself. It runs as a real HTTP server on 127.0.0.1 (the only place plain HTTP is allowed)."""

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import replace

import numpy as np
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.testclient import TestClient

from vinylcheck import learning, network, vinylmind
from vinylcheck.config import Config
from vinylcheck.web import create_app

# The feature list of feature version "1" as the protocol document states it (not taken from learning.py)
FEATURES_V1 = ["log_strength", "amp_rel", "log_dur", "gc", "gc_missing", "music", "both_ch", "log_density", "amp_rel2",
               "strength_x_amp"]
INVITE = "K7QM-2XPA"


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _raw(pub) -> bytes:
    return pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


class FakeHub:
    """A minimal Vinyl-Mind hub after the protocol document: join with invitation or approval, signed requests with
    replay protection, contributions, a signed model with ETag, reports, leave."""

    def __init__(self):
        self.key = Ed25519PrivateKey.generate()
        self.pub = _raw(self.key.public_key())
        self.fp = hashlib.sha256(self.pub).hexdigest()
        self.stations: dict = {}
        self.contributions: dict = {}          # (station, profile_id) → payload
        self.reports: list = []
        self.nonces: dict = {}
        self.requests: list = []               # (method, path, headers) of every request
        self.model = None
        self.fail_next: list = []               # statuses to answer before handling the next requests
        self.app = self._build()
        self.url = ""

    @staticmethod
    def error(status: int, code: str, **extra) -> JSONResponse:
        return JSONResponse({"error": {"code": code, "message": code.replace("_", " ")} | extra}, status_code=status)

    def verify(self, request: Request, body: bytes, join_key: bytes | None = None):
        """The station id, or an error response (checks in the order of the protocol)."""
        h = request.headers
        names = ("vm-station", "vm-timestamp", "vm-nonce", "vm-content-sha256", "vm-signature")
        if any(n not in h for n in names):
            return self.error(401, "signature_missing")
        if hashlib.sha256(body).hexdigest() != h["vm-content-sha256"]:
            return self.error(400, "digest_mismatch")
        if abs(time.time() - int(h["vm-timestamp"])) > 300:
            return self.error(401, "clock_skew", server_time=time.time())
        sid = h["vm-station"]
        if join_key is not None:
            pub = join_key if hashlib.sha256(join_key).hexdigest() == sid else None
        else:
            pub = (self.stations.get(sid) or {}).get("public")
        if pub is None:
            return self.error(401, "unknown_station")
        path = request.scope["raw_path"].decode() + (f"?{request.url.query}" if request.url.query else "")
        canonical = "\n".join(("VM1-ED25519", request.method, path, h["vm-content-sha256"], h["vm-timestamp"],
                               h["vm-nonce"], sid)).encode()
        try:
            Ed25519PublicKey.from_public_bytes(pub).verify(_unb64u(h["vm-signature"]), canonical)
        except (InvalidSignature, ValueError):
            return self.error(401, "bad_signature")
        if (sid, h["vm-nonce"]) in self.nonces:
            return self.error(401, "replay")
        self.nonces[(sid, h["vm-nonce"])] = time.time()
        return sid

    def approved(self, request: Request, body: bytes):
        sid = self.verify(request, body)
        if isinstance(sid, Response):
            return sid
        if self.stations[sid]["status"] != "approved":
            return self.error(403, "pending")
        return sid

    def publish(self, kinds: dict, version: str = "t-1", key=None, tamper: str | None = None) -> dict:
        model = {"type": "vinyl-mind/model", "protocol": 0, "version": version, "feature_version": "1",
                 "features": FEATURES_V1, "detector_version": 1,
                 "chain_class": {"connection": "any", "cartridge": "any"}, "created": "2026-10-24T12:00:00Z",
                 "kinds": kinds}
        k = key or self.key
        signed = json.dumps(model, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        model["signature"] = {"alg": "ed25519", "key": hashlib.sha256(_raw(k.public_key())).hexdigest(),
                              "value": _b64u(k.sign(signed))}
        if tamper == "unsigned":
            del model["signature"]
        elif tamper == "weights":
            first = next(iter(model["kinds"].values()))
            first["weights"] = [w + 0.5 for w in first["weights"]]
        self.model = model
        return model

    def combine(self) -> dict:
        """Plain federated averaging of the received weights (the real hub weights and trims)."""
        out = {}
        for kind in ("click", "tick", "pop"):
            ws = [c["kinds"][kind] for c in self.contributions.values() if kind in c["kinds"]]
            if ws:
                out[kind] = {"intercept": round(float(np.mean([w["intercept"] for w in ws])), 6),
                             "weights": [round(float(x), 6) for x in np.mean([w["weights"] for w in ws], axis=0)],
                             "profiles": len(ws), "examples": sum(w["examples"]["master"] for w in ws)}
        return out

    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.middleware("http")
        async def log_and_fail(request: Request, call_next):
            self.requests.append((request.method, request.url.path, dict(request.headers)))
            if self.fail_next:
                return self.error(self.fail_next.pop(0), "server_error")
            return await call_next(request)

        @app.get("/v0/server")
        def server():
            return {"type": "vinyl-mind/server", "protocol": 0, "name": "Test hub", "public_key": _b64u(self.pub),
                    "fingerprint": self.fp, "feature_versions": ["1"], "join": {"invitation": True, "approval": True}}

        @app.post("/v0/stations")
        async def join(request: Request):
            body = await request.body()
            data = json.loads(body)
            if data.get("type") != "vinyl-mind/join" or data.get("protocol") != 0:
                return self.error(422, "invalid_payload")
            sid = self.verify(request, body, join_key=_unb64u(data["public_key"]))
            if isinstance(sid, Response):
                return sid
            known = sid in self.stations
            if not known:
                if data.get("invitation") not in (None, INVITE):
                    return self.error(403, "invitation_invalid")
                self.stations[sid] = {"public": _unb64u(data["public_key"]), "joined_at": time.time(),
                                      "status": "approved" if data.get("invitation") == INVITE else "pending"}
            return JSONResponse({"station": sid, "status": self.stations[sid]["status"], "server_fingerprint": self.fp},
                                status_code=200 if known else 201)

        @app.get("/v0/stations/me")
        async def me(request: Request):
            sid = self.verify(request, await request.body())
            if isinstance(sid, Response):
                return sid
            return {"station": sid, "status": self.stations[sid]["status"], "joined_at": "2026-10-24T12:00:00Z",
                    "contributions": [{"profile_id": p, "received_at": "x"} for s, p in self.contributions if s == sid]}

        @app.delete("/v0/stations/me")
        async def leave(request: Request):
            sid = self.verify(request, await request.body())
            if isinstance(sid, Response):
                return sid
            del self.stations[sid]
            for k in [k for k in self.contributions if k[0] == sid]:
                del self.contributions[k]
            self.reports = [r for r in self.reports if r[0] != sid]
            return Response(status_code=204)

        @app.put("/v0/contributions/{pid}")
        async def contribute(pid: str, request: Request):
            body = await request.body()
            sid = self.approved(request, body)
            if isinstance(sid, Response):
                return sid
            c = json.loads(body)
            if c.get("profile_id") != pid:
                return self.error(400, "profile_mismatch")
            if c.get("type") != "vinyl-mind/contribution" or c.get("features") != FEATURES_V1 or any(
                    len(k["weights"]) != len(FEATURES_V1) for k in c["kinds"].values()):
                return self.error(422, "invalid_payload")
            self.contributions[(sid, pid)] = c
            return {"profile_id": pid, "received_at": "2026-10-24T13:10:00Z"}

        @app.delete("/v0/contributions/{pid}")
        async def withdraw(pid: str, request: Request):
            sid = self.approved(request, await request.body())
            if isinstance(sid, Response):
                return sid
            self.contributions.pop((sid, pid), None)
            return Response(status_code=204)

        @app.get("/v0/models/current")
        async def model(request: Request, feature_version: str, detector_version: int):
            sid = self.approved(request, await request.body())
            if isinstance(sid, Response):
                return sid
            if self.model is None or feature_version != "1" or detector_version != 1:
                return self.error(404, "no_model")
            etag = f'"{self.model["version"]}"'
            if request.headers.get("if-none-match") == etag:
                return Response(status_code=304, headers={"ETag": etag})
            return JSONResponse(self.model, headers={"ETag": etag})

        @app.post("/v0/reports")
        async def report(request: Request):
            body = await request.body()
            sid = self.approved(request, body)
            if isinstance(sid, Response):
                return sid
            self.reports.append((sid, json.loads(body)))
            return JSONResponse({"received": True}, status_code=202)

        return app


@pytest.fixture()
def hub():
    h = FakeHub()
    srv = network.ServerThread(h.app, network.bind_socket("127.0.0.1", 0), name="fake-hub")
    srv.start()
    h.url = f"http://127.0.0.1:{srv.port}"
    yield h
    srv.stop()


def _events(rng, n_real, n_music, ref=-15.0):
    """Synthetic events of one recording: real clicks quiet and short, music false alarms as loud as the music."""
    ev = []
    for i in range(n_real):
        ev.append({"t": round(i * 1.7 + rng.uniform(0, 0.2), 4), "kind": "click", "region": "music", "ch": 2,
                   "strength": float(rng.uniform(40, 200)), "amp_dbfs": float(ref - rng.uniform(20, 35)),
                   "dur_ms": 0.4, "gc": None, "_y": 1})
    for i in range(n_music):
        ev.append({"t": round(0.8 + i * 2.3 + rng.uniform(0, 0.2), 4), "kind": "click", "region": "music", "ch": 3,
                   "strength": float(rng.uniform(30, 120)), "amp_dbfs": float(ref + rng.uniform(-4, 2)),
                   "dur_ms": 0.9, "gc": None, "_y": 0})
    return ev


def _seed(db, name, device, cartridge, recordings=3, seed=1) -> int:
    """A turntable profile with recordings that a master comparison fully classified."""
    rng = np.random.default_rng(seed)
    pid = db.add_profile(name=name, device=device, cartridge=cartridge)
    rid = db.add_record("Artist of " + name, "Title of " + name)
    for i in range(recordings):
        rec = db.add_recording(record_id=rid, profile_id=pid, side="A", kind="before", status="analyzed")
        ev = _events(rng, 40 + 5 * i, 25 + 3 * i)
        db.save_analysis(rec, {"metrics": {"music_ref_dbfs": -15.0}, "grading": {"grade": None, "score": None},
                               "warnings": [], "overview": {},
                               "events": [{k: e[k] for k in ("t", "dur_ms", "strength", "amp_dbfs", "ch", "kind",
                                                             "region", "gc")} for e in ev]}, "test", 1.0)
        db.set_master_check(rec, status="done", result={"clicks": {"events": [[e["t"], "v" if e["_y"] else "m"]
                                                                              for e in ev]}})
    return pid


def _app(tmp_path, name="station", **kw):
    cfg = replace(Config(), data_dir=tmp_path / name / "data", state_dir=tmp_path / name / "state", min_free_gb=0.0,
                  **kw)
    app = create_app(cfg)
    app.state.vinylmind.client_options = {"backoff": (0.0,), "sleep": lambda s: None, "timeout": 5.0}
    return app


@pytest.fixture()
def station(tmp_path):
    app = _app(tmp_path)
    db = app.state.db
    _seed(db, "Living room deck", "Core Audio: USB Audio CODEC", "Moving magnet, elliptical", seed=1)
    _seed(db, "Studio deck", "hw:CARD=Interface,DEV=0", "MC low output", seed=2)
    with TestClient(app) as c:
        c.app_ = app
        yield c


def _join(c, hub, invitation=INVITE):
    assert c.put("/api/vinylmind", json={"url": hub.url}).status_code == 200
    info = c.get("/api/vinylmind/server").json()
    assert info["fingerprint"] == hub.fp and info["supported"]
    r = c.post("/api/vinylmind/join", json={"invitation": invitation, "fingerprint": info["fingerprint_display"]})
    assert r.status_code == 200, r.text
    return r.json()


# ─── Identity and signing ────────────────────────────────────────────────────

def test_station_key_file(tmp_path):
    path = tmp_path / "state" / vinylmind.KEY_FILE
    key = vinylmind.StationKey.load_or_create(path)
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    again = vinylmind.StationKey.load_or_create(path)
    assert again.station_id == key.station_id == hashlib.sha256(key.public_raw).hexdigest()
    assert key.station_id[:12] in repr(key) and "PRIVATE" not in repr(key)
    if os.name == "posix":                                   # a file readable by others is narrowed again
        os.chmod(path, 0o644)
        vinylmind.StationKey.load(path)
        assert path.stat().st_mode & 0o777 == 0o600
    shown = vinylmind.show_fingerprint(key.station_id)
    assert len(shown.split()) == 16 and shown.replace(" ", "") == key.station_id
    vinylmind.StationKey.delete(path)
    assert not path.exists() and vinylmind.StationKey.load(path) is None


def test_signed_headers_verify_independently(tmp_path):
    key = vinylmind.StationKey.load_or_create(tmp_path / "k")
    body = b'{"a":1}'
    h = vinylmind.sign_headers(key, "put", "/prefix/v0/contributions/x?y=1", body)
    assert h["VM-Content-SHA256"] == hashlib.sha256(body).hexdigest() and len(h["VM-Nonce"]) == 32
    canonical = "\n".join(("VM1-ED25519", "PUT", "/prefix/v0/contributions/x?y=1", h["VM-Content-SHA256"],
                           h["VM-Timestamp"], h["VM-Nonce"], h["VM-Station"])).encode()
    Ed25519PublicKey.from_public_bytes(key.public_raw).verify(_unb64u(h["VM-Signature"]), canonical)
    assert vinylmind.sign_headers(key, "PUT", "/p", body)["VM-Nonce"] != h["VM-Nonce"]


def test_url_rules():
    for ok in ("https://vinyl-mind.example", "https://example.org/vinyl-mind/", "http://127.0.0.1:8123",
               "http://localhost:8080", "http://[::1]:9000"):
        assert vinylmind.normalize_url(ok) == ok.rstrip("/")
    for bad in ("http://vinyl-mind.example", "ftp://example.org", "https://user:pw@example.org",
                "https://example.org/?x=1", "https://example.org/#top", "", "https://", "http://192.168.1.10"):
        with pytest.raises(vinylmind.HubError):
            vinylmind.normalize_url(bad)


def test_replay_and_tampering_are_rejected_by_the_hub(hub, tmp_path):
    key = vinylmind.StationKey.load_or_create(tmp_path / "k")
    vinylmind.Client(hub.url, key).join(INVITE)

    def send(method, path, body=b"", headers=None):
        req = urllib.request.Request(hub.url + path, data=body or None, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    h = vinylmind.sign_headers(key, "GET", "/v0/stations/me", b"")
    assert send("GET", "/v0/stations/me", headers=h)[0] == 200
    status, err = send("GET", "/v0/stations/me", headers=h)                       # the same request again
    assert status == 401 and err["error"]["code"] == "replay"
    h = vinylmind.sign_headers(key, "GET", "/v0/stations/me", b"")
    assert send("DELETE", "/v0/stations/me", headers=h)[1]["error"]["code"] == "bad_signature"   # other method
    h = vinylmind.sign_headers(key, "PUT", "/v0/contributions/" + "a" * 32, b'{"x":1}')
    assert send("PUT", "/v0/contributions/" + "a" * 32, b'{"x":2}', h)[1]["error"]["code"] == "digest_mismatch"
    h = vinylmind.sign_headers(key, "GET", "/v0/stations/me", b"", now=time.time() - 900)
    assert send("GET", "/v0/stations/me", headers=h)[1]["error"]["code"] == "clock_skew"
    other = vinylmind.StationKey.load_or_create(tmp_path / "other")
    h = vinylmind.sign_headers(other, "GET", "/v0/stations/me", b"")
    assert send("GET", "/v0/stations/me", headers=h)[1]["error"]["code"] == "unknown_station"
    # the station's client signs every attempt anew, so its own retries are never replays
    hub.fail_next = [503]
    assert vinylmind.Client(hub.url, key, backoff=(0.0,)).me()["status"] == "approved"


def test_client_is_https_only_and_retries(hub, tmp_path):
    with pytest.raises(vinylmind.HubError):
        vinylmind.Client("http://vinyl-mind.example")
    hub.fail_next = [503, 502]
    slept = []
    assert vinylmind.Client(hub.url, sleep=slept.append).server_info()["fingerprint"] == hub.fp
    assert slept == [1.0, 2.0]                                            # backoff between the attempts
    hub.fail_next = [503, 503, 503]
    with pytest.raises(vinylmind.HubError) as e:
        vinylmind.Client(hub.url, backoff=(0.0,)).server_info()
    assert e.value.status == 503
    with pytest.raises(vinylmind.HubError) as e:                         # nothing listens there
        vinylmind.Client("http://127.0.0.1:9", backoff=(0.0,), timeout=1.0).server_info()
    assert e.value.code == "unreachable"


# ─── Contributions, model, report, leave ─────────────────────────────────────

def test_contribution_payload(station):
    db = station.app_.state.db
    prev = station.get("/api/vinylmind/preview").json()
    assert len(prev["profiles"]) == 2
    for row in prev["profiles"]:
        p = row["payload"]
        assert vinylmind.validate_contribution(p) == [], vinylmind.validate_contribution(p)
        assert p["features"] == FEATURES_V1 and p["feature_version"] == "1" and p["detector_version"] == 1
        assert p["profile_id"] == row["vm_id"] == db.profile_vm_id(row["id"]) and len(p["profile_id"]) == 32
        click = p["kinds"]["click"]
        assert click["quality"]["usable"] and click["quality"]["auc"] > 0.9
        assert click["examples"]["master"] > 100 and click["examples"]["manual"] == 0
        assert click["informative"][FEATURES_V1.index("gc_missing")] is False        # always missing here
        assert click["weights"][FEATURES_V1.index("gc_missing")] == 0.0
        text = json.dumps(p)
        for private in (row["name"], "USB Audio CODEC", "Interface", "elliptical", "Artist of", "Title of"):
            assert private not in text                                   # names never leave the station
    chains = {r["name"]: r["chain"] for r in prev["profiles"]}
    assert chains["Living room deck"] == {"connection": "usb_turntable", "cartridge": "mm"}
    assert chains["Studio deck"] == {"connection": "interface", "cartridge": "mc"}
    # the profile id is random, stable, and not derived from anything
    assert station.get("/api/vinylmind/preview").json()["profiles"][0]["vm_id"] == prev["profiles"][0]["vm_id"]
    # the chain class can be set by hand
    pid = prev["profiles"][0]["id"]
    r = station.put("/api/vinylmind", json={"chain": {str(pid): {"connection": "interface", "cartridge": "unknown"}}})
    assert r.status_code == 200
    row = next(x for x in station.get("/api/vinylmind/preview").json()["profiles"] if x["id"] == pid)
    assert row["chain_set"] and row["payload"]["chain_class"] == {"connection": "interface", "cartridge": "unknown"}
    assert station.put("/api/vinylmind", json={"chain": {str(pid): {"connection": "x"}}}).status_code == 422


def test_join_contribute_adopt_report_leave(station, hub):
    app = station.app_
    st = _join(station, hub)
    assert st["state"] == "approved" and st["joined"]["fingerprint"] == vinylmind.show_fingerprint(hub.fp)
    key_file = app.state.cfg.state_dir / vinylmind.KEY_FILE
    assert key_file.exists() and len(hub.stations) == 1
    raw_db = app.state.cfg.db_path.read_bytes()
    assert b"PRIVATE KEY" not in raw_db and key_file.read_bytes() not in raw_db     # the key never goes to the DB

    # contribute: exactly what the preview showed
    preview = {r["vm_id"]: r["payload"] for r in station.get("/api/vinylmind/preview").json()["profiles"]}
    assert station.put("/api/vinylmind", json={"contribute": True}).json()["contribute"] is True
    r = station.post("/api/vinylmind/sync")
    assert r.status_code == 200, r.text
    assert r.json()["result"]["contributions"]["sent"] == 2
    got = {pid: c for (_, pid), c in hub.contributions.items()}
    assert got == preview
    assert r.json()["status"]["last_contribution"]["profiles"] == 2
    # nothing changed: nothing is sent again
    n = len(hub.requests)
    assert station.post("/api/vinylmind/sync").json()["result"]["contributions"]["sent"] == 0
    assert not [q for q in hub.requests[n:] if q[0] == "PUT"]

    # the hub combines and publishes a signed model; the station verifies, checks, adopts and reports
    hub.publish(hub.combine(), version="t-1")
    assert station.put("/api/vinylmind", json={"use_model": True}).status_code == 200
    r = station.post("/api/vinylmind/sync").json()
    assert r["result"]["model"] == "new", r
    st = r["status"]
    assert st["model"]["version"] == "t-1" and st["model"]["adopted"] == ["click"]
    assert st["prior"]["source"] == "server" and st["prior"]["in_use"]
    learned = json.loads(app.state.db.setting("learn_model"))
    assert learned["prior"] == {"source": "server", "type": "model", "ref": "t-1"}
    assert learned["prior_kinds"] == ["click"]
    assert len(hub.reports) == 1
    report = hub.reports[0][1]
    assert vinylmind.validate_report(report) == [] and report["model_version"] == "t-1"
    assert report["adopted"] == {"click": True} and len(report["profiles"]) == 2
    check = report["profiles"][0]["kinds"]["click"]
    assert check["recordings"] == 3 and check["global_error"] is not None and check["local_error"] is not None
    # asked again: the ETag answers 304 without a body, nothing is reported twice
    n = len(hub.requests)
    assert station.post("/api/vinylmind/sync").json()["result"]["model"] == "unchanged"
    model_req = [q for q in hub.requests[n:] if q[1] == "/v0/models/current"][0]
    assert model_req[2].get("if-none-match") == '"t-1"' and len(hub.reports) == 1
    # turning the switch off removes the adopted model from the training
    st = station.put("/api/vinylmind", json={"use_model": False}).json()
    assert st["prior"] is None and "prior" not in json.loads(app.state.db.setting("learn_model"))

    # a deleted profile's contribution is withdrawn
    victim = next(p for p in app.state.db.profiles() if p["name"] == "Studio deck")
    app.state.db.delete_profile(victim["id"])
    app.state.vinylmind.note_retrained()
    r = station.post("/api/vinylmind/sync").json()["result"]["contributions"]
    assert r["withdrawn"] == 1 and len(hub.contributions) == 1

    # the server URL cannot change while joined
    assert station.put("/api/vinylmind", json={"url": "https://other.example"}).status_code == 409
    # leave: the hub forgets the station, the key file is gone
    st = station.post("/api/vinylmind/leave").json()
    assert st["state"] == "not_joined" and st["station_fingerprint"] is None
    assert not hub.stations and not hub.contributions and not key_file.exists()
    assert station.put("/api/vinylmind", json={"reset_url": True}).json()["url"] == ""


def test_pending_until_approved(station, hub):
    st = _join(station, hub, invitation=None)
    assert st["state"] == "pending"
    station.put("/api/vinylmind", json={"contribute": True})
    r = station.post("/api/vinylmind/sync").json()
    assert r["result"] == {"status": "pending"} and not hub.contributions
    sid = next(iter(hub.stations))
    hub.stations[sid]["status"] = "approved"                              # the operator approves
    r = station.post("/api/vinylmind/sync").json()
    assert r["status"]["state"] == "approved" and r["result"]["contributions"]["sent"] == 2
    bad = station.post("/api/vinylmind/join", json={"invitation": None, "fingerprint": "00" * 32})
    assert bad.status_code == 409 and "Nothing was sent" in bad.json()["detail"]


@pytest.mark.parametrize("tamper", ["unsigned", "wrong_key", "weights"])
def test_refuses_unsigned_or_wrongly_signed_model(station, hub, tamper):
    _join(station, hub)
    station.put("/api/vinylmind", json={"contribute": True})
    station.post("/api/vinylmind/sync")
    other = Ed25519PrivateKey.generate()
    hub.publish(hub.combine(), version="evil", key=other if tamper == "wrong_key" else None,
                tamper=tamper if tamper != "wrong_key" else None)
    station.put("/api/vinylmind", json={"use_model": True})
    r = station.post("/api/vinylmind/sync")
    assert r.status_code == 502
    expected = {"unsigned": "not signed", "wrong_key": "another key", "weights": "signature is not valid"}[tamper]
    assert expected in r.json()["detail"]
    st = station.get("/api/vinylmind").json()
    assert st["model"] is None and st["prior"] is None and expected in st["last_error"]
    assert not hub.reports and "prior" not in json.loads(station.app_.state.db.setting("learn_model") or "{}")


def test_switches_off_nothing_is_sent(station, hub):
    vm = station.app_.state.vinylmind
    _join(station, hub)
    n = len(hub.requests)
    assert vm.run_round(force=True) == {"skipped": "off"}
    assert station.post("/api/vinylmind/sync").status_code == 409
    vm.note_retrained()
    assert vm.run_round() == {"skipped": "off"}
    assert len(hub.requests) == n                                          # not a single request
    # contributing on, but a recording runs: still nothing
    station.put("/api/vinylmind", json={"contribute": True})
    vm.busy = lambda: True
    assert vm.run_round(force=True) == {"skipped": "recording"}
    assert station.post("/api/vinylmind/join", json={}).status_code == 409
    assert len(hub.requests) == n
    vm.busy = lambda: False
    assert vm.run_round(force=True)["contributions"]["sent"] == 2
    # a recording that starts during the round stops it before the next request
    calls = iter([False, True])
    vm.busy = lambda: next(calls, True)
    vm.note_retrained()
    app_db = station.app_.state.db
    app_db.set_setting(vinylmind.S_SENT, "{}")                            # pretend nothing was sent yet
    n = len(hub.requests)
    assert vm.run_round(force=False)["skipped"] == "recording"
    assert len(hub.requests) - n <= 1


# ─── Package files ───────────────────────────────────────────────────────────

def test_export_import_round_trip(station, tmp_path):
    prev = station.get("/api/vinylmind/preview").json()["profiles"][0]
    r = station.get("/api/vinylmind/export", params={"profile": prev["id"]})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    package = r.json()
    assert package == prev["payload"] and vinylmind.validate_contribution(package) == []
    # a station's own contribution is refused as a prior
    own = station.post("/api/vinylmind/import", content=r.content)
    assert own.status_code == 422 and "own turntable profiles" in own.json()["detail"]

    other = _app(tmp_path, "other")
    _seed(other.state.db, "Other deck", "", "", recordings=1, seed=9)
    with TestClient(other) as c:
        d = c.post("/api/vinylmind/import", content=r.content).json()["package"]       # first: what is it?
        assert d["type"] == "contribution" and d["kinds"]["click"]["usable"] and d["ref"] == package["profile_id"][:8]
        assert c.get("/api/vinylmind").json()["prior"] is None                       # nothing changed yet
        st = c.post("/api/vinylmind/import", params={"confirm": True}, content=r.content).json()["status"]
        assert st["prior"]["source"] == "import" and st["prior"]["kinds"] == ["click"] and st["prior"]["in_use"]
        learned = json.loads(other.state.db.setting("learn_model"))
        assert learned["prior"]["source"] == "import" and learned["prior_kinds"] == ["click"]
        # a model file shared by hand works too, signed or not
        model = dict(package, type="vinyl-mind/model", version="file-1", created="2026-10-24T12:00:00Z",
                     chain_class={"connection": "any", "cartridge": "any"},
                     kinds={"click": {k: package["kinds"]["click"][k] for k in ("intercept", "weights")}})
        for k in ("profile_id", "vinyl_check_version"):
            model.pop(k)
        d = c.post("/api/vinylmind/import", json=model).json()["package"]
        assert d["type"] == "model" and d["signature"] == "none"
        # another feature version, broken files
        bad = dict(package, feature_version="2")
        assert "Feature version 2" in c.post("/api/vinylmind/import", json=bad).json()["detail"]
        bad = json.loads(r.content)
        bad["kinds"]["click"]["weights"] = bad["kinds"]["click"]["weights"][:3]
        assert "expected 10 entries" in c.post("/api/vinylmind/import", json=bad).json()["detail"]
        assert c.post("/api/vinylmind/import", content=b"not json").status_code == 422
        assert c.post("/api/vinylmind/import", content=b"x" * (vinylmind.MAX_PACKAGE_BYTES + 1)).status_code == 413
        st = c.delete("/api/vinylmind/prior").json()
        assert st["prior"] is None and "prior" not in json.loads(other.state.db.setting("learn_model"))


def test_model_check_and_adoption_rule():
    rng = np.random.default_rng(3)
    data = {"click": None}
    X, y, rec, mas = [], [], [], []
    for g in range(1, 4):
        ev = _events(rng, 40, 25)
        F = learning.features(ev, -15.0)
        for i, e in enumerate(ev):
            X.append(F[i]); y.append(e["_y"]); rec.append(g); mas.append(True)
    data["click"] = {"X": np.array(X), "y": np.array(y, float), "w": np.ones(len(y)), "rec": np.array(rec),
                     "record": np.ones(len(y)), "manual": np.zeros(len(y), bool), "master": np.array(mas),
                     "raw_n": {g: rec.count(g) for g in set(rec)}}
    good = learning.to_fixed(learning.train(data)["kinds"]["click"]["model"])
    bad = {"intercept": 5.0, "weights": [0.0] * len(good["weights"])}       # calls everything a real click
    ok = vinylmind.check_model(data, {"click": good})["click"]
    worse = vinylmind.check_model(data, {"click": bad})["click"]
    assert ok["recordings"] == 3 and ok["global_error"] < 0.2 and vinylmind.adopt(ok)
    assert worse["global_error"] > 0.5
    assert vinylmind.adopt({"local_error": None, "prior_error": None, "global_error": None, "recordings": 0})
    assert not vinylmind.adopt({"local_error": 0.05, "prior_error": 0.08, "global_error": 0.4, "recordings": 3})

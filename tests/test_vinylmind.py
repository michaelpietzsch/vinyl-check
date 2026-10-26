"""Vinyl-Mind, station side, against the hub's specification (protocol version 1).

The fake hub and the fake account service below implement the specification on their own (spec/signing.md,
docs/api.md, docs/connect.md of the Vinyl-Mind repository), without the station's helpers, so a mistake in the
station's signed message, its encodings or its documents shows up as a refused request instead of agreeing with
itself. Every document the fake hub receives is validated against the hub's JSON schemas (a copy in
tests/vinyl_mind_spec). Both run as real HTTP servers on 127.0.0.1, the only place plain HTTP is allowed. The test
against the real hub is in test_vinylmind_hub.py."""

import base64
import copy
import hashlib
import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.testclient import TestClient

from vinylcheck import detect, learning, network, vinylmind
from vinylcheck.config import Config
from vinylcheck.web import create_app

jsonschema = pytest.importorskip("jsonschema")

SPEC = Path(__file__).parent / "vinyl_mind_spec" / "v1"
FEATURES = json.loads((SPEC / "features.json").read_text())
SCHEMAS = {n: json.loads((SPEC / f"{n}.schema.json").read_text())
           for n in ("contribution", "global-model", "check-report")}
CLIENT_RE = re.compile(r"^[a-z0-9-]{1,32}/[0-9A-Za-z][0-9A-Za-z.+_-]{0,31}$")
LOCAL = {"base_url": "http://127.0.0.1:8123", "client": ("127.0.0.1", 50123)}   # the browser on this computer


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _raw(pub) -> bytes:
    return pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _fp(raw: bytes) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def _strict(raw: bytes):
    def pairs(items):
        keys = [k for k, _ in items]
        assert len(keys) == len(set(keys)), "duplicate key"
        return dict(items)
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                      parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))


def _jws(key, header: dict, payload: dict) -> str:
    h = _b64u(json.dumps(header, separators=(",", ":")).encode())
    p = _b64u(json.dumps(payload, separators=(",", ":")).encode())
    return f"{h}.{p}.{_b64u(key.sign(f'{h}.{p}'.encode()))}"


def _schema(name: str, feature_version: int = 1) -> dict:
    """The hub's schema; feature version 2 (not in the specification yet) as the same form with 12 features."""
    s = copy.deepcopy(SCHEMAS[name])
    if feature_version != 1:
        s["properties"]["feature_version"]["const"] = feature_version
        if name == "contribution":
            s["$defs"]["kind"]["properties"]["coef"].update(minItems=13, maxItems=13)
            s["$defs"]["kind"]["properties"]["informative"].update(minItems=12, maxItems=12)
    return s


def _schema_errors(name: str, doc, feature_version: int = 1) -> list[str]:
    v = jsonschema.Draft202012Validator(_schema(name, feature_version))
    return [f"{'/'.join(map(str, e.path))}: {e.message}" for e in v.iter_errors(doc)]


# ─── A hub after the specification ───────────────────────────────────────────────────────────────────────────────

class FakeHub:
    """Join with invitation (also bound ones with a binding, docs/connect.md) or approval, signed requests with
    replay protection and the client header, contributions, signed models with ETag, check reports, leave."""

    def __init__(self, *, join_mode="invitation", feature_versions=(1,), clients=(), detector_versions=(),
                 connect_url=None):
        self.key = Ed25519PrivateKey.generate()
        self.pub = _raw(self.key.public_key())
        self.fp = _fp(self.pub)
        self.join_mode, self.feature_versions = join_mode, list(feature_versions)
        self.clients, self.detector_versions, self.connect_url = list(clients), list(detector_versions), connect_url
        self.binding_keys: dict = {}            # account service key fingerprint → raw public key
        self.stations: dict = {}                # station fingerprint → {"public", "status", "account", "invitation"}
        self.invitations: dict = {}             # code → {"id", "uses", "bound", "revoked"}
        self.contributions: dict = {}           # (station, profile) → document
        self.reports: dict = {}                 # (station, profile, version) → document
        self.models: dict = {}                  # version → {"status", "payload", "jws", "reason"}
        self.nonces: set = set()
        self.requests: list = []                # (method, target, headers, body) of every request
        self.fail_next: list = []               # statuses to answer before handling the next requests
        self.join_limit = False
        self.time_offset = 0
        self.app = self._build()
        self.url = ""

    def invite(self, bound: bool = False, uses: int = 1) -> tuple[int, str]:
        iid, code = len(self.invitations) + 1, _b64u(secrets.token_bytes(16))
        self.invitations[code] = {"id": iid, "uses": uses, "bound": bound, "revoked": False}
        return iid, code

    @staticmethod
    def error(status: int, code: str, headers: dict | None = None) -> JSONResponse:
        return JSONResponse({"error": code, "message": code.replace("_", " ")}, status_code=status, headers=headers)

    def _check_client(self, request: Request):
        c = request.headers.get("vinyl-mind-client", "")
        if not CLIENT_RE.match(c):
            return self.error(400, "bad_client")
        if self.clients and not any(c == a or (a.endswith("*") and c.startswith(a[:-1])) for a in self.clients):
            return self.error(403, "client_not_accepted")
        return None

    def _verify(self, request: Request, body: bytes, join_key: bytes | None = None):
        """The station's fingerprint after checking the signature (spec/signing.md), or an error response."""
        h = request.headers
        names = ("vinyl-mind-key", "vinyl-mind-timestamp", "vinyl-mind-nonce", "vinyl-mind-signature")
        if any(n not in h for n in names):
            return self.error(401, "unsigned")
        kfp, ts, nonce = h["vinyl-mind-key"], h["vinyl-mind-timestamp"], h["vinyl-mind-nonce"]
        if not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", kfp) or not re.fullmatch(r"[A-Za-z0-9_-]{22,64}", nonce) \
                or not ts.isdigit():
            return self.error(401, "malformed_signature")
        if abs(time.time() + self.time_offset - int(ts)) > 300:
            return self.error(401, "stale")
        if join_key is not None:
            pub = join_key
        else:
            st = self.stations.get(kfp)
            if st is None:
                return self.error(401, "unknown_key")
            pub = st["public"]
        if _fp(pub) != kfp:
            return self.error(401, "bad_signature")
        target = request.scope["raw_path"].decode() + (
            "?" + request.scope["query_string"].decode() if request.scope["query_string"] else "")
        message = "\n".join(("vinyl-mind-request-v1", self.fp, request.method, target, hashlib.sha256(body).hexdigest(),
                             ts, nonce, kfp)).encode()
        try:
            Ed25519PublicKey.from_public_bytes(pub).verify(_unb64u(h["vinyl-mind-signature"]), message)
        except (InvalidSignature, ValueError):
            return self.error(401, "bad_signature")
        if (kfp, nonce) in self.nonces:
            return self.error(401, "replayed")
        self.nonces.add((kfp, nonce))
        return kfp

    def _station(self, request: Request, body: bytes, allow_pending: bool = False):
        kfp = self._verify(request, body)
        if isinstance(kfp, Response):
            return kfp
        st = self.stations[kfp]
        if st["status"] == "revoked":
            return self.error(403, "revoked")
        if st["status"] == "pending" and not allow_pending:
            return self.error(403, "pending")
        if not allow_pending:
            bad = self._check_client(request)
            if bad:
                return bad
        return kfp

    def _check_binding(self, binding: str, station: str, invitation_id: int):
        """docs/connect.md, Join: the checks in their order; None when the binding holds."""
        parts = binding.split(".")
        if len(parts) != 3:
            return "parts"
        try:
            header, payload = _strict(_unb64u(parts[0])), _strict(_unb64u(parts[1]))
        except (ValueError, AssertionError):
            return "json"
        if header != {"alg": "EdDSA", "typ": "vinyl-mind-binding+json", "kid": header.get("kid")}:
            return "header"
        if header["kid"] not in self.binding_keys:
            return "kid"
        try:
            Ed25519PublicKey.from_public_bytes(self.binding_keys[header["kid"]]).verify(
                _unb64u(parts[2]), f"{parts[0]}.{parts[1]}".encode())
        except (InvalidSignature, ValueError):
            return "signature"
        if set(payload) != {"v", "invitation_id", "station", "account", "iat", "exp"} or payload["v"] != 1:
            return "payload"
        now = time.time()
        if payload["iat"] > now + 300 or payload["exp"] <= now or payload["exp"] - payload["iat"] > 7 * 86400:
            return "time"
        if payload["station"] != station or payload["invitation_id"] != invitation_id:
            return "station"
        return None

    def payload(self, coefs: dict, version: int, *, chain_class="all", scaling=None, server=None,
                feature_version=1, detector_version="1") -> dict:
        return {"spec": "vinyl-mind/global-model", "version": version, "feature_version": feature_version,
                "detector_version": detector_version, "chain_class": chain_class, "created_at": int(time.time()),
                "server": server or self.fp, "features": [f["name"] for f in FEATURES["features"]],
                "scaling": scaling or {"center": [f["center"] for f in FEATURES["features"]],
                                       "scale": [f["scale"] for f in FEATURES["features"]]},
                "aggregation": {"method": "trimmed_mean", "trim": 0.2, "manual_weight": 5.0, "max_est_error": 0.3,
                                "max_station_share": 0.25},
                "kinds": {k: {"coef": c, "spread": [0.1] * len(c), "profiles": 3, "stations": 2, "examples": 900.0}
                          for k, c in coefs.items()}}

    def publish(self, coefs: dict, status: str = "published", *, key=None, tamper: str | None = None,
                **payload_kw) -> int:
        version = max(self.models, default=0) + 1
        p = self.payload(coefs, version, **payload_kw)
        if payload_kw.get("feature_version", 1) == 1 and not tamper:
            assert not _schema_errors("global-model", p), _schema_errors("global-model", p)
        k = key or self.key
        header = {"alg": "EdDSA", "typ": "vinyl-mind-model+json", "kid": _fp(_raw(k.public_key()))}
        if tamper == "alg":
            header["alg"] = "HS256"
        if tamper == "kid":
            header["kid"] = _fp(b"x" * 32)
        token = _jws(k, header, p)
        if tamper == "signature":
            h, pl, s = token.split(".")
            p2 = dict(p, kinds={kk: dict(v, coef=[x + 0.5 for x in v["coef"]]) for kk, v in p["kinds"].items()})
            token = f"{h}.{_b64u(json.dumps(p2, separators=(',', ':')).encode())}.{s}"
        if tamper == "unsigned":
            token = _b64u(json.dumps(p).encode())
        self.models[version] = {"status": status, "payload": p, "jws": token, "reason": ""}
        return version

    def combine(self, feature_version: int = 1) -> dict:
        """Plain mean of the received coefficients of one feature version (the real hub weights and trims)."""
        out = {}
        for kind in ("click", "tick", "pop"):
            cs = [c["kinds"][kind]["coef"] for c in self.contributions.values()
                  if kind in c["kinds"] and c["feature_version"] == feature_version]
            if cs:
                out[kind] = [round(float(x), 6) for x in np.mean(cs, axis=0)]
        return out

    def entry(self, version: int) -> dict:
        m = self.models[version]
        e = {"version": version, "status": m["status"], "feature_version": m["payload"]["feature_version"],
             "detector_version": m["payload"]["detector_version"], "chain_class": m["payload"]["chain_class"],
             "created_at": m["payload"]["created_at"], "published_at": None, "withdrawn_at": None}
        if m["status"] == "withdrawn":
            e["reason"] = m["reason"]
        else:
            e["jws"] = m["jws"]
        return e

    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.middleware("http")
        async def log_and_fail(request: Request, call_next):
            body = await request.body()
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            self.requests.append((request.method, target, dict(request.headers), body))
            if request.method in ("POST", "PUT") and ("content-length" not in request.headers
                                                      or "chunked" in request.headers.get("transfer-encoding", "")):
                return self.error(411, "length_required")
            if self.fail_next:
                return self.error(self.fail_next.pop(0), "server_error")
            return await call_next(request)

        @app.get("/v1/server")
        def server():
            out = {"name": "vinyl-mind", "version": "test", "public_key": _b64u(self.pub), "fingerprint": self.fp,
                   "join": self.join_mode, "feature_versions": self.feature_versions, "clients": self.clients,
                   "detector_versions": self.detector_versions, "time": int(time.time() + self.time_offset),
                   "limits": {"max_body": 16384, "max_profiles": 16}}
            if self.connect_url:
                out["connect_url"] = self.connect_url
            return out

        @app.post("/v1/join")
        async def join(request: Request):
            body = await request.body()
            try:
                data = _strict(body)
            except (ValueError, AssertionError):
                return self.error(422, "invalid")
            if not isinstance(data, dict) or set(data) - {"public_key", "invitation", "binding"}:
                return self.error(422, "invalid")
            inv = data.get("invitation")
            if inv is not None and not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", inv):
                return self.error(403, "invitation_invalid")
            kfp = self._verify(request, body, join_key=_unb64u(data["public_key"]))
            if isinstance(kfp, Response):
                return kfp
            bad = self._check_client(request)
            if bad:
                return bad
            known = self.stations.get(kfp)
            if known is None and self.join_limit:
                return self.error(429, "join_limit", headers={"Retry-After": "3600"})
            if known is None:
                if inv is None:
                    if self.join_mode == "invitation":
                        return self.error(403, "invitation_required")
                    status, account, iid = "pending", None, None
                else:
                    i = self.invitations.get(inv)
                    if not i or i["uses"] < 1 or i["revoked"]:
                        return self.error(403, "invitation_invalid")
                    if i["bound"] and not data.get("binding"):
                        return self.error(403, "binding_required")
                    if data.get("binding"):
                        if self._check_binding(data["binding"], kfp, i["id"]):
                            return self.error(403, "binding_invalid")
                    i["uses"] -= 1
                    status, iid = "active", i["id"]
                    account = _strict(_unb64u(data["binding"].split(".")[1]))["account"] if data.get("binding") \
                        else None
                self.stations[kfp] = known = {"public": _unb64u(data["public_key"]), "status": status,
                                              "account": account, "invitation": iid}
                code = 201
            else:
                code = 200
            if known["status"] == "revoked":
                return self.error(403, "revoked")
            return JSONResponse({"station": kfp, "status": known["status"], "server": self.fp}, status_code=code)

        @app.get("/v1/me")
        async def me(request: Request):
            kfp = self._station(request, await request.body(), allow_pending=True)
            if isinstance(kfp, Response):
                return kfp
            return {"station": kfp, "status": self.stations[kfp]["status"], "joined_at": 1, "approved_at": None,
                    "profiles": [{"profile_id": p} for s, p in self.contributions if s == kfp], "reports": []}

        @app.delete("/v1/me")
        async def leave(request: Request):
            kfp = self._station(request, await request.body(), allow_pending=True)
            if isinstance(kfp, Response):
                return kfp
            del self.stations[kfp]
            for k in [k for k in self.contributions if k[0] == kfp]:
                del self.contributions[k]
            for k in [k for k in self.reports if k[0] == kfp]:
                del self.reports[k]
            return Response(status_code=204)

        @app.put("/v1/profiles/{pid}")
        async def contribute(pid: str, request: Request):
            body = await request.body()
            kfp = self._station(request, body)
            if isinstance(kfp, Response):
                return kfp
            if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", pid):
                return self.error(400, "bad_profile_id")
            try:
                c = _strict(body)
            except (ValueError, AssertionError):
                return self.error(422, "invalid")
            fv = c.get("feature_version") if isinstance(c, dict) else None
            if fv not in self.feature_versions:
                return self.error(422, "feature_version_not_accepted")
            errs = _schema_errors("contribution", c, fv)
            for k, v in c.get("kinds", {}).items():
                if v["n_manual"] + v["n_master"] != v["n_real"] + v["n_false"]:
                    errs.append(f"kinds/{k}: counts")
            if errs:
                return JSONResponse({"error": "invalid", "message": errs[0]}, status_code=422)
            if f"{c['client']['name']}/{c['client']['version']}" != request.headers["vinyl-mind-client"]:
                return self.error(422, "invalid")
            if self.detector_versions and c["detector_version"] not in self.detector_versions:
                return self.error(422, "detector_version_not_accepted")
            changed = self.contributions.get((kfp, pid)) != c
            self.contributions[(kfp, pid)] = c
            return {"profile_id": pid, "changed": changed, "received_at": int(time.time())}

        @app.delete("/v1/profiles/{pid}")
        async def withdraw(pid: str, request: Request):
            kfp = self._station(request, await request.body())
            if isinstance(kfp, Response):
                return kfp
            if self.contributions.pop((kfp, pid), None) is None:
                return self.error(404, "not_found")
            return Response(status_code=204)

        @app.get("/v1/models")
        async def models(request: Request, feature_version: int, detector_version: str, chain_class: str = "all"):
            kfp = self._station(request, await request.body())
            if isinstance(kfp, Response):
                return kfp

            def pick(status):
                for cls in (chain_class, "all"):
                    vs = [v for v, m in self.models.items() if m["status"] == status
                          and m["payload"]["feature_version"] == feature_version
                          and m["payload"]["detector_version"] == detector_version
                          and m["payload"]["chain_class"] == cls]
                    if vs:
                        return max(vs)
                return None
            pub, cand = pick("published"), pick("candidate")
            etag = f'"m-{pub or 0}-{cand or 0}"'
            if request.headers.get("if-none-match") == etag:
                return Response(status_code=304, headers={"ETag": etag})
            return JSONResponse({"published": self.entry(pub) if pub else None,
                                 "candidate": self.entry(cand) if cand else None}, headers={"ETag": etag})

        @app.get("/v1/models/{version}")
        async def model(version: int, request: Request):
            kfp = self._station(request, await request.body())
            if isinstance(kfp, Response):
                return kfp
            if version not in self.models:
                return self.error(404, "not_found")
            return self.entry(version)

        @app.put("/v1/profiles/{pid}/checks/{version}")
        async def check(pid: str, version: int, request: Request):
            body = await request.body()
            kfp = self._station(request, body)
            if isinstance(kfp, Response):
                return kfp
            m = self.models.get(version)
            if m is None:
                return self.error(404, "not_found")
            if m["status"] == "withdrawn":
                return self.error(409, "withdrawn")
            rep = _strict(body)
            errs = _schema_errors("check-report", rep, m["payload"]["feature_version"])
            if errs or set(rep["kinds"]) - set(m["payload"]["kinds"]):
                return JSONResponse({"error": "invalid", "message": (errs or ["kinds"])[0]}, status_code=422)
            self.reports[(kfp, pid, version)] = rep
            worse = np.mean([k["global_err"] - k["local_err"] for k in rep["kinds"].values()]) > 0.01
            return {"verdict": "worse" if worse else "not_worse", "model_status": m["status"]}

        return app


# ─── An account service after docs/connect.md ────────────────────────────────────────────────────────────────────

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class FakeAccount:
    """Flow A (the connect page redirecting to the loopback address with a one-time code, PKCE) and flow B (device
    authorization), and the token endpoint, which hands out a bound invitation of the hub with a signed binding."""

    def __init__(self, hub, invite=None):
        """`hub`: the FakeHub (or anything with `url` and `fp`); `invite(bound=True)` → (id, code) takes an invitation
        from the hub's pool (the FakeHub's own by default, the real hub's command line in the end-to-end test)."""
        self.hub = hub
        self.invite = invite or hub.invite
        self.key = Ed25519PrivateKey.generate()
        self.public_b64u = _b64u(_raw(self.key.public_key()))
        self.kid = _fp(_raw(self.key.public_key()))
        if hasattr(hub, "binding_keys"):
            hub.binding_keys[self.kid] = _raw(self.key.public_key())
        self.account = _b64u(secrets.token_bytes(16))
        self.codes: dict = {}
        self.devices: dict = {}
        self.token_calls: list = []             # every body sent to the token endpoint
        self.issued: list = []                  # every invitation and binding handed out
        self.device_script: list = []           # outcomes of the next device polls; then "approve"
        self.decline = False
        self.server_url = self.server_fp = None  # defaults: the hub's
        self.omit_binding = False
        self.sign_with = None
        self.bind_station = None                # bind the invitation to another station key
        self.rate_limit_next = 0
        self.interval = 1
        self.app = self._build()
        self.url = ""

    @property
    def connect_url(self) -> str:
        return self.url + "/account/connect/"

    def browse(self, url: str) -> str:
        """The browser at the connect page: the user signs in and confirms; returns where the page redirects to."""
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **kw):
                return None
        try:
            urllib.request.build_opener(NoRedirect).open(url, timeout=5)
        except urllib.error.HTTPError as e:
            assert e.code == 302, e.read()
            return e.headers["Location"]
        raise AssertionError("no redirect")

    @staticmethod
    def error(status: int, code: str, headers: dict | None = None) -> JSONResponse:
        return JSONResponse({"error": code, "error_description": code}, status_code=status, headers=headers)

    def binding(self, invitation_id: int, station: str, key=None) -> str:
        """As internal/binding.Sign of the hub does it."""
        now = int(time.time())
        return _jws(key or self.key, {"alg": "EdDSA", "typ": "vinyl-mind-binding+json", "kid": self.kid},
                    {"v": 1, "invitation_id": invitation_id, "station": station, "account": self.account, "iat": now,
                     "exp": now + 72 * 3600})

    def _grant(self, station: str) -> dict:
        iid, code = self.invite(bound=True)
        out = {"invitation": code, "binding": self.binding(iid, self.bind_station or station, self.sign_with),
               "server_url": self.server_url or self.hub.url, "server_fingerprint": self.server_fp or self.hub.fp}
        if self.omit_binding:
            del out["binding"]
        self.issued.append(out | {"invitation_id": iid})
        return out

    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.get("/account/connect/")
        def connect(request: Request):
            q = dict(request.query_params)
            want = {"response_type", "client_id", "client", "redirect_uri", "state", "code_challenge",
                    "code_challenge_method", "station", "lang"}
            u = urllib.parse.urlsplit(q.get("redirect_uri", ""))
            if set(q) - want or q.get("response_type") != "code" or q.get("client_id") != "vinyl-check" \
                    or q.get("code_challenge_method") != "S256" or u.scheme != "http" \
                    or u.hostname not in ("127.0.0.1", "::1") or u.path != "/vinyl-mind/callback" \
                    or not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", q.get("station", "")) \
                    or not CLIENT_RE.match(q.get("client", "")) or not q.get("state"):
                return Response("refused", status_code=400)
            back = q["redirect_uri"] + "?"
            if self.decline:
                return RedirectResponse(back + urllib.parse.urlencode({"error": "access_denied", "state": q["state"]}),
                                        status_code=302)
            code = "".join(secrets.choice(CROCKFORD) for _ in range(8))
            self.codes[code] = {"challenge": q["code_challenge"], "redirect_uri": q["redirect_uri"],
                                "client_id": q["client_id"], "station": q["station"], "expires": time.time() + 60,
                                "used": False, "fails": 0}
            return RedirectResponse(back + urllib.parse.urlencode({"code": code, "state": q["state"]}),
                                    status_code=302)

        @app.post("/api/account/device")
        async def device(request: Request):
            d = _strict(await request.body())
            if set(d) != {"station", "client"}:
                return self.error(400, "invalid_request")
            dc = _b64u(secrets.token_bytes(24))
            uc = "".join(secrets.choice(CROCKFORD) for _ in range(8))
            uc = f"{uc[:4]}-{uc[4:]}"
            self.devices[dc] = {"station": d["station"], "user_code": uc}
            uri = self.url + "/account/activate/"
            return {"device_code": dc, "user_code": uc, "verification_uri": uri,
                    "verification_uri_complete": f"{uri}?code={uc}", "interval": self.interval, "expires_in": 600}

        @app.post("/api/account/connect/token")
        async def token(request: Request):
            d = _strict(await request.body())
            self.token_calls.append(d)
            if self.rate_limit_next:
                self.rate_limit_next -= 1
                return self.error(429, "rate_limited", {"Retry-After": "1"})
            if d.get("grant_type") == "authorization_code":
                if set(d) != {"grant_type", "code", "code_verifier", "redirect_uri", "client_id", "station"}:
                    return self.error(400, "invalid_request")
                c = self.codes.get(d["code"])
                if c is None or c["used"] or c["fails"] >= 5 or time.time() > c["expires"]:
                    return self.error(400, "invalid_grant")
                challenge = _b64u(hashlib.sha256(d["code_verifier"].encode()).digest())
                if challenge != c["challenge"] or d["redirect_uri"] != c["redirect_uri"] \
                        or d["client_id"] != c["client_id"] or d["station"] != c["station"]:
                    c["fails"] += 1
                    return self.error(400, "invalid_grant")
                c["used"] = True
                return JSONResponse(self._grant(d["station"]), headers={"Cache-Control": "no-store"})
            if d.get("grant_type") == "urn:ietf:params:oauth:grant-type:device_code":
                if set(d) != {"grant_type", "device_code", "client_id", "station"}:
                    return self.error(400, "invalid_request")
                dev = self.devices.get(d["device_code"])
                if dev is None or dev["station"] != d["station"]:
                    return self.error(400, "invalid_grant")
                outcome = self.device_script.pop(0) if self.device_script else "approve"
                if outcome == "pending":
                    return self.error(400, "authorization_pending")
                if outcome in ("slow_down", "expired_token", "access_denied", "invalid_grant"):
                    return self.error(400, outcome)
                del self.devices[d["device_code"]]
                return JSONResponse(self._grant(d["station"]), headers={"Cache-Control": "no-store"})
            return self.error(400, "invalid_request")

        return app


def _serve(app, name: str):
    srv = network.ServerThread(app, network.bind_socket("127.0.0.1", 0), name=name)
    srv.start()
    return srv, f"http://127.0.0.1:{srv.port}"


@pytest.fixture()
def hub():
    h = FakeHub()
    srv, h.url = _serve(h.app, "fake-hub")
    yield h
    srv.stop()


@pytest.fixture()
def account(hub):
    a = FakeAccount(hub)
    srv, a.url = _serve(a.app, "fake-account")
    hub.connect_url = a.connect_url
    yield a
    srv.stop()


# ─── The station ─────────────────────────────────────────────────────────────────────────────────────────────────

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
                  vinyl_mind_url="", **kw)
    app = create_app(cfg)
    app.state.vinylmind.client_options = {"backoff": (0.0,), "sleep": lambda s: None, "timeout": 5.0}
    app.state.vinylmind.open_browser = lambda url: False
    return app


@pytest.fixture()
def station(tmp_path):
    app = _app(tmp_path)
    db = app.state.db
    _seed(db, "Living room deck", "Core Audio: USB Audio CODEC", "Moving magnet, elliptical", seed=1)
    _seed(db, "Studio deck", "hw:CARD=Interface,DEV=0", "MC low output", seed=2)
    with TestClient(app, **LOCAL) as c:
        c.app_ = app
        yield c


def _set_server(c, hub) -> dict:
    assert c.put("/api/vinylmind", json={"url": hub.url}).status_code == 200
    info = c.get("/api/vinylmind/server").json()
    assert info["fingerprint"] == hub.fp
    return info


def _join(c, hub, invitation: str | None = "new", fingerprint: str | None = None) -> dict:
    info = _set_server(c, hub)
    if invitation == "new":
        invitation = hub.invite()[1]
    r = c.post("/api/vinylmind/join",
               json={"invitation": invitation, "fingerprint": fingerprint or info["fingerprint"]})
    assert r.status_code == 200, r.text
    return r.json()


def _key_dir(app) -> Path:
    return Path(app.state.cfg.state_dir) / vinylmind.KEY_DIR


# ─── The specification's test vector, encodings and signatures ───────────────────────────────────────────────────

def test_signing_test_vector():
    """spec/signing.md: the public key of RFC 8032 test 1, its base64url and its fingerprint."""
    raw = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    assert vinylmind.b64u(raw) == "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"
    assert vinylmind.fingerprint(raw) == "SHA256:If4x36FUomFia/hUBG/SJxt77UtqvkWqWId+9H+XIbk"
    assert vinylmind.unb64u("11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo") == raw
    for bad in ("11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo=", "11qY+YKx", "a", "AB"):    # padding, alphabet, tail
        with pytest.raises(ValueError):
            vinylmind.unb64u(bad)


def test_signed_request_verifies_independently(tmp_path):
    key = vinylmind.StationKey.generate()
    body = b'{"a":1}'
    server = "SHA256:If4x36FUomFia/hUBG/SJxt77UtqvkWqWId+9H+XIbk"
    h = vinylmind.sign_headers(key, server, "put", "/v1/profiles/abc?x=1", body, now=1793000000)
    assert h["Vinyl-Mind-Key"] == _fp(key.public_raw) and h["Vinyl-Mind-Timestamp"] == "1793000000"
    assert re.fullmatch(r"[A-Za-z0-9_-]{22,64}", h["Vinyl-Mind-Nonce"]) and len(_unb64u(h["Vinyl-Mind-Nonce"])) >= 16
    message = "\n".join(("vinyl-mind-request-v1", server, "PUT", "/v1/profiles/abc?x=1",
                         hashlib.sha256(body).hexdigest(), "1793000000", h["Vinyl-Mind-Nonce"],
                         h["Vinyl-Mind-Key"])).encode()
    Ed25519PublicKey.from_public_bytes(key.public_raw).verify(_unb64u(h["Vinyl-Mind-Signature"]), message)
    assert not message.endswith(b"\n")
    assert vinylmind.sign_headers(key, server, "PUT", "/p", body)["Vinyl-Mind-Nonce"] != h["Vinyl-Mind-Nonce"]
    # an empty body is signed as the hash of the empty string
    assert hashlib.sha256(b"").hexdigest().encode() in vinylmind.request_message(server, "GET", "/v1/me", b"", "1", "n",
                                                                                  "k")


def test_jws_verification():
    server = Ed25519PrivateKey.generate()
    raw, kid = _raw(server.public_key()), _fp(_raw(server.public_key()))
    payload = {"server": kid, "x": 1}
    head = {"alg": "EdDSA", "typ": "vinyl-mind-model+json", "kid": kid}
    assert vinylmind.verify_jws(_jws(server, head, payload), raw, kid) == payload
    other = Ed25519PrivateKey.generate()
    cases = {
        "alg": _jws(server, head | {"alg": "none"}, payload),
        "typ": _jws(server, head | {"typ": "JWT"}, payload),
        "kid": _jws(server, head | {"kid": _fp(b"y" * 32)}, payload),
        "signature": _jws(other, head, payload),
        "server": _jws(server, head, {"server": _fp(b"z" * 32)}),
        "parts": "a.b",
        "not a string": 7,
    }
    tok = _jws(server, head, payload).split(".")
    cases["payload changed"] = ".".join((tok[0], _b64u(b'{"server":"' + kid.encode() + b'","x":2}'), tok[2]))
    for name, token in cases.items():
        with pytest.raises(vinylmind.HubError) as e:
            vinylmind.verify_jws(token, raw, kid)
        assert e.value.code == "model_rejected", name


def test_fixed_scaling_is_the_specifications():
    """learning's shared scaling of feature version 1 is exactly spec/v1/features.json, in its order."""
    assert FEATURES["feature_version"] == 1
    assert [f["name"] for f in FEATURES["features"]] == list(learning.FEATURES)
    assert {f["name"]: (f["center"], f["scale"]) for f in FEATURES["features"]} == learning.FIXED_SCALING
    assert FEATURES["kinds"] == list(learning.KINDS) and FEATURES["coef_bound"] == vinylmind.COEF_BOUND
    # the conversion of spec/README.md is learning.to_coef
    rng = np.random.default_rng(2)
    m = {"mu": rng.normal(size=10).tolist(), "sd": rng.uniform(0.2, 2, 10).tolist(), "b": rng.normal(size=11).tolist()}
    c, s = np.array([f["center"] for f in FEATURES["features"]]), np.array([f["scale"] for f in FEATURES["features"]])
    mu, sd, b = np.array(m["mu"]), np.array(m["sd"]), np.array(m["b"])
    want = [b[0] + sum(b[i + 1] / sd[i] * (c[i] - mu[i]) for i in range(10))]
    want += [b[i + 1] * s[i] / sd[i] for i in range(10)]
    assert np.allclose(learning.to_coef(m), want, atol=1e-5)
    X = rng.normal(size=(20, 10))
    assert np.allclose(learning._predict({"mu": c.tolist(), "sd": s.tolist(), "b": learning.to_coef(m)}, X),
                       learning._predict(m, X), atol=1e-5)


def test_station_validators_agree_with_the_schemas():
    good = json.loads((SPEC / "examples" / "contribution.json").read_text())
    assert vinylmind.validate_contribution(good) == [] and not _schema_errors("contribution", good)
    for f in sorted((SPEC / "examples" / "invalid").glob("*.json")):
        assert vinylmind.validate_contribution(json.loads(f.read_text())), f.name
    report = json.loads((SPEC / "examples" / "check-report.json").read_text())
    assert vinylmind.validate_check_report(report) == [] and not _schema_errors("check-report", report)
    assert vinylmind.validate_check_report(report | {"kinds": {"click": {"local_err": 0.1, "global_err": 0.1,
                                                                         "master_recordings": 1}}})
    hub = FakeHub()
    model = hub.payload({"click": [0.1] * 11}, 3)
    assert vinylmind.validate_model(model) == [] and not _schema_errors("global-model", model)
    assert vinylmind.validate_model(model | {"extra": 1}) and vinylmind.validate_model(model | {"version": 0})


def test_canonical_server_url():
    """docs/connect.md: https, scheme and host in lower case, no :443, no path, no trailing slash (plain HTTP only for
    this computer, in tests and development)."""
    for given, canonical in (("https://api.vinylcheck.net", "https://api.vinylcheck.net"),
                             ("https://api.vinylcheck.net/", "https://api.vinylcheck.net"),
                             ("HTTPS://API.VinylCheck.NET:443/", "https://api.vinylcheck.net"),
                             ("  https://Hub.Example.org:8443 ", "https://hub.example.org:8443"),
                             ("http://127.0.0.1:8123/", "http://127.0.0.1:8123"),
                             ("HTTP://LocalHost:80", "http://localhost"),
                             ("http://[::1]:9000", "http://[::1]:9000")):
        assert vinylmind.canonical_url(given) == canonical, given
    for bad, why in (("https://example.org/vinyl-mind/", "without a path"),
                     ("https://example.org/v1", "without a path"),
                     ("http://vinyl-mind.example", "https://"), ("http://192.168.1.10", "https://"),
                     ("ftp://example.org", "https://"), ("https://user:pw@example.org", "user name"),
                     ("https://example.org/?x=1", "query"), ("https://example.org/#top", "fragment"),
                     ("", "host"), ("https://", "host"), ("https://example.org:99999", "https://")):
        with pytest.raises(vinylmind.HubError) as e:
            vinylmind.canonical_url(bad)
        assert why in str(e.value), (bad, str(e.value))
    assert vinylmind.DEFAULT_VINYL_MIND_URL == vinylmind.canonical_url(vinylmind.DEFAULT_VINYL_MIND_URL)
    assert vinylmind.accepts_client([]) and vinylmind.accepts_client(["vinyl-check/*"])
    assert vinylmind.accepts_client([f"vinyl-check/{vinylmind.__version__.rsplit('.', 1)[0]}.*"])
    assert not vinylmind.accepts_client(["vinyl-check/0.9.*", "other/*"])


def test_station_key_files(tmp_path):
    fp = "SHA256:If4x36FUomFia/hUBG/SJxt77UtqvkWqWId+9H+XIbk"
    path = vinylmind.key_file(tmp_path, fp)
    assert path.parent == tmp_path / vinylmind.KEY_DIR and vinylmind.key_file(tmp_path, fp, pending=True) != path
    assert vinylmind.key_file(tmp_path, "SHA256:" + "A" * 43) != path                     # one key per hub
    key = vinylmind.StationKey.generate()
    key.save(path)
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
        os.chmod(path, 0o644)                                                              # narrowed again on load
    again = vinylmind.StationKey.load(path)
    assert again.fingerprint == key.fingerprint == _fp(key.public_raw)
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    assert repr(key) == f"StationKey({key.fingerprint})" and "PRIVATE" not in repr(key)
    vinylmind.StationKey.delete(path)
    assert vinylmind.StationKey.load(path) is None


# ─── Contributions ───────────────────────────────────────────────────────────────────────────────────────────────

def test_contribution_documents(station):
    db = station.app_.state.db
    prev = station.get("/api/vinylmind/preview").json()
    assert len(prev["profiles"]) == 2
    for row in prev["profiles"]:
        doc = row["payload"]
        assert vinylmind.validate_contribution(doc) == [] and not _schema_errors("contribution", doc)
        assert doc["feature_version"] == 1 and doc["detector_version"] == detect.VERSION == "1"
        assert doc["client"] == {"name": "vinyl-check", "version": vinylmind.__version__}
        assert re.fullmatch(r"[A-Za-z0-9_-]{22}", row["vm_id"]) and len(_unb64u(row["vm_id"])) == 16
        assert row["vm_id"] == db.profile_vm_id(row["id"])
        click = doc["kinds"]["click"]
        assert len(click["coef"]) == 11 and click["master_recordings"] == 3 and click["auc"] > 0.9
        assert click["n_manual"] == 0 and click["n_master"] == click["n_real"] + click["n_false"]
        # without a second opinion gc is always 0.5 and gc_missing always 1, and these events all lie in the music:
        # not informative, reported with the prior's weight (0, no prior) as the specification says
        names = [f["name"] for f in FEATURES["features"]]
        constant = {"gc", "gc_missing", "music"}
        assert click["informative"] == [n not in constant for n in names]
        assert all(click["coef"][names.index(n) + 1] == 0.0 for n in constant)
        text = json.dumps(doc)
        for private in (row["name"], "USB Audio CODEC", "Interface", "elliptical", "Artist of", "Title of",
                        row["vm_id"]):
            assert private not in text                           # names never leave the station, the ID is in the URL
    chains = {r["name"]: r["chain"] for r in prev["profiles"]}
    assert chains["Living room deck"] == {"source": "usb_turntable", "cartridge": "mm"}
    assert chains["Studio deck"] == {"source": "turntable_interface", "cartridge": "mc"}
    # the chain class is two fields of the profile, set in the profile dialog; empty means "guess"
    pid = prev["profiles"][0]["id"]
    r = station.patch(f"/api/profiles/{pid}",
                      json={"chain_source": "turntable_interface", "chain_cartridge": "unknown"})
    assert r.status_code == 200 and r.json()["chain"] == {"source": "turntable_interface", "cartridge": "unknown"}
    row = next(x for x in station.get("/api/vinylmind/preview").json()["profiles"] if x["id"] == pid)
    assert row["chain_set"] and row["payload"]["chain"] == {"source": "turntable_interface", "cartridge": "unknown"}
    assert station.patch(f"/api/profiles/{pid}", json={"chain_source": "interface"}).status_code == 422
    r = station.patch(f"/api/profiles/{pid}", json={"chain_source": "", "chain_cartridge": None}).json()
    assert r["chain"] == r["chain_guess"]


def _click_data(rng, recordings=3, gc=None):
    X, y, rec = [], [], []
    for g in range(1, recordings + 1):
        ev = _events(rng, 40, 25)
        for e in ev:
            e["gc"] = gc
        F = learning.features(ev, -15.0)
        for i, e in enumerate(ev):
            X.append(F[i]); y.append(e["_y"]); rec.append(g)
    return {"click": {"X": np.array(X), "y": np.array(y, float), "w": np.ones(len(y)), "rec": np.array(rec),
                      "record": np.ones(len(y)), "manual": np.zeros(len(y), bool), "master": np.ones(len(y), bool),
                      "raw_n": {g: rec.count(g) for g in set(rec)}}}


@pytest.mark.parametrize("with_prior", [False, True])
def test_features_that_never_vary(with_prior):
    """spec/README.md, "Features that never vary": the prior's weight in place of the learned one, the intercept
    shifted at the constant value, and every prediction on the examples stays the same."""
    rng = np.random.default_rng(11)
    data = _click_data(rng, gc=0.8)                    # a second opinion that always says 0.8: gc_missing stays 0
    X = data["click"]["X"]
    names = list(learning.FEATURES)
    prior = {"intercept": 0.3, "weights": [0.5 + 0.1 * i for i in range(10)]} if with_prior else None
    model = learning.train(data, now=0.0, prior={"click": prior} if prior else None)["kinds"]["click"]["model"]
    coef = learning.to_coef(model)
    masked, informative = learning.mask_uninformative(coef, X, prior)
    assert informative == [n not in ("gc", "gc_missing", "music") for n in names]
    for n in ("gc", "gc_missing", "music"):
        assert masked[names.index(n) + 1] == (prior["weights"][names.index(n)] if prior else 0.0)
    for n in names:
        if n not in ("gc", "gc_missing", "music"):
            assert masked[names.index(n) + 1] == round(coef[names.index(n) + 1], 6)
    if with_prior:                                    # away from its centre, the constant moves the intercept
        assert masked[0] != round(coef[0], 6)
    before = learning.predict_fixed(learning.from_coef(coef), X)
    after = learning.predict_fixed(learning.from_coef(masked), X)
    assert np.allclose(before, after, atol=1e-5)
    # where every feature varies, nothing changes
    varied = X + rng.normal(0, 0.01, X.shape)
    assert learning.mask_uninformative(coef, varied, prior) == ([round(x, 6) for x in coef], [True] * 10)


def test_cross_check_errors_are_clipped_not_dropped():
    rng = np.random.default_rng(12)
    data = _click_data(rng)
    info = dict(learning.train(data, now=0.0)["kinds"]["click"], cv_err_est=12.5, cv_err_raw=150.0)
    k = vinylmind.contribution_kind(info, data["click"]["X"])
    assert k["cv_err_est"] == 10.0 and k["cv_err_raw"] == 100.0
    doc = {"spec": "vinyl-mind/contribution", "feature_version": 1, "detector_version": "1",
           "client": {"name": "vinyl-check", "version": vinylmind.__version__},
           "chain": {"source": "usb_turntable", "cartridge": "mm"}, "kinds": {"click": k}}
    assert vinylmind.validate_contribution(doc) == [] and not _schema_errors("contribution", doc)


def test_server_address_in_the_settings(station):
    r = station.put("/api/vinylmind", json={"url": "HTTPS://Hub.Example.org:443/"})
    assert r.status_code == 200 and r.json()["url"] == "https://hub.example.org"
    r = station.put("/api/vinylmind", json={"url": "https://hub.example.org/vinyl-mind"})
    assert r.status_code == 422 and "without a path" in r.json()["detail"]
    r = station.put("/api/vinylmind", json={"url": "http://hub.example.org"})
    assert r.status_code == 422 and "https://" in r.json()["detail"]
    assert station.get("/api/vinylmind").json()["url"] == "https://hub.example.org"


def test_old_profile_ids_and_draft_state_migrate(tmp_path):
    """Hex profile IDs of the draft become the same 16 bytes in base64url; its chain classes move to the profile."""
    app = _app(tmp_path)
    db = app.state.db
    pid = db.add_profile(name="Deck", device="hw:1")
    db.x("UPDATE profiles SET vm_id = ? WHERE id = ?", ("00112233445566778899aabbccddeeff", pid))
    db.set_setting(vinylmind.S_CHAIN_LEGACY, json.dumps({"00112233445566778899aabbccddeeff":
                                                         {"connection": "usb_turntable", "cartridge": "mc"}}))
    db.set_setting(vinylmind.S_JOIN, json.dumps({"url": "https://old.example", "fingerprint": "ab" * 32}))
    legacy = Path(app.state.cfg.state_dir) / vinylmind.LEGACY_KEY_FILE
    legacy.write_text("old")
    db.conn.close()
    app = _app(tmp_path)
    db = app.state.db
    assert db.profile_vm_id(pid) == _b64u(bytes.fromhex("00112233445566778899aabbccddeeff"))
    p = db.profile(pid)
    assert (p["chain_source"], p["chain_cartridge"]) == ("usb_turntable", "mc")
    assert app.state.vinylmind.joined() is None and not legacy.exists()


# ─── Joining with an invitation code, contributing, models, reports, leaving ─────────────────────────────────────

def test_join_contribute_adopt_report_leave(station, hub):
    app = station.app_
    st = _join(station, hub)
    assert st["state"] == "active" and st["joined"]["fingerprint"] == hub.fp and st["joined"]["via"] == "invitation"
    key_files = list(_key_dir(app).glob("*.key"))
    assert len(key_files) == 1 and len(hub.stations) == 1
    assert st["station_fingerprint"] == next(iter(hub.stations))
    raw_db = app.state.cfg.db_path.read_bytes()
    assert b"PRIVATE KEY" not in raw_db and key_files[0].read_bytes() not in raw_db     # the key never goes to the DB

    # contribute: exactly what the preview showed (but for the time it was trained)
    preview = {r["vm_id"]: r["payload"] for r in station.get("/api/vinylmind/preview").json()["profiles"]}
    assert station.put("/api/vinylmind", json={"contribute": True}).json()["contribute"] is True
    r = station.post("/api/vinylmind/sync")
    assert r.status_code == 200, r.text
    assert r.json()["result"]["contributions"]["sent"] == 2
    got = {pid: c for (_, pid), c in hub.contributions.items()}
    strip = lambda d: {k: v for k, v in d.items() if k != "trained_at"}        # noqa: E731
    assert {k: strip(v) for k, v in got.items()} == {k: strip(v) for k, v in preview.items()}
    # every request names the client; every body announces its length
    for method, target, headers, body in hub.requests:
        assert headers.get("vinyl-mind-client") == f"vinyl-check/{vinylmind.__version__}", target
        if body:
            assert headers["content-length"] == str(len(body)) and "transfer-encoding" not in headers
    # nothing changed: nothing is sent again
    n = len(hub.requests)
    assert station.post("/api/vinylmind/sync").json()["result"]["contributions"]["sent"] == 0
    assert not [q for q in hub.requests[n:] if q[0] == "PUT"]

    # the hub publishes a signed model; the station verifies, checks, adopts and reports per profile
    v1 = hub.publish(hub.combine())
    assert station.put("/api/vinylmind", json={"use_model": True}).status_code == 200
    r = station.post("/api/vinylmind/sync").json()
    assert r["result"]["model"] == "new", r
    st = r["status"]
    assert st["model"]["version"] == v1 and st["model"]["adopted"] == ["click"] and st["model"]["in_use"]
    assert st["prior"]["source"] == "server" and st["prior"]["in_use"]
    learned = json.loads(app.state.db.setting("learn_model"))
    assert learned["prior"] == {"source": "server", "type": "model", "ref": v1} and learned["prior_kinds"] == ["click"]
    reports = {pid: rep for (_, pid, v), rep in hub.reports.items() if v == v1}
    assert set(reports) == set(preview)
    for rep in reports.values():
        assert not _schema_errors("check-report", rep) and rep["adopted"] is True
        assert rep["kinds"]["click"]["master_recordings"] == 3
    # asked again: the ETag answers 304 without a body, nothing is reported twice
    n = len(hub.requests)
    assert station.post("/api/vinylmind/sync").json()["result"]["model"] == "unchanged"
    model_req = [q for q in hub.requests[n:] if q[1].startswith("/v1/models?")][0]
    assert model_req[2].get("if-none-match") == f'"m-{v1}-0"'
    assert "chain_class=all" in model_req[1]                              # no active profile here
    assert not [q for q in hub.requests[n:] if "/checks/" in q[1]]
    # the station asks for the model of its active profile's chain class
    app.state.db.activate_profile(next(p["id"] for p in app.state.db.profiles() if p["name"] == "Studio deck"))
    v_cls = hub.publish(hub.combine(), chain_class="turntable_interface.mc", status="candidate")
    n = len(hub.requests)
    station.post("/api/vinylmind/sync")
    model_req = [q for q in hub.requests[n:] if q[1].startswith("/v1/models?")][0]
    assert "chain_class=turntable_interface.mc" in model_req[1]
    hub.models[v_cls]["status"] = "withdrawn"

    # a candidate is checked and reported, but not adopted
    v2 = hub.publish(hub.combine(), status="candidate")
    r = station.post("/api/vinylmind/sync").json()
    assert r["result"]["model"] == "new" and r["status"]["prior"]["ref"] == v1
    assert all(rep["adopted"] is False for (_, _, v), rep in hub.reports.items() if v == v2)

    # the hub withdraws the model in use: the station drops it and falls back to its local model
    hub.models[v1].update(status="withdrawn", reason="worse for most stations")
    hub.models[v2]["status"] = "withdrawn"
    r = station.post("/api/vinylmind/sync").json()
    assert r["result"]["model"] == "withdrawn" and r["status"]["prior"] is None
    assert r["status"]["withdrawn"]["version"] == v1 and "worse" in r["status"]["withdrawn"]["reason"]
    assert "prior" not in json.loads(app.state.db.setting("learn_model"))

    # turning the switch off forgets the models; on again, they are fetched anew
    v3 = hub.publish(hub.combine())
    station.put("/api/vinylmind", json={"use_model": False})
    assert station.get("/api/vinylmind").json()["model"] is None
    station.put("/api/vinylmind", json={"use_model": True})
    assert station.post("/api/vinylmind/sync").json()["status"]["model"]["version"] == v3
    station.put("/api/vinylmind", json={"use_model": False})

    # a deleted profile's contribution is withdrawn
    victim = next(p for p in app.state.db.profiles() if p["name"] == "Studio deck")
    app.state.db.delete_profile(victim["id"])
    app.state.vinylmind.note_retrained()
    r = station.post("/api/vinylmind/sync").json()["result"]["contributions"]
    assert r["withdrawn"] == 1 and len(hub.contributions) == 1

    # the server URL cannot change while connected; leaving deletes the station on the hub and the key file
    assert station.put("/api/vinylmind", json={"url": "https://other.example"}).status_code == 409
    st = station.post("/api/vinylmind/leave").json()
    assert st["state"] == "not_joined" and st["station_fingerprint"] is None
    assert not hub.stations and not hub.contributions and not list(_key_dir(app).glob("*.key"))


def test_join_needs_the_confirmed_fingerprint_and_reports_hub_errors(station, hub):
    _set_server(station, hub)
    code = hub.invite()[1]
    r = station.post("/api/vinylmind/join", json={"invitation": code, "fingerprint": None})
    assert r.status_code == 409 and not hub.stations
    r = station.post("/api/vinylmind/join", json={"invitation": code, "fingerprint": _fp(b"q" * 32)})
    assert r.status_code == 409 and "Nothing was sent" in r.json()["detail"] and not hub.stations
    for inv, expect in ((None, "only admits stations with an invitation code"), ("unknown-code", "not valid")):
        r = station.post("/api/vinylmind/join", json={"invitation": inv, "fingerprint": hub.fp})
        assert r.status_code == 502 and expect in r.json()["detail"], r.json()
    hub.join_limit = True
    r = station.post("/api/vinylmind/join", json={"invitation": code, "fingerprint": hub.fp})
    assert "no more new stations today" in r.json()["detail"] and hub.invitations[code]["uses"] == 1
    hub.join_limit = False
    bound = hub.invite(bound=True)[1]
    r = station.post("/api/vinylmind/join", json={"invitation": bound, "fingerprint": hub.fp})
    assert "confirmation from the account" in r.json()["detail"]
    assert not list(_key_dir(station.app_).glob("*.key"))          # a refused join leaves no key behind
    r = station.post("/api/vinylmind/join", json={"invitation": code, "fingerprint": hub.fp})
    assert r.json()["state"] == "active"
    assert station.post("/api/vinylmind/join", json={"invitation": code, "fingerprint": hub.fp}).status_code == 409


def test_pending_until_approved(station, hub):
    hub.join_mode = "approval"
    st = _join(station, hub, invitation=None)
    assert st["state"] == "pending" and st["joined"]["via"] == "approval"
    station.put("/api/vinylmind", json={"contribute": True})
    r = station.post("/api/vinylmind/sync").json()
    assert r["result"] == {"status": "pending"} and not hub.contributions
    assert station.post("/api/vinylmind/refresh").json()["state"] == "pending"
    next(iter(hub.stations.values()))["status"] = "active"               # the operator approves
    station.app_.state.vinylmind.last_refresh = 0
    assert station.post("/api/vinylmind/refresh").json()["state"] == "active"
    assert station.post("/api/vinylmind/sync").json()["result"]["contributions"]["sent"] == 2


def test_revoked_and_client_not_accepted(station, hub):
    vm = station.app_.state.vinylmind
    _join(station, hub)
    station.put("/api/vinylmind", json={"contribute": True})
    # the hub stops accepting this version: contributing stops, status and leaving keep working
    hub.clients = ["vinyl-check/9.*"]
    r = station.post("/api/vinylmind/sync")
    assert r.status_code == 502 and "update vinyl-check" in r.json()["detail"]
    st = station.get("/api/vinylmind").json()
    assert st["client_rejected"] and st["state"] == "active"
    n = len(hub.requests)
    assert vm.run_round(force=False)["skipped"] in ("client_not_accepted", "backoff")
    vm.next_attempt = 0
    assert vm.run_round()["skipped"] == "client_not_accepted" and not [q for q in hub.requests[n:] if q[0] == "PUT"]
    assert station.get("/api/vinylmind/server").json()["client_accepted"] is False
    hub.clients = ["vinyl-check/1.1.*"]                                   # accepted again after the hub's change
    vm.next_attempt = 0
    assert station.post("/api/vinylmind/sync").json()["result"]["contributions"]["sent"] == 2
    assert not station.get("/api/vinylmind").json()["client_rejected"]
    # the operator revokes the station: the key and the state for that hub go here too, and the settings say why
    next(iter(hub.stations.values()))["status"] = "revoked"
    vm.note_retrained()
    station.app_.state.db.set_setting(vinylmind.S_SENT, "{}")
    assert station.post("/api/vinylmind/sync").status_code == 502
    st = station.get("/api/vinylmind").json()
    assert st["state"] == "not_joined" and st["notice"]["code"] == "revoked" and st["last_error"] is None
    assert "disconnected this station" in st["notice"]["message"] and hub.url in st["notice"]["message"]
    station.cookies.set("vc_lang", "de")                             # the interface's language switch
    de = station.get("/api/vinylmind").json()["notice"]["message"]
    station.cookies.delete("vc_lang")
    assert "hat diese Station getrennt" in de
    assert not list(_key_dir(station.app_).glob("*.key")) and st["station_fingerprint"] is None
    assert station.put("/api/vinylmind", json={"dismiss_notice": True}).json()["notice"] is None
    # connecting again makes a new key and clears the note
    _join(station, hub)
    assert station.get("/api/vinylmind").json()["state"] == "active"


@pytest.mark.parametrize("code", ["revoked", "unknown"])
def test_leaving_a_station_the_hub_dropped(station, hub, code):
    """403 revoked on DELETE /v1/me (the hub keeps the key on record) or 401 unknown_key: disconnected here anyway."""
    st = _join(station, hub)
    if code == "revoked":
        hub.stations[st["station_fingerprint"]]["status"] = "revoked"
    else:
        del hub.stations[st["station_fingerprint"]]
    r = station.post("/api/vinylmind/leave")
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["state"] == "not_joined" and st["notice"]["code"] == code
    assert not list(_key_dir(station.app_).glob("*.key"))
    # the same when a waiting station asks for its approval
    hub.join_mode = "approval"
    st = _join(station, hub, invitation=None)
    assert st["state"] == "pending"
    hub.stations[st["station_fingerprint"]]["status"] = "revoked"
    station.app_.state.vinylmind.last_refresh = 0
    st = station.post("/api/vinylmind/refresh").json()
    assert st["state"] == "not_joined" and st["notice"]["code"] == "revoked"


def test_published_model_never_rolls_back(station, hub):
    """A hub that withdraws a model and shows an older one as published again cannot put the old model back into
    use: the station keeps its local model until a newer one comes. A model of another group than the one asked
    for is refused."""
    app = station.app_
    _join(station, hub)
    assert station.put("/api/vinylmind", json={"contribute": True, "use_model": True}).status_code == 200
    station.post("/api/vinylmind/sync")                                   # contributions: something to combine
    v1 = hub.publish(hub.combine())
    v2 = hub.publish(hub.combine())                                       # the station only ever sees v2
    r = station.post("/api/vinylmind/sync").json()
    assert r["status"]["prior"]["ref"] == v2 and r["status"]["model"]["version"] == v2
    # v2 is withdrawn, v1 shows as published again: older than the model adopted before, so not used
    hub.models[v2].update(status="withdrawn", reason="rolled\nback\x1b[0m")
    r = station.post("/api/vinylmind/sync").json()
    assert r["status"]["prior"] is None and r["status"]["withdrawn"]["version"] == v2
    assert r["status"]["withdrawn"]["reason"] == "rolledback[0m"           # no control characters
    state = json.loads(app.state.db.setting(vinylmind.S_MODEL))
    assert state["checked"][str(v1)]["rollback"] is True and state["checked"][str(v1)]["adopted"] == []
    assert state["adopted"]["1/1/all"]["version"] == v2
    assert all(rep["adopted"] is False for (_, _, v), rep in hub.reports.items() if v == v1)
    assert "prior" not in json.loads(app.state.db.setting("learn_model"))
    assert station.post("/api/vinylmind/sync").json()["result"]["model"] == "unchanged"
    # a newer model is adopted again
    v3 = hub.publish(hub.combine())
    r = station.post("/api/vinylmind/sync").json()
    assert r["status"]["prior"]["ref"] == v3 and json.loads(app.state.db.setting(vinylmind.S_MODEL))["adopted"]["1/1/all"]["version"] == v3
    # the signed payload names another chain class than the entry the hub lists it under: refused, v3 stays
    v4 = hub.publish(hub.combine(), chain_class="usb_turntable.mm")
    hub.models[v4]["payload"]["chain_class"] = "all"
    resp = station.post("/api/vinylmind/sync")
    assert resp.status_code >= 400 and "(group)" in resp.json()["detail"]
    assert station.get("/api/vinylmind").json()["prior"]["ref"] == v3
    assert str(v4) not in json.loads(app.state.db.setting(vinylmind.S_MODEL))["checked"]


def test_hub_messages_lose_control_characters():
    assert vinylmind._text("ok\n\x1b[31mred\r\n" + "x" * 400) == "ok[31mred" + "x" * 291
    assert vinylmind._text(None) == "None" and vinylmind._text("") == ""


def test_models_outlive_a_disconnect_by_the_hub(station, hub):
    """unknown_key or revoked end the connection only: the profiles' models and an adopted global model stay (the hub
    may have started afresh with the same key); joining a hub with another key drops them."""
    app = station.app_
    _join(station, hub)
    station.put("/api/vinylmind", json={"contribute": True, "use_model": True})
    station.post("/api/vinylmind/sync")
    v1 = hub.publish(hub.combine())
    assert station.post("/api/vinylmind/sync").json()["status"]["model"]["adopted"] == ["click"]
    profiles = app.state.db.profiles()
    # the hub lost its database and knows the station no more
    hub.stations.clear()
    app.state.vinylmind.note_retrained()
    assert station.post("/api/vinylmind/sync").status_code == 502
    st = station.get("/api/vinylmind").json()
    assert st["state"] == "not_joined" and st["notice"]["code"] == "unknown"
    assert st["prior"]["source"] == "server" and st["prior"]["ref"] == v1 and st["prior"]["in_use"]
    assert st["model"]["version"] == v1
    assert json.loads(app.state.db.setting("learn_model"))["prior"]["ref"] == v1
    assert app.state.db.profiles() == profiles and not list(_key_dir(app).glob("*.key"))
    # connecting again to the same hub key keeps them
    st = _join(station, hub)
    assert st["state"] == "active" and st["prior"]["ref"] == v1
    # revoked: the same
    hub.stations[st["station_fingerprint"]]["status"] = "revoked"
    app.state.vinylmind.note_retrained()
    station.post("/api/vinylmind/sync")
    st = station.get("/api/vinylmind").json()
    assert st["notice"]["code"] == "revoked" and st["prior"]["ref"] == v1
    # a hub with another key: what the old one signed goes
    hub.key = Ed25519PrivateKey.generate()
    hub.pub = _raw(hub.key.public_key())
    hub.fp = _fp(hub.pub)
    hub.stations.clear()
    st = _join(station, hub)
    assert st["state"] == "active" and st["prior"] is None and st["model"] is None


@pytest.mark.parametrize("tamper", ["unsigned", "kid", "signature", "alg", "scaling", "server"])
def test_refuses_unsigned_or_wrongly_signed_model(station, hub, tamper):
    _join(station, hub)
    station.put("/api/vinylmind", json={"contribute": True})
    station.post("/api/vinylmind/sync")
    kw = {}
    if tamper == "scaling":
        kw["scaling"] = {"center": [0.0] * 10, "scale": [1.0] * 10}
    elif tamper == "server":
        kw["server"] = _fp(b"s" * 32)
    hub.publish(hub.combine(), tamper=tamper if tamper in ("unsigned", "kid", "signature", "alg") else None, **kw)
    station.put("/api/vinylmind", json={"use_model": True})
    r = station.post("/api/vinylmind/sync")
    assert r.status_code == 502
    expected = {"unsigned": "not signed", "kid": "another key", "signature": "signature is not valid",
                "alg": "not signed", "scaling": "shared scaling", "server": "another key"}[tamper]
    assert expected in r.json()["detail"], r.json()
    st = station.get("/api/vinylmind").json()
    assert st["model"] is None and st["prior"] is None and expected in st["last_error"]
    assert not hub.reports and "prior" not in json.loads(station.app_.state.db.setting("learn_model") or "{}")


def test_switches_off_or_recording_nothing_is_sent(station, hub):
    vm = station.app_.state.vinylmind
    _join(station, hub)
    n = len(hub.requests)
    assert vm.run_round(force=True) == {"skipped": "off"}
    assert station.post("/api/vinylmind/sync").status_code == 409
    vm.note_retrained()
    assert vm.run_round() == {"skipped": "off"}
    assert len(hub.requests) == n                                          # not a single request
    station.put("/api/vinylmind", json={"contribute": True})
    vm.busy = lambda: True
    assert vm.run_round(force=True) == {"skipped": "recording"}
    assert station.post("/api/vinylmind/leave").status_code == 409
    assert len(hub.requests) == n
    vm.busy = lambda: False
    assert vm.run_round(force=True)["contributions"]["sent"] == 2


def test_clock_skew_is_shown(station, hub):
    hub.time_offset = 120
    info = _set_server(station, hub)
    assert info["skew_warning"] and 115 <= info["skew_s"] <= 125
    hub.time_offset = 0
    assert not station.get("/api/vinylmind/server").json()["skew_warning"]


# ─── Feature version 2 ───────────────────────────────────────────────────────────────────────────────────────────

def test_feature_version_2_only_to_a_hub_that_lists_it(tmp_path, hub):
    app = _app(tmp_path, "v2", feature_version="2")
    db = app.state.db
    measured = _seed(db, "Measured deck", "hw:CARD=Interface,DEV=0", "MC low output", seed=3)
    db.update_profile(measured, accepted_at=1.0, chain_db=-95.0, ref_dbfs=-19.0, floor_clicks_per_min=4.0,
                      floor_ticks_per_min=30.0)
    _seed(db, "Plain deck", "Core Audio: USB Audio CODEC", "Moving magnet", seed=4)          # never measured
    with TestClient(app, **LOCAL) as c:
        rows = {r["name"]: r for r in c.get("/api/vinylmind/preview").json()["profiles"]}
        doc = rows["Measured deck"]["payload"]
        assert vinylmind.validate_contribution(doc, "2") == [] and not _schema_errors("contribution", doc, 2)
        assert doc["feature_version"] == 2 and all(len(k["coef"]) == 13 for k in doc["kinds"].values())
        assert rows["Plain deck"]["payload"] is None and "measured turntable profile" in rows["Plain deck"]["reason"]
        assert vinylmind.validate_contribution(doc) != []                  # never valid as feature version 1
        # the hub knows only feature version 1: nothing is sent, the status says why
        assert not _set_server(c, hub)["feature_supported"]
        _join(c, hub)
        c.put("/api/vinylmind", json={"contribute": True, "use_model": True})
        r = c.post("/api/vinylmind/sync").json()
        assert r["result"] == {"skipped": "feature_version"} and r["status"]["feature_supported"] is False
        assert not hub.contributions and not [q for q in hub.requests if q[1].startswith("/v1/models")]
        assert c.get("/api/vinylmind/preview").json()["feature_supported"] is False
        # a hub that lists version 2 gets it (once it refreshed what the hub accepts)
        hub.feature_versions = [1, 2]
        r = c.post("/api/vinylmind/sync").json()
        assert r["result"]["contributions"]["sent"] == 1
        assert next(iter(hub.contributions.values()))["feature_version"] == 2


# ─── Package files ───────────────────────────────────────────────────────────────────────────────────────────────

def test_export_import_round_trip(station, tmp_path, hub):
    prev = station.get("/api/vinylmind/preview").json()["profiles"][0]
    r = station.get("/api/vinylmind/export", params={"profile": prev["id"]})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    package = r.json()
    assert not _schema_errors("contribution", package) and package["kinds"] == prev["payload"]["kinds"]
    own = station.post("/api/vinylmind/import", content=r.content)
    assert own.status_code == 422 and "own turntable profiles" in own.json()["detail"]

    other = _app(tmp_path, "other")
    _seed(other.state.db, "Other deck", "", "", recordings=1, seed=9)
    with TestClient(other, **LOCAL) as c:
        d = c.post("/api/vinylmind/import", content=r.content).json()["package"]       # first: what is it?
        assert d["type"] == "contribution" and "_doc" not in d and d["kinds"]["click"]["examples"] > 100
        assert c.get("/api/vinylmind").json()["prior"] is None                       # nothing changed yet
        st = c.post("/api/vinylmind/import", params={"confirm": True}, content=r.content).json()["status"]
        assert st["prior"]["source"] == "import" and st["prior"]["kinds"] == ["click"] and st["prior"]["in_use"]
        assert json.loads(other.state.db.setting("learn_model"))["prior_kinds"] == ["click"]
        # a global model as the hub delivers it (JWS): verified once the station accepted that hub's key
        v = hub.publish({"click": package["kinds"]["click"]["coef"]})
        token = hub.models[v]["jws"]
        assert c.post("/api/vinylmind/import", content=token.encode()).json()["package"]["signature"] == "unverified"
        _join(c, hub)
        d = c.post("/api/vinylmind/import", content=token.encode()).json()["package"]
        assert d["type"] == "model" and d["signature"] == "verified" and d["ref"] == str(v)
        # another feature version, broken files
        r2 = c.post("/api/vinylmind/import", json=package | {"feature_version": 2})
        assert "Feature version 2" in r2.json()["detail"]
        bad = json.loads(r.content)
        bad["kinds"]["click"]["coef"] = bad["kinds"]["click"]["coef"][:3]
        assert "expected 11 entries" in c.post("/api/vinylmind/import", json=bad).json()["detail"]
        assert c.post("/api/vinylmind/import", content=b"not json").status_code == 422
        assert c.post("/api/vinylmind/import", content=b"x" * (vinylmind.MAX_PACKAGE_BYTES + 1)).status_code == 413
        st = c.delete("/api/vinylmind/prior").json()
        assert st["prior"] is None and "prior" not in json.loads(other.state.db.setting("learn_model"))


def test_model_check_and_adoption_rule():
    rng = np.random.default_rng(3)
    X, y, rec = [], [], []
    for g in range(1, 4):
        ev = _events(rng, 40, 25)
        F = learning.features(ev, -15.0)
        for i, e in enumerate(ev):
            X.append(F[i]); y.append(e["_y"]); rec.append(g)
    data = {"click": {"X": np.array(X), "y": np.array(y, float), "w": np.ones(len(y)), "rec": np.array(rec),
                      "record": np.ones(len(y)), "manual": np.zeros(len(y), bool), "master": np.ones(len(y), bool),
                      "raw_n": {g: rec.count(g) for g in set(rec)}}}
    good = learning.to_fixed(learning.train(data)["kinds"]["click"]["model"])
    bad = {"intercept": 5.0, "weights": [0.0] * len(good["weights"])}       # calls everything a real click
    ok = vinylmind.check_model(data, {"click": good})["click"]
    worse = vinylmind.check_model(data, {"click": bad})["click"]
    assert ok["master_recordings"] == 3 and ok["global_err"] < 0.2 and vinylmind.adopt(ok)
    assert worse["global_err"] > 0.5 and worse["master_recordings"] == ok["master_recordings"]
    assert vinylmind.adopt({"local_err": None, "prior_err": None, "global_err": None, "master_recordings": 0})
    assert not vinylmind.adopt({"local_err": 0.05, "prior_err": 0.08, "global_err": 0.4, "master_recordings": 3})


# ─── Signing in with an account (docs/connect.md) ────────────────────────────────────────────────────────────────

def _connect(c, account, **body) -> dict:
    _set_server(c, account.hub)
    r = c.post("/api/vinylmind/connect", json={"flow": "browser"} | body)
    assert r.status_code == 200, r.text
    return r.json()


def _secrets_absent(c, app, account, caplog, extra=()):
    """None of the flow's secrets appears in the status, the database, the logs or next to the key."""
    values = [v for call in account.token_calls for k, v in call.items()
              if k in ("code", "code_verifier", "device_code")]
    values += [v for g in account.issued for k, v in g.items() if k in ("invitation", "binding") and v]
    values += list(extra)
    assert values
    text = json.dumps(c.get("/api/vinylmind").json()) + json.dumps(c.get("/api/vinylmind/preview").json())
    raw_db = app.state.cfg.db_path.read_bytes() + (app.state.cfg.db_path.with_name(
        app.state.cfg.db_path.name + "-wal").read_bytes() if app.state.cfg.db_path.with_name(
        app.state.cfg.db_path.name + "-wal").exists() else b"")
    files = b"".join(p.read_bytes() for p in Path(app.state.cfg.state_dir).rglob("*") if p.is_file())
    # the station's own logs (the test's HTTP client logs the URLs it opens as the browser would)
    logs = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("httpx"))
    for v in values:
        assert v not in text and v.encode() not in raw_db and v.encode() not in files and v not in logs, v


def test_connect_in_the_browser(station, account, caplog):
    caplog.set_level(logging.DEBUG)
    app, hub = station.app_, account.hub
    assert _set_server(station, hub)["connect"] == {"site": "127.0.0.1"}
    r = _connect(station, account)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r["url"]).query))
    assert r["url"].startswith(account.connect_url + "?")
    assert q["response_type"] == "code" and q["client_id"] == "vinyl-check" and q["client"] == f"vinyl-check/{vinylmind.__version__}"
    assert q["redirect_uri"] == "http://127.0.0.1:8123/vinyl-mind/callback" and q["code_challenge_method"] == "S256"
    assert len(_unb64u(q["state"])) == 16 and len(_unb64u(q["code_challenge"])) == 32 and q["lang"] == "en"
    assert "%2B" in r["url"] or "+" not in q["station"]                    # the fingerprint is URL-encoded
    st = r["status"]
    assert st["connect"]["kind"] == "browser" and st["connect"]["status"] == "waiting"
    assert q["state"] not in json.dumps(st)                               # the interface sees the progress only
    pending_key = vinylmind.key_file(app.state.cfg.state_dir, hub.fp, pending=True)
    assert pending_key.exists() and vinylmind.StationKey.load(pending_key).fingerprint == q["station"]

    back = account.browse(r["url"])                                        # sign in and confirm on the website
    assert back.startswith("http://127.0.0.1:8123/vinyl-mind/callback?")
    page = station.get(back)
    assert page.status_code == 200 and "Connected" in page.text and "no-store" in page.headers["cache-control"]
    st = station.get("/api/vinylmind").json()
    assert st["state"] == "active" and st["joined"]["via"] == "account" and st["connect"] is None
    assert st["station_fingerprint"] == q["station"] and hub.stations[q["station"]]["account"] == account.account
    call = account.token_calls[-1]
    assert call["grant_type"] == "authorization_code" and call["redirect_uri"] == q["redirect_uri"]
    assert call["client_id"] == "vinyl-check" and call["station"] == q["station"]
    assert _b64u(hashlib.sha256(call["code_verifier"].encode()).digest()) == q["code_challenge"]
    join = [b for m, t, h, b in hub.requests if t == "/v1/join"][-1]
    assert set(json.loads(join)) == {"public_key", "invitation", "binding"}
    assert not pending_key.exists() and vinylmind.key_file(app.state.cfg.state_dir, hub.fp).exists()
    # the same callback again: refused, nothing changes
    again = station.get(back)
    assert again.status_code == 400 and "Not connected" in again.text and len(account.token_calls) == 1
    _secrets_absent(station, app, account, caplog)


def test_connect_callback_checks(station, account):
    hub = account.hub
    r = _connect(station, account)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r["url"]).query))
    back = account.browse(r["url"])
    forged = back.replace(q["state"], _b64u(secrets.token_bytes(16)))
    assert station.get(forged).status_code == 400                          # another state: refused …
    assert station.get("/vinyl-mind/callback?code=X").status_code == 400
    assert station.get("/api/vinylmind").json()["connect"]["status"] == "waiting"   # … and the flow stays open
    assert not account.token_calls
    # from another device the callback is refused (the redirect only ever goes to this computer)
    remote = TestClient(station.app_)
    assert remote.get(back).status_code == 403
    assert remote.post("/api/vinylmind/connect", json={"flow": "browser"}).status_code == 409
    assert station.get(back).status_code == 200 and station.get("/api/vinylmind").json()["state"] == "active"
    assert len(hub.stations) == 1


def test_connect_declined_or_refused(station, account):
    account.decline = True
    r = _connect(station, account)
    page = station.get(account.browse(r["url"]))
    assert page.status_code == 400 and "declined" in page.text and not account.token_calls
    st = station.get("/api/vinylmind").json()
    assert st["connect"]["status"] == "failed" and st["state"] == "not_joined"
    assert not list(_key_dir(station.app_).glob("*.key"))                  # the unused key is gone
    # the account service does not accept the code: no second exchange, start again
    account.decline = False
    r = _connect(station, account)
    back = account.browse(r["url"])
    next(iter(account.codes.values()))["expires"] = 0
    page = station.get(back)
    assert "start again" in page.text and len(account.token_calls) == 1
    assert station.get("/api/vinylmind").json()["connect"]["status"] == "failed"


@pytest.mark.parametrize("case", ["fingerprint", "server_url", "url_case", "url_slash", "url_path", "binding_key",
                                  "no_binding"])
def test_connect_stops_on_a_wrong_answer(station, account, case):
    hub = account.hub
    if case == "fingerprint":
        account.server_fp = _fp(b"o" * 32)
    elif case == "server_url":
        account.server_url = "https://elsewhere.example"
    elif case.startswith("url_"):                  # server_url is compared as a string with the canonical form
        account.server_url = {"url_case": hub.url.replace("http://", "HTTP://"), "url_slash": hub.url + "/",
                              "url_path": hub.url + "/v1"}[case]
    elif case == "binding_key":
        account.sign_with = Ed25519PrivateKey.generate()
    else:
        account.omit_binding = True
    r = _connect(station, account)
    page = station.get(account.browse(r["url"]))
    assert page.status_code == 400
    expect = {"binding_key": "does not match", "no_binding": "needs the confirmation"}.get(case, "another server")
    assert expect in page.text, page.text
    assert not hub.stations and station.get("/api/vinylmind").json()["state"] == "not_joined"
    if case not in ("binding_key", "no_binding"):
        assert not [t for m, t, h, b in hub.requests if t == "/v1/join"]   # nothing was sent to the hub


def test_connect_with_a_device_code(station, account, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(vinylmind.Account, "SLOW_DOWN_S", 0.2)
    app, hub = station.app_, account.hub
    _set_server(station, hub)
    account.device_script = ["pending", "slow_down", "pending"]
    remote = TestClient(app)                                               # a phone: no browser on this computer
    r = remote.post("/api/vinylmind/connect", json={"flow": "device"})
    assert r.status_code == 200, r.text
    c = r.json()["status"]["connect"]
    assert c["kind"] == "device" and re.fullmatch(r"[0-9A-Z]{4}-[0-9A-Z]{4}", c["user_code"])
    assert c["verification_uri"] == account.url + "/account/activate/"
    assert c["verification_uri_complete"].endswith(c["user_code"]) and "<svg" in (c["qr"] or "<svg")
    deadline = time.time() + 15
    while time.time() < deadline and remote.get("/api/vinylmind").json()["state"] != "active":
        time.sleep(0.2)
    st = remote.get("/api/vinylmind").json()
    assert st["state"] == "active" and st["joined"]["via"] == "account"
    grants = [t["grant_type"] for t in account.token_calls]
    assert grants == ["urn:ietf:params:oauth:grant-type:device_code"] * 4
    assert all(set(t) == {"grant_type", "device_code", "client_id", "station"} for t in account.token_calls)
    _secrets_absent(station, app, account, caplog)


@pytest.mark.parametrize("outcome", ["expired_token", "access_denied", "invalid_grant"])
def test_device_code_flow_ends(station, account, outcome):
    _set_server(station, account.hub)
    account.device_script = ["pending", outcome]
    station.post("/api/vinylmind/connect", json={"flow": "device"})
    deadline = time.time() + 10
    while time.time() < deadline and station.get("/api/vinylmind").json()["connect"]["status"] == "waiting":
        time.sleep(0.2)
    st = station.get("/api/vinylmind").json()
    assert st["connect"]["status"] == "failed" and st["state"] == "not_joined" and len(account.token_calls) == 2


def test_connect_later_when_the_hub_is_full_and_reconnect(station, account):
    app, hub = station.app_, account.hub
    vm = app.state.vinylmind
    hub.join_limit = True
    r = _connect(station, account)
    page = station.get(account.browse(r["url"]))
    assert page.status_code == 200 and "tries again" in page.text
    st = station.get("/api/vinylmind").json()
    assert st["connect"]["status"] == "retry" and st["state"] == "not_joined"
    hub.join_limit = False
    vm.flow.retry_at = time.time() - 1
    vm._retry_flow_join()
    st = station.get("/api/vinylmind").json()
    assert st["state"] == "active" and st["connect"] is None
    first = st["station_fingerprint"]
    station.put("/api/vinylmind", json={"contribute": True})
    station.post("/api/vinylmind/sync")
    assert {s for s, _ in hub.contributions} == {first}

    # reconnect: a new key pair joins through the flow; the old station leaves; the station keeps working meanwhile
    r = station.post("/api/vinylmind/connect", json={"flow": "browser", "reconnect": True}).json()
    assert station.get("/api/vinylmind").json()["state"] == "active"
    assert station.get(account.browse(r["url"])).status_code == 200
    st = station.get("/api/vinylmind").json()
    assert st["state"] == "active" and st["station_fingerprint"] not in (None, first)
    assert set(hub.stations) == {st["station_fingerprint"]} and len(list(_key_dir(app).glob("*.key"))) == 1
    assert station.post("/api/vinylmind/sync").json()["result"]["contributions"]["sent"] == 2
    # without accounts on the hub there is no sign-in
    hub.connect_url = None
    station.post("/api/vinylmind/leave")
    assert station.post("/api/vinylmind/connect", json={"flow": "browser"}).status_code == 409


def test_account_rate_limit_and_desktop_browser(station, account, monkeypatch):
    app = station.app_
    vm = app.state.vinylmind
    opened = []
    vm.open_browser = lambda url: opened.append(url) or True
    monkeypatch.setattr(vm, "desktop", True)
    r = _connect(station, account)
    assert r["opened"] and opened == [r["url"]]                          # the system browser, not the window
    account.rate_limit_next = 1
    page = station.get(account.browse(r["url"]))
    assert page.status_code == 400 and "busy" in page.text


def test_private_key_is_never_served(station, hub):
    app = station.app_
    _join(station, hub)
    pem = next(_key_dir(app).glob("*.key")).read_bytes()
    key = serialization.load_pem_private_key(pem, password=None)
    secret = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                               serialization.NoEncryption())
    pid = app.state.db.profiles()[0]["id"]
    texts = [station.get(p).content for p in ("/api/vinylmind", "/api/vinylmind/server", "/api/vinylmind/preview",
                                              f"/api/vinylmind/export?profile={pid}", "/api/profiles",
                                              "/api/settings", "/api/info")]
    texts.append(station.post("/api/vinylmind/sync").content)
    for t in texts:
        for needle in (pem, b"PRIVATE KEY", secret, _b64u(secret).encode(), secret.hex().encode()):
            assert needle not in t
    assert network.Access is not None and vinylmind.Account.CALLBACK_PATH in app.state.network.open_paths

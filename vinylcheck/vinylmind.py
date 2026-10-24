"""Station side of Vinyl-Mind: identity, contributions, the signed client, adopting global models, package files.

Vinyl-Mind is a separate project: a hub that combines what the learning models (learning.py) of many stations have
learned, by federated averaging. This module implements the station's part of the contract in
docs/vinyl-mind-protocol.md (draft v0); docs/architecture.md explains the idea.

- **Identity** (`StationKey`): an Ed25519 key pair, created on first use as a file in the state directory with mode
  600. Only the public key leaves the station; the private key is never stored in the database, never logged and
  deleted when the station leaves.
- **Contributions** (`build_contribution`): per turntable profile, the learning model trained on the recordings made
  with that profile, without any prior, in the fixed common scaling of the feature version, plus counts, self-check
  quality and a coarse chain class. Each profile has a random id (`DB.profile_vm_id`).
- **Client** (`Client`): signed requests over HTTPS (plain HTTP only to this computer, for tests), timeouts, retries
  with backoff, no redirects, size limits; signed models are verified against the accepted server key before anything
  in them is used.
- **Adopting** (`check_model`): a global model is checked on the station's own master recordings and becomes the
  prior of the training (learning.train(prior=...)) per kind only if it does not make the estimate worse.
- **Package files**: export a profile's contribution as a file, import a contribution or model file as the prior
  (stage 1, no hub needed).
- `VinylMind` keeps the state in the database settings, runs the background round (never while a recording runs,
  nothing at all while both switches are off) and `install` adds the routes /api/vinylmind*.

Server catalog: ``vinylmind``. The settings section is in the web catalog ``vinylmind``.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from fastapi import HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from . import __version__, detect, i18n, learning

log = logging.getLogger("vinylcheck.vinylmind")

# The preset server. No default server is decided yet, so it is empty: the settings say so, and a configuration file
# can preset one (`vinyl_mind_url`) for a group of stations.
DEFAULT_VINYL_MIND_URL = ""

PROTOCOL = 0
API = "/v0"
SIGNING_SCHEME = "VM1-ED25519"
KINDS = learning.KINDS
CONNECTIONS = ("usb_turntable", "interface", "unknown")
CARTRIDGES = ("mm", "mc", "unknown")
MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MAX_PACKAGE_BYTES = 64 * 1024
MAX_WEIGHT = 1000.0
MAX_COUNT = 10_000_000
MAX_REPORT_PROFILES = 32
TIMEOUT_S = 10.0
RETRIES = 3                                  # attempts per request for transient failures
BACKOFF_S = (1.0, 2.0, 4.0)                  # waits between them (or Retry-After, at most MAX_RETRY_AFTER_S)
MAX_RETRY_AFTER_S = 60.0
RETRY_STATUS = (429, 502, 503, 504)
ROUND_S = 60.0                               # background round
MIN_CONTRIBUTION_INTERVAL_S = 3600.0         # per profile, unless "Send now"
MODEL_POLL_S = 6 * 3600.0                    # ask for a newer global model (ETag, usually without a body)
STATUS_POLL_S = 600.0                        # while waiting for approval
MAX_ROUND_BACKOFF_S = 6 * 3600.0
KEY_FILE = "vinyl-mind-station.key"
# Keys in the settings table of the database
S_CONTRIBUTE, S_USE, S_URL = "vinyl_mind_contribute", "vinyl_mind_use_model", "vinyl_mind_url"
S_JOIN, S_SENT, S_MODEL = "vinyl_mind_join", "vinyl_mind_sent", "vinyl_mind_model"
S_PRIOR, S_CHAIN = "vinyl_mind_prior", "vinyl_mind_chain"
# Capture devices that are a turntable's own USB converter (most USB turntables use a PCM290x codec that reports
# itself as "USB Audio CODEC"); anything else counts as an interface behind a phono stage
USB_TURNTABLE_HINTS = ("usb audio codec", "usb audio  codec", "pcm290", "turntable")


class HubError(i18n.MessageError):
    """A failure talking to the hub or with what it sent. `code` is the protocol's error code where there is one."""

    def __init__(self, message, code: str | None = None, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.code, self.status, self.retry_after = code, status, retry_after


# ─── Encodings ──────────────────────────────────────────────────────────────

def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64u(text: str) -> bytes:
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9_-]*", text):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def key_id(raw_public: bytes) -> str:
    """Station id / key fingerprint: hex SHA-256 of the raw 32-byte public key."""
    return hashlib.sha256(raw_public).hexdigest()


def show_fingerprint(fp: str | None) -> str | None:
    """A fingerprint for people: 16 groups of 4 hex characters."""
    return " ".join(fp[i:i + 4] for i in range(0, len(fp), 4)) if fp else None


def canonical_json(obj) -> bytes:
    """The bytes a model signature covers (docs/vinyl-mind-protocol.md, "Model signing")."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def canonical_request(method: str, path: str, body_sha256: str, timestamp: str, nonce: str, station: str) -> bytes:
    """The bytes a request signature covers (docs/vinyl-mind-protocol.md, "Request signing")."""
    return "\n".join((SIGNING_SCHEME, method.upper(), path, body_sha256, timestamp, nonce, station)).encode("utf-8")


def rfc3339(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def normalize_url(url) -> str:
    """The server URL without a trailing slash. HTTPS only; plain HTTP is accepted for this computer itself (tests,
    development). Raises HubError with a message saying what is wrong."""
    url = str(url or "").strip().rstrip("/")
    try:
        u = urllib.parse.urlsplit(url)
        host = u.hostname or ""
        u.port                                          # noqa: B018 — raises ValueError on a bad port
    except ValueError:
        raise HubError(i18n.msg("vinylmind.url_invalid")) from None
    if u.username or u.password or u.query or u.fragment or not host:
        raise HubError(i18n.msg("vinylmind.url_parts"))
    if u.scheme == "https":
        return url
    if u.scheme == "http" and _is_local_host(host):
        return url
    raise HubError(i18n.msg("vinylmind.url_invalid"))


def _is_local_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# ─── Station identity ───────────────────────────────────────────────────────

class StationKey:
    """The station's Ed25519 key pair. The private key stays inside this object and its file (mode 600): it is never
    written to the database, returned by the API or logged (repr shows only the beginning of the station id)."""

    def __init__(self, private: Ed25519PrivateKey):
        self._private = private
        self.public_raw = private.public_key().public_bytes(serialization.Encoding.Raw,
                                                            serialization.PublicFormat.Raw)
        self.station_id = key_id(self.public_raw)

    def __repr__(self) -> str:
        return f"StationKey({self.station_id[:12]}…)"

    @property
    def public_b64u(self) -> str:
        return b64u(self.public_raw)

    def sign(self, data: bytes) -> bytes:
        return self._private.sign(data)

    @classmethod
    def load(cls, path: Path) -> StationKey | None:
        """The key from its file, or None if there is none. Permissions wider than 600 are narrowed (POSIX)."""
        try:
            pem = path.read_bytes()
        except FileNotFoundError:
            return None
        if os.name == "posix" and path.stat().st_mode & 0o077:
            log.warning("Vinyl-Mind key file %s was readable by others; permissions set to 600", path)
            os.chmod(path, 0o600)
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError(f"{path} does not hold an Ed25519 key")
        return cls(key)

    @classmethod
    def load_or_create(cls, path: Path) -> StationKey:
        """The key from its file; on first use a new key pair, written with mode 600 before any content goes in."""
        key = cls.load(path)
        if key:
            return key
        private = Ed25519PrivateKey.generate()
        pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
        os.chmod(tmp, 0o600)                             # independent of the umask
        os.replace(tmp, path)
        log.info("Vinyl-Mind: new station key %s…", key_id(cls(private).public_raw)[:12])
        return cls(private)

    @staticmethod
    def delete(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def sign_headers(key: StationKey, method: str, path: str, body: bytes, now: float | None = None,
                 nonce: str | None = None) -> dict:
    """The signing headers of one request."""
    digest = hashlib.sha256(body).hexdigest()
    ts = str(int(now if now is not None else time.time()))
    nonce = nonce or secrets.token_hex(16)
    sig = key.sign(canonical_request(method, path, digest, ts, nonce, key.station_id))
    return {"VM-Station": key.station_id, "VM-Timestamp": ts, "VM-Nonce": nonce, "VM-Content-SHA256": digest,
            "VM-Signature": b64u(sig)}


# ─── Validation (the JSON schemas of the protocol, checked by hand) ─────────

def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


class _Check:
    """Collects schema violations as "path: problem" lines (English, for logs and error details)."""

    def __init__(self):
        self.errors: list[str] = []

    def bad(self, path: str, problem: str) -> None:
        self.errors.append(f"{path}: {problem}")

    def obj(self, v, path: str, required: tuple, optional: tuple = ()) -> bool:
        if not isinstance(v, dict):
            self.bad(path, "expected an object")
            return False
        for k in required:
            if k not in v:
                self.bad(path, f"missing {k}")
        for k in v:
            if k not in required and k not in optional:
                self.bad(path, f"unknown member {k}")
        return all(k in v for k in required)

    def number(self, v, path: str, lo: float, hi: float, null: bool = False) -> None:
        if v is None and null:
            return
        if not _num(v) or not lo <= v <= hi:
            self.bad(path, f"expected a number in [{lo:g}, {hi:g}]")

    def integer(self, v, path: str, lo: int, hi: int) -> None:
        if not _int(v) or not lo <= v <= hi:
            self.bad(path, f"expected an integer in [{lo}, {hi}]")

    def string(self, v, path: str, pattern: str) -> None:
        if not isinstance(v, str) or not re.fullmatch(pattern, v):
            self.bad(path, f"expected a string matching {pattern}")

    def const(self, v, path: str, want) -> None:
        if v != want or type(v) is not type(want):
            self.bad(path, f"expected {want!r}")

    def enum(self, v, path: str, allowed) -> None:
        if v not in allowed:
            self.bad(path, f"expected one of {', '.join(allowed)}")

    def weights(self, v, path: str, n: int) -> None:
        if not isinstance(v, list) or len(v) != n:
            self.bad(path, f"expected {n} entries")
            return
        for i, x in enumerate(v):
            self.number(x, f"{path}[{i}]", -MAX_WEIGHT, MAX_WEIGHT)

    def header(self, obj: dict, kind: str) -> None:
        """The members every payload shares: type, protocol, feature version and list, detector version."""
        self.const(obj.get("type"), "type", f"vinyl-mind/{kind}")
        self.const(obj.get("protocol"), "protocol", PROTOCOL)
        self.string(obj.get("feature_version"), "feature_version", r"[0-9]{1,4}")
        f = obj.get("features")
        if not isinstance(f, list) or not 1 <= len(f) <= 64 or not all(
                isinstance(x, str) and re.fullmatch(r"[a-z0-9_]{1,40}", x) for x in f):
            self.bad("features", "expected 1 to 64 feature names")
        self.integer(obj.get("detector_version"), "detector_version", 1, 100000)


_VERSION_RE = r"[0-9]+\.[0-9]+\.[0-9]+[0-9A-Za-z.+-]{0,32}"
_MODEL_VERSION_RE = r"[A-Za-z0-9._-]{1,64}"


def validate_contribution(obj) -> list[str]:
    """Schema and plausibility of a contribution; [] when it is valid."""
    c = _Check()
    req = ("type", "protocol", "profile_id", "feature_version", "features", "detector_version", "vinyl_check_version",
           "chain_class", "kinds")
    if not c.obj(obj, "contribution", req):
        return c.errors
    c.header(obj, "contribution")
    c.string(obj["profile_id"], "profile_id", r"[0-9a-f]{32}")
    c.string(obj["vinyl_check_version"], "vinyl_check_version", _VERSION_RE)
    cc = obj["chain_class"]
    if c.obj(cc, "chain_class", ("connection", "cartridge")):
        c.enum(cc["connection"], "chain_class.connection", CONNECTIONS)
        c.enum(cc["cartridge"], "chain_class.cartridge", CARTRIDGES)
    n = len(obj["features"]) if isinstance(obj["features"], list) else 0
    kinds = obj["kinds"]
    if not c.obj(kinds, "kinds", (), KINDS) or not kinds:
        if isinstance(kinds, dict) and not kinds:
            c.bad("kinds", "expected at least one kind")
        return c.errors
    for k, v in kinds.items():
        p = f"kinds.{k}"
        if not c.obj(v, p, ("intercept", "weights", "informative", "examples", "recordings", "quality")):
            continue
        c.number(v["intercept"], f"{p}.intercept", -MAX_WEIGHT, MAX_WEIGHT)
        c.weights(v["weights"], f"{p}.weights", n)
        inf = v["informative"]
        if not isinstance(inf, list) or len(inf) != n or not all(isinstance(x, bool) for x in inf):
            c.bad(f"{p}.informative", f"expected {n} booleans")
        ex = v["examples"]
        if c.obj(ex, f"{p}.examples", ("master", "manual", "real", "false_alarm")):
            for kk in ("master", "manual", "real", "false_alarm"):
                c.integer(ex[kk], f"{p}.examples.{kk}", 0, MAX_COUNT)
            if all(_int(ex[kk]) for kk in ex) and ex["real"] + ex["false_alarm"] != ex["master"] + ex["manual"]:
                c.bad(f"{p}.examples", "real + false_alarm must equal master + manual")
        rc = v["recordings"]
        if c.obj(rc, f"{p}.recordings", ("total", "master")):
            c.integer(rc["total"], f"{p}.recordings.total", 0, MAX_COUNT)
            c.integer(rc["master"], f"{p}.recordings.master", 0, MAX_COUNT)
            if _int(rc["total"]) and _int(rc["master"]) and rc["master"] > rc["total"]:
                c.bad(f"{p}.recordings", "master must not exceed total")
        q = v["quality"]
        if c.obj(q, f"{p}.quality", ("est_error", "raw_error", "auc", "usable")):
            c.number(q["est_error"], f"{p}.quality.est_error", 0, 100, null=True)
            c.number(q["raw_error"], f"{p}.quality.raw_error", 0, 100, null=True)
            c.number(q["auc"], f"{p}.quality.auc", 0, 1, null=True)
            if not isinstance(q["usable"], bool):
                c.bad(f"{p}.quality.usable", "expected a boolean")
            elif q["usable"] and not (_num(q["est_error"]) and q["est_error"] <= learning.MAX_EST_ERROR
                                      and (q["raw_error"] is None or q["est_error"] < q["raw_error"])):
                c.bad(f"{p}.quality", "usable needs est_error <= 0.30 below raw_error")
    return c.errors


def validate_model(obj, require_signature: bool = True) -> list[str]:
    """Schema of a global model; [] when it is valid. A model file shared by hand may come without a signature."""
    c = _Check()
    req = ("type", "protocol", "version", "feature_version", "features", "detector_version", "chain_class", "created",
           "kinds")
    if not c.obj(obj, "model", req + (("signature",) if require_signature else ()), ("signature",)):
        return c.errors
    c.header(obj, "model")
    c.string(obj["version"], "version", _MODEL_VERSION_RE)
    if not isinstance(obj["created"], str) or len(obj["created"]) > 40:
        c.bad("created", "expected a date-time")
    cc = obj["chain_class"]
    if c.obj(cc, "chain_class", ("connection", "cartridge")):
        c.enum(cc["connection"], "chain_class.connection", CONNECTIONS + ("any",))
        c.enum(cc["cartridge"], "chain_class.cartridge", CARTRIDGES + ("any",))
    if "signature" in obj and c.obj(obj["signature"], "signature", ("alg", "key", "value")):
        c.const(obj["signature"]["alg"], "signature.alg", "ed25519")
        c.string(obj["signature"]["key"], "signature.key", r"[0-9a-f]{64}")
        c.string(obj["signature"]["value"], "signature.value", r"[A-Za-z0-9_-]{86}")
    n = len(obj["features"]) if isinstance(obj["features"], list) else 0
    kinds = obj["kinds"]
    if c.obj(kinds, "kinds", (), KINDS):
        if not kinds:
            c.bad("kinds", "expected at least one kind")
        for k, v in kinds.items():
            p = f"kinds.{k}"
            if c.obj(v, p, ("intercept", "weights"), ("profiles", "examples")):
                c.number(v["intercept"], f"{p}.intercept", -MAX_WEIGHT, MAX_WEIGHT)
                c.weights(v["weights"], f"{p}.weights", n)
                if "profiles" in v:
                    c.integer(v["profiles"], f"{p}.profiles", 0, MAX_COUNT)
                if "examples" in v:
                    c.integer(v["examples"], f"{p}.examples", 0, 1_000_000_000)
    return c.errors


def validate_report(obj) -> list[str]:
    """Schema of a check report; [] when it is valid."""
    c = _Check()
    req = ("type", "protocol", "model_version", "feature_version", "detector_version", "vinyl_check_version", "adopted",
           "profiles")
    if not c.obj(obj, "report", req):
        return c.errors
    c.const(obj["type"], "type", "vinyl-mind/report")
    c.const(obj["protocol"], "protocol", PROTOCOL)
    c.string(obj["model_version"], "model_version", _MODEL_VERSION_RE)
    c.string(obj["feature_version"], "feature_version", r"[0-9]{1,4}")
    c.integer(obj["detector_version"], "detector_version", 1, 100000)
    c.string(obj["vinyl_check_version"], "vinyl_check_version", _VERSION_RE)
    if c.obj(obj["adopted"], "adopted", (), KINDS):
        for k, v in obj["adopted"].items():
            if not isinstance(v, bool):
                c.bad(f"adopted.{k}", "expected a boolean")
    profs = obj["profiles"]
    if not isinstance(profs, list) or not 1 <= len(profs) <= MAX_REPORT_PROFILES:
        c.bad("profiles", f"expected 1 to {MAX_REPORT_PROFILES} entries")
        return c.errors
    for i, pr in enumerate(profs):
        p = f"profiles[{i}]"
        if not c.obj(pr, p, ("profile_id", "kinds")):
            continue
        c.string(pr["profile_id"], f"{p}.profile_id", r"[0-9a-f]{32}")
        if c.obj(pr["kinds"], f"{p}.kinds", (), KINDS):
            for k, v in pr["kinds"].items():
                q = f"{p}.kinds.{k}"
                if c.obj(v, q, ("recordings", "local_error", "global_error", "prior_error")):
                    c.integer(v["recordings"], f"{q}.recordings", 0, 100000)
                    for e in ("local_error", "global_error", "prior_error"):
                        c.number(v[e], f"{q}.{e}", 0, 100, null=True)
    return c.errors


def check_versions(obj: dict) -> None:
    """A model or package must have exactly this station's feature version, feature list and detector version."""
    if obj.get("feature_version") != learning.FEATURE_VERSION:
        raise HubError(i18n.msg("vinylmind.version_mismatch", got=str(obj.get("feature_version")),
                                want=learning.FEATURE_VERSION))
    if obj.get("features") != list(learning.FEATURES):
        raise HubError(i18n.msg("vinylmind.features_mismatch", version=learning.FEATURE_VERSION))
    if obj.get("detector_version") != detect.VERSION:
        raise HubError(i18n.msg("vinylmind.detector_mismatch", got=str(obj.get("detector_version")),
                                want=detect.VERSION))


def verify_model(model, server_key_b64u: str) -> None:
    """Check a model's signature against the accepted server key. Runs before anything else in the model is looked
    at; raises HubError if the model is unsigned, signed with another key or the signature does not verify."""
    rejected = "model_rejected"
    if not isinstance(model, dict) or not isinstance(model.get("signature"), dict):
        raise HubError(i18n.msg("vinylmind.model_unsigned"), code=rejected)
    sig = model["signature"]
    raw = unb64u(server_key_b64u)
    if sig.get("alg") != "ed25519":
        raise HubError(i18n.msg("vinylmind.model_unsigned"), code=rejected)
    if sig.get("key") != key_id(raw):
        raise HubError(i18n.msg("vinylmind.model_wrong_key", fp=show_fingerprint(str(sig.get("key"))[:64]) or "–"),
                       code=rejected)
    try:
        Ed25519PublicKey.from_public_bytes(raw).verify(
            unb64u(sig.get("value", "")), canonical_json({k: v for k, v in model.items() if k != "signature"}))
    except (InvalidSignature, ValueError, TypeError):
        raise HubError(i18n.msg("vinylmind.model_bad_signature"), code=rejected) from None


# ─── Contributions ──────────────────────────────────────────────────────────

def guess_chain_class(profile: dict) -> dict:
    """Coarse chain class from the profile: the capture device tells a USB turntable from an interface, the cartridge
    text MM from MC. Only the class is sent, never the names; "unknown" where the text says nothing."""
    dev = (profile.get("device") or "").lower()
    connection = ("unknown" if not dev else "usb_turntable" if any(h in dev for h in USB_TURNTABLE_HINTS)
                  else "interface")
    cart = profile.get("cartridge") or ""
    if re.search(r"\bMC\b|moving[ -]?coil", cart, re.I):
        cartridge = "mc"
    elif re.search(r"\bM[MI]\b|moving[ -]?(magnet|iron)", cart, re.I):
        cartridge = "mm"
    else:
        cartridge = "unknown"
    return {"connection": connection, "cartridge": cartridge}


def build_contribution(db, profile: dict, chain: dict | None = None) -> tuple[dict | None, dict | None]:
    """(contribution, None), or (None, reason) when no event kind of the profile has a trained model yet.

    The profile's own recordings only, trained without any prior: weights pulled towards the global model would feed
    the hub's own result back to it. Kinds that are trained but not verifiable are included with null quality; the
    hub decides what it uses."""
    data, _ = learning.training_data(db, profile["id"])
    model = learning.train(data, now=0.0)
    kinds = {}
    for kind in KINDS:
        info = model["kinds"][kind]
        if not info.get("model"):
            continue
        fixed = learning.to_fixed(info["model"])
        informative = [bool(x) for x in data[kind]["X"].std(axis=0) >= 1e-9]
        n_manual, n_all = int(info["manual"]), int(info["n_real"]) + int(info["n_false"])
        kinds[kind] = {
            "intercept": fixed["intercept"],
            "weights": [w if inf else 0.0 for w, inf in zip(fixed["weights"], informative)],
            "informative": informative,
            "examples": {"master": n_all - n_manual, "manual": n_manual, "real": int(info["n_real"]),
                         "false_alarm": int(info["n_false"])},
            "recordings": {"total": int(info["recordings"]), "master": int(info.get("master_recordings", 0))},
            "quality": {"est_error": info.get("cv_err_est"), "raw_error": info.get("cv_err_raw"),
                        "auc": info.get("cv_auc"), "usable": bool(info["usable"])},
        }
    if not kinds:
        return None, i18n.msg("vinylmind.nothing_yet", min=learning.MIN_PER_CLASS)
    return {
        "type": "vinyl-mind/contribution",
        "protocol": PROTOCOL,
        "profile_id": db.profile_vm_id(profile["id"]),
        "feature_version": learning.FEATURE_VERSION,
        "features": list(learning.FEATURES),
        "detector_version": detect.VERSION,
        "vinyl_check_version": __version__,
        "chain_class": dict(chain or guess_chain_class(profile)),
        "kinds": kinds,
    }, None


def payload_sha256(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


# ─── Checking a global model on the station's own master recordings ─────────

def _direct_error(fixed: dict, d: dict) -> tuple[float | None, int]:
    """Estimate error of weights applied without any training, per master recording as in learning.train's
    cross-check (recordings with fewer than 3 real events say nothing). Returns (error, recordings)."""
    if d is None or not len(d["y"]):
        return None, 0
    rec, master, y = np.asarray(d["rec"]), np.asarray(d["master"], bool), np.asarray(d["y"], float)
    errs = []
    for g in sorted(set(rec[master].tolist())):
        te = (rec == g) & master
        true = float(y[te].sum())
        if true < 3:
            continue
        errs.append(abs(float(learning.predict_fixed(fixed, d["X"][te]).sum()) - true) / true)
    return (round(float(np.mean(errs)), 3), len(errs)) if errs else (None, 0)


def check_model(data: dict, model_kinds: dict) -> dict:
    """Per kind of the model: {"recordings", "local_error", "global_error", "prior_error"} on the given training data
    (learning.training_data): the local cross-check without prior, the model applied directly, and the local
    cross-check with the model as prior."""
    prior = {k: {"intercept": v["intercept"], "weights": v["weights"]} for k, v in model_kinds.items() if k in KINDS}
    local = learning.train(data, now=0.0)
    with_prior = learning.train(data, now=0.0, prior=prior)
    out = {}
    for kind, fixed in prior.items():
        g_err, n = _direct_error(fixed, data.get(kind))
        out[kind] = {"recordings": n, "local_error": local["kinds"][kind].get("cv_err_est"), "global_error": g_err,
                     "prior_error": with_prior["kinds"][kind].get("cv_err_est")}
    return out


def adopt(check: dict) -> bool:
    """Use the global model as the prior of a kind? If the cross-check with it is not worse than without, or if
    there is no own cross-check yet: the model is then only a starting point, and estimates are still used only once
    the station's own cross-check makes the kind usable."""
    if check.get("local_error") is None:
        return True
    return check.get("prior_error") is not None and check["prior_error"] <= check["local_error"]


# ─── Client ─────────────────────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A signed request is bound to its path, and a redirect could leave HTTPS: never follow one."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    """Requests to one Vinyl-Mind hub. `abort()` is asked before every attempt (a recording started: nothing is sent);
    `sleep` and `backoff` can be replaced in tests."""

    def __init__(self, base_url: str, key: StationKey | None = None, *, timeout: float = TIMEOUT_S,
                 retries: int = RETRIES, backoff=BACKOFF_S, sleep=time.sleep, abort=lambda: False):
        self.base = normalize_url(base_url)
        self.key, self.timeout, self.retries, self.backoff, self.sleep, self.abort = (
            key, timeout, retries, tuple(backoff), sleep, abort)
        self.opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(
            context=ssl.create_default_context()))

    def _target(self, path: str) -> tuple[str, str]:
        """(full URL, signed path) for an API path such as "/v0/server?x=1"."""
        u = urllib.parse.urlsplit(self.base)
        full_path = u.path.rstrip("/") + path
        return urllib.parse.urlunsplit((u.scheme, u.netloc, "", "", "")) + full_path, full_path

    def _once(self, method: str, path: str, body: bytes, signed: bool, extra: dict) -> tuple[int, dict, bytes]:
        url, spath = self._target(path)
        headers = {"Accept": "application/json", "User-Agent": f"vinyl-check/{__version__}"} | extra
        if body:
            headers["Content-Type"] = "application/json"
        if signed:
            headers |= sign_headers(self.key, method, spath, body)
        req = urllib.request.Request(url, data=body or None, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                return resp.status, dict(resp.headers.items()), self._read(resp)
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400 and e.code != 304:      # 304 Not Modified is the ETag answer, not a redirect
                raise HubError(i18n.msg("vinylmind.redirect"), status=e.code) from None
            return e.code, dict(e.headers.items()) if e.headers else {}, self._read(e)

    @staticmethod
    def _read(resp) -> bytes:
        data = resp.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise HubError(i18n.msg("vinylmind.too_large", kb=MAX_RESPONSE_BYTES // 1024))
        return data

    def request(self, method: str, path: str, payload=None, *, signed: bool = True, ok=(200,),
                headers: dict | None = None) -> tuple[int, dict, object]:
        """Send a request (signed anew on every attempt: a fresh timestamp and nonce) and return (status, headers,
        JSON or None). Connection failures, timeouts, 429/502/503/504 and a replay rejection are retried; other
        answers outside `ok` raise HubError with the hub's error code."""
        body = canonical_json(payload) if payload is not None else b""
        if len(body) > MAX_REQUEST_BYTES:
            raise HubError(i18n.msg("vinylmind.request_too_large", kb=MAX_REQUEST_BYTES // 1024))
        last: HubError | None = None
        for attempt in range(max(1, self.retries)):
            if self.abort():
                raise HubError(i18n.msg("vinylmind.recording"), code="recording")
            try:
                status, hdrs, raw = self._once(method, path, body, signed, headers or {})
            except OSError as e:                         # URLError, timeouts, refused connections, TLS failures
                reason = getattr(e, "reason", e)
                last = HubError(i18n.msg("vinylmind.unreachable", url=self.base, error=str(reason)), code="unreachable")
                self._wait(attempt, None)
                continue
            data = None
            if raw:
                try:
                    data = json.loads(raw)
                except ValueError:
                    if status in ok:
                        raise HubError(i18n.msg("vinylmind.server_bad", error="no JSON")) from None
            if status in ok:
                return status, {k.lower(): v for k, v in hdrs.items()}, data
            err = (data or {}).get("error") if isinstance(data, dict) else None
            code = err.get("code") if isinstance(err, dict) else None
            message = err.get("message") if isinstance(err, dict) else None
            retry_after = _retry_after({k.lower(): v for k, v in hdrs.items()})
            last = _hub_error(status, code, message, err, retry_after)
            if status in RETRY_STATUS or (code == "replay" and attempt == 0):
                self._wait(attempt, retry_after)
                continue
            raise last
        raise last

    def _wait(self, attempt: int, retry_after: float | None) -> None:
        if attempt + 1 >= max(1, self.retries):
            return
        delay = min(retry_after, MAX_RETRY_AFTER_S) if retry_after is not None else \
            self.backoff[min(attempt, len(self.backoff) - 1)] if self.backoff else 0.0
        if delay > 0:
            self.sleep(delay)

    # ─── Endpoints ──────────────────────────────────────────────────────────
    def server_info(self) -> dict:
        """GET /v0/server (unsigned), checked: the fingerprint must be the hash of the public key."""
        _, _, info = self.request("GET", f"{API}/server", signed=False)
        if not isinstance(info, dict) or info.get("type") != "vinyl-mind/server" or info.get("protocol") != PROTOCOL:
            raise HubError(i18n.msg("vinylmind.server_bad", error="type/protocol"))
        try:
            raw = unb64u(info["public_key"])
            Ed25519PublicKey.from_public_bytes(raw)
        except (TypeError, KeyError, ValueError):
            raise HubError(i18n.msg("vinylmind.server_bad", error="public_key")) from None
        if info.get("fingerprint") != key_id(raw):
            raise HubError(i18n.msg("vinylmind.server_bad", error="fingerprint"))
        return info

    def join(self, invitation: str | None) -> dict:
        _, _, r = self.request("POST", f"{API}/stations", {
            "type": "vinyl-mind/join", "protocol": PROTOCOL, "public_key": self.key.public_b64u,
            "invitation": invitation or None, "vinyl_check_version": __version__}, ok=(200, 201))
        if not isinstance(r, dict) or r.get("status") not in ("pending", "approved"):
            raise HubError(i18n.msg("vinylmind.server_bad", error="status"))
        return r

    def me(self) -> dict:
        _, _, r = self.request("GET", f"{API}/stations/me")
        if not isinstance(r, dict) or r.get("status") not in ("pending", "approved"):
            raise HubError(i18n.msg("vinylmind.server_bad", error="status"))
        return r

    def leave(self) -> None:
        self.request("DELETE", f"{API}/stations/me", ok=(200, 204))

    def put_contribution(self, payload: dict) -> dict:
        _, _, r = self.request("PUT", f"{API}/contributions/{payload['profile_id']}", payload)
        return r or {}

    def delete_contribution(self, profile_id: str) -> None:
        self.request("DELETE", f"{API}/contributions/{profile_id}", ok=(200, 204, 404))

    def current_model(self, chain: dict, etag: str | None) -> tuple[int, dict | None, str | None]:
        """(200, model, etag), (304, None, etag) or (404, None, None) when the hub has no model yet."""
        q = urllib.parse.urlencode({"feature_version": learning.FEATURE_VERSION, "detector_version": detect.VERSION,
                                    "connection": chain.get("connection", "unknown"),
                                    "cartridge": chain.get("cartridge", "unknown")})
        try:
            status, hdrs, model = self.request("GET", f"{API}/models/current?{q}", ok=(200, 304),
                                               headers={"If-None-Match": etag} if etag else None)
        except HubError as e:
            if e.code == "no_model":
                return 404, None, None
            raise
        return status, (model if status == 200 else None), hdrs.get("etag") or etag

    def report(self, payload: dict) -> None:
        self.request("POST", f"{API}/reports", payload, ok=(200, 202))


def _retry_after(headers: dict) -> float | None:
    try:
        return max(0.0, float(headers.get("retry-after")))
    except (TypeError, ValueError):
        return None


def _hub_error(status: int, code: str | None, message: str | None, err, retry_after) -> HubError:
    """The hub's error as a message for people: known codes get their own text, others the hub's message."""
    if code in ("pending", "unknown_station", "invitation_required", "invitation_invalid", "rate_limited",
                "unsupported_version", "no_model"):
        return HubError(i18n.msg(f"vinylmind.err_{code}"), code=code, status=status, retry_after=retry_after)
    if code == "clock_skew":
        skew = None
        try:
            skew = round(float(err.get("server_time")) - time.time())
        except (TypeError, ValueError, AttributeError):
            pass
        return HubError(i18n.msg("vinylmind.err_clock_skew", s=abs(skew) if skew is not None else "?"), code=code,
                        status=status)
    return HubError(i18n.msg("vinylmind.http_error", status=status, message=str(message or code or "–")[:300]),
                    code=code, status=status, retry_after=retry_after)


# ─── Vinyl-Mind on this station ─────────────────────────────────────────────

class VinylMind:
    """Switches, server, joined state, contributions, the adopted model and the prior of one station.

    State lives in the database settings (S_*), the station key in its file. `busy()` says whether a recording runs
    (then nothing is sent); `retrain()` retrains the station's learning model after the prior changed. One lock keeps
    a background round and the user's actions (join, leave, import) from overlapping."""

    def __init__(self, cfg, db, *, busy=lambda: False, retrain=lambda: None):
        self.cfg, self.db, self.busy, self.retrain = cfg, db, busy, retrain
        self.key_path = Path(cfg.state_dir) / KEY_FILE
        self.lock = threading.RLock()
        self.dirty = True                  # contributions need a look: after start and after every retraining
        self.failures, self.next_attempt = 0, 0.0
        self.last_attempt: float | None = None
        self.last_error = None             # i18n message of the last failed round
        self.client_options: dict = {}     # tests: backoff, sleep, timeout
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ─── Stored state ───────────────────────────────────────────────────────
    def _get(self, key: str, default=None):
        try:
            v = json.loads(self.db.setting(key) or "null")
        except ValueError:
            return default
        return default if v is None else v

    def _put(self, key: str, value) -> None:
        self.db.set_setting(key, json.dumps(value))

    def contribute(self) -> bool:
        return self.db.setting(S_CONTRIBUTE) == "true"

    def use_model(self) -> bool:
        return self.db.setting(S_USE) == "true"

    def preset_url(self) -> str:
        """What "Reset to default" restores: the configuration file's server, else the built-in default."""
        return str(getattr(self.cfg, "vinyl_mind_url", "") or DEFAULT_VINYL_MIND_URL or "").strip().rstrip("/")

    def url(self) -> str:
        return self.db.setting(S_URL) or self.preset_url()

    def joined(self) -> dict | None:
        j = self._get(S_JOIN)
        return j if isinstance(j, dict) and j.get("url") else None

    def _set_join(self, **fields) -> dict:
        j = (self.joined() or {}) | fields
        self._put(S_JOIN, j)
        return j

    def _client(self, url: str, key: StationKey | None) -> Client:
        return Client(url, key, abort=self.busy, **self.client_options)

    def _retrain(self) -> None:
        try:
            self.retrain()
        except Exception:  # noqa: BLE001 — a failed retraining is reported by the learning loop itself
            log.exception("Retraining after a change of the Vinyl-Mind prior failed")

    def _chains(self) -> dict:
        c = self._get(S_CHAIN, {})
        return c if isinstance(c, dict) else {}

    def chain_of(self, profile: dict) -> dict:
        """The chain class sent for a profile: set by the user in the settings, else guessed."""
        vid = profile.get("vm_id")
        return self._chains().get(vid) if vid and vid in self._chains() else guess_chain_class(profile)

    # ─── Status and settings ────────────────────────────────────────────────
    def status(self) -> dict:
        """Everything the settings section shows. No secret: the private key never appears here."""
        j = self.joined()
        sent = self._get(S_SENT, {}) or {}
        last_at = max((v.get("at") or 0 for v in sent.values()), default=0) or None
        m = self._get(S_MODEL, {}) or {}
        model = m.get("model")
        prior = self._get(S_PRIOR)
        url = self.url()
        try:
            key = StationKey.load(self.key_path)
        except (OSError, ValueError):
            key = None
        return {
            "url": url, "preset_url": self.preset_url(), "url_is_preset": url == self.preset_url(),
            "contribute": self.contribute(), "use_model": self.use_model(),
            "state": j.get("status", "pending") if j else "not_joined",
            "joined": {"url": j["url"], "name": j.get("name"), "fingerprint": show_fingerprint(j.get("fingerprint")),
                       "joined_at": j.get("joined_at"), "matches_url": j["url"] == url} if j else None,
            "station_fingerprint": show_fingerprint(key.station_id) if key else None,
            "last_contribution": {"at": last_at, "profiles": len(sent)} if last_at else None,
            "model": {"version": model.get("version"), "created": model.get("created"),
                      "chain_class": model.get("chain_class"), "checked_at": m.get("checked_at"),
                      "adopted": sorted(k for k, v in (m.get("adopted") or {}).items() if v),
                      "in_use": isinstance(prior, dict) and prior.get("source") == "server"
                      and prior.get("ref") == model.get("version") and self.use_model(),
                      "check": m.get("check")} if isinstance(model, dict) else None,
            "polled_at": m.get("polled_at"),
            "prior": {"source": prior.get("source"), "type": prior.get("type"), "ref": prior.get("ref"),
                      "kinds": sorted(prior.get("kinds") or {}), "accepted_at": prior.get("accepted_at"),
                      "in_use": self.prior_for_training() is not None} if isinstance(prior, dict) else None,
            "last_error": self.last_error, "last_attempt": self.last_attempt,
            "next_attempt": self.next_attempt or None, "recording": bool(self.busy()),
            "protocol": PROTOCOL, "feature_version": learning.FEATURE_VERSION, "detector_version": detect.VERSION,
        }

    def update(self, body: dict) -> dict:
        """Switches, server URL ("reset_url" restores the preset) and chain classes per local profile id."""
        with self.lock:
            if "url" in body or body.get("reset_url"):
                raw = "" if body.get("reset_url") else str(body.get("url") or "").strip()
                new = normalize_url(raw) if raw else ""
                j = self.joined()
                if j and j.get("status") != "revoked" and (new or self.preset_url()) != j["url"]:
                    raise HubError(i18n.msg("vinylmind.leave_first", url=j["url"]), code="leave_first")
                self.db.set_setting(S_URL, "" if new == self.preset_url() else new)
            if "chain" in body:
                chains = self._chains()
                for pid, cc in (body["chain"] or {}).items():
                    p = self.db.profile(int(pid))
                    if not p:
                        raise HubError(i18n.msg("api.profile_not_found"), code="not_found")
                    vid = self.db.profile_vm_id(p["id"])
                    if cc is None:
                        chains.pop(vid, None)
                    elif (isinstance(cc, dict) and cc.get("connection") in CONNECTIONS
                          and cc.get("cartridge") in CARTRIDGES):
                        chains[vid] = {"connection": cc["connection"], "cartridge": cc["cartridge"]}
                    else:
                        raise HubError(i18n.msg("vinylmind.chain_invalid"))
                self._put(S_CHAIN, chains)
                self.dirty = True
            if "contribute" in body:
                self.db.set_setting(S_CONTRIBUTE, "true" if body["contribute"] else "false")
                if body["contribute"]:
                    self.dirty, self.next_attempt = True, 0.0
            if "use_model" in body:
                self.db.set_setting(S_USE, "true" if body["use_model"] else "false")
                prior = self._get(S_PRIOR)
                if not body["use_model"]:
                    # the fetched model goes with the switch; switched on again, the station fetches and checks anew
                    self._put(S_MODEL, None)
                    if isinstance(prior, dict) and prior.get("source") == "server":
                        self._put(S_PRIOR, None)
                        self._retrain()
                if body["use_model"]:
                    self.next_attempt = 0.0
        return self.status()

    # ─── Joining and leaving ────────────────────────────────────────────────
    def server_info(self) -> dict:
        """What the configured server says about itself (unsigned), to confirm its key before joining."""
        url = self.url()
        if not url:
            raise HubError(i18n.msg("vinylmind.no_server"), code="no_server")
        info = self._client(url, None).server_info()
        return {"url": url, "name": str(info.get("name") or "")[:100], "fingerprint": info["fingerprint"],
                "fingerprint_display": show_fingerprint(info["fingerprint"]),
                "feature_versions": [str(v) for v in info.get("feature_versions") or []][:20],
                "supported": learning.FEATURE_VERSION in (info.get("feature_versions") or []),
                "join": info.get("join") if isinstance(info.get("join"), dict) else {}}

    def join(self, invitation: str | None = None, fingerprint: str | None = None) -> dict:
        """Register with the configured server. `fingerprint` is the server key the user confirmed; the join stops
        if the server presents another one. The accepted key is stored and verifies every model from then on."""
        with self.lock:
            url = self.url()
            if not url:
                raise HubError(i18n.msg("vinylmind.no_server"), code="no_server")
            if self.busy():
                raise HubError(i18n.msg("vinylmind.recording"), code="recording")
            j = self.joined()
            if j and j["url"] != url and j.get("status") != "revoked":
                raise HubError(i18n.msg("vinylmind.leave_first", url=j["url"]), code="leave_first")
            if j and j.get("status") == "revoked":
                StationKey.delete(self.key_path)          # a revoked key cannot join again: start a new identity
            key = StationKey.load_or_create(self.key_path)
            client = self._client(url, key)
            info = client.server_info()
            want = re.sub(r"[^0-9a-f]", "", str(fingerprint or "").lower()) or (
                j.get("fingerprint") if j and j["url"] == url and j.get("status") != "revoked" else None)
            if want and want != info["fingerprint"]:
                raise HubError(i18n.msg("vinylmind.key_changed", got=show_fingerprint(info["fingerprint"]),
                                        want=show_fingerprint(want)), code="key_changed")
            r = client.join((invitation or "").strip()[:64] or None)
            self._put(S_JOIN, {"url": url, "server_key": info["public_key"], "fingerprint": info["fingerprint"],
                               "name": str(info.get("name") or "")[:100], "station": key.station_id,
                               "status": r["status"], "joined_at": time.time(), "checked_at": time.time()})
            self.dirty, self.failures, self.next_attempt, self.last_error = True, 0, 0.0, None
            log.info("Vinyl-Mind: joined %s as %s… (%s)", url, key.station_id[:12], r["status"])
            return self.status()

    def leave(self, forget: bool = False) -> dict:
        """Leave the server: it deletes this station's contributions and reports. `forget` clears the local state
        even when the server cannot be reached (for a server that is gone)."""
        with self.lock:
            j = self.joined()
            if not j:
                StationKey.delete(self.key_path)
                return self.status()
            if self.busy():
                raise HubError(i18n.msg("vinylmind.recording"), code="recording")
            key = StationKey.load(self.key_path)
            if key and j.get("status") != "revoked":
                try:
                    self._client(j["url"], key).leave()
                except HubError as e:
                    if e.code != "unknown_station" and not forget:
                        raise
            for s in (S_JOIN, S_SENT, S_MODEL):
                self._put(s, None)
            StationKey.delete(self.key_path)
            prior = self._get(S_PRIOR)
            if isinstance(prior, dict) and prior.get("source") == "server":
                self._put(S_PRIOR, None)
                self._retrain()
            self.failures, self.next_attempt, self.last_error = 0, 0.0, None
            log.info("Vinyl-Mind: left %s", j["url"])
            return self.status()

    # ─── Contributions, preview, packages ───────────────────────────────────
    def entries(self) -> list[dict]:
        """Per turntable profile: the contribution as it would be sent now, or why there is none."""
        out = []
        chains = self._chains()
        for p in self.db.profiles():
            vid = self.db.profile_vm_id(p["id"])
            guess = guess_chain_class(p)
            payload, reason = build_contribution(self.db, p, chains.get(vid) or guess)
            out.append({"profile": p, "vm_id": vid, "chain": chains.get(vid) or guess, "chain_guess": guess,
                        "chain_set": vid in chains, "payload": payload, "reason": reason})
        return out

    def preview(self) -> dict:
        """The exact payloads the next contribution would send, per profile (the names are shown only here)."""
        sent = self._get(S_SENT, {}) or {}
        rows = []
        for e in self.entries():
            h = payload_sha256(e["payload"]) if e["payload"] else None
            prev = sent.get(e["vm_id"]) or {}
            rows.append({"id": e["profile"]["id"], "name": e["profile"]["name"], "active": bool(e["profile"]["active"]),
                         "vm_id": e["vm_id"], "chain": e["chain"], "chain_guess": e["chain_guess"],
                         "chain_set": e["chain_set"], "payload": e["payload"], "reason": e["reason"], "sha256": h,
                         "sent_at": prev.get("at"), "unchanged": bool(h) and prev.get("sha256") == h})
        return {"url": self.url(), "contribute": self.contribute(), "profiles": rows}

    def export(self, pid: int) -> dict:
        p = self.db.profile(pid)
        if not p:
            raise HubError(i18n.msg("api.profile_not_found"), code="not_found")
        p["vm_id"] = self.db.profile_vm_id(pid)
        payload, reason = build_contribution(self.db, p, self.chain_of(p))
        if not payload:
            raise HubError(reason, code="nothing")
        return payload

    def describe(self, pkg) -> dict:
        """What a package file is, after checking it: type, versions, kinds and their examples. Raises HubError."""
        if not isinstance(pkg, dict):
            raise HubError(i18n.msg("vinylmind.package_type"))
        kind = {"vinyl-mind/contribution": "contribution", "vinyl-mind/model": "model"}.get(pkg.get("type"))
        if not kind:
            raise HubError(i18n.msg("vinylmind.package_type"))
        errs = validate_contribution(pkg) if kind == "contribution" else validate_model(pkg, require_signature=False)
        if errs:
            raise HubError(i18n.msg("vinylmind.package_invalid", error=errs[0], n=len(errs)))
        check_versions(pkg)
        if kind == "contribution":
            own = {r["vm_id"] for r in self.db.q("SELECT vm_id FROM profiles WHERE vm_id IS NOT NULL")}
            if pkg["profile_id"] in own:
                raise HubError(i18n.msg("vinylmind.package_own"))
            kinds = {k: {"examples": v["examples"]["master"] + v["examples"]["manual"],
                         "manual": v["examples"]["manual"], "est_error": v["quality"]["est_error"],
                         "usable": v["quality"]["usable"]} for k, v in pkg["kinds"].items()}
            ref, signature = pkg["profile_id"][:8], None
        else:
            kinds = {k: {"examples": v.get("examples"), "profiles": v.get("profiles")} for k, v in pkg["kinds"].items()}
            ref, signature = pkg["version"], "none"
            j = self.joined()
            if "signature" in pkg:
                signature = "unverified"
                if j:
                    try:
                        verify_model(pkg, j["server_key"])
                        signature = "verified"
                    except HubError:
                        pass
        return {"type": kind, "ref": ref, "feature_version": pkg["feature_version"],
                "detector_version": pkg["detector_version"], "chain_class": pkg["chain_class"], "kinds": kinds,
                "signature": signature, "vinyl_check_version": pkg.get("vinyl_check_version")}

    def import_package(self, pkg) -> dict:
        """Use a package's weights as the prior of the training (after the user confirmed `describe`)."""
        with self.lock:
            d = self.describe(pkg)
            self._put(S_PRIOR, {"source": "import", "type": d["type"], "ref": d["ref"],
                                "feature_version": pkg["feature_version"], "detector_version": pkg["detector_version"],
                                "kinds": {k: {"intercept": v["intercept"], "weights": v["weights"]}
                                          for k, v in pkg["kinds"].items()},
                                "accepted_at": time.time()})
            self._retrain()
            log.info("Vinyl-Mind: imported %s %s as the prior", d["type"], d["ref"])
            return {"package": d, "status": self.status()}

    def remove_prior(self) -> dict:
        with self.lock:
            self._put(S_PRIOR, None)
            self._retrain()
            return self.status()

    def prior_info(self) -> dict | None:
        """The prior in effect for the training ({"source", "type", "ref", "kinds"}), or None. A prior of another
        feature or detector version is ignored; an adopted server model only counts while its switch is on."""
        p = self._get(S_PRIOR)
        if not isinstance(p, dict) or p.get("feature_version") != learning.FEATURE_VERSION \
                or p.get("detector_version") != detect.VERSION:
            return None
        if p.get("source") == "server" and not self.use_model():
            return None
        kinds = {k: v for k, v in (p.get("kinds") or {}).items() if k in KINDS}
        if not kinds:
            return None
        return {"source": p.get("source"), "type": p.get("type"), "ref": p.get("ref"), "kinds": kinds}

    def prior_for_training(self) -> dict | None:
        """{kind: {"intercept", "weights"}} for learning.train(prior=...), or None."""
        info = self.prior_info()
        return info["kinds"] if info else None

    def note_retrained(self) -> None:
        self.dirty = True

    # ─── The round ──────────────────────────────────────────────────────────
    def run_round(self, force: bool = False) -> dict:
        """Contribute changed profiles and look for a newer global model, as the switches allow. Never while a
        recording runs and nothing at all while both switches are off. `force` ("Send now") ignores the intervals
        and raises the error of a failed round."""
        with self.lock:
            if not (self.contribute() or self.use_model()):
                return {"skipped": "off"}
            j = self.joined()
            if not j or j["url"] != self.url() or j.get("status") == "revoked":
                return {"skipped": "not_joined"}
            if self.busy():
                return {"skipped": "recording"}
            now = time.time()
            if not force and now < self.next_attempt:
                return {"skipped": "backoff"}
            try:
                key = StationKey.load(self.key_path)
            except (OSError, ValueError):
                key = None
            if key is None or key.station_id != j.get("station"):
                # the key file is gone or replaced (restored backup, deleted by hand): no longer the joined station
                self._set_join(status="revoked")
                return {"skipped": "not_joined"}
            self.last_attempt, out = now, {}
            try:
                client = self._client(j["url"], key)
                if j.get("status") != "approved":
                    if not force and now - (j.get("checked_at") or 0) < STATUS_POLL_S:
                        return {"skipped": "pending"}
                    j = self._set_join(status=client.me()["status"], checked_at=now)
                    if j["status"] != "approved":
                        return {"status": j["status"]}
                if self.contribute() and (force or self.dirty):
                    out["contributions"] = self._contribute(client, force)
                if self.use_model():
                    m = self._get(S_MODEL, {}) or {}
                    if force or now - (m.get("polled_at") or 0) >= MODEL_POLL_S:
                        out["model"] = self._fetch_model(client, j, now)
            except HubError as e:
                if e.code == "recording":                # a recording started meanwhile: try again next round
                    if force:
                        raise
                    return {"skipped": "recording"}
                if e.code == "unknown_station":
                    self._set_join(status="revoked")
                elif e.code == "pending":
                    self._set_join(status="pending", checked_at=now)
                self.failures += 1
                self.next_attempt = now + min(ROUND_S * 2 ** self.failures, MAX_ROUND_BACKOFF_S)
                self.last_error = e.message
                log.warning("Vinyl-Mind round failed: %s", i18n.render(e.message, "en"))
                if force:
                    raise
                return {"error": e.message}
            self.failures, self.next_attempt, self.last_error = 0, 0.0, None
            return out

    def _contribute(self, client: Client, force: bool) -> dict:
        """PUT every profile whose payload changed (at most once per MIN_CONTRIBUTION_INTERVAL_S unless forced) and
        withdraw contributions of profiles that are gone or have nothing to contribute any more."""
        sent = self._get(S_SENT, {}) or {}
        current, n, waiting = set(), 0, False
        for e in self.entries():
            payload = e["payload"]
            if payload is None:
                continue
            errs = validate_contribution(payload)
            if errs:                                     # a bug on this side: never send what the hub must reject
                log.error("Vinyl-Mind: contribution of profile %s not sent, invalid: %s", e["profile"]["id"], errs[:3])
                continue
            vid, h = payload["profile_id"], payload_sha256(payload)
            current.add(vid)
            prev = sent.get(vid) or {}
            if prev.get("sha256") == h:
                continue
            if not force and prev.get("at") and time.time() - prev["at"] < MIN_CONTRIBUTION_INTERVAL_S:
                waiting = True
                continue
            client.put_contribution(payload)
            sent[vid] = {"sha256": h, "at": time.time()}
            self._put(S_SENT, sent)                      # after each success: a later failure does not resend it
            n += 1
        withdrawn = 0
        for vid in [v for v in sent if v not in current]:
            client.delete_contribution(vid)
            sent.pop(vid)
            self._put(S_SENT, sent)
            withdrawn += 1
        self.dirty = waiting
        return {"sent": n, "withdrawn": withdrawn, "waiting": waiting}

    def _active_chain(self) -> dict:
        p = self.db.active_profile()
        return self.chain_of(p) if p else {"connection": "unknown", "cartridge": "unknown"}

    def _fetch_model(self, client: Client, j: dict, now: float) -> str:
        """Ask for the current global model; a new one is verified, checked on the own master recordings, adopted
        per kind as the prior where it is not worse, and reported."""
        m = self._get(S_MODEL, {}) or {}
        status, model, etag = client.current_model(self._active_chain(), m.get("etag") if m.get("model") else None)
        if status == 304:
            self._put(S_MODEL, m | {"polled_at": now})
            return "unchanged"
        if status == 404:                                # nothing published (any more): stop using a server prior
            self._put(S_MODEL, {"polled_at": now})
            prior = self._get(S_PRIOR)
            if isinstance(prior, dict) and prior.get("source") == "server":
                self._put(S_PRIOR, None)
                self._retrain()
            return "none"
        verify_model(model, j["server_key"])             # before anything else in the model is looked at
        errs = validate_model(model)
        if errs:
            raise HubError(i18n.msg("vinylmind.model_invalid", error=errs[0]), code="model_rejected")
        try:
            check_versions(model)
        except HubError as e:                            # the hub sent a model of another version: its fault
            raise HubError(e.message, code="model_rejected") from None
        data, _ = learning.training_data(self.db)
        check = check_model(data, model["kinds"])
        adopted = {k: adopt(v) for k, v in check.items()}
        self._put(S_MODEL, {"model": model, "etag": etag, "polled_at": now, "checked_at": time.time(), "check": check,
                            "adopted": adopted})
        prior = self._get(S_PRIOR)
        if any(adopted.values()):
            self._put(S_PRIOR, {"source": "server", "type": "model", "ref": model["version"],
                                "feature_version": model["feature_version"],
                                "detector_version": model["detector_version"],
                                "kinds": {k: {"intercept": model["kinds"][k]["intercept"],
                                              "weights": model["kinds"][k]["weights"]}
                                          for k, ok in adopted.items() if ok},
                                "accepted_at": time.time()})
        elif isinstance(prior, dict) and prior.get("source") == "server":
            self._put(S_PRIOR, None)
        self._retrain()
        log.info("Vinyl-Mind: model %s checked, adopted for %s", model["version"],
                 [k for k, ok in adopted.items() if ok] or "no kind")
        report = self.build_report(model, adopted)
        if report:
            client.report(report)
        return "new"

    def build_report(self, model: dict, adopted: dict) -> dict | None:
        """The check report of a model: per profile with master recordings, the three estimate errors per kind."""
        profiles = []
        for p in self.db.profiles():
            data, _ = learning.training_data(self.db, p["id"])
            kinds = {k: v for k, v in check_model(data, model["kinds"]).items()
                     if v["recordings"] or v["local_error"] is not None}
            if kinds:
                profiles.append({"profile_id": self.db.profile_vm_id(p["id"]), "kinds": kinds})
        if not profiles:
            return None
        report = {"type": "vinyl-mind/report", "protocol": PROTOCOL, "model_version": model["version"],
                  "feature_version": model["feature_version"], "detector_version": model["detector_version"],
                  "vinyl_check_version": __version__, "adopted": {k: bool(v) for k, v in adopted.items()},
                  "profiles": profiles[:MAX_REPORT_PROFILES]}
        errs = validate_report(report)
        if errs:
            log.error("Vinyl-Mind: report not sent, invalid: %s", errs[:3])
            return None
        return report

    # ─── Background ─────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="vinylcheck-vinylmind")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(ROUND_S):
            try:
                self.run_round()
            except Exception:  # noqa: BLE001 — the next round tries again
                log.exception("Vinyl-Mind round failed")


# ─── Routes ─────────────────────────────────────────────────────────────────

# HubErrors that describe a conflict with the station's state (HTTP 409); other local ones are invalid input (422),
# and failures of the hub itself or of the connection are 502
_CONFLICT = ("recording", "leave_first", "key_changed", "no_server", "not_joined", "switches_off", "nothing")


def install(app, vm: VinylMind) -> VinylMind:
    """Add the routes /api/vinylmind* to `app` (also app.state.vinylmind)."""
    app.state.vinylmind = vm

    def http(e: HubError) -> HTTPException:
        if e.code == "not_found":
            return HTTPException(404, e.message)
        if e.code in _CONFLICT:
            return HTTPException(409, e.message)
        if e.status is None and e.code is None:
            return HTTPException(422, e.message)
        return HTTPException(502, e.message)

    def call(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except HubError as e:
            raise http(e) from None

    def flag(body: dict, key: str):
        v = body.get(key)
        if not isinstance(v, bool):
            raise HTTPException(422, i18n.msg("vinylmind.bad_field", field=key))
        return v

    @app.get("/api/vinylmind")
    def vm_status():
        return vm.status()

    @app.put("/api/vinylmind")
    def vm_update(body: dict):
        unknown = set(body) - {"contribute", "use_model", "url", "reset_url", "chain"}
        if unknown:
            raise HTTPException(422, i18n.msg("vinylmind.bad_field", field=", ".join(sorted(unknown))))
        for k in ("contribute", "use_model", "reset_url"):
            if k in body:
                flag(body, k)
        if "url" in body and not isinstance(body["url"], (str, type(None))):
            raise HTTPException(422, i18n.msg("vinylmind.bad_field", field="url"))
        if "chain" in body and not isinstance(body["chain"], dict):
            raise HTTPException(422, i18n.msg("vinylmind.bad_field", field="chain"))
        return call(vm.update, body)

    @app.get("/api/vinylmind/server")
    def vm_server():
        return call(vm.server_info)

    @app.get("/api/vinylmind/preview")
    def vm_preview():
        return vm.preview()

    @app.post("/api/vinylmind/join")
    def vm_join(body: dict | None = None):
        body = body or {}
        inv, fp = body.get("invitation"), body.get("fingerprint")
        if not isinstance(inv, (str, type(None))) or not isinstance(fp, (str, type(None))):
            raise HTTPException(422, i18n.msg("vinylmind.bad_field", field="invitation"))
        return call(vm.join, inv, fp)

    @app.post("/api/vinylmind/leave")
    def vm_leave(body: dict | None = None):
        return call(vm.leave, bool((body or {}).get("forget")))

    @app.post("/api/vinylmind/sync")
    def vm_sync():
        if not (vm.contribute() or vm.use_model()):
            raise HTTPException(409, i18n.msg("vinylmind.switches_off"))
        j = vm.joined()
        if not j or j["url"] != vm.url() or j.get("status") == "revoked":
            raise HTTPException(409, i18n.msg("vinylmind.not_joined"))
        result = call(vm.run_round, force=True)
        return {"result": result, "status": vm.status()}

    @app.get("/api/vinylmind/export")
    def vm_export(profile: int):
        payload = call(vm.export, profile)
        name = f"vinyl-mind-contribution-{payload['profile_id'][:8]}.json"
        return Response(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})

    @app.post("/api/vinylmind/import")
    async def vm_import(request: Request, confirm: bool = False):
        # the request body is the package file itself, read with a size limit
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_PACKAGE_BYTES:
                raise HTTPException(413, i18n.msg("vinylmind.package_too_large", kb=MAX_PACKAGE_BYTES // 1024))
        try:
            pkg = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise HTTPException(422, i18n.msg("vinylmind.package_json")) from None
        if not confirm:
            return {"package": await run_in_threadpool(call, vm.describe, pkg)}
        return await run_in_threadpool(call, vm.import_package, pkg)

    @app.delete("/api/vinylmind/prior")
    def vm_prior_delete():
        return vm.remove_prior()

    return vm

"""Station side of Vinyl-Mind: joining a hub (sign-in), contributions, global models, package files.

Vinyl-Mind is a separate project: a hub that combines what the learning models (learning.py) of many stations have
learned. Its specification is the contract, and this module follows it exactly: protocol version 1 of the Vinyl-Mind
repository (`spec/README.md`, `spec/signing.md`, `spec/v1/*.schema.json`, `spec/v1/features.json`, `docs/api.md`,
`docs/station-side.md`, and `docs/connect.md` for signing in with an account). docs/vinyl-mind.md describes what the
station does with it.

- **Keys** (`StationKey`): one Ed25519 key pair per hub, a file in the state directory with mode 600. Only the public
  key leaves the station; the private key is never stored in the database, returned by the API or logged, and it is
  deleted when the station leaves.
- **Joining**: with an account (`docs/connect.md`: flow A, the browser on this computer returns to the engine's
  loopback address; flow B, a code to confirm on a phone), or with an invitation code / the operator's approval for
  hubs without accounts. The secrets of these flows (one-time code, PKCE verifier, device code, invitation, binding)
  live in memory only: never stored, logged or sent to the web interface.
- **Client** (`Client`): signed requests (`spec/signing.md`) over HTTPS (plain HTTP only to this computer, for tests
  and a hub on the same machine), bodies as bytes with Content-Length, the header `Vinyl-Mind-Client` on every
  request, timeouts, retries with backoff and Retry-After, no redirects, size limits.
- **Contributions** (`build_contribution`): per turntable profile, the learning model trained on that profile's
  recordings without any prior, in the shared scaling of the feature version, with counts and the cross-check.
- **Global models**: verified as a JWS against the accepted server key before anything in them is parsed, validated
  against the schema, checked on the station's own master recordings (`check_model`), reported, and used as the prior
  of the training only if they do not make the estimate worse.
- `VinylMind` keeps the state in the database settings, runs the background round (never while a recording runs,
  nothing at all while both switches are off) and `install` adds the routes /api/vinylmind* and /vinyl-mind/callback.

Server catalog: ``vinylmind``. The settings section is in the web catalog ``vinylmind``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
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
import webbrowser
from pathlib import Path

import numpy as np
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from . import __version__, detect, i18n, learning, network

log = logging.getLogger("vinylcheck.vinylmind")

# ─── Protocol (the hub's specification, version 1) ─────────────────────────────────────────────────────────────────

# The preset server: the project's own hub. A configuration file can preset another one (`vinyl_mind_url`) for a group
# of stations with a self-hosted hub.
DEFAULT_VINYL_MIND_URL = "https://api.vinylcheck.net"

API = "/v1"
CLIENT_NAME = "vinyl-check"
CLIENT = f"{CLIENT_NAME}/{__version__}"         # Vinyl-Mind-Client header and the `client` of a contribution
SPEC_FEATURE_VERSIONS = (1,)                    # feature versions the specification defines (spec/v<n>/)
SOURCES = ("usb_turntable", "turntable_interface")
CARTRIDGES = ("mm", "mc", "unknown")
KINDS = learning.KINDS
COEF_BOUND = 50.0                               # spec/v1/features.json, coef_bound
MAX_COUNT = 10_000_000
MAX_REQUEST_BYTES = 16 * 1024                   # the hub refuses larger bodies
MAX_RESPONSE_BYTES = 256 * 1024
MAX_PACKAGE_BYTES = 64 * 1024
CLOCK_WINDOW_S = 300                            # signed requests further off the hub's clock are refused …
SKEW_WARN_S = 60                                # … so the station warns well before
TIMEOUT_S = 10.0
RETRIES = 3                                     # attempts per request for transient failures
BACKOFF_S = (1.0, 2.0, 4.0)                     # waits between them
MAX_RETRY_AFTER_S = 60.0                        # longer Retry-After: give up now, the round tries again later
RETRY_STATUS = (502, 503, 504)
ROUND_S = 60.0                                  # background round
MIN_CONTRIBUTION_INTERVAL_S = 3600.0            # per profile, unless "Send now"
MODEL_POLL_S = 6 * 3600.0                       # ask for newer global models (ETag, usually 304 without a body)
STATUS_POLL_S = 600.0                           # while waiting for approval, in the background
REFRESH_MIN_S = 5.0                             # "refresh now" while waiting, at most this often
SERVER_REFRESH_S = 24 * 3600.0                  # what the hub accepts (feature, detector and client versions)
MAX_ROUND_BACKOFF_S = 6 * 3600.0
KEY_DIR = "vinyl-mind"                          # in the state directory: one key file per hub
LEGACY_KEY_FILE = "vinyl-mind-station.key"      # the single key of the draft protocol, never valid for version 1
FINGERPRINT_RE = r"SHA256:[A-Za-z0-9+/]{43}"
TOKEN_RE = r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,31}"
PROFILE_ID_RE = r"[A-Za-z0-9_-]{16,64}"
INVITATION_RE = r"[A-Za-z0-9_-]{8,64}"
JWS_RE = r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
MODEL_TYPE = "vinyl-mind-model+json"


class Account:
    """Signing in with an account: the names of the hub's docs/connect.md, in one place so that they can follow the
    agreement between hub, account service and station."""

    SERVER_FIELD = "connect_url"                # GET /v1/server: the account service's connect page
    TOKEN_PATH = "/api/account/connect/token"   # at the account service's origin
    DEVICE_PATH = "/api/account/device"
    CALLBACK_PATH = "/vinyl-mind/callback"      # on this station's engine, loopback only
    CLIENT_ID = CLIENT_NAME
    GRANT_CODE = "authorization_code"
    GRANT_DEVICE = "urn:ietf:params:oauth:grant-type:device_code"
    FLOW_TTL_S = 600.0                          # a browser flow stays open this long
    DEVICE_INTERVAL_S = 5                       # default polling interval of flow B
    DEVICE_TTL_S = 600                          # default lifetime of a device code
    SLOW_DOWN_S = 5                             # RFC 8628: slow_down adds this to the interval
    JOIN_RETRY_S = 60.0                         # a join refused for a while (join_limit, recording) is tried again
    LANGS = ("en", "de")


# Keys in the settings table of the database
S_CONTRIBUTE, S_USE, S_URL = "vinyl_mind_contribute", "vinyl_mind_use_model", "vinyl_mind_url"
S_JOIN, S_SENT, S_MODEL, S_PRIOR = "vinyl_mind_join", "vinyl_mind_sent", "vinyl_mind_model", "vinyl_mind_prior"
S_CHAIN_LEGACY = "vinyl_mind_chain"            # chain classes of the draft, now two fields of the profile
# Capture devices that are a turntable's own USB converter (most USB turntables use a PCM290x codec that reports
# itself as "USB Audio CODEC"); anything else counts as an interface behind a phono stage
USB_TURNTABLE_HINTS = ("usb audio codec", "usb audio  codec", "pcm290", "turntable")
# Hub error codes with a text of their own in the catalog (vinylmind.err_<code>); others show the hub's message
HUB_ERRORS = ("invitation_invalid", "invitation_required", "client_not_accepted", "revoked", "rate_limited",
              "join_limit", "pending", "unknown_key", "stale", "bad_signature", "replayed", "unsigned",
              "malformed_signature", "bad_client", "binding_invalid", "binding_required", "too_many_profiles",
              "feature_version_not_accepted", "detector_version_not_accepted", "withdrawn", "bad_profile_id",
              "too_large", "length_required", "not_found")
ACCOUNT_ERRORS = ("invalid_request", "invalid_grant", "access_denied", "authorization_pending", "slow_down",
                  "expired_token", "rate_limited")


class HubError(i18n.MessageError):
    """A failure talking to the hub or the account service, or with what they sent. `code` is the protocol's error
    code where there is one."""

    def __init__(self, message, code: str | None = None, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.code, self.status, self.retry_after = code, status, retry_after


# ─── Encodings (spec/signing.md) ───────────────────────────────────────────────────────────────────────────────────

def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64u(text: str) -> bytes:
    """Strict base64url without padding: one value has exactly one spelling, as the hub decodes it."""
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9_-]*", text) or len(text) % 4 == 1:
        raise ValueError("not base64url without padding")
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if b64u(raw) != text:
        raise ValueError("not the canonical base64url spelling")
    return raw


def fingerprint(raw_public: bytes) -> str:
    """`SHA256:` and the SHA-256 of the raw 32-byte public key in standard base64 without padding."""
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw_public).digest()).decode("ascii").rstrip("=")


def short_fingerprint(fp: str | None) -> str | None:
    """The beginning of a fingerprint, for a line of text (the full value is shown where it is compared)."""
    return f"{fp[:19]}…" if fp else None


def compact_json(obj) -> bytes:
    """The bytes of a request body: compact UTF-8 JSON, finite numbers only (the hub refuses anything else)."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def strict_json(raw: bytes | str):
    """Parse JSON as the hub does: valid UTF-8, no duplicate keys, no NaN or Infinity. ValueError otherwise."""
    def pairs(items):
        out = {}
        for k, v in items:
            if k in out:
                raise ValueError(f"duplicate key {k!r}")
            out[k] = v
        return out

    def constant(name):
        raise ValueError(f"not a finite number: {name}")

    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def doc_sha256(doc: dict, ignore: tuple = ()) -> str:
    return hashlib.sha256(compact_json({k: v for k, v in sorted(doc.items()) if k not in ignore})).hexdigest()


def is_local_host(host: str) -> bool:
    host = (host or "").strip("[]")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def normalize_url(url, *, path_ok: bool = True) -> str:
    """A server URL without a trailing slash. HTTPS only; plain HTTP is accepted for this computer itself (tests, a hub
    on the same machine). Raises HubError with a message saying what is wrong."""
    url = str(url or "").strip().rstrip("/")
    try:
        u = urllib.parse.urlsplit(url)
        host = u.hostname or ""
        u.port                                          # noqa: B018 — raises ValueError on a bad port
    except ValueError:
        raise HubError(i18n.msg("vinylmind.url_invalid")) from None
    if u.username or u.password or u.query or u.fragment or not host or (u.path and not path_ok):
        raise HubError(i18n.msg("vinylmind.url_parts"))
    if u.scheme == "https" or (u.scheme == "http" and is_local_host(host)):
        return url
    raise HubError(i18n.msg("vinylmind.url_invalid"))


def _web_url(url, field: str) -> str:
    """A URL the hub or the account service names (connect_url, verification_uri): HTTPS (HTTP only on this computer),
    without user info or fragment. Raises HubError."""
    try:
        u = urllib.parse.urlsplit(str(url or ""))
        ok = (u.scheme == "https" or (u.scheme == "http" and is_local_host(u.hostname or ""))) and u.hostname \
            and not u.username and not u.password and not u.fragment and len(str(url)) <= 2000
        u.port                                          # noqa: B018
    except ValueError:
        ok = False
    if not ok:
        raise HubError(i18n.msg("vinylmind.server_bad", error=field))
    return str(url)


def origin(url: str) -> str:
    u = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((u.scheme, u.netloc, "", "", ""))


def accepts_client(patterns, client: str = CLIENT) -> bool:
    """Does the hub's list of accepted clients take this one? "name/version", "name/prefix*" or "name/*"; an empty
    list accepts everything (docs/api.md, GET /v1/server)."""
    if not patterns:
        return True
    for p in patterns:
        p = str(p)
        if (p.endswith("*") and client.startswith(p[:-1])) or client == p:
            return True
    return False


# ─── Keys ──────────────────────────────────────────────────────────────────────────────────────────────────────────

class StationKey:
    """One Ed25519 key pair of the station for one hub. The private key stays inside this object and its file (mode
    600): it is never written to the database, returned by the API or logged (repr shows only the fingerprint)."""

    def __init__(self, private: Ed25519PrivateKey):
        self._private = private
        self.public_raw = private.public_key().public_bytes(serialization.Encoding.Raw,
                                                            serialization.PublicFormat.Raw)
        self.fingerprint = fingerprint(self.public_raw)

    def __repr__(self) -> str:
        return f"StationKey({self.fingerprint})"

    @property
    def public_b64u(self) -> str:
        return b64u(self.public_raw)

    def sign(self, data: bytes) -> bytes:
        return self._private.sign(data)

    @classmethod
    def generate(cls) -> StationKey:
        return cls(Ed25519PrivateKey.generate())

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

    def save(self, path: Path) -> None:
        """Write the key with mode 600 (the file is created with that mode before any content goes in)."""
        pem = self._private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption())
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(path.parent, 0o700)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
        os.chmod(tmp, 0o600)                             # independent of the umask
        os.replace(tmp, path)

    @staticmethod
    def delete(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def key_file(state_dir, server_fp: str, pending: bool = False) -> Path:
    """Where the station's key for one hub lives. `pending`: a key made for a join that has not succeeded yet."""
    name = hashlib.sha256(server_fp.encode("utf-8")).hexdigest()[:32]
    return Path(state_dir) / KEY_DIR / f"{name}{'.new' if pending else ''}.key"


# ─── Signed requests and signed documents (spec/signing.md) ────────────────────────────────────────────────────────

def request_message(server_fp: str, method: str, target: str, body: bytes, timestamp: str, nonce: str,
                    key_fp: str) -> bytes:
    """The eight lines a station signs: prefix, server key, method, path and query as sent, body hash, timestamp,
    nonce, station key; joined with a line feed, none at the end."""
    return "\n".join(("vinyl-mind-request-v1", server_fp, method.upper(), target, hashlib.sha256(body).hexdigest(),
                      timestamp, nonce, key_fp)).encode("utf-8")


def sign_headers(key: StationKey, server_fp: str, method: str, target: str, body: bytes, now: float | None = None,
                 nonce: str | None = None) -> dict:
    """The signature headers of one request, with a new nonce each time."""
    ts = str(int(now if now is not None else time.time()))
    nonce = nonce or b64u(secrets.token_bytes(16))
    sig = key.sign(request_message(server_fp, method, target, body, ts, nonce, key.fingerprint))
    return {"Vinyl-Mind-Key": key.fingerprint, "Vinyl-Mind-Timestamp": ts, "Vinyl-Mind-Nonce": nonce,
            "Vinyl-Mind-Signature": b64u(sig)}


def verify_jws(token, public_raw: bytes, kid: str, typ: str = MODEL_TYPE) -> dict:
    """Check a signed model before anything in it is used: three parts, the header {"alg": "EdDSA", "typ", "kid"}
    with `kid` the accepted server fingerprint, the signature with the accepted key; only then is the payload parsed,
    and its `server` must equal `kid`. Returns the payload; raises HubError (code "model_rejected")."""
    def bad(key, **p):
        return HubError(i18n.msg(f"vinylmind.{key}", **p), code="model_rejected")
    if not isinstance(token, str) or len(token) > MAX_RESPONSE_BYTES or not re.fullmatch(JWS_RE, token):
        raise bad("model_unsigned")
    head_b64, payload_b64, sig_b64 = token.split(".")
    try:
        header = strict_json(unb64u(head_b64))
    except (ValueError, UnicodeDecodeError):
        raise bad("model_unsigned") from None
    if not isinstance(header, dict) or header.get("alg") != "EdDSA" or header.get("typ") != typ:
        raise bad("model_unsigned")
    if header.get("kid") != kid:
        raise bad("model_wrong_key", fp=short_fingerprint(str(header.get("kid"))[:60]) or "–")
    try:
        Ed25519PublicKey.from_public_bytes(public_raw).verify(unb64u(sig_b64),
                                                              f"{head_b64}.{payload_b64}".encode("ascii"))
    except (InvalidSignature, ValueError):
        raise bad("model_bad_signature") from None
    try:
        payload = strict_json(unb64u(payload_b64))
    except (ValueError, UnicodeDecodeError):
        raise bad("model_invalid", error="payload") from None
    if not isinstance(payload, dict) or payload.get("server") != kid:
        raise bad("model_wrong_key", fp=short_fingerprint(str((payload or {}).get("server"))[:60]
                                                         if isinstance(payload, dict) else "") or "–")
    return payload


# ─── Validation (the JSON schemas of spec/v1, checked by hand) ─────────────────────────────────────────────────────

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

    def numbers(self, v, path: str, n: int, lo: float = -math.inf, hi: float = math.inf) -> None:
        if not isinstance(v, list) or len(v) != n:
            self.bad(path, f"expected {n} entries")
            return
        for i, x in enumerate(v):
            if not _num(x) or not lo <= x <= hi:
                self.bad(f"{path}[{i}]", "expected a number" + (f" in [{lo:g}, {hi:g}]" if math.isfinite(lo) else ""))

    def kinds(self, v, path: str) -> list[tuple[str, dict]]:
        if not self.obj(v, path, (), KINDS):
            return []
        if not v:
            self.bad(path, "expected at least one kind")
        return list(v.items())


def _ncoef(version) -> int:
    return len(learning.feature_names(str(version))) + 1


def validate_contribution(doc, version: str = learning.FEATURE_VERSION) -> list[str]:
    """contribution.schema.json (of feature version `version`; version 2 has the same form with its own number of
    coefficients) plus what the hub checks beyond it: n_manual + n_master = n_real + n_false. [] when valid."""
    c = _Check()
    if not c.obj(doc, "contribution", ("spec", "feature_version", "detector_version", "client", "chain", "kinds"),
                 ("trained_at",)):
        return c.errors
    c.const(doc["spec"], "spec", "vinyl-mind/contribution")
    c.const(doc["feature_version"], "feature_version", int(version))
    c.string(doc["detector_version"], "detector_version", TOKEN_RE)
    if c.obj(doc["client"], "client", ("name", "version")):
        c.string(doc["client"]["name"], "client.name", r"[a-z0-9-]{1,32}")
        c.string(doc["client"]["version"], "client.version", TOKEN_RE)
    if c.obj(doc["chain"], "chain", ("source", "cartridge")):
        c.enum(doc["chain"]["source"], "chain.source", SOURCES)
        c.enum(doc["chain"]["cartridge"], "chain.cartridge", CARTRIDGES)
    if "trained_at" in doc:
        c.integer(doc["trained_at"], "trained_at", 1500000000, 4102444800)
    for k, v in c.kinds(doc["kinds"], "kinds"):
        p = f"kinds.{k}"
        if not c.obj(v, p, ("coef", "n_real", "n_false", "n_manual", "n_master", "master_recordings", "cv_err_est",
                            "cv_err_raw"), ("auc",)):
            continue
        c.numbers(v["coef"], f"{p}.coef", _ncoef(version), -COEF_BOUND, COEF_BOUND)
        c.integer(v["n_real"], f"{p}.n_real", 10, MAX_COUNT)
        c.integer(v["n_false"], f"{p}.n_false", 10, MAX_COUNT)
        c.integer(v["n_manual"], f"{p}.n_manual", 0, MAX_COUNT)
        c.integer(v["n_master"], f"{p}.n_master", 0, MAX_COUNT)
        c.integer(v["master_recordings"], f"{p}.master_recordings", 2, 100000)
        c.number(v["cv_err_est"], f"{p}.cv_err_est", 0, 10)
        c.number(v["cv_err_raw"], f"{p}.cv_err_raw", 0, 100)
        if "auc" in v:
            c.number(v["auc"], f"{p}.auc", 0, 1, null=True)
        if all(_int(v[n]) for n in ("n_real", "n_false", "n_manual", "n_master")) \
                and v["n_manual"] + v["n_master"] != v["n_real"] + v["n_false"]:
            c.bad(p, "n_manual + n_master must equal n_real + n_false")
    return c.errors


def validate_model(doc, version: str = learning.FEATURE_VERSION) -> list[str]:
    """global-model.schema.json; [] when valid."""
    c = _Check()
    req = ("spec", "version", "feature_version", "detector_version", "chain_class", "created_at", "server",
           "features", "scaling", "aggregation", "kinds")
    if not c.obj(doc, "model", req):
        return c.errors
    n = _ncoef(version) - 1
    c.const(doc["spec"], "spec", "vinyl-mind/global-model")
    c.integer(doc["version"], "version", 1, 2**53)
    c.const(doc["feature_version"], "feature_version", int(version))
    c.string(doc["detector_version"], "detector_version", TOKEN_RE)
    c.string(doc["chain_class"], "chain_class", r"all|(usb_turntable|turntable_interface)\.(mm|mc|unknown)")
    c.integer(doc["created_at"], "created_at", -2**53, 2**53)
    c.string(doc["server"], "server", FINGERPRINT_RE)
    f = doc["features"]
    if not isinstance(f, list) or len(f) != n or not all(isinstance(x, str) for x in f):
        c.bad("features", f"expected {n} names")
    if c.obj(doc["scaling"], "scaling", ("center", "scale")):
        c.numbers(doc["scaling"]["center"], "scaling.center", n)
        c.numbers(doc["scaling"]["scale"], "scaling.scale", n)
        sc = doc["scaling"]["scale"]
        if isinstance(sc, list) and any(_num(x) and x <= 0 for x in sc):
            c.bad("scaling.scale", "expected numbers above 0")
    a = doc["aggregation"]
    if c.obj(a, "aggregation", ("method", "trim", "manual_weight", "max_est_error", "max_station_share")):
        c.enum(a["method"], "aggregation.method", ("trimmed_mean", "median"))
        c.number(a["trim"], "aggregation.trim", 0, 0.45)
        c.number(a["manual_weight"], "aggregation.manual_weight", 0, math.inf)
        c.number(a["max_est_error"], "aggregation.max_est_error", 0, math.inf)
        c.number(a["max_station_share"], "aggregation.max_station_share", 0, 1)
        if _num(a["max_station_share"]) and a["max_station_share"] <= 0:
            c.bad("aggregation.max_station_share", "expected a number above 0")
    for k, v in c.kinds(doc["kinds"], "kinds"):
        p = f"kinds.{k}"
        if c.obj(v, p, ("coef", "spread", "profiles", "stations", "examples")):
            c.numbers(v["coef"], f"{p}.coef", n + 1)
            c.numbers(v["spread"], f"{p}.spread", n + 1)
            c.integer(v["profiles"], f"{p}.profiles", 1, 2**53)
            c.integer(v["stations"], f"{p}.stations", 1, 2**53)
            c.number(v["examples"], f"{p}.examples", 0, math.inf)
    return c.errors


def validate_check_report(doc, version: str = learning.FEATURE_VERSION) -> list[str]:
    """check-report.schema.json; [] when valid."""
    c = _Check()
    if not c.obj(doc, "report", ("spec", "feature_version", "kinds", "adopted")):
        return c.errors
    c.const(doc["spec"], "spec", "vinyl-mind/check-report")
    c.const(doc["feature_version"], "feature_version", int(version))
    if not isinstance(doc["adopted"], bool):
        c.bad("adopted", "expected a boolean")
    for k, v in c.kinds(doc["kinds"], "kinds"):
        p = f"kinds.{k}"
        if c.obj(v, p, ("local_err", "global_err", "master_recordings")):
            c.number(v["local_err"], f"{p}.local_err", 0, 100)
            c.number(v["global_err"], f"{p}.global_err", 0, 100)
            c.integer(v["master_recordings"], f"{p}.master_recordings", 2, 100000)
    return c.errors


def model_fits(doc: dict, version: str = learning.FEATURE_VERSION) -> None:
    """A model the station may use: its own feature version with exactly its features and shared scaling, and its own
    detector version. Another version is refused, never converted. Raises HubError."""
    if doc.get("feature_version") != int(version):
        raise HubError(i18n.msg("vinylmind.version_mismatch", got=str(doc.get("feature_version")), want=version))
    names = list(learning.feature_names(version))
    centre, scale = (list(map(float, a)) for a in learning._scaling(version))
    if doc.get("features") != names or not isinstance(doc.get("scaling"), dict) \
            or [float(x) for x in doc["scaling"].get("center", [])] != centre \
            or [float(x) for x in doc["scaling"].get("scale", [])] != scale:
        raise HubError(i18n.msg("vinylmind.features_mismatch", version=version))
    if doc.get("detector_version") != detect.VERSION:
        raise HubError(i18n.msg("vinylmind.detector_mismatch", got=str(doc.get("detector_version")),
                                want=detect.VERSION))


# ─── Turntable profiles: chain class ───────────────────────────────────────────────────────────────────────────────

def guess_chain(profile: dict) -> dict:
    """The chain class guessed from the profile: the capture device tells a USB turntable from an interface, the
    cartridge text MM from MC. Only the class is ever sent, never the names; "unknown" where the text says nothing."""
    dev = (profile.get("device") or "").lower()
    source = "usb_turntable" if any(h in dev for h in USB_TURNTABLE_HINTS) else "turntable_interface"
    cart = profile.get("cartridge") or ""
    if re.search(r"\bMC\b|moving[ -]?coil", cart, re.I):
        cartridge = "mc"
    elif re.search(r"\bM[MI]\b|moving[ -]?(magnet|iron)", cart, re.I):
        cartridge = "mm"
    else:
        cartridge = "unknown"
    return {"source": source, "cartridge": cartridge}


def chain_of(profile: dict) -> dict:
    """The chain class of a profile: its two fields where the user set them, else the guess."""
    guess = guess_chain(profile)
    src, cart = profile.get("chain_source"), profile.get("chain_cartridge")
    return {"source": src if src in SOURCES else guess["source"],
            "cartridge": cart if cart in CARTRIDGES else guess["cartridge"]}


def chain_class(chain: dict) -> str:
    return f"{chain['source']}.{chain['cartridge']}"


# ─── Contributions ─────────────────────────────────────────────────────────────────────────────────────────────────

def _contribution_data(db, profile_id: int, version: str) -> tuple[dict, bool]:
    """Training data of one profile; for version 2 only recordings with a complete zero point (learning.drop_fallback).
    Returns (data, had rows before dropping)."""
    data, _ = learning.training_data(db, profile_id, version=version)
    had = sum(len(d["y"]) for d in data.values())
    if version != "1":
        data = learning.drop_fallback(data)
    return data, bool(had)


def build_contribution(db, profile: dict, version: str = learning.FEATURE_VERSION,
                       trained_at: int | None = None) -> tuple[dict | None, dict | None]:
    """(contribution, None), or (None, reason) when no kind of the profile has a cross-checked model yet.

    The profile's own recordings only, trained without any prior: weights pulled towards the global model would feed
    the hub's own result back to it. A kind is included once it has a fitted model cross-checked on at least two
    recordings with a master (whether it is usable or not: the hub filters by the self-check), and only while its
    numbers are within the specification's bounds. For feature version 2 only recordings with a complete zero point
    count: a profile that was never measured and accepted has nothing to contribute."""
    data, had = _contribution_data(db, profile["id"], version)
    if version != "1" and had and not sum(len(d["y"]) for d in data.values()):
        return None, i18n.msg("vinylmind.profile_not_measured", version=version)
    model = learning.train(data, now=0.0, version=version)
    kinds, fitted = {}, False
    for kind in KINDS:
        info = model["kinds"][kind]
        if not info.get("model"):
            continue
        fitted = True
        if info.get("cv_err_est") is None or int(info.get("master_recordings") or 0) < 2:
            continue
        coef = learning.to_coef(info["model"], version)
        if any(abs(x) > COEF_BOUND for x in coef) or info["cv_err_est"] > 10 or info["cv_err_raw"] > 100:
            log.info("Vinyl-Mind: %s of profile %s not contributed, outside the specification's bounds", kind,
                     profile["id"])
            continue
        n_real, n_false, n_manual = int(info["n_real"]), int(info["n_false"]), int(info["manual"])
        k = {"coef": coef, "n_real": n_real, "n_false": n_false, "n_manual": n_manual,
             "n_master": n_real + n_false - n_manual, "master_recordings": int(info["master_recordings"]),
             "cv_err_est": float(info["cv_err_est"]), "cv_err_raw": float(info["cv_err_raw"])}
        if info.get("cv_auc") is not None:
            k["auc"] = float(info["cv_auc"])
        kinds[kind] = k
    if not kinds:
        return None, (i18n.msg("vinylmind.not_checked_yet", min=learning.MIN_MASTER_RECORDINGS) if fitted
                      else i18n.msg("vinylmind.nothing_yet", min=learning.MIN_PER_CLASS))
    doc = {"spec": "vinyl-mind/contribution", "feature_version": int(version), "detector_version": detect.VERSION,
           "client": {"name": CLIENT_NAME, "version": __version__}, "chain": chain_of(profile), "kinds": kinds}
    if trained_at is not None:
        doc["trained_at"] = int(trained_at)
    return doc, None


def contribution_sha256(doc: dict) -> str:
    """What decides whether a contribution changed: everything but the time it was trained."""
    return doc_sha256(doc, ignore=("trained_at",))


# ─── Checking a global model on the station's own master recordings ────────────────────────────────────────────────

def _cv_rows(info: dict) -> dict:
    """{recording: (true, estimate)} of a kind's cross-check (learning.train: recordings with at least 3 real ones)."""
    return {int(r["recording"]): (float(r["true"]), float(r["est"])) for r in (info.get("cv") or [])}


def _mean_err(rows: dict, recs) -> float:
    return round(float(np.mean([abs(rows[g][1] - rows[g][0]) / rows[g][0] for g in recs])), 4)


def check_model(data: dict, model_kinds: dict, version: str = learning.FEATURE_VERSION) -> dict:
    """Per kind of the model ({kind: {"intercept", "weights"}} in the shared scaling): the estimate error per master
    recording of the local model (cross-check without prior), of the global model applied as it is, and of the local
    model trained with the global one as its prior, all three on the same recordings: those the cross-check covers.
    {kind: {"master_recordings", "local_err", "global_err", "prior_err"}}; errors are None without such recordings."""
    prior = {k: v for k, v in model_kinds.items() if k in KINDS}
    local = learning.train(data, now=0.0, version=version)
    with_prior = learning.train(data, now=0.0, prior=prior, version=version)
    out = {}
    for kind, fixed in prior.items():
        d = data.get(kind)
        loc, pri = _cv_rows(local["kinds"][kind]), _cv_rows(with_prior["kinds"][kind])
        glob = {}
        if d is not None and len(d["y"]):
            rec, master, y = np.asarray(d["rec"]), np.asarray(d["master"], bool), np.asarray(d["y"], float)
            for g in loc:
                te = (rec == g) & master
                if te.any():
                    glob[g] = (float(y[te].sum()), float(learning.predict_fixed(fixed, d["X"][te], version).sum()))
        recs = sorted(set(loc) & set(glob) & set(pri))
        if not recs:
            out[kind] = {"master_recordings": 0, "local_err": None, "global_err": None, "prior_err": None}
            continue
        out[kind] = {"master_recordings": len(recs), "local_err": _mean_err(loc, recs),
                     "global_err": _mean_err(glob, recs), "prior_err": _mean_err(pri, recs)}
    return out


def adopt(check: dict) -> bool:
    """Use the global model as the prior of a kind? If the cross-check with it as the prior is not worse than without,
    or if there is no own cross-check yet: the model is then only a starting point, and the estimate is still used only
    once the station's own cross-check (with the prior) makes the kind usable."""
    if check.get("local_err") is None:
        return True
    return check.get("prior_err") is not None and check["prior_err"] <= check["local_err"]


# ─── HTTP ──────────────────────────────────────────────────────────────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A signed request is bound to its path, and a redirect could leave HTTPS: never follow one."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener():
    return urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))


def _read(resp) -> bytes:
    data = resp.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise HubError(i18n.msg("vinylmind.too_large", kb=MAX_RESPONSE_BYTES // 1024))
    return data


def _send(opener, url: str, method: str, body: bytes | None, headers: dict, timeout: float):
    """One HTTP exchange: (status, headers with lower-case names, body). A body is always bytes with Content-Length
    (the hub refuses chunked bodies). Raises OSError for network failures, HubError for a redirect."""
    headers = dict(headers)
    if body is not None:
        headers["Content-Length"] = str(len(body))
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, _read(resp)
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400 and e.code != 304:        # 304 Not Modified is the ETag answer, not a redirect
            raise HubError(i18n.msg("vinylmind.redirect"), status=e.code) from None
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, _read(e)


def _retry_after(headers: dict) -> float | None:
    try:
        return max(0.0, float(headers.get("retry-after")))
    except (TypeError, ValueError):
        return None


def _hub_error(status: int, code: str | None, message: str | None, retry_after: float | None) -> HubError:
    """The hub's error as a message for people: known codes get their own text, others the hub's message."""
    if code in HUB_ERRORS:
        return HubError(i18n.msg(f"vinylmind.err_{code}"), code=code, status=status, retry_after=retry_after)
    if code == "invalid":
        return HubError(i18n.msg("vinylmind.err_invalid", message=str(message or "–")[:300]), code=code,
                        status=status)
    return HubError(i18n.msg("vinylmind.http_error", status=status, message=str(message or code or "–")[:300]),
                    code=code, status=status, retry_after=retry_after)


class Client:
    """Requests to one Vinyl-Mind hub as one station (`key`, signed against the accepted `server_fp`). `abort()` is
    asked before every attempt (a recording started: nothing is sent); `sleep` and `backoff` can be replaced in
    tests."""

    def __init__(self, base_url: str, key: StationKey | None = None, server_fp: str | None = None, *,
                 timeout: float = TIMEOUT_S, retries: int = RETRIES, backoff=BACKOFF_S, sleep=time.sleep,
                 abort=lambda: False):
        self.base = normalize_url(base_url)
        self.key, self.server_fp = key, server_fp
        self.timeout, self.retries, self.backoff, self.sleep, self.abort = (
            timeout, retries, tuple(backoff), sleep, abort)
        self.opener = _opener()

    def _target(self, path: str) -> tuple[str, str]:
        """(full URL, the path and query as sent and signed) for an API path such as "/v1/models?x=1"."""
        u = urllib.parse.urlsplit(self.base)
        target = u.path.rstrip("/") + path
        return urllib.parse.urlunsplit((u.scheme, u.netloc, "", "", "")) + target, target

    def request(self, method: str, path: str, payload=None, *, signed: bool = True, ok=(200,),
                headers: dict | None = None) -> tuple[int, dict, object]:
        """Send a request (signed anew on every attempt: a fresh timestamp and nonce) and return (status, headers,
        JSON or None). Connection failures, timeouts and 502/503/504 are retried with backoff, 429 after its
        Retry-After when that is short; other answers outside `ok` raise HubError with the hub's error code."""
        body = compact_json(payload) if payload is not None else None
        if body is not None and len(body) > MAX_REQUEST_BYTES:
            raise HubError(i18n.msg("vinylmind.request_too_large", kb=MAX_REQUEST_BYTES // 1024))
        url, target = self._target(path)
        last: HubError | None = None
        for attempt in range(max(1, self.retries)):
            if self.abort():
                raise HubError(i18n.msg("vinylmind.recording"), code="recording")
            h = {"Accept": "application/json", "User-Agent": CLIENT, "Vinyl-Mind-Client": CLIENT} | (headers or {})
            if body is not None:
                h["Content-Type"] = "application/json"
            if signed:
                h |= sign_headers(self.key, self.server_fp, method, target, body or b"")
            try:
                status, hdrs, raw = _send(self.opener, url, method, body, h, self.timeout)
            except OSError as e:                         # URLError, timeouts, refused connections, TLS failures
                last = HubError(i18n.msg("vinylmind.unreachable", url=self.base, error=str(getattr(e, "reason", e))),
                                code="unreachable")
                self._wait(attempt, None)
                continue
            data = None
            if raw:
                try:
                    data = strict_json(raw)
                except (ValueError, UnicodeDecodeError):
                    if status in ok:
                        raise HubError(i18n.msg("vinylmind.server_bad", error="no JSON")) from None
            if status in ok:
                return status, hdrs, data
            err = data if isinstance(data, dict) else {}
            code = err.get("error") if isinstance(err.get("error"), str) else None
            retry_after = _retry_after(hdrs)
            last = _hub_error(status, code, err.get("message"), retry_after)
            if status in RETRY_STATUS or (status == 429 and retry_after is not None
                                          and retry_after <= MAX_RETRY_AFTER_S):
                self._wait(attempt, retry_after)
                continue
            raise last
        raise last

    def _wait(self, attempt: int, retry_after: float | None) -> None:
        if attempt + 1 >= max(1, self.retries):
            return
        delay = retry_after if retry_after is not None else \
            self.backoff[min(attempt, len(self.backoff) - 1)] if self.backoff else 0.0
        if delay > 0:
            self.sleep(delay)

    # ─── Endpoints (docs/api.md) ───────────────────────────────────────────────────────────────────────────────────
    def server_info(self) -> dict:
        """GET /v1/server (unsigned), checked: the public key must match the fingerprint. Adds `skew_s`, the hub's
        clock minus this computer's."""
        _, _, info = self.request("GET", f"{API}/server", signed=False)
        if not isinstance(info, dict):
            raise HubError(i18n.msg("vinylmind.server_bad", error="no object"))
        try:
            raw = unb64u(info.get("public_key"))
            Ed25519PublicKey.from_public_bytes(raw)
        except (TypeError, ValueError):
            raise HubError(i18n.msg("vinylmind.server_bad", error="public_key")) from None
        if len(raw) != 32 or info.get("fingerprint") != fingerprint(raw):
            raise HubError(i18n.msg("vinylmind.server_bad", error="fingerprint"))
        out = {"public_key": info["public_key"], "fingerprint": info["fingerprint"],
               "name": str(info.get("name") or "")[:100], "version": str(info.get("version") or "")[:40],
               "join": info.get("join") if info.get("join") in ("approval", "invitation") else None,
               "feature_versions": [v for v in info.get("feature_versions") or [] if _int(v)][:20],
               "clients": [str(v)[:80] for v in info.get("clients") or []][:50],
               "detector_versions": [str(v)[:40] for v in info.get("detector_versions") or []][:50],
               "skew_s": round(info["time"] - time.time()) if _num(info.get("time")) else None,
               "connect_url": None}
        if info.get(Account.SERVER_FIELD):
            out["connect_url"] = _web_url(info[Account.SERVER_FIELD], Account.SERVER_FIELD)
        return out

    def join(self, invitation: str | None = None, binding: str | None = None) -> dict:
        """POST /v1/join, signed with the new key itself. The invitation and the binding are sent only when there is
        one, and never logged."""
        body = {"public_key": self.key.public_b64u}
        if invitation:
            body["invitation"] = invitation
        if binding:
            body["binding"] = binding
        _, _, r = self.request("POST", f"{API}/join", body, ok=(200, 201))
        if not isinstance(r, dict) or r.get("status") not in ("active", "pending") \
                or r.get("station") != self.key.fingerprint or r.get("server") != self.server_fp:
            raise HubError(i18n.msg("vinylmind.server_bad", error="join"))
        return r

    def me(self) -> dict:
        _, _, r = self.request("GET", f"{API}/me")
        if not isinstance(r, dict) or r.get("status") not in ("active", "pending", "revoked"):
            raise HubError(i18n.msg("vinylmind.server_bad", error="status"))
        return r

    def leave(self) -> None:
        self.request("DELETE", f"{API}/me", ok=(200, 204))

    def put_contribution(self, profile_id: str, doc: dict) -> dict:
        _, _, r = self.request("PUT", f"{API}/profiles/{profile_id}", doc)
        return r if isinstance(r, dict) else {}

    def delete_contribution(self, profile_id: str) -> None:
        self.request("DELETE", f"{API}/profiles/{profile_id}", ok=(200, 204, 404))

    def models(self, feature_version: int, detector_version: str, chain_cls: str,
               etag: str | None) -> tuple[int, dict | None, str | None]:
        """(200, {"published", "candidate"}, etag) or (304, None, etag)."""
        q = urllib.parse.urlencode({"feature_version": feature_version, "detector_version": detector_version,
                                    "chain_class": chain_cls})
        status, hdrs, data = self.request("GET", f"{API}/models?{q}", ok=(200, 304),
                                          headers={"If-None-Match": etag} if etag else None)
        if status == 304:
            return 304, None, etag
        if not isinstance(data, dict):
            raise HubError(i18n.msg("vinylmind.server_bad", error="models"))
        return 200, data, hdrs.get("etag")

    def model(self, version: int) -> dict | None:
        try:
            _, _, r = self.request("GET", f"{API}/models/{int(version)}")
        except HubError as e:
            if e.code == "not_found":
                return None
            raise
        return r if isinstance(r, dict) else None

    def put_check(self, profile_id: str, version: int, report: dict) -> dict:
        _, _, r = self.request("PUT", f"{API}/profiles/{profile_id}/checks/{int(version)}", report)
        return r if isinstance(r, dict) else {}


class AccountClient:
    """The account service of a hub (docs/connect.md): the connect page, device authorization and the token endpoint.
    Nothing that passes through here is logged: the code, the verifier, the device code, the invitation and the
    binding are secrets."""

    def __init__(self, connect_url: str, *, timeout: float = TIMEOUT_S):
        self.connect_url = _web_url(connect_url, Account.SERVER_FIELD)
        self.base = origin(self.connect_url)
        self.timeout = timeout
        self.opener = _opener()

    @property
    def host(self) -> str:
        return urllib.parse.urlsplit(self.base).hostname or ""

    def authorize_url(self, *, station: str, state: str, challenge: str, redirect_uri: str, lang: str) -> str:
        q = {"response_type": "code", "client_id": Account.CLIENT_ID, "client": CLIENT, "redirect_uri": redirect_uri,
             "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "station": station}
        if lang in Account.LANGS:
            q["lang"] = lang                             # accepted by the account service, not part of the spec
        sep = "&" if urllib.parse.urlsplit(self.connect_url).query else "?"
        return self.connect_url + sep + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)

    def _post(self, path: str, body: dict) -> dict:
        try:
            status, hdrs, raw = _send(self.opener, self.base + path, "POST", compact_json(body),
                                      {"Accept": "application/json", "Content-Type": "application/json",
                                       "User-Agent": CLIENT, "Vinyl-Mind-Client": CLIENT}, self.timeout)
        except OSError as e:
            raise HubError(i18n.msg("vinylmind.unreachable", url=self.base, error=str(getattr(e, "reason", e))),
                           code="unreachable") from None
        try:
            data = strict_json(raw) if raw else None
        except (ValueError, UnicodeDecodeError):
            data = None
        if status == 200 and isinstance(data, dict):
            return data
        code = data.get("error") if isinstance(data, dict) and isinstance(data.get("error"), str) else None
        retry = _retry_after(hdrs)
        if code in ACCOUNT_ERRORS:
            raise HubError(i18n.msg(f"vinylmind.acc_{code}"), code=code, status=status, retry_after=retry)
        raise HubError(i18n.msg("vinylmind.acc_failed", status=status), code=code or "account_error", status=status,
                       retry_after=retry)

    def device(self, station: str) -> dict:
        """POST /api/account/device: the codes of flow B, checked."""
        d = self._post(Account.DEVICE_PATH, {"station": station, "client": CLIENT})
        try:
            out = {"device_code": d["device_code"], "user_code": d["user_code"],
                   "verification_uri": _web_url(d["verification_uri"], "verification_uri"),
                   "verification_uri_complete": _web_url(d.get("verification_uri_complete")
                                                         or d["verification_uri"], "verification_uri_complete"),
                   "interval": int(d.get("interval") or Account.DEVICE_INTERVAL_S),
                   "expires_in": int(d.get("expires_in") or Account.DEVICE_TTL_S)}
        except (KeyError, TypeError, ValueError):
            raise HubError(i18n.msg("vinylmind.server_bad", error="device")) from None
        if not (isinstance(out["device_code"], str) and 0 < len(out["device_code"]) <= 512
                and isinstance(out["user_code"], str) and re.fullmatch(r"[0-9A-Za-z-]{4,16}", out["user_code"])
                and 1 <= out["interval"] <= 60 and 1 <= out["expires_in"] <= 3600):
            raise HubError(i18n.msg("vinylmind.server_bad", error="device"))
        return out

    def token(self, body: dict) -> dict:
        """POST /api/account/connect/token: {invitation, binding, server_url, server_fingerprint}, checked."""
        d = self._post(Account.TOKEN_PATH, body)
        inv, binding = d.get("invitation"), d.get("binding")
        if not (isinstance(inv, str) and re.fullmatch(INVITATION_RE, inv)) \
                or not (binding is None or (isinstance(binding, str) and len(binding) <= 8192
                                            and re.fullmatch(JWS_RE, binding))) \
                or not isinstance(d.get("server_url"), str) \
                or not (isinstance(d.get("server_fingerprint"), str)
                        and re.fullmatch(FINGERPRINT_RE, d["server_fingerprint"])):
            raise HubError(i18n.msg("vinylmind.server_bad", error="token"))
        return {"invitation": inv, "binding": binding, "server_url": d["server_url"],
                "server_fingerprint": d["server_fingerprint"]}


# ─── Signing in: one connect flow at a time ────────────────────────────────────────────────────────────────────────

class _Flow:
    """One open connect flow. Its secrets (verifier, device code, invitation, binding) stay in this object, in memory
    only; `public()` is what the web interface may see."""

    def __init__(self, kind: str, hub: dict, account: AccountClient, key: StationKey, reconnect: bool):
        self.kind, self.hub, self.account, self.key, self.reconnect = kind, hub, account, key, reconnect
        self.started = time.time()
        self.expires = self.started + Account.FLOW_TTL_S
        self.status = "waiting"            # waiting · joining · retry · done · failed
        self.error = None                  # i18n message
        self.cancelled = threading.Event()
        # flow A
        self.state = self.verifier = self.redirect_uri = None
        self.state_used = False
        # flow B
        self.device_code = None
        self.user_code = self.verification_uri = self.verification_uri_complete = self.qr = None
        self.interval = Account.DEVICE_INTERVAL_S
        # a join refused for a while: tried again with the same invitation
        self.retry_at: float | None = None
        self.grant: dict | None = None

    def __repr__(self) -> str:
        return f"_Flow({self.kind}, {self.status})"

    def wipe(self) -> None:
        """Forget every secret of the flow."""
        self.verifier = self.device_code = self.grant = None

    def public(self) -> dict:
        out = {"kind": self.kind, "status": self.status, "error": self.error, "reconnect": self.reconnect,
               "started_at": self.started, "expires_at": self.expires, "site": self.account.host,
               "retry_at": self.retry_at}
        if self.kind == "device":
            out |= {"user_code": self.user_code, "verification_uri": self.verification_uri,
                    "verification_uri_complete": self.verification_uri_complete, "qr": self.qr}
        return out


def _pkce() -> tuple[str, str]:
    """(code_verifier, code_challenge): 32 random bytes in base64url, and base64url(SHA-256(verifier)) (RFC 7636)."""
    verifier = b64u(secrets.token_bytes(32))
    return verifier, b64u(hashlib.sha256(verifier.encode("ascii")).digest())


# ─── Vinyl-Mind on this station ────────────────────────────────────────────────────────────────────────────────────

class VinylMind:
    """Switches, server, joined state, contributions, global models and the prior of one station.

    State lives in the database settings (S_*), the station's key in its file, a connect flow in memory. `busy()` says
    whether a recording runs (then nothing is sent); `retrain()` retrains the station's learning model after the prior
    changed. One lock keeps a background round and the user's actions (join, leave, import) from overlapping."""

    def __init__(self, cfg, db, *, busy=lambda: False, retrain=lambda: None, desktop: bool = False):
        self.cfg, self.db, self.busy, self.retrain, self.desktop = cfg, db, busy, retrain, desktop
        # the station's feature version: what its contributions carry and the only version of model or prior it uses
        self.feature_version = str(getattr(cfg, "feature_version", learning.FEATURE_VERSION))
        self.state_dir = Path(cfg.state_dir)
        self.lock = threading.RLock()
        self.dirty = True                  # contributions need a look: after start and after every retraining
        self.failures, self.next_attempt = 0, 0.0
        self.last_attempt: float | None = None
        self.last_error = None             # i18n message of the last failed round
        self.last_error_code: str | None = None
        self.last_refresh = 0.0
        self.skew_s: int | None = None     # the hub's clock minus this computer's, from the last GET /v1/server
        self.client_options: dict = {}     # tests: backoff, sleep, timeout
        self.flow: _Flow | None = None
        self.open_browser = webbrowser.open
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._migrate()

    def _migrate(self) -> None:
        """State of the draft protocol (before 1.1.0): its chain classes move into the profiles' two fields, its joined
        state and its single key are dropped (that hub never existed, and version 1 keys are per hub)."""
        chains = self._get(S_CHAIN_LEGACY)
        if isinstance(chains, dict):
            source = {"usb_turntable": "usb_turntable", "interface": "turntable_interface"}
            for p in self.db.profiles():
                vid = p.get("vm_id") or ""
                try:                                     # the draft keyed them by the hex form of the profile ID
                    old_id = unb64u(vid).hex() if vid else ""
                except ValueError:
                    old_id = ""
                cc = chains.get(vid) or chains.get(old_id) or {}
                fields = {"chain_source": source.get(cc.get("connection")),
                          "chain_cartridge": cc.get("cartridge") if cc.get("cartridge") in CARTRIDGES else None}
                if any(fields.values()) and not (p.get("chain_source") or p.get("chain_cartridge")):
                    self.db.update_profile(p["id"], **fields)
            self.db.set_setting(S_CHAIN_LEGACY, "null")
        j = self._get(S_JOIN)
        if isinstance(j, dict) and not str(j.get("fingerprint") or "").startswith("SHA256:"):
            for s in (S_JOIN, S_SENT, S_MODEL):
                self._put(s, None)
            p = self._get(S_PRIOR)
            if isinstance(p, dict) and p.get("source") == "server":
                self._put(S_PRIOR, None)
        StationKey.delete(self.state_dir / LEGACY_KEY_FILE)
        for stale in (self.state_dir / KEY_DIR).glob("*.new.key") if (self.state_dir / KEY_DIR).is_dir() else ():
            StationKey.delete(stale)                    # a join that never finished (the engine stopped meanwhile)

    # ─── Stored state ──────────────────────────────────────────────────────────────────────────────────────────────
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
        return str(getattr(self.cfg, "vinyl_mind_url", DEFAULT_VINYL_MIND_URL) or "").strip().rstrip("/")

    def url(self) -> str:
        return self.db.setting(S_URL) or self.preset_url()

    def joined(self) -> dict | None:
        j = self._get(S_JOIN)
        return j if isinstance(j, dict) and j.get("url") else None

    def _set_join(self, **fields) -> dict:
        j = (self.joined() or {}) | fields
        self._put(S_JOIN, j)
        return j

    def _client(self, url: str, key: StationKey | None = None, server_fp: str | None = None) -> Client:
        return Client(url, key, server_fp, abort=self.busy, **self.client_options)

    def _key(self, j: dict) -> StationKey | None:
        try:
            key = StationKey.load(key_file(self.state_dir, j["fingerprint"]))
        except (OSError, ValueError):
            return None
        return key if key and key.fingerprint == j.get("station") else None

    def _retrain(self) -> None:
        try:
            self.retrain()
        except Exception:  # noqa: BLE001 — a failed retraining is reported by the learning loop itself
            log.exception("Retraining after a change of the Vinyl-Mind prior failed")

    def _drop_server_prior(self) -> None:
        prior = self._get(S_PRIOR)
        if isinstance(prior, dict) and prior.get("source") == "server":
            self._put(S_PRIOR, None)
            self._retrain()

    def _usable_join(self) -> dict | None:
        """The joined state if it belongs to the server set here and can still sign requests."""
        j = self.joined()
        if not j or j["url"] != self.url() or j.get("status") in ("revoked", "key_changed", "key_lost"):
            return None
        return j

    # ─── Status and settings ───────────────────────────────────────────────────────────────────────────────────────
    def _feature_supported(self, j: dict | None) -> bool | None:
        fvs = ((j or {}).get("server") or {}).get("feature_versions")
        return None if fvs is None else int(self.feature_version) in fvs

    def _detector_supported(self, j: dict | None) -> bool | None:
        s = (j or {}).get("server")
        if s is None:
            return None
        return not s.get("detector_versions") or detect.VERSION in s["detector_versions"]

    def status(self) -> dict:
        """Everything the settings section shows. No secret: neither the private key nor anything of a connect flow
        but its progress appears here."""
        j = self.joined()
        sent = self._get(S_SENT, {}) or {}
        last_at = max((v.get("at") or 0 for v in sent.values()), default=0) or None
        m = self._get(S_MODEL, {}) or {}
        prior = self._get(S_PRIOR)
        url = self.url()
        key = self._key(j) if j else None
        in_use = prior.get("ref") if isinstance(prior, dict) and prior.get("source") == "server" else None
        current = (m.get("checked") or {}).get(str(in_use)) if in_use is not None else None
        latest = max((int(v) for v in (m.get("checked") or {})), default=None)
        shown = current or ((m.get("checked") or {}).get(str(latest)) if latest is not None else None)
        flow = self.flow
        if flow and flow.status == "waiting" and time.time() > flow.expires:
            with self.lock:
                self._fail_flow(flow, i18n.msg("vinylmind.connect_expired"))
        return {
            "url": url, "preset_url": self.preset_url(), "url_is_preset": url == self.preset_url(),
            "contribute": self.contribute(), "use_model": self.use_model(),
            "state": j.get("status", "pending") if j else "not_joined",
            "joined": {"url": j["url"], "name": j.get("name"), "fingerprint": j.get("fingerprint"),
                       "joined_at": j.get("joined_at"), "via": j.get("via"), "site": j.get("site"),
                       "matches_url": j["url"] == url} if j else None,
            "station_fingerprint": key.fingerprint if key else None,
            "client": CLIENT, "client_rejected": bool(j and j.get("client_rejected") == __version__),
            "feature_version": self.feature_version, "feature_supported": self._feature_supported(j),
            "detector_version": detect.VERSION, "detector_supported": self._detector_supported(j),
            "unreachable": self.last_error_code == "unreachable", "skew_s": self.skew_s,
            "skew_warning": self.skew_s is not None and abs(self.skew_s) > SKEW_WARN_S,
            "last_contribution": {"at": last_at, "profiles": len(sent)} if last_at else None,
            "model": ({"version": shown.get("version"), "status": shown.get("status"),
                       "chain_class": shown.get("chain_class"), "checked_at": shown.get("at"),
                       "adopted": shown.get("adopted") or [], "check": shown.get("check"),
                       "in_use": in_use is not None and shown.get("version") == in_use and self.use_model()}
                      if shown else None),
            "withdrawn": m.get("withdrawn"),
            "polled_at": m.get("polled_at"),
            "prior": {"source": prior.get("source"), "type": prior.get("type"), "ref": prior.get("ref"),
                      "kinds": sorted(prior.get("kinds") or {}), "accepted_at": prior.get("accepted_at"),
                      "in_use": self.prior_for_training() is not None} if isinstance(prior, dict) else None,
            "last_error": self.last_error, "last_attempt": self.last_attempt,
            "next_attempt": self.next_attempt or None, "recording": bool(self.busy()),
            "connect": flow.public() if flow else None,
            "invite_url": str(getattr(self.cfg, "vinyl_mind_invite_url", "") or "") or None,
            "desktop": self.desktop,       # the engine opens the sign-in page in the system browser itself
        }

    def update(self, body: dict) -> dict:
        """Switches and server URL ("reset_url" restores the preset)."""
        with self.lock:
            if "url" in body or body.get("reset_url"):
                raw = "" if body.get("reset_url") else str(body.get("url") or "").strip()
                new = normalize_url(raw) if raw else ""
                j = self.joined()
                if j and j.get("status") not in ("revoked", "key_lost") and (new or self.preset_url()) != j["url"]:
                    raise HubError(i18n.msg("vinylmind.leave_first", url=j["url"]), code="leave_first")
                self.db.set_setting(S_URL, "" if new == self.preset_url() else new)
            if "contribute" in body:
                self.db.set_setting(S_CONTRIBUTE, "true" if body["contribute"] else "false")
                if body["contribute"]:
                    self.dirty, self.next_attempt = True, 0.0
            if "use_model" in body:
                self.db.set_setting(S_USE, "true" if body["use_model"] else "false")
                if not body["use_model"]:
                    # the fetched models go with the switch; switched on again, the station fetches and checks anew
                    self._put(S_MODEL, None)
                    self._drop_server_prior()
                else:
                    self.next_attempt = 0.0
        return self.status()

    def server_info(self) -> dict:
        """What the configured server says about itself (unsigned), to confirm its key before joining."""
        url = self.url()
        if not url:
            raise HubError(i18n.msg("vinylmind.no_server"), code="no_server")
        info = self._client(url).server_info()
        self.skew_s = info["skew_s"]
        j = self.joined()
        connect = None
        if info["connect_url"]:
            connect = {"site": urllib.parse.urlsplit(info["connect_url"]).hostname}
        return {"url": url, "name": info["name"], "version": info["version"], "fingerprint": info["fingerprint"],
                "join": info["join"], "connect": connect, "client": CLIENT,
                "client_accepted": accepts_client(info["clients"]),
                "feature_versions": info["feature_versions"],
                "feature_supported": int(self.feature_version) in info["feature_versions"],
                "detector_supported": not info["detector_versions"] or detect.VERSION in info["detector_versions"],
                "skew_s": info["skew_s"],
                "skew_warning": info["skew_s"] is not None and abs(info["skew_s"]) > SKEW_WARN_S,
                "same_key": (j["fingerprint"] == info["fingerprint"]) if j and j["url"] == url else None}

    @staticmethod
    def _server_facts(info: dict) -> dict:
        return {k: info[k] for k in ("feature_versions", "detector_versions", "clients", "join")} | {
            "checked_at": time.time(), "connect": bool(info.get("connect_url"))}

    # ─── Joining with an invitation code or the operator's approval ────────────────────────────────────────────────
    def _check_can_join(self, url: str, reconnect: bool = False) -> dict | None:
        """Raise HubError if the station cannot start joining `url` now; returns the joined state if any."""
        if not url:
            raise HubError(i18n.msg("vinylmind.no_server"), code="no_server")
        if self.busy():
            raise HubError(i18n.msg("vinylmind.recording"), code="recording")
        j = self.joined()
        if j and j.get("status") not in ("revoked", "key_lost"):
            if j["url"] != url:
                raise HubError(i18n.msg("vinylmind.leave_first", url=j["url"]), code="leave_first")
            if not reconnect and j.get("status") != "key_changed":
                raise HubError(i18n.msg("vinylmind.already_joined"), code="already_joined")
        return j

    def join(self, invitation: str | None = None, fingerprint_: str | None = None) -> dict:
        """Join the configured server with an invitation code, or without one (then the operator approves). The user
        must have confirmed the server key's fingerprint (`fingerprint_`); the join stops if the server presents
        another key. The accepted key verifies every model from then on. The code is never stored."""
        with self.lock:
            url = self.url()
            self._check_can_join(url)
            client = self._client(url)
            info = client.server_info()
            self.skew_s = info["skew_s"]
            want = str(fingerprint_ or "").strip()
            if not want:
                raise HubError(i18n.msg("vinylmind.confirm_fingerprint"), code="confirm_fingerprint")
            if want != info["fingerprint"]:
                raise HubError(i18n.msg("vinylmind.key_changed", got=info["fingerprint"], want=want),
                               code="key_changed")
            invitation = (invitation or "").strip() or None
            if invitation and not re.fullmatch(INVITATION_RE, invitation):
                raise HubError(i18n.msg("vinylmind.err_invitation_invalid"), code="invitation_invalid")
            key = StationKey.generate()
            pending = key_file(self.state_dir, info["fingerprint"], pending=True)
            key.save(pending)
            try:
                r = self._client(url, key, info["fingerprint"]).join(invitation)
            except HubError:
                StationKey.delete(pending)
                raise
            self._joined(url, info, key, r["status"], via="invitation" if invitation else "approval")
            return self.status()

    def _joined(self, url: str, info: dict, key: StationKey, status: str, via: str, site: str | None = None) -> None:
        """Make a successful join the station's state: the pending key becomes the hub's key (the previous key of a
        reconnect or of a revoked station is replaced), the accepted server key is stored."""
        old = self.joined()
        if old and old.get("fingerprint") != info["fingerprint"]:
            StationKey.delete(key_file(self.state_dir, old["fingerprint"]))     # a key for a key the hub dropped
        os.replace(key_file(self.state_dir, info["fingerprint"], pending=True), key_file(self.state_dir,
                                                                                         info["fingerprint"]))
        self._put(S_JOIN, {"url": url, "server_key": info["public_key"], "fingerprint": info["fingerprint"],
                           "name": info.get("name") or "", "station": key.fingerprint, "status": status,
                           "joined_at": time.time(), "checked_at": time.time(), "via": via, "site": site,
                           "server": self._server_facts(info), "client_rejected": None})
        self._put(S_SENT, None)                         # a new station sends everything anew
        m = self._get(S_MODEL)
        if isinstance(m, dict):                         # check reports belong to the station, models to the hub
            for v in (m.get("checked") or {}).values():
                v["reported"] = []
            self._put(S_MODEL, m)
        if old and old.get("fingerprint") != info["fingerprint"]:
            self._put(S_MODEL, None)
            self._drop_server_prior()
        self.dirty, self.failures, self.next_attempt, self.last_error, self.last_error_code = True, 0, 0.0, None, None
        log.info("Vinyl-Mind: joined %s as %s (%s, %s)", url, key.fingerprint, status, via)

    # ─── Joining by signing in with an account (docs/connect.md) ───────────────────────────────────────────────────
    def connect_start(self, kind: str, *, redirect_host: str = "127.0.0.1", port: int | None = None, lang: str = "en",
                      reconnect: bool = False, open_browser: bool = False) -> dict:
        """Start flow A ("browser": returns the URL of the account service's connect page, opened in the system
        browser when `open_browser`) or flow B ("device": a code to confirm on another device; the station polls in
        the background). A new key pair is made for the join; with `reconnect`, the station stays joined with its old
        key until the new one has joined."""
        with self.lock:
            j = self.joined()
            url = j["url"] if reconnect and j else self.url()
            if reconnect and not j:
                raise HubError(i18n.msg("vinylmind.not_joined"), code="not_joined")
            self._check_can_join(url, reconnect=reconnect)
            info = self._client(url).server_info()
            self.skew_s = info["skew_s"]
            if reconnect and j.get("status") != "key_changed" and info["fingerprint"] != j["fingerprint"]:
                raise HubError(i18n.msg("vinylmind.key_changed", got=info["fingerprint"], want=j["fingerprint"]),
                               code="key_changed")
            if not info["connect_url"]:
                raise HubError(i18n.msg("vinylmind.no_accounts"), code="no_accounts")
            if not accepts_client(info["clients"]):
                raise HubError(i18n.msg("vinylmind.err_client_not_accepted"), code="client_not_accepted")
            account = AccountClient(info["connect_url"], timeout=self.client_options.get("timeout", TIMEOUT_S))
            self.connect_cancel()
            key = StationKey.generate()
            key.save(key_file(self.state_dir, info["fingerprint"], pending=True))
            hub = {"url": url, "public_key": info["public_key"], "fingerprint": info["fingerprint"],
                   "name": info["name"], "info": info}
            flow = _Flow(kind, hub, account, key, reconnect)
            if kind == "browser":
                flow.state = b64u(secrets.token_bytes(16))
                flow.verifier, challenge = _pkce()
                host = "[::1]" if redirect_host.strip("[]") == "::1" else "127.0.0.1"
                flow.redirect_uri = f"http://{host}:{int(port or self.cfg.port)}{Account.CALLBACK_PATH}"
                target = account.authorize_url(station=key.fingerprint, state=flow.state, challenge=challenge,
                                               redirect_uri=flow.redirect_uri, lang=lang)
                self.flow = flow
                opened = False
                if open_browser:
                    try:
                        opened = bool(self.open_browser(target))
                    except Exception:  # noqa: BLE001 — no browser: the interface opens the link instead
                        opened = False
                log.info("Vinyl-Mind: sign-in started in the browser for %s", url)
                return {"url": target, "opened": opened, "status": self.status()}
            if kind != "device":
                raise HubError(i18n.msg("vinylmind.bad_field", field="flow"))
            try:
                d = account.device(key.fingerprint)
            except HubError:
                StationKey.delete(key_file(self.state_dir, info["fingerprint"], pending=True))
                raise
            flow.device_code, flow.user_code = d["device_code"], d["user_code"]
            flow.verification_uri, flow.verification_uri_complete = d["verification_uri"], d["verification_uri_complete"]
            flow.qr = network.qr_svg(flow.verification_uri_complete)
            flow.interval, flow.expires = d["interval"], time.time() + d["expires_in"]
            self.flow = flow
            threading.Thread(target=self._poll_device, args=(flow,), daemon=True, name="vinylcheck-vm-device").start()
            log.info("Vinyl-Mind: sign-in started with a device code for %s", url)
            return {"status": self.status()}

    def connect_cancel(self) -> dict:
        f = self.flow
        if f:
            f.cancelled.set()
            f.wipe()
            if f.status not in ("done",):
                StationKey.delete(key_file(self.state_dir, f.hub["fingerprint"], pending=True))
            self.flow = None
        return self.status()

    def _fail_flow(self, f: _Flow, message) -> None:
        """End a flow without joining; it stays visible with its error until the next start or a cancel. A flow that
        was replaced meanwhile changes nothing (its pending key file now belongs to the new flow)."""
        if self.flow is not f:
            f.cancelled.set()
            f.wipe()
            return
        f.status, f.error = "failed", message
        f.cancelled.set()
        f.wipe()
        StationKey.delete(key_file(self.state_dir, f.hub["fingerprint"], pending=True))
        log.info("Vinyl-Mind: sign-in ended without joining: %s", i18n.render(message, "en"))

    def connect_callback(self, code: str | None, state: str | None, error: str | None) -> tuple[bool, dict]:
        """GET /vinyl-mind/callback: the account service sends the browser back with `code` and `state` (or
        `error=access_denied`). Accepted only for the open browser flow, with its state, once and within its
        lifetime; a callback that does not match changes nothing. Returns (joined, message for the page)."""
        with self.lock:
            f = self.flow
            if not f or f.kind != "browser" or f.status != "waiting" or f.state_used or time.time() > f.expires \
                    or not isinstance(state, str) or not hmac.compare_digest(state.encode(), (f.state or "").encode()):
                return False, i18n.msg("vinylmind.callback_unknown")
            f.state_used = True
            if error:
                self._fail_flow(f, i18n.msg("vinylmind.acc_access_denied" if error == "access_denied"
                                            else "vinylmind.acc_failed_code", code=str(error)[:40]))
                return False, f.error
            if not code or len(code) > 512:
                self._fail_flow(f, i18n.msg("vinylmind.acc_invalid_request"))
                return False, f.error
            body = {"grant_type": Account.GRANT_CODE, "code": code, "code_verifier": f.verifier,
                    "redirect_uri": f.redirect_uri, "client_id": Account.CLIENT_ID, "station": f.key.fingerprint}
            f.verifier = None                            # one exchange only: a failed code is not tried again
            try:
                grant = f.account.token(body)
            except HubError as e:
                self._fail_flow(f, e.message)
                return False, e.message
            return self._finish(f, grant)

    def _poll_device(self, f: _Flow) -> None:
        """Flow B: ask the token endpoint every `interval` seconds until the user confirmed (RFC 8628)."""
        while not f.cancelled.wait(f.interval):
            if self.flow is not f or f.status != "waiting":
                return
            if time.time() > f.expires:
                with self.lock:
                    self._fail_flow(f, i18n.msg("vinylmind.acc_expired_token"))
                return
            body = {"grant_type": Account.GRANT_DEVICE, "device_code": f.device_code, "client_id": Account.CLIENT_ID,
                    "station": f.key.fingerprint}
            try:
                grant = f.account.token(body)
            except HubError as e:
                if e.code in ("authorization_pending", "unreachable"):
                    continue
                if e.code == "slow_down":
                    f.interval += Account.SLOW_DOWN_S
                    continue
                if e.code == "rate_limited":
                    f.interval = max(f.interval, int(e.retry_after or 0) or f.interval + Account.SLOW_DOWN_S)
                    continue
                with self.lock:
                    self._fail_flow(f, e.message)
                return
            with self.lock:
                if self.flow is f and f.status == "waiting":
                    self._finish(f, grant)
            return

    def _finish(self, f: _Flow, grant: dict) -> tuple[bool, dict]:
        """After the token response: the hub it names must be the configured one with the accepted key; then join with
        the invitation and the binding."""
        f.device_code = None
        try:
            same_url = normalize_url(grant["server_url"]) == f.hub["url"]
        except HubError:
            same_url = False
        if not same_url or grant["server_fingerprint"] != f.hub["fingerprint"]:
            self._fail_flow(f, i18n.msg("vinylmind.connect_other_server"))
            return False, f.error
        try:
            info = self._client(f.hub["url"]).server_info()
        except HubError as e:
            self._fail_flow(f, e.message)
            return False, e.message
        if info["fingerprint"] != grant["server_fingerprint"]:
            self._fail_flow(f, i18n.msg("vinylmind.key_changed", got=info["fingerprint"],
                                        want=grant["server_fingerprint"]))
            return False, f.error
        f.hub["info"] = info
        f.grant = {"invitation": grant["invitation"], "binding": grant.get("binding")}
        return self._join_grant(f)

    def _join_grant(self, f: _Flow) -> tuple[bool, dict]:
        """POST /v1/join with the flow's invitation. A join refused for a while (the hub's daily limit, a recording)
        keeps the invitation in memory and is tried again; anything else ends the flow."""
        f.status = "joining"
        if self.busy():
            f.status, f.retry_at = "retry", time.time() + Account.JOIN_RETRY_S
            return True, i18n.msg("vinylmind.connect_later_recording")
        info = f.hub["info"]
        try:
            r = self._client(f.hub["url"], f.key, info["fingerprint"]).join(f.grant["invitation"],
                                                                            f.grant.get("binding"))
        except HubError as e:
            if e.code in ("join_limit", "rate_limited", "unreachable", "recording") or (e.status or 0) >= 500:
                f.status = "retry"
                f.retry_at = time.time() + max(Account.JOIN_RETRY_S, min(e.retry_after or 0, 3600))
                f.error = e.message
                return True, i18n.msg("vinylmind.connect_later", error=e.message)
            self._fail_flow(f, e.message)
            return False, e.message
        old = self.joined()
        old_key = self._key(old) if old and f.reconnect else None
        if old_key and old.get("status") in ("active", "pending"):
            try:                                         # best effort: the account service disconnects it anyway
                self._client(old["url"], old_key, old["fingerprint"]).leave()
            except HubError:
                pass
        self._joined(f.hub["url"], info, f.key, r["status"], via="account", site=f.account.host)
        f.status, f.error, f.retry_at = "done", None, None
        f.wipe()
        self.flow = None
        return True, i18n.msg("vinylmind.connect_done" if r["status"] == "active" else "vinylmind.connect_pending")

    def _retry_flow_join(self) -> None:
        f = self.flow
        if f and f.status == "retry" and f.retry_at and time.time() >= f.retry_at and f.grant:
            with self.lock:
                if self.flow is f and f.status == "retry":
                    self._join_grant(f)

    # ─── Status refresh and leaving ────────────────────────────────────────────────────────────────────────────────
    def refresh(self) -> dict:
        """Ask the hub now whether a waiting station was approved (the settings ask while they show "waiting")."""
        with self.lock:
            j = self._usable_join()
            if not j or j.get("status") != "pending" or time.time() - self.last_refresh < REFRESH_MIN_S:
                return self.status()
            self.last_refresh = time.time()
            key = self._key(j)
            if not key:
                self._set_join(status="key_lost")
                return self.status()
            try:
                me = self._client(j["url"], key, j["fingerprint"]).me()
            except HubError as e:
                self._note_error(e, j)
                return self.status()
            self._set_join(status=me["status"], checked_at=time.time())
            if me["status"] == "active":
                self.dirty, self.next_attempt, self.last_error, self.last_error_code = True, 0.0, None, None
            return self.status()

    def leave(self, forget: bool = False) -> dict:
        """Leave the server: it deletes this station and everything it sent (DELETE /v1/me), and the key file is
        deleted. `forget` clears the local state even when the server cannot be reached (for a server that is
        gone)."""
        with self.lock:
            self.connect_cancel()
            j = self.joined()
            if not j:
                return self.status()
            if self.busy():
                raise HubError(i18n.msg("vinylmind.recording"), code="recording")
            key = self._key(j)
            if key and j.get("status") in ("active", "pending"):
                try:
                    self._client(j["url"], key, j["fingerprint"]).leave()
                except HubError as e:
                    if e.code not in ("unknown_key", "revoked") and not forget:
                        raise
            for s in (S_JOIN, S_SENT, S_MODEL):
                self._put(s, None)
            StationKey.delete(key_file(self.state_dir, j["fingerprint"]))
            self._drop_server_prior()
            self.failures, self.next_attempt, self.last_error, self.last_error_code = 0, 0.0, None, None
            log.info("Vinyl-Mind: left %s", j["url"])
            return self.status()

    # ─── Contributions, preview, packages ──────────────────────────────────────────────────────────────────────────
    def entries(self) -> list[dict]:
        """Per turntable profile: the contribution as it would be sent now, or why there is none."""
        sent = self._get(S_SENT, {}) or {}
        out = []
        for p in self.db.profiles():
            vid = self.db.profile_vm_id(p["id"])
            prev = sent.get(vid) or {}
            doc, reason = build_contribution(self.db, p, self.feature_version)
            if doc:
                h = contribution_sha256(doc)
                doc["trained_at"] = prev.get("trained_at") if prev.get("sha256") == h and prev.get("trained_at") \
                    else int(time.time())
            out.append({"profile": p, "vm_id": vid, "chain": chain_of(p), "chain_guess": guess_chain(p),
                        "chain_set": bool(p.get("chain_source") or p.get("chain_cartridge")), "doc": doc,
                        "reason": reason, "sha256": contribution_sha256(doc) if doc else None, "sent": prev})
        return out

    def preview(self) -> dict:
        """The exact documents the next contribution would send, per profile (the names are shown only here)."""
        j = self.joined()
        rows = []
        for e in self.entries():
            rows.append({"id": e["profile"]["id"], "name": e["profile"]["name"], "active": bool(e["profile"]["active"]),
                         "vm_id": e["vm_id"], "chain": e["chain"], "chain_guess": e["chain_guess"],
                         "chain_set": e["chain_set"], "payload": e["doc"], "reason": e["reason"],
                         "sha256": e["sha256"], "sent_at": e["sent"].get("at"),
                         "unchanged": bool(e["sha256"]) and e["sent"].get("sha256") == e["sha256"]})
        return {"url": self.url(), "contribute": self.contribute(), "profiles": rows,
                "feature_version": self.feature_version, "feature_supported": self._feature_supported(j)}

    def export(self, pid: int) -> dict:
        p = self.db.profile(pid)
        if not p:
            raise HubError(i18n.msg("api.profile_not_found"), code="not_found")
        doc, reason = build_contribution(self.db, p, self.feature_version, trained_at=int(time.time()))
        if not doc:
            raise HubError(reason, code="nothing")
        return doc

    def describe(self, pkg) -> dict:
        """What a package file is, after checking it: a contribution or a global model (its payload, or the signed
        JWS as the hub delivers it), with versions, kinds and their examples. Raises HubError."""
        signature = "none"
        if isinstance(pkg, str):
            j = self.joined()
            token = pkg.strip()
            if not re.fullmatch(JWS_RE, token):
                raise HubError(i18n.msg("vinylmind.package_type"))
            if j and j.get("server_key"):
                try:
                    pkg = verify_jws(token, unb64u(j["server_key"]), j["fingerprint"])
                    signature = "verified"
                except HubError:
                    pkg = None
            if not isinstance(pkg, dict):
                try:                                     # another hub's model: shown as not checked
                    pkg = strict_json(unb64u(token.split(".")[1]))
                except (ValueError, UnicodeDecodeError):
                    raise HubError(i18n.msg("vinylmind.package_type")) from None
                signature = "unverified"
        if not isinstance(pkg, dict):
            raise HubError(i18n.msg("vinylmind.package_type"))
        kind = {"vinyl-mind/contribution": "contribution", "vinyl-mind/global-model": "model"}.get(pkg.get("spec"))
        if not kind:
            raise HubError(i18n.msg("vinylmind.package_type"))
        if pkg.get("feature_version") != int(self.feature_version) and _int(pkg.get("feature_version")):
            raise HubError(i18n.msg("vinylmind.version_mismatch", got=str(pkg.get("feature_version")),
                                    want=self.feature_version))
        errs = validate_contribution(pkg, self.feature_version) if kind == "contribution" \
            else validate_model(pkg, self.feature_version)
        if errs:
            raise HubError(i18n.msg("vinylmind.package_invalid", error=errs[0], n=len(errs)))
        if kind == "model":
            model_fits(pkg, self.feature_version)
            kinds = {k: {"examples": v["examples"], "profiles": v["profiles"]} for k, v in pkg["kinds"].items()}
            ref = str(pkg["version"])
        else:
            if pkg["detector_version"] != detect.VERSION:
                raise HubError(i18n.msg("vinylmind.detector_mismatch", got=pkg["detector_version"],
                                        want=detect.VERSION))
            own = {contribution_sha256(e["doc"]) for e in self.entries() if e["doc"]}
            if contribution_sha256(pkg) in own:
                raise HubError(i18n.msg("vinylmind.package_own"))
            kinds = {k: {"examples": v["n_real"] + v["n_false"], "manual": v["n_manual"],
                         "est_error": v["cv_err_est"]} for k, v in pkg["kinds"].items()}
            ref = contribution_sha256(pkg)[:8]
        cls = chain_class(pkg["chain"]) if kind == "contribution" else pkg["chain_class"]
        return {"type": kind, "ref": ref, "feature_version": str(pkg["feature_version"]),
                "detector_version": pkg["detector_version"], "chain_class": cls, "kinds": kinds,
                "signature": signature, "client": (pkg.get("client") or {}).get("version"), "_doc": pkg}

    def import_package(self, pkg) -> dict:
        """Use a package's coefficients as the prior of the training (after the user confirmed `describe`)."""
        with self.lock:
            d = self.describe(pkg)
            doc = d.pop("_doc")
            self._put(S_PRIOR, {"source": "import", "type": d["type"], "ref": d["ref"],
                                "feature_version": self.feature_version, "detector_version": doc["detector_version"],
                                "kinds": {k: learning.from_coef(v["coef"], self.feature_version)
                                          for k, v in doc["kinds"].items()},
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
        if not isinstance(p, dict) or p.get("feature_version") != self.feature_version \
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

    # ─── The round ─────────────────────────────────────────────────────────────────────────────────────────────────
    def _note_error(self, e: HubError, j: dict) -> None:
        """What a hub error means for the joined state."""
        if e.code == "client_not_accepted":
            self._set_join(client_rejected=__version__)
        elif e.code in ("revoked", "unknown_key"):
            self._set_join(status="revoked")
        elif e.code == "pending":
            self._set_join(status="pending", checked_at=time.time())
        elif e.code in ("feature_version_not_accepted", "detector_version_not_accepted"):
            self._set_join(server=(j.get("server") or {}) | {"checked_at": 0})
        elif e.code in ("bad_signature", "stale"):
            # a hub that changed its key refuses every signature; a wrong clock makes them stale
            try:
                info = self._client(j["url"]).server_info()
                self.skew_s = info["skew_s"]
                if info["fingerprint"] != j["fingerprint"]:
                    self._set_join(status="key_changed")
                    e.message = i18n.msg("vinylmind.server_key_changed", got=info["fingerprint"])
                elif e.code == "stale" and info["skew_s"] is not None:
                    e.message = i18n.msg("vinylmind.err_clock", s=abs(info["skew_s"]))
            except HubError:
                pass
        self.last_error, self.last_error_code = e.message, e.code

    def run_round(self, force: bool = False) -> dict:
        """Contribute changed profiles and look for newer global models, as the switches allow. Never while a
        recording runs and nothing at all while both switches are off. `force` ("Send now") ignores the intervals
        and raises the error of a failed round."""
        with self.lock:
            if not (self.contribute() or self.use_model()):
                return {"skipped": "off"}
            j = self._usable_join()
            if not j:
                return {"skipped": "not_joined"}
            if self.busy():
                return {"skipped": "recording"}
            now = time.time()
            if not force and now < self.next_attempt:
                return {"skipped": "backoff"}
            key = self._key(j)
            if key is None:
                # the key file is gone or replaced (restored backup, deleted by hand): no longer the joined station
                self._set_join(status="key_lost")
                return {"skipped": "not_joined"}
            self.last_attempt, out = now, {}
            try:
                client = self._client(j["url"], key, j["fingerprint"])
                if j.get("status") != "active":
                    if not force and now - (j.get("checked_at") or 0) < STATUS_POLL_S:
                        return {"skipped": "pending"}
                    j = self._set_join(status=client.me()["status"], checked_at=now)
                    if j["status"] != "active":
                        return {"status": j["status"]}
                if force or now - ((j.get("server") or {}).get("checked_at") or 0) >= SERVER_REFRESH_S:
                    info = client.server_info()
                    self.skew_s = info["skew_s"]
                    if info["fingerprint"] != j["fingerprint"]:
                        j = self._set_join(status="key_changed")
                        raise HubError(i18n.msg("vinylmind.server_key_changed", got=info["fingerprint"]),
                                       code="key_changed")
                    j = self._set_join(server=self._server_facts(info))
                if j.get("client_rejected") == __version__:
                    if not accepts_client((j.get("server") or {}).get("clients")):
                        return {"skipped": "client_not_accepted"}
                    j = self._set_join(client_rejected=None)  # the hub accepts this version again
                if not self._feature_supported(j):
                    return {"skipped": "feature_version"}
                if not self._detector_supported(j):
                    return {"skipped": "detector_version"}
                if self.contribute() and (force or self.dirty):
                    out["contributions"] = self._contribute(client, force)
                if self.use_model():
                    m = self._get(S_MODEL, {}) or {}
                    if force or now - (m.get("polled_at") or 0) >= MODEL_POLL_S:
                        out["model"] = self._poll_models(client, j, now)
            except HubError as e:
                if e.code == "recording":                # a recording started meanwhile: try again next round
                    if force:
                        raise
                    return {"skipped": "recording"}
                self._note_error(e, j)
                self.failures += 1
                wait = min(ROUND_S * 2 ** self.failures, MAX_ROUND_BACKOFF_S)
                self.next_attempt = now + max(wait, min(e.retry_after or 0, MAX_ROUND_BACKOFF_S))
                log.warning("Vinyl-Mind round failed: %s", i18n.render(e.message, "en"))
                if force:
                    raise
                return {"error": e.message}
            self.failures, self.next_attempt, self.last_error, self.last_error_code = 0, 0.0, None, None
            return out

    def _contribute(self, client: Client, force: bool) -> dict:
        """PUT every profile whose contribution changed (at most once per MIN_CONTRIBUTION_INTERVAL_S unless forced)
        and withdraw contributions of profiles that are gone or have nothing to contribute any more."""
        sent = self._get(S_SENT, {}) or {}
        current, n, waiting = set(), 0, False
        for e in self.entries():
            doc, vid = e["doc"], e["vm_id"]
            if doc is None:
                continue
            errs = validate_contribution(doc, self.feature_version)
            if errs or not re.fullmatch(PROFILE_ID_RE, vid or ""):
                # a bug on this side: never send what the hub must reject
                log.error("Vinyl-Mind: contribution of profile %s not sent, invalid: %s", e["profile"]["id"], errs[:3])
                continue
            current.add(vid)
            prev = sent.get(vid) or {}
            if prev.get("sha256") == e["sha256"]:
                continue
            if not force and prev.get("at") and time.time() - prev["at"] < MIN_CONTRIBUTION_INTERVAL_S:
                waiting = True
                continue
            client.put_contribution(vid, doc)
            sent[vid] = {"sha256": e["sha256"], "at": time.time(), "trained_at": doc.get("trained_at")}
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

    def _active_chain_class(self) -> str:
        p = self.db.active_profile()
        return chain_class(chain_of(p)) if p else "all"

    def _poll_models(self, client: Client, j: dict, now: float) -> str:
        """GET /v1/models (usually 304). The model in use is dropped when the hub withdrew it; a new published model
        or candidate is verified, checked on the own master recordings and reported; a published one is adopted as the
        prior where it is not worse."""
        m = self._get(S_MODEL, {}) or {}
        query = [int(self.feature_version), detect.VERSION, self._active_chain_class()]
        etag = m.get("etag") if m.get("query") == query else None
        status, data, etag = client.models(*query, etag)
        if status == 304:
            self._put(S_MODEL, m | {"polled_at": now})
            return "unchanged"
        # the new ETag is stored only once every entry has been handled: a failure halfway asks again in full
        m = m | {"etag": None, "query": query}
        m.setdefault("checked", {})
        result = "unchanged"
        prior = self._get(S_PRIOR)
        in_use = prior.get("ref") if isinstance(prior, dict) and prior.get("source") == "server" else None
        pub, cand = data.get("published"), data.get("candidate")
        if in_use is not None and (not isinstance(pub, dict) or pub.get("version") != in_use):
            entry = client.model(in_use)
            if entry is None or entry.get("status") == "withdrawn":
                m["withdrawn"] = {"version": in_use, "reason": str((entry or {}).get("reason") or "")[:300],
                                  "at": now}
                self._drop_server_prior()
                result = "withdrawn"
        for entry in (cand, pub):
            if not isinstance(entry, dict) or not _int(entry.get("version")):
                continue
            seen = m["checked"].get(str(entry["version"]))
            if seen and seen.get("status") == entry.get("status"):
                continue                                 # checked and reported in this state already
            m["checked"][str(entry["version"])] = self._take_model(client, j, entry)
            self._put(S_MODEL, m)
            result = "new"
        # keep the state small: the model in use and the newest few
        keep = sorted((int(v) for v in m["checked"]), reverse=True)[:4]
        m["checked"] = {v: c for v, c in m["checked"].items() if int(v) in keep or (in_use is not None
                                                                                 and int(v) == in_use)}
        self._put(S_MODEL, m | {"etag": etag, "polled_at": now})
        return result

    def _take_model(self, client: Client, j: dict, entry: dict) -> dict:
        """Verify, validate, check, adopt (published models only) and report one model entry."""
        if entry.get("status") not in ("published", "candidate") or not entry.get("jws"):
            raise HubError(i18n.msg("vinylmind.model_invalid", error="entry"), code="model_rejected")
        payload = verify_jws(entry["jws"], unb64u(j["server_key"]), j["fingerprint"])   # before anything else
        errs = validate_model(payload, self.feature_version)
        if errs:
            raise HubError(i18n.msg("vinylmind.model_invalid", error=errs[0]), code="model_rejected")
        try:
            model_fits(payload, self.feature_version)
        except HubError as e:                            # the hub sent a model of another version: its fault
            raise HubError(e.message, code="model_rejected") from None
        if payload["version"] != entry["version"]:
            raise HubError(i18n.msg("vinylmind.model_invalid", error="version"), code="model_rejected")
        kinds = {k: learning.from_coef(v["coef"], self.feature_version) for k, v in payload["kinds"].items()}
        data, _ = learning.training_data(self.db, version=self.feature_version)
        check = check_model(data, kinds, self.feature_version)
        published = entry["status"] == "published"
        adopted = sorted(k for k, c in check.items() if adopt(c)) if published else []
        if published:
            if adopted:
                self._put(S_PRIOR, {"source": "server", "type": "model", "ref": payload["version"],
                                    "feature_version": self.feature_version,
                                    "detector_version": payload["detector_version"], "hub": j["fingerprint"],
                                    "kinds": {k: kinds[k] for k in adopted}, "accepted_at": time.time()})
                self._retrain()
            else:
                prior = self._get(S_PRIOR)
                if isinstance(prior, dict) and prior.get("source") == "server" and prior.get("ref") == payload["version"]:
                    self._drop_server_prior()
        log.info("Vinyl-Mind: model %s (%s) checked; adopted for %s", payload["version"], entry["status"],
                 adopted or "no kind")
        return {"version": payload["version"], "status": entry["status"], "chain_class": payload["chain_class"],
                "created_at": payload["created_at"], "check": check, "adopted": adopted, "at": time.time(),
                "reported": self._report(client, payload, kinds, bool(adopted))}

    def _report(self, client: Client, payload: dict, kinds: dict, adopted: bool) -> list:
        """PUT a check report per profile that has at least two master recordings for a kind of the model; returns
        the profile IDs reported."""
        done = []
        for p in self.db.profiles():
            vid = self.db.profile_vm_id(p["id"])
            data, _ = _contribution_data(self.db, p["id"], self.feature_version)
            check = check_model(data, kinds, self.feature_version)
            ks = {k: {"local_err": c["local_err"], "global_err": c["global_err"],
                      "master_recordings": c["master_recordings"]}
                  for k, c in check.items() if c["master_recordings"] >= 2 and c["local_err"] is not None
                  and c["global_err"] is not None and c["local_err"] <= 100 and c["global_err"] <= 100}
            if not ks:
                continue
            report = {"spec": "vinyl-mind/check-report", "feature_version": int(self.feature_version),
                      "adopted": adopted, "kinds": ks}
            errs = validate_check_report(report, self.feature_version)
            if errs:
                log.error("Vinyl-Mind: check report not sent, invalid: %s", errs[:3])
                continue
            try:
                client.put_check(vid, payload["version"], report)
            except HubError as e:
                if e.code in ("withdrawn", "not_found", "too_many_profiles"):
                    log.info("Vinyl-Mind: report on model %s refused (%s)", payload["version"], e.code)
                    break
                raise
            done.append(vid)
        return done

    # ─── Background ────────────────────────────────────────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="vinylcheck-vinylmind")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        f = self.flow
        if f:
            f.cancelled.set()

    def _loop(self) -> None:
        while not self._stop.wait(ROUND_S):
            try:
                self._retry_flow_join()
                self.run_round()
            except Exception:  # noqa: BLE001 — the next round tries again
                log.exception("Vinyl-Mind round failed")


# ─── Routes ────────────────────────────────────────────────────────────────────────────────────────────────────────

# HubErrors that describe a conflict with the station's state (HTTP 409); other local ones are invalid input (422),
# and failures of the hub, the account service or the connection are 502
_CONFLICT = ("recording", "leave_first", "key_changed", "no_server", "not_joined", "switches_off", "nothing",
             "already_joined", "confirm_fingerprint", "no_accounts", "not_local")

_CALLBACK_PAGE = """<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ color-scheme: dark; --bg: #141516; --card: #202224; --ink: #e9eaeb; --muted: #9da1a5; --line: #34373a;
  --accent: #e05a48; --ok: #6cc28a; }}
@media (prefers-color-scheme: light) {{ :root {{ color-scheme: light; --bg: #eef0f1; --card: #ffffff; --ink: #1a1b1c;
  --muted: #5f6468; --line: #d5d8db; --accent: #b7382c; --ok: #2f7d4a; }} }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--bg); color: var(--ink);
  font: 16px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; padding: 16px; }}
main {{ width: 100%; max-width: 420px; background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: 28px 24px; text-align: center; }}
h1 {{ font-size: 1.3rem; margin: 10px 0 6px; font-weight: 600; color: var(--{tone}); }}
p {{ margin: 0 0 12px; color: var(--muted); }}
</style></head>
<body><main>
{signet}
<h1>{title}</h1>
<p>{text}</p>
<p>{hint}</p>
</main></body></html>
"""


def callback_page(ok: bool, message, status_code: int = 200) -> HTMLResponse:
    """The small page the browser shows after the account service sent it back to the station."""
    t = lambda k: html.escape(i18n.t(f"vinylmind.{k}"))   # noqa: E731
    page = _CALLBACK_PAGE.format(lang=i18n.current_lang(), title=t("page_ok_title" if ok else "page_fail_title"),
                                 text=html.escape(str(i18n.render(message))), hint=t("page_hint"),
                                 tone="ok" if ok else "accent", signet=network._SIGNET)
    return HTMLResponse(page, status_code=status_code,
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


def install(app, vm: VinylMind) -> VinylMind:
    """Add the routes /api/vinylmind* and /vinyl-mind/callback to `app` (also app.state.vinylmind)."""
    app.state.vinylmind = vm
    access = getattr(app.state, "network", None)
    if access is not None:
        # the account service sends the browser here; it completes nothing but the open flow with its state
        access.open_paths.add(Account.CALLBACK_PATH)

    def http(e: HubError) -> HTTPException:
        if e.code == "not_found" and e.status is None:
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

    def local(request: Request) -> bool:
        """Is the browser on this computer? Only then can the account service send it back to 127.0.0.1."""
        return network.is_loopback((request.client.host if request.client else "") or "")

    @app.get("/api/vinylmind")
    def vm_status(request: Request):
        return vm.status() | {"browser_here": local(request)}

    @app.put("/api/vinylmind")
    def vm_update(body: dict, request: Request):
        unknown = set(body) - {"contribute", "use_model", "url", "reset_url"}
        if unknown:
            raise HTTPException(422, i18n.msg("vinylmind.bad_field", field=", ".join(sorted(unknown))))
        for k in ("contribute", "use_model", "reset_url"):
            if k in body:
                flag(body, k)
        if "url" in body and not isinstance(body["url"], (str, type(None))):
            raise HTTPException(422, i18n.msg("vinylmind.bad_field", field="url"))
        return call(vm.update, body) | {"browser_here": local(request)}

    @app.get("/api/vinylmind/server")
    def vm_server():
        return call(vm.server_info)

    @app.get("/api/vinylmind/preview")
    def vm_preview():
        return vm.preview()

    @app.post("/api/vinylmind/join")
    def vm_join(request: Request, body: dict | None = None):
        body = body or {}
        inv, fp = body.get("invitation"), body.get("fingerprint")
        if not isinstance(inv, (str, type(None))) or not isinstance(fp, (str, type(None))):
            raise HTTPException(422, i18n.msg("vinylmind.bad_field", field="invitation"))
        return call(vm.join, inv, fp) | {"browser_here": local(request)}

    @app.post("/api/vinylmind/connect")
    def vm_connect(request: Request, body: dict | None = None):
        body = body or {}
        kind, reconnect = body.get("flow", "browser"), body.get("reconnect", False)
        if kind not in ("browser", "device") or not isinstance(reconnect, bool):
            raise HTTPException(422, i18n.msg("vinylmind.bad_field", field="flow"))
        if kind == "browser" and not local(request):
            raise HTTPException(409, i18n.msg("vinylmind.connect_not_local"))
        server = request.scope.get("server") or ("127.0.0.1", vm.cfg.port)
        lang = i18n.current_lang()
        r = call(vm.connect_start, kind, redirect_host=str(server[0] or ""), port=server[1], lang=lang,
                 reconnect=reconnect, open_browser=kind == "browser" and vm.desktop)
        r["status"] |= {"browser_here": local(request)}
        return r

    @app.post("/api/vinylmind/connect/cancel")
    def vm_connect_cancel(request: Request):
        with vm.lock:
            return vm.connect_cancel() | {"browser_here": local(request)}

    @app.get(Account.CALLBACK_PATH, include_in_schema=False)
    def vm_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
        if not local(request):
            return callback_page(False, i18n.msg("vinylmind.callback_unknown"), 403)
        try:
            ok, message = vm.connect_callback(code, state, error)
        except HubError as e:                            # pragma: no cover — every failure ends as a message
            ok, message = False, e.message
        return callback_page(ok, message, 200 if ok else 400)

    @app.post("/api/vinylmind/refresh")
    def vm_refresh(request: Request):
        return vm.refresh() | {"browser_here": local(request)}

    @app.post("/api/vinylmind/leave")
    def vm_leave(request: Request, body: dict | None = None):
        return call(vm.leave, bool((body or {}).get("forget"))) | {"browser_here": local(request)}

    @app.post("/api/vinylmind/sync")
    def vm_sync(request: Request):
        if not (vm.contribute() or vm.use_model()):
            raise HTTPException(409, i18n.msg("vinylmind.switches_off"))
        if not vm._usable_join():
            raise HTTPException(409, i18n.msg("vinylmind.not_joined"))
        result = call(vm.run_round, force=True)
        return {"result": result, "status": vm.status() | {"browser_here": local(request)}}

    @app.get("/api/vinylmind/export")
    def vm_export(profile: int):
        doc = call(vm.export, profile)
        vid = vm.db.profile_vm_id(profile) or "profile"
        name = f"vinyl-mind-contribution-{vid[:8]}.json"
        return Response(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})

    @app.post("/api/vinylmind/import")
    async def vm_import(request: Request, confirm: bool = False):
        # the request body is the package file itself (JSON, or a signed model as JWS text), read with a size limit
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_PACKAGE_BYTES:
                raise HTTPException(413, i18n.msg("vinylmind.package_too_large", kb=MAX_PACKAGE_BYTES // 1024))
        try:
            text = raw.decode("utf-8").strip()
            pkg = text if re.fullmatch(JWS_RE, text) else strict_json(text)
        except (UnicodeDecodeError, ValueError):
            raise HTTPException(422, i18n.msg("vinylmind.package_json")) from None
        if not confirm:
            d = await run_in_threadpool(call, vm.describe, pkg)
            d.pop("_doc", None)
            return {"package": d}
        return await run_in_threadpool(call, vm.import_package, pkg)

    @app.delete("/api/vinylmind/prior")
    def vm_prior_delete():
        return vm.remove_prior()

    return vm

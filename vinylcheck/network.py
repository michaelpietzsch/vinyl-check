"""Access from other devices on the network: access code, login page, and the network listener of the desktop app.

The desktop app (desktop.py) serves the interface on 127.0.0.1 only. The setting "Allow access from other devices on
the network" (off by default) starts a second listener on all interfaces (`ServerThread` on 0.0.0.0), so a phone or
tablet at the turntable can use the same interface. Requests that do not come from this computer must then present
the access code: entered once on a small login page (/access), or carried by the QR code in the settings, it is kept
as a cookie. Requests from the computer itself (loopback) never need it. A new code signs all devices out.

A service installation (Raspberry Pi station) is meant for a trusted local network and stays open as before, unless
`access_code` is set in its configuration file; then the same check applies with that fixed code.

On the desktop two more checks keep web pages open in the user's own browser away from the local engine: the Host
header must name this computer (an IP address, localhost, a .local name or the host name), which defeats DNS
rebinding, and a request that changes something, or opens the WebSocket, must come from the interface's own origin
(cross-site request forgery).

Server catalog: ``network`` (login page and messages). The settings section is in the web catalog ``network``.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import io
import ipaddress
import logging
import secrets
import socket
import sys
import threading
import time
import urllib.parse
from functools import lru_cache

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.datastructures import Headers
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketClose

from . import i18n

log = logging.getLogger("vinylcheck.network")

COOKIE = "vc_access"
LOGIN_PATH = "/access"
# No 0/O and 1/I/L: the code is read off a screen and typed on a phone. 8 characters of 31 give about 40 bits,
# far beyond what can be tried over a local network with the pause after repeated wrong codes.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LEN = 8
COOKIE_MAX_AGE = 180 * 24 * 3600         # a phone stays signed in for half a year, or until the code changes
MAX_FAILURES, LOCK_S = 5, 60.0           # wrong codes from one address before a pause of LOCK_S seconds
LAN_HOST = "0.0.0.0"
# Keys in the settings table of the database (not part of web.SETTINGS: they are not edited in the settings form)
S_LAN, S_CODE, S_SECRET, S_PORT = "lan_access", "access_code", "access_secret", "lan_port"


# ─── Helpers ────────────────────────────────────────────────────────────────

def new_code() -> str:
    """A random access code, e.g. "K7QM-2XPA"."""
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_code(code) -> str:
    """Upper case without spaces and dashes, so "k7qm 2xpa" and "K7QM-2XPA" are the same code."""
    return "".join(c for c in str(code or "").upper() if c.isalnum())


def is_loopback(host: str | None) -> bool:
    """Is this client address the computer itself? Anything that is not an IP address (a Unix socket, the name a test
    client uses) is not provably local and counts as remote."""
    try:
        ip = ipaddress.ip_address((host or "").split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


@lru_cache(maxsize=1)
def _own_names() -> frozenset[str]:
    name = socket.gethostname().lower()
    return frozenset({name, name.split(".")[0] + ".local"})


def host_ok(host_header: str | None) -> bool:
    """Does the Host header name this computer? IP literals, localhost, .local (mDNS) names and the host name pass.
    A DNS rebinding attack needs a public domain name that points to this computer, and fails here."""
    if not host_header:
        return True                     # HTTP/1.0 clients; browsers always send a Host header
    try:
        name = urllib.parse.urlsplit("//" + host_header).hostname
    except ValueError:                  # malformed, e.g. an unclosed IPv6 bracket
        return False
    if not name:
        return False
    if name == "localhost" or name.endswith(".local") or name in _own_names():
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def same_origin(origin: str, host_header: str | None) -> bool:
    """Is the browser's Origin header the interface's own address (scheme://host:port of the request)?"""
    return bool(host_header) and urllib.parse.urlsplit(origin).netloc.lower() == host_header.lower()


def lan_addresses(timeout: float = 1.5) -> list[str]:
    """IPv4 addresses of this computer in the local network, the one of the default route first.

    No extra package: a UDP socket "connected" to a documentation address reveals the interface of the default route
    (nothing is sent). The host name lookup adds further interfaces; it can hang on a misconfigured network (macOS
    waits for mDNS), so it runs with a time limit."""
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))                 # TEST-NET-1 (RFC 5737), never routed anywhere
            found.append(s.getsockname()[0])
    except OSError:
        pass
    more: list[str] = []

    def lookup():
        try:
            more.extend(i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
        except OSError:
            pass
    t = threading.Thread(target=lookup, daemon=True, name="vinylcheck-hostname")
    t.start()
    t.join(timeout)
    out: list[str] = []
    for a in found + list(more):
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or a in out:
            continue
        out.append(a)
    return out


def qr_svg(text: str) -> str | None:
    """QR code of `text` as an inline SVG (black on white, also in the dark theme), or None without `segno`."""
    try:
        import segno
    except ImportError:
        return None
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=4, border=3, dark="#000000", light="#ffffff",
                                    xmldecl=False, omitsize=True, title="QR")
    return buf.getvalue().decode("utf-8")


def bind_socket(host: str, port: int) -> socket.socket:
    """A TCP socket bound to host:port (port 0 = any free port). Binding before the server starts reports a port in
    use at once, as an OSError."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform != "win32":
        # quick restart while old connections linger in TIME_WAIT; on Windows the same option would let two programs
        # share the port, so it stays off there
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise
    return sock


class ServerThread:
    """A uvicorn server for the app on an already bound socket, in a background thread.

    The desktop app runs two of them: the main one on 127.0.0.1 (with the app's start-up and shut-down, `lifespan`)
    and, when network access is on, one on all interfaces without lifespan, so the app starts only once."""

    def __init__(self, app, sock: socket.socket, lifespan: bool = True, name: str = "vinylcheck-http"):
        import uvicorn

        self.sock = sock
        self.port: int = sock.getsockname()[1]
        config = uvicorn.Config(app, lifespan="on" if lifespan else "off", log_config=None, access_log=False,
                                ws_ping_interval=20, timeout_graceful_shutdown=3)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [sock]}, daemon=True, name=name)

    def start(self, timeout: float = 60.0) -> None:
        """Start and wait until the server accepts connections; RuntimeError if it does not come up."""
        self.thread.start()
        deadline = time.monotonic() + timeout
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("the web server did not start")
            time.sleep(0.05)

    def stop(self, timeout: float = 10.0) -> None:
        self.server.should_exit = True
        self.thread.join(timeout)
        try:
            self.sock.close()
        except OSError:
            pass

    @property
    def running(self) -> bool:
        return self.thread.is_alive()


# ─── Access state ───────────────────────────────────────────────────────────

class Access:
    """Network access of one app: whether a code is required, the code and its cookie, the LAN listener."""

    def __init__(self, cfg, db, desktop: bool):
        self.cfg, self.db, self.desktop = cfg, db, desktop
        self.app = None                                 # set by install(); served by the LAN listener
        self.lan: ServerThread | None = None
        self.error = None                               # i18n message: why the LAN listener is not running
        self._token: str | None = None
        self._failures: dict[str, tuple[int, float]] = {}
        self._lock = threading.RLock()

    @property
    def required(self) -> bool:
        """Do requests from other devices need the code? Always on the desktop, on a service only if configured."""
        return self.desktop or bool(self.cfg.access_code)

    def code(self) -> str:
        if not self.desktop:
            return self.cfg.access_code
        with self._lock:
            c = self.db.setting(S_CODE)
            if not c:
                c = new_code()
                self.db.set_setting(S_CODE, c)
            return c

    def new_code(self) -> str:
        """Replace the code: every device has to enter the new one."""
        with self._lock:
            self.db.set_setting(S_CODE, new_code())
            self.db.set_setting(S_SECRET, secrets.token_hex(32))
            self._token = None
            self._failures.clear()
        log.info("New access code; other devices have to sign in again")
        return self.code()

    def token(self) -> str:
        """Cookie value for the current code: an HMAC with a secret of this installation, so the cookie neither
        reveals the code nor stays valid after a new code. Cached: it is checked on every request from the LAN."""
        with self._lock:
            if self._token is None:
                secret = self.db.setting(S_SECRET)
                if not secret:
                    secret = secrets.token_hex(32)
                    self.db.set_setting(S_SECRET, secret)
                self._token = hmac.new(bytes.fromhex(secret), normalize_code(self.code()).encode(),
                                       hashlib.sha256).hexdigest()
            return self._token

    def cookie_ok(self, value) -> bool:
        return bool(value) and hmac.compare_digest(str(value).encode("utf-8"), self.token().encode())

    def code_ok(self, code) -> bool:
        want = normalize_code(self.code())
        return bool(want) and hmac.compare_digest(normalize_code(code).encode("utf-8"), want.encode())

    # Brute-force brake: a pause after MAX_FAILURES wrong codes from one address
    def locked(self, client: str) -> bool:
        n, since = self._failures.get(client, (0, 0.0))
        return n >= MAX_FAILURES and time.monotonic() - since < LOCK_S

    def note_failure(self, client: str) -> None:
        n, since = self._failures.get(client, (0, 0.0))
        if n >= MAX_FAILURES and time.monotonic() - since >= LOCK_S:
            n = 0                                       # the pause is over: a fresh round of attempts
        self._failures[client] = (n + 1, time.monotonic())
        log.info("Wrong access code from %s", client)

    def note_success(self, client: str) -> None:
        self._failures.pop(client, None)

    # ─── LAN listener (desktop) ─────────────────────────────────────────────
    def lan_enabled(self) -> bool:
        return self.desktop and self.db.setting(S_LAN) == "true"

    def set_lan(self, on: bool) -> None:
        self.db.set_setting(S_LAN, "true" if on else "false")
        if on:
            self.start_lan()
        else:
            self.stop_lan()

    def start_lan(self) -> None:
        """Start the listener on all interfaces if the setting is on. The port of the last run is preferred (the
        address on the phone stays the same), then the configured port, then any free one."""
        with self._lock:
            if not self.lan_enabled() or (self.lan and self.lan.running) or self.app is None:
                return
            self.error = None
            tried = []
            for port in (self.db.setting(S_PORT), self.cfg.port, 0):
                try:
                    port = int(port)
                except (TypeError, ValueError):
                    continue
                if port in tried:
                    continue
                tried.append(port)
                try:
                    sock = bind_socket(LAN_HOST, port)
                except OSError as e:
                    log.warning("Network access: port %s not available (%s)", port or "any", e)
                    continue
                try:
                    self.lan = ServerThread(self.app, sock, lifespan=False, name="vinylcheck-lan")
                    self.lan.start()
                except Exception as e:  # noqa: BLE001 — reported in the settings instead of failing the request
                    sock.close()
                    self.lan, self.error = None, i18n.msg("network.lan_failed", error=str(e))
                    log.warning("Network access could not start: %s", e)
                    return
                self.db.set_setting(S_PORT, str(self.lan.port))
                log.info("Network access on port %d", self.lan.port)
                return
            self.error = i18n.msg("network.lan_no_port")

    def stop_lan(self) -> None:
        with self._lock:
            if self.lan is not None:
                self.lan.stop()
                self.lan = None
                log.info("Network access stopped")
            self.error = None

    def status(self, local: bool) -> dict:
        """What the settings page shows. The code (and the QR code carrying it) only go to this computer itself."""
        running = bool(self.lan and self.lan.running)
        urls = []
        if running:
            show_code = local and self.required
            for addr in lan_addresses()[:4]:
                url = f"http://{addr}:{self.lan.port}/"
                login = f"http://{addr}:{self.lan.port}{LOGIN_PATH}?code={self.code()}"
                urls.append({"url": url, "qr": qr_svg(login) if show_code else None})
        return {"desktop": self.desktop, "required": self.required, "local": local,
                "lan_enabled": self.lan_enabled(), "running": running, "port": self.lan.port if running else None,
                "urls": urls, "code": self.code() if local and self.required else None, "error": self.error}


# ─── Middleware and routes ──────────────────────────────────────────────────

class AccessMiddleware:
    """Checks every HTTP and WebSocket request (see the module docstring). Runs inside the language middleware, so its
    messages come in the language of the request."""

    def __init__(self, app, access: Access):
        self.app, self.access = app, access

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        acc, headers = self.access, Headers(scope=scope)
        if acc.desktop:
            if not host_ok(headers.get("host")):
                return await self._deny(scope, receive, send, 400, "network.bad_host")
            origin = headers.get("origin")
            changes = scope["type"] == "websocket" or scope.get("method") not in ("GET", "HEAD", "OPTIONS")
            if origin and changes and not same_origin(origin, headers.get("host")):
                return await self._deny(scope, receive, send, 403, "network.cross_site")
        client = (scope.get("client") or ("", 0))[0]
        if not acc.required or is_loopback(client) or scope["path"] == LOGIN_PATH:
            return await self.app(scope, receive, send)
        if acc.cookie_ok(HTTPConnection(scope).cookies.get(COOKIE)):
            return await self.app(scope, receive, send)
        if scope["type"] == "http" and not scope["path"].startswith("/api/"):
            return await RedirectResponse(LOGIN_PATH, status_code=303)(scope, receive, send)
        return await self._deny(scope, receive, send, 401, "network.access_required")

    @staticmethod
    async def _deny(scope, receive, send, status: int, key: str):
        if scope["type"] == "websocket":
            return await WebSocketClose(code=1008)(scope, receive, send)       # policy violation
        return await JSONResponse({"detail": i18n.t(key)}, status_code=status)(scope, receive, send)


_SIGNET = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="56" height="56" aria-hidden="true">'
           '<circle cx="16" cy="16" r="13" fill="none" stroke="currentColor" stroke-width="3"/>'
           '<circle cx="16" cy="16" r="3.5" fill="#e05a48"/><circle cx="27" cy="5" r="2.5" fill="currentColor"/>'
           '<path d="M25.2 6.8 18.5 13.5" fill="none" stroke="currentColor" stroke-width="3" '
           'stroke-linecap="square"/></svg>')

# The login page stands alone: until the code is entered, nothing else of the interface (style sheet, scripts) is
# served to this device. Colours are the design system's tokens (style.css), dark first like the interface.
_PAGE = """<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ color-scheme: dark; --bg: #141516; --card: #202224; --ink: #e9eaeb; --muted: #9da1a5; --line: #34373a;
  --accent: #e05a48; --accent-ink: #141516; }}
@media (prefers-color-scheme: light) {{ :root {{ color-scheme: light; --bg: #eef0f1; --card: #ffffff; --ink: #1a1b1c;
  --muted: #5f6468; --line: #d5d8db; --accent: #b7382c; --accent-ink: #ffffff; }} }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--bg); color: var(--ink);
  font: 16px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; padding: 16px; }}
main {{ width: 100%; max-width: 380px; background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: 28px 24px; text-align: center; }}
h1 {{ font-size: 1.3rem; margin: 10px 0 6px; font-weight: 600; }}
p {{ margin: 0 0 16px; color: var(--muted); }}
label {{ display: block; text-align: left; font-weight: 600; margin-bottom: 6px; }}
input {{ width: 100%; font: 600 1.4rem/1.2 ui-monospace, "SF Mono", Menlo, Consolas, monospace; letter-spacing: .12em;
  text-transform: uppercase; text-align: center; padding: 10px; border: 1px solid var(--line); border-radius: 8px;
  background: var(--bg); color: var(--ink); }}
button {{ margin-top: 14px; width: 100%; font: inherit; font-weight: 600; padding: 11px; border: 0; border-radius: 8px;
  background: var(--accent); color: var(--accent-ink); cursor: pointer; }}
.err {{ color: var(--accent); margin: 12px 0 0; font-weight: 600; }}
.hint {{ font-size: .85rem; margin: 18px 0 0; }}
</style></head>
<body><main>
{signet}
<h1>{title}</h1>
<p>{intro}</p>
<form method="post" action="{action}">
<label for="code">{label}</label>
<input id="code" name="code" autocomplete="one-time-code" autocapitalize="characters" autocorrect="off"
  spellcheck="false" required autofocus>
<button type="submit">{submit}</button>
</form>
{error}
<p class="hint">{hint}</p>
</main></body></html>
"""


def login_page(error_key: str | None = None, status_code: int = 200) -> HTMLResponse:
    t = lambda k: html.escape(i18n.t(f"network.{k}"))
    page = _PAGE.format(lang=i18n.current_lang(), title=t("login_title"), intro=t("login_intro"), action=LOGIN_PATH,
                        label=t("login_label"), submit=t("login_submit"), hint=t("login_hint"),
                        signet=_SIGNET, error=f'<p class="err" role="alert">{t(error_key)}</p>' if error_key else "")
    return HTMLResponse(page, status_code=status_code, headers={"Cache-Control": "no-store"})


def install(app: FastAPI, cfg, db, desktop: bool) -> Access:
    """Add the access check and the routes /access and /api/network to `app`. Call it before adding the language
    middleware, which must wrap this one. Returns the access state (also app.state.network)."""
    access = Access(cfg, db, desktop)
    access.app = app
    app.state.network = access
    app.add_middleware(AccessMiddleware, access=access)

    def client_of(request: Request) -> str:
        return request.client.host if request.client else ""

    def sign_in(request: Request, code: str):
        client = client_of(request)
        if not access.required or is_loopback(client):
            return RedirectResponse("/", status_code=303)
        if access.locked(client):
            return login_page("login_locked", 429)
        if not access.code_ok(code):
            access.note_failure(client)
            return login_page("login_wrong", 401)
        access.note_success(client)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, access.token(), max_age=COOKIE_MAX_AGE, path="/", httponly=True, samesite="lax")
        return resp

    @app.get(LOGIN_PATH, include_in_schema=False)
    def access_page(request: Request, code: str = ""):
        # The QR code in the settings carries the code in the address: scanning it signs the phone in directly
        if code:
            return sign_in(request, code)
        if not access.required or is_loopback(client_of(request)):
            return RedirectResponse("/", status_code=303)
        return login_page()

    @app.post(LOGIN_PATH, include_in_schema=False)
    async def access_submit(request: Request):
        # a plain HTML form (application/x-www-form-urlencoded), read without the python-multipart package
        form = urllib.parse.parse_qs((await request.body())[:4096].decode("utf-8", "replace"))
        return sign_in(request, (form.get("code") or [""])[0])

    def local_only(request: Request) -> None:
        if not is_loopback(client_of(request)):
            raise HTTPException(403, i18n.msg("network.local_only"))

    @app.get("/api/network")
    def network_status(request: Request):
        return access.status(is_loopback(client_of(request)))

    @app.put("/api/network")
    def network_put(request: Request, body: dict):
        local_only(request)
        if not access.desktop:
            raise HTTPException(409, i18n.msg("network.desktop_only"))
        if "lan_enabled" in body:
            access.set_lan(bool(body["lan_enabled"]))
        return access.status(True)

    @app.post("/api/network/code")
    def network_new_code(request: Request):
        local_only(request)
        if not access.desktop:
            raise HTTPException(409, i18n.msg("network.desktop_only"))
        access.new_code()
        return access.status(True)

    return access

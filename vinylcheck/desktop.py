"""Desktop app: the engine on 127.0.0.1 and the web interface in a native window.

`vinyl-check-desktop` and the packaged apps for macOS and Windows start the same engine as the service, bound to the
loopback interface only, and show the web interface in a native window through pywebview (WKWebView on macOS, WebView2
on Windows, GTK or Qt on Linux). Closing the window stops the engine; while a recording or an analysis runs, the app
asks first. The analysis process is shut down cleanly (`shutdown`, jobs.Worker.close), with a hard exit as the
fallback when something hangs. During a recording the computer is kept awake (`KeepAwake`). Configuration, database
and recordings live in the user's profile (`config.user_dirs`), the log in `<data>/logs/`. Other devices can be let
in from the settings (network.py).

Without pywebview, or with `--browser`, the interface opens in the default browser instead and the engine runs until
the process is stopped (Ctrl+C).

`--selftest` starts the engine without a window, on a temporary data directory with a synthetic test side, and checks
the API, the interface, the WebSocket, a complete analysis in the worker process, a PDF report with the bundled fonts,
the QR code, HTTPS with the system's certificates, PortAudio (host APIs and input devices) and the window toolkit. It prints one line per check and exits with 0 (all passed) or 1. CI runs it on the
built apps; `--output FILE` also writes the result to a file, because a windowed Windows app has no console.

Analyses run in a spawned child process (jobs.py). A frozen app (PyInstaller) starts that child from its own
executable, so `main()` calls multiprocessing.freeze_support() before anything else. Heavy modules are imported
inside the functions, so the child does not load the window toolkit.
"""

from __future__ import annotations

import argparse
import json
import logging
import multiprocessing
import os
import shutil
import signal
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__

log = logging.getLogger("vinylcheck.desktop")

APP_TITLE = "vinyl-check"
WINDOW_SIZE = (1280, 860)
MIN_SIZE = (900, 600)
S_PORT = "desktop_port"     # settings key: loopback port of the last run (keeps the interface's local storage)
# Upper bound for quitting: the web server waits up to 3 s for open connections and joins within 10 s, the analysis
# process gets a few seconds more (jobs.CLOSE_TIMEOUT_S); anything beyond that is a hang
STOP_TIMEOUT_S = 20.0


# ─── Engine ─────────────────────────────────────────────────────────────────

class _LanguageTap:
    """Remembers the language of the interface's own requests (cookie of the language switch, else the system's
    language through the web view's Accept-Language), so native dialogs of the window use the same language."""

    def __init__(self, app):
        self.app, self.lang = app, None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/":
            from starlette.requests import HTTPConnection

            from . import i18n
            conn = HTTPConnection(scope)
            self.lang = i18n.resolve(conn.query_params.get("lang"), conn.cookies.get(i18n.COOKIE),
                                     conn.headers.get("accept-language"))
        await self.app(scope, receive, send)


class KeepAwake:
    """Keeps the computer awake while a recording runs: sleep ends a recording, and macOS's App Nap throttles an app
    whose window is in the background. A thread looks at the recorder every two seconds.

    macOS: an NSProcessInfo activity (pyobjc comes with pywebview); Windows: SetThreadExecutionState, which holds for
    the calling thread, so the same thread sets and clears it. Elsewhere, or without pyobjc, nothing happens."""

    POLL_S = 2.0

    def __init__(self, active):
        self.active = active                         # callable: is a recording running?
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="vinylcheck-keep-awake")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(5)

    def _loop(self) -> None:
        held = None                                  # None = not held, else what _end() needs
        try:
            while not self._stop.is_set():
                try:
                    want = bool(self.active())
                    if want and held is None:
                        held = self._begin()
                        log.info("Recording: keeping the computer awake")
                    elif not want and held is not None:
                        self._end(held)
                        held = None
                except Exception as e:  # noqa: BLE001 — never let this helper disturb a recording
                    log.warning("Keep-awake: %s", e)
                self._stop.wait(self.POLL_S)
        finally:
            if held is not None:
                self._end(held)

    @staticmethod
    def _begin():
        if sys.platform == "darwin":
            try:
                import Foundation
            except ImportError:
                return False
            # "user initiated" includes NSActivityIdleSystemSleepDisabled: neither App Nap nor idle sleep
            opts = getattr(Foundation, "NSActivityUserInitiated", 0x00FFFFFF)
            return Foundation.NSProcessInfo.processInfo().beginActivityWithOptions_reason_(opts, "Recording")
        if sys.platform == "win32":
            import ctypes
            es_continuous, es_system_required = 0x80000000, 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | es_system_required)
            return True
        return False

    @staticmethod
    def _end(held) -> None:
        if not held:
            return
        if sys.platform == "darwin":
            import Foundation
            Foundation.NSProcessInfo.processInfo().endActivity_(held)
        elif sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)          # ES_CONTINUOUS: back to normal


class Engine:
    """The app in desktop mode with its main web server on 127.0.0.1, in a background thread.

    `port`: None = the port of the last run if it is free (the interface keeps theme and view in the browser storage
    of its address, which includes the port), else any free port; a number = exactly that one (0 = any)."""

    def __init__(self, cfg, port: int | None = None):
        from . import network, web

        self.cfg = cfg
        self.app = web.create_app(cfg, desktop=True)
        self.tap = _LanguageTap(self.app)
        db = self.app.state.db
        remember = port is None
        if remember:
            try:
                port = int(db.setting(S_PORT) or 0)
            except ValueError:
                port = 0
        try:
            sock = network.bind_socket("127.0.0.1", port)
        except OSError:
            if not remember:
                raise
            sock = network.bind_socket("127.0.0.1", 0)
        self.server = network.ServerThread(self.tap, sock, lifespan=True, name="vinylcheck-http")
        if remember:
            db.set_setting(S_PORT, str(self.server.port))
        self.url = f"http://127.0.0.1:{self.server.port}/"
        self.awake = KeepAwake(lambda: self.app.state.recorder.active)

    def start(self) -> None:
        self.server.start()
        self.app.state.network.start_lan()          # network access, if it was on at the last run
        self.awake.start()

    def stop(self) -> None:
        try:
            self.awake.stop()
            self.app.state.network.stop_lan()
        finally:
            try:
                self.server.stop()                  # the app's shut-down stops the recorder and closes the worker
            finally:
                # Again, in case the server never got to its shut-down (connections that do not close): the analysis
                # process must end before the app exits. A second close() returns at once.
                self.app.state.worker.close()

    def busy(self) -> bool:
        """Is a recording or an analysis running (closing the window would interrupt it)?"""
        ws = self.app.state.worker.status()
        return bool(self.app.state.recorder.active or ws["current"] or ws["queued"])


def shutdown(engine: Engine, timeout: float = STOP_TIMEOUT_S) -> bool:
    """Stop the engine (web server, network access, keep-awake, analysis process) and wait at most `timeout` seconds.

    The stop runs in a helper thread, so a part that hangs cannot keep the app open after its window has closed;
    _exit() then ends the process regardless. Returns True if everything stopped in time."""
    done = threading.Event()

    def stop() -> None:
        try:
            engine.stop()
        except Exception:  # noqa: BLE001 — the app is quitting either way
            log.exception("Stopping the engine failed")
        finally:
            done.set()

    threading.Thread(target=stop, daemon=True, name="vinylcheck-stop").start()
    if done.wait(timeout):
        return True
    log.warning("The engine did not stop within %.0f s; quitting anyway", timeout)
    return False


# ─── Window ─────────────────────────────────────────────────────────────────

def _import_webview():
    """pywebview, or None if it is missing or has no usable toolkit (then the browser takes over)."""
    try:
        import webview
        return webview
    except Exception as e:  # noqa: BLE001 — ImportError, or a toolkit that fails to load
        log.warning("No native window (pywebview not available: %s); opening the browser instead", e)
        return None


def _system_lang() -> str:
    """The system's language for the start page (the interface itself follows its own setting later)."""
    from . import i18n

    cand: list[str] = []
    if sys.platform == "darwin":
        try:
            import Foundation
            cand += [str(x) for x in Foundation.NSLocale.preferredLanguages()]
        except Exception:  # noqa: BLE001 — pyobjc missing: the environment decides
            pass
    import locale
    cand += [(locale.getlocale()[0] or "").replace("_", "-"), os.environ.get("LANG", "").split(".")[0].replace("_", "-")]
    return i18n.negotiate(",".join(c for c in cand if c))


def _page(title: str, body: str, spinner: bool = False) -> str:
    """A small page of its own for the window before the interface is there (starting, or why it did not start)."""
    spin = '<div class="spin"></div>' if spinner else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
    html,body{{margin:0;height:100%;background:#141516;color:#e8e6e3;font:15px/1.5 -apple-system,"Segoe UI",system-ui,sans-serif}}
    main{{max-width:640px;margin:0 auto;padding:12vh 24px 24px}} h1{{font-size:22px;margin:0 0 .6em}}
    code{{background:#24262a;padding:.15em .4em;border-radius:4px;word-break:break-all}} pre{{white-space:pre-wrap;
    background:#24262a;padding:.8em;border-radius:6px;font-size:13px;max-height:40vh;overflow:auto}}
    button{{font:inherit;background:#b7382c;color:#fff;border:0;border-radius:6px;padding:.5em 1em;cursor:pointer}}
    .spin{{width:28px;height:28px;border:3px solid #444;border-top-color:#b7382c;border-radius:50%;
    animation:r 1s linear infinite;margin-bottom:1em}} @keyframes r{{to{{transform:rotate(360deg)}}}}
    </style></head><body><main>{spin}<h1>{title}</h1>{body}</main></body></html>"""


class _WindowApi:
    """Functions the start and error pages may call (pywebview js_api)."""

    def __init__(self, log_file: Path):
        self._log_file = log_file

    def reveal_log(self) -> None:
        """Show the log file in the Finder or Explorer (its folder elsewhere)."""
        import subprocess
        f = self._log_file
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(f)])
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", f"/select,{f}"])
        else:
            subprocess.Popen(["xdg-open", str(f.parent)])


STARTING_SLOW_S = 45.0      # after this long without the interface, the start page says where the log is


def _show_window(webview, cfg, log_file: Path) -> Engine | None:
    """Open the window at once with a start page, start the engine meanwhile and then show the interface; if the
    engine does not start, the window says why and where the log is (a bouncing icon tells nobody anything).
    Returns when the window is closed, with the engine (None if it never started)."""
    from html import escape

    from . import i18n

    lang = _system_lang()
    tt = lambda key, **kw: escape(i18n.t(f"desktop.{key}", lang, **kw))   # noqa: E731
    log_html = f"<p>{tt('log_at')}<br><code>{escape(str(log_file))}</code></p>" \
               f"<p><button onclick=\"pywebview.api.reveal_log()\">{tt('show_log')}</button></p>"

    for key, value in (("ALLOW_DOWNLOADS", True),              # PDF reports and WAV exports are download links
                       ("OPEN_EXTERNAL_LINKS_IN_BROWSER", True)):  # Discogs pages and the like open in the browser
        try:
            webview.settings[key] = value
        except (AttributeError, KeyError, TypeError):            # older pywebview without this setting
            pass
    window = webview.create_window(APP_TITLE, html=_page(tt("starting"), "", spinner=True), js_api=_WindowApi(log_file),
                                   width=WINDOW_SIZE[0], height=WINDOW_SIZE[1], min_size=MIN_SIZE, text_select=True,
                                   background_color="#141516")
    state: dict = {"engine": None, "failed": False}

    def boot():
        t0 = time.time()
        slow = threading.Timer(STARTING_SLOW_S, lambda: state["engine"] is None and not state["failed"] and window.load_html(
            _page(tt("starting"), f"<p>{tt('starting_slow')}</p>{log_html}", spinner=True)))
        slow.daemon = True
        slow.start()
        try:
            engine = Engine(cfg)
            engine.start()
        except Exception as e:  # noqa: BLE001 — whatever it is, the window must say it
            state["failed"] = True
            log.exception("vinyl-check did not start")
            window.load_html(_page(tt("start_failed"), f"<pre>{escape(f'{type(e).__name__}: {e}')}</pre>"
                                                       f"<p>{tt('send_log')}</p>{log_html}"))
            return
        finally:
            slow.cancel()
        state["engine"] = engine
        log.info("vinyl-check %s at %s after %.1f s (recordings in %s, database in %s)", __version__, engine.url,
                 time.time() - t0, cfg.data_dir, cfg.state_dir)
        window.load_url(engine.url)

    def on_closing():
        # Ask only while something would be interrupted, in the language of the interface
        engine = state["engine"]
        busy = bool(engine and engine.busy())
        window.confirm_close = busy
        if busy and isinstance(getattr(window, "localization", None), dict):
            ui_lang = engine.tap.lang or i18n.DEFAULT_LANG
            window.localization.update({"global.quitConfirmation": i18n.t("desktop.quit_busy", ui_lang),
                                        "global.quit": i18n.t("desktop.quit", ui_lang),
                                        "global.cancel": i18n.t("desktop.cancel", ui_lang)})

    window.events.closing += on_closing
    # private_mode=False keeps cookies and local storage (language, theme, current view) between runs
    webview.start(boot, private_mode=False, storage_path=str(cfg.state_dir / "webview"))
    return state["engine"]


def _wait_until_stopped() -> None:
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except (ValueError, OSError):                            # not the main thread
            pass
    while not stop.wait(1.0):                                    # short waits: Windows delivers Ctrl+C in between
        pass


def setup_logging(log_dir: Path | None, level: int = logging.INFO) -> None:
    """Log to stderr (when there is one) and to a rotating file: a windowed app has no console to look at."""
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            handlers.append(RotatingFileHandler(log_dir / "vinyl-check.log", maxBytes=2_000_000, backupCount=3,
                                                encoding="utf-8"))
        except OSError:
            pass
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers,
                        force=True)


def run(browser: bool = False) -> int:
    """The desktop app: engine, window, clean stop. Returns the exit code."""
    from .config import load, load_user

    # The desktop app uses the user's configuration even on a computer that also has a service installed; an explicit
    # VINYLCHECK_CONFIG is still honoured
    cfg = load() if os.environ.get("VINYLCHECK_CONFIG") else load_user()
    cfg = replace(cfg, host="127.0.0.1")
    setup_logging(cfg.state_dir / "logs")
    log.info("vinyl-check %s starting (%s, Python %s)", __version__, sys.platform, sys.version.split()[0])
    webview = None if browser else _import_webview()
    if webview is not None:
        # the window comes first and starts the engine itself, so a start that fails or hangs is seen
        engine = _show_window(webview, cfg, cfg.state_dir / "logs" / "vinyl-check.log")
        if engine is not None:
            log.info("Stopping")
            shutdown(engine)
        return 0
    engine = Engine(cfg)
    engine.start()
    log.info("vinyl-check %s at %s (recordings in %s, database in %s)", __version__, engine.url, cfg.data_dir,
             cfg.state_dir)
    try:
        import webbrowser
        webbrowser.open(engine.url)
        print(f"vinyl-check {__version__} is running at {engine.url}; stop it with Ctrl+C", flush=True)
        _wait_until_stopped()
    finally:
        log.info("Stopping")
        shutdown(engine)
    return 0


# ─── Self-test ──────────────────────────────────────────────────────────────

def _http(base: str, path: str, body: dict | None = None, timeout: float = 30.0) -> tuple[int, bytes, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method="POST" if data is not None else "GET",
                                 headers={"Content-Type": "application/json", "Accept-Language": "en"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def _gui_check() -> str:
    """Load the window toolkit the app would use, without opening a window."""
    import webview  # noqa: F401
    if sys.platform == "darwin":
        import webview.platforms.cocoa  # noqa: F401
        return "Cocoa / WKWebView"
    if sys.platform == "win32":
        import webview.platforms.winforms  # noqa: F401
        return "WinForms / WebView2"
    return "pywebview"


class _Skip(Exception):
    """A self-test check that does not apply here (reported as "skip", not as a failure)."""


def _audio_check() -> str:
    """Load PortAudio through sounddevice, as the capture backend does, and count the input devices. In a built app
    a missing library is a failure; from source without sounddevice the check is skipped."""
    import importlib.util
    frozen = getattr(sys, "frozen", False)
    if sys.platform == "win32" and frozen:
        # the built app must also carry the ASIO build of PortAudio; set only in the app's own self-test process
        os.environ.setdefault("SD_ENABLE_ASIO", "1")
    backend = importlib.util.find_spec("vinylcheck.capture_portaudio") is not None
    if backend:
        importlib.import_module("vinylcheck.capture_portaudio")
    try:
        import sounddevice as sd
    except (ImportError, OSError) as e:          # OSError: the PortAudio library itself is missing (Linux)
        if frozen and (backend or isinstance(e, OSError)):
            raise
        raise _Skip(f"not available from this installation ({e})") from None
    apis = [h["name"] for h in sd.query_hostapis()]
    inputs = [d for d in sd.query_devices() if d["max_input_channels"] > 0]
    return f"{sd.get_portaudio_version()[1]}; {', '.join(apis)}; {len(inputs)} input device(s)"


def selftest(output: str | None = None, timeout: float = 240.0) -> int:
    """Run the checks described in the module docstring. Returns 0 if all passed, else 1."""
    from .config import Config

    results: list[tuple[bool | None, str, str]] = []

    def check(name: str, fn) -> None:
        t0 = time.monotonic()
        try:
            detail = fn()
            results.append((True, name, f"{detail} ({time.monotonic() - t0:.1f} s)" if detail else ""))
        except _Skip as e:
            results.append((None, name, str(e)))
        except Exception as e:  # noqa: BLE001 — every failure is reported, the remaining checks still run
            results.append((False, name, f"{type(e).__name__}: {e}"))

    def skip(name: str, why: str) -> None:
        results.append((None, name, why))

    tmp = Path(tempfile.mkdtemp(prefix="vinyl-check-selftest-"))
    engine = None
    try:
        import soundfile as sf

        from . import synth
        audio, _ = synth.make_side(duration_s=20, seed=3, clicks_per_min=30)
        wav = tmp / "side.wav"
        sf.write(wav, audio, 48000, subtype="PCM_16")
        cfg = replace(Config(), data_dir=tmp / "data", state_dir=tmp / "state", host="127.0.0.1",
                      fake_source=str(wav), fake_speed=100.0, min_free_gb=0.0)
        engine = Engine(cfg, port=0)
        engine.start()
        base = engine.url.rstrip("/")

        def health():
            st, body, _ = _http(base, "/api/health")
            d = json.loads(body)
            assert st == 200 and d.get("ok") and d.get("version") == __version__, (st, d)
            return f"version {d['version']}"

        def index():
            st, body, _ = _http(base, "/")
            assert st == 200 and b"vinyl-check" in body and b"app.js" in body, st
            st, body, _ = _http(base, "/i18n/catalog.js")
            assert st == 200 and body.startswith(b"window.VC_I18N="), st
            st, body, _ = _http(base, "/style.css")
            assert st == 200 and body, st
            return "index.html, catalog, style sheet"

        def websocket():
            from websockets.sync.client import connect
            with connect(base.replace("http://", "ws://") + "/api/ws", open_timeout=10) as ws:
                d = json.loads(ws.recv(timeout=10))
            assert d.get("version") == __version__, d
            return "status received"

        rec_id: dict = {}

        def analysis():
            st, body, _ = _http(base, "/api/records", {"artist": "Self-test", "title": "Synthetic side"})
            assert st == 200, (st, body[:200])
            rid = json.loads(body)["id"]
            st, body, _ = _http(base, "/api/recordings/start", {"record_id": rid, "side": "A", "kind": "before"})
            assert st == 200, (st, body[:200])
            rec_id["id"] = json.loads(body)["recording_id"]
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                r = json.loads(_http(base, f"/api/recordings/{rec_id['id']}")[1])
                if r["status"] in ("analyzed", "error"):
                    break
                time.sleep(0.3)
            assert r["status"] == "analyzed", f"status {r['status']}: {r.get('error')}"
            g = r["analysis"]["grading"]["grade"]
            assert g, r["analysis"]["grading"]
            return f"grade {g}, computed in the worker process"

        def pdf():
            assert rec_id, "no analysed recording"
            st, body, _ = _http(base, f"/api/recordings/{rec_id['id']}/report.pdf", timeout=120)
            assert st == 200 and body.startswith(b"%PDF"), st
            assert b"IBMPlexSans" in body, "IBM Plex not embedded (Helvetica fallback)"
            return f"{len(body) // 1024} KB, IBM Plex embedded"

        def net():
            st, body, _ = _http(base, "/api/network")
            d = json.loads(body)
            assert st == 200 and d["desktop"] and d["local"] and d["code"], d
            from . import network
            assert network.qr_svg("http://192.0.2.1:8080/") is not None, "segno missing: no QR code"
            return "access code and QR code"

        def https():
            # A packaged app on macOS or Windows must use the system's trusted certificates: its own OpenSSL points to
            # a directory of the build machine (that is how every HTTPS connection once failed on users' Macs while
            # CI, which has that directory, passed). Then one real connection to the project's hub.
            from . import tls, vinylmind
            if getattr(sys, "frozen", False) and sys.platform in ("darwin", "win32"):
                assert tls.source() == "system", f"certificates from {tls.source()}, not from the system"
            req = urllib.request.Request(vinylmind.DEFAULT_VINYL_MIND_URL + "/v1/server",
                                         headers={"User-Agent": f"vinyl-check/{__version__} selftest"})
            try:
                with urllib.request.urlopen(req, timeout=20, context=tls.context()) as r:
                    assert r.status == 200, r.status
            except urllib.error.URLError as e:
                if isinstance(e.reason, ssl.SSLError):
                    raise
                raise _Skip(f"no network ({e.reason}), certificates from {tls.source()}") from None
            return f"certificates from {tls.source()}, hub reachable"

        check("health", health)
        check("interface", index)
        check("websocket", websocket)
        check("analysis", analysis)
        check("pdf report", pdf)
        check("network access", net)
        check("https", https)
        check("audio (PortAudio)", _audio_check)
        if getattr(sys, "frozen", False) or _import_webview() is not None:
            check("window toolkit", _gui_check)
        else:
            skip("window toolkit", "pywebview not installed")
    except Exception as e:  # noqa: BLE001
        results.append((False, "engine", f"{type(e).__name__}: {e}"))
    finally:
        if engine is not None:
            try:
                engine.stop()
            except Exception as e:  # noqa: BLE001
                results.append((False, "stop", f"{type(e).__name__}: {e}"))
        shutil.rmtree(tmp, ignore_errors=True)

    passed = all(ok is not False for ok, _, _ in results) and any(ok for ok, _, _ in results)
    mark = {True: "ok  ", False: "FAIL", None: "skip"}
    lines = [f"vinyl-check {__version__} self-test ({sys.platform}, Python {sys.version.split()[0]}"
             f"{', frozen' if getattr(sys, 'frozen', False) else ''})"]
    lines += [f"{mark[ok]} {name}{': ' + detail if detail else ''}" for ok, name, detail in results]
    lines.append("self-test passed" if passed else "self-test FAILED")
    text = "\n".join(lines)
    print(text, flush=True)
    if output:
        Path(output).write_text(text + "\n", encoding="utf-8")
    return 0 if passed else 1


# ─── Entry point ────────────────────────────────────────────────────────────

def _ensure_streams() -> None:
    """A windowed app on Windows has no console: sys.stdout/stderr are None, and writing to them would fail."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def _exit(code: int) -> None:
    """End the process at once: the fallback after shutdown(), which has normally closed the analysis process already.
    Whatever still runs after a hang (a child process, a thread) must not keep the app alive; concurrent.futures would
    otherwise wait for a running analysis at interpreter exit (it is queued again at the next start)."""
    for child in multiprocessing.active_children():
        child.terminate()
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (AttributeError, OSError, ValueError):
            pass
    os._exit(code)


def main(argv: list[str] | None = None) -> None:
    multiprocessing.freeze_support()            # in a frozen app this turns the process into the worker child
    _ensure_streams()
    p = argparse.ArgumentParser(prog="vinyl-check-desktop", description="vinyl-check as a desktop app.")
    p.add_argument("--browser", action="store_true", help="open the interface in the default browser, no window")
    p.add_argument("--selftest", action="store_true", help="check the installation without a window and exit")
    p.add_argument("--output", metavar="FILE", help="with --selftest: also write the result to FILE")
    p.add_argument("--version", action="version", version=f"vinyl-check {__version__}")
    args = p.parse_args(argv)
    if args.selftest:
        setup_logging(None, logging.WARNING)
        code = selftest(args.output)
    else:
        code = run(browser=args.browser)
    _exit(code)


if __name__ == "__main__":
    main()

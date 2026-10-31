"""Desktop app: user directories per platform, configuration without a service, no idle restart, the engine on
127.0.0.1, the window (pywebview replaced by a stand-in), the browser fallback, a clean shutdown of the analysis
process, the self-test and the bundled fonts."""

import json
import multiprocessing
import os
import subprocess
import sys
import threading
import time
import types
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vinylcheck import config, desktop, jobs, report_pdf
from vinylcheck.config import Config
from vinylcheck.db import DB
from vinylcheck.web import create_app

ROOT = Path(__file__).resolve().parents[1]


def _cfg(tmp_path, **kw):
    return replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state", min_free_gb=0.0, **kw)


def _local(app):
    """The app as seen from this computer (the test client itself reports the client address "testclient")."""
    async def wrapped(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = dict(scope, client=("127.0.0.1", 50000))
        await app(scope, receive, send)
    return wrapped


def _health(url):
    with urllib.request.urlopen(url + "api/health", timeout=5) as r:
        return json.loads(r.read())


# ─── Paths and configuration ────────────────────────────────────────────────

def test_user_dirs_per_platform(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sys, "platform", "darwin")
    d = tmp_path / "Library" / "Application Support" / "vinyl-check"
    assert config.user_dirs() == (d, d)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert config.user_dirs() == (tmp_path / "Local" / "vinyl-check",) * 2
    monkeypatch.delenv("LOCALAPPDATA")
    assert config.user_dirs()[1] == tmp_path / "AppData" / "Local" / "vinyl-check"

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert config.user_dirs() == (tmp_path / ".config" / "vinyl-check", tmp_path / ".local" / "share" / "vinyl-check")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    assert config.user_dirs() == (tmp_path / "c" / "vinyl-check", tmp_path / "d" / "vinyl-check")


@pytest.fixture()
def desktop_env(tmp_path, monkeypatch):
    """No service configuration; the user's directories are inside tmp_path."""
    conf, data = tmp_path / "conf", tmp_path / "userdata"
    monkeypatch.delenv("VINYLCHECK_CONFIG", raising=False)
    for v in ("VINYLCHECK_DATA_DIR", "VINYLCHECK_STATE_DIR", "VINYLCHECK_PORT"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(config, "SERVICE_CONFIG", tmp_path / "etc" / "config.toml")
    monkeypatch.setattr(config, "user_dirs", lambda: (conf, data))
    return conf, data


def test_load_without_service_uses_user_dirs(desktop_env, monkeypatch):
    conf, data = desktop_env
    assert config.config_file() == (conf / "config.toml", False)
    cfg = config.load()
    assert cfg.data_dir == data and cfg.state_dir == data and cfg.db_path == data / "vinyl-check.sqlite"
    assert cfg.rec_dir == data / "recordings"
    conf.mkdir()
    (conf / "config.toml").write_text('port = 9001\nstate_dir = "/elsewhere/state"\n')
    cfg = config.load()
    assert cfg.port == 9001 and cfg.state_dir == Path("/elsewhere/state") and cfg.data_dir == data
    monkeypatch.setenv("VINYLCHECK_DATA_DIR", "/override")
    assert config.load().data_dir == Path("/override")                     # the environment still wins


def test_service_paths_unchanged(desktop_env, tmp_path, monkeypatch):
    """With a service configuration nothing moves: the systemd unit sets VINYLCHECK_CONFIG."""
    service_file = tmp_path / "service.toml"
    service_file.write_text("port = 8080\n")
    monkeypatch.setenv("VINYLCHECK_CONFIG", str(service_file))
    assert config.config_file() == (service_file, True)
    cfg = config.load()
    assert cfg.data_dir == Path("/mnt/data/vinyl-check") and cfg.state_dir == Path("/var/lib/vinyl-check")
    assert cfg.host == "0.0.0.0" and cfg.access_code == ""
    assert config.load(str(service_file)).state_dir == Path("/var/lib/vinyl-check")   # explicit path: as before


@pytest.mark.skipif(sys.platform == "win32", reason="/etc is not looked for on Windows")
def test_etc_file_means_service(desktop_env, tmp_path, monkeypatch):
    conf, data = desktop_env
    etc = tmp_path / "etc" / "config.toml"
    etc.parent.mkdir()
    etc.write_text("port = 8090\n")
    assert config.config_file() == (etc, True)
    cfg = config.load()
    assert cfg.port == 8090 and cfg.state_dir == Path("/var/lib/vinyl-check")
    user = config.load_user()                                   # the desktop app keeps to the user's directories
    assert user.state_dir == data and user.port == 8080


# ─── Desktop mode of the app ────────────────────────────────────────────────

def test_no_idle_restart_in_desktop_app(tmp_path):
    cfg = _cfg(tmp_path)
    app = create_app(cfg, desktop=True)
    app.state.restart["poll"] = 0.05
    app.state.restart["do"] = lambda: (_ for _ in ()).throw(AssertionError("the desktop app must not restart"))
    with TestClient(_local(app), base_url="http://127.0.0.1:8123") as c:
        (cfg.state_dir / "restart-when-idle").touch()
        assert c.get("/api/status").json()["restart_pending"] is False
        time.sleep(0.3)
        info = c.get("/api/settings").json()["info"]
        assert info["desktop"] is True and info["config_file"].endswith("config.toml")


def test_engine_starts_and_stops(tmp_path):
    cfg = _cfg(tmp_path)
    eng = desktop.Engine(cfg)
    eng.start()
    try:
        assert eng.url.startswith("http://127.0.0.1:") and _health(eng.url)["ok"]
        assert not eng.busy()
    finally:
        eng.stop()
    assert not eng.server.running
    with pytest.raises(OSError):
        _health(eng.url)
    # the next start takes the same port: the interface keeps its local storage (theme, view)
    again = desktop.Engine(cfg)
    try:
        if sys.platform != "win32":
            assert again.url == eng.url
    finally:
        again.server.sock.close()


def test_keep_awake_while_recording(monkeypatch):
    state = {"active": False}
    calls = []
    monkeypatch.setattr(desktop.KeepAwake, "POLL_S", 0.02)
    monkeypatch.setattr(desktop.KeepAwake, "_begin", staticmethod(lambda: calls.append("begin") or "token"))
    monkeypatch.setattr(desktop.KeepAwake, "_end", staticmethod(lambda held: calls.append(("end", held))))
    awake = desktop.KeepAwake(lambda: state["active"])
    awake.start()

    def settle():
        time.sleep(0.15)
    settle()
    assert calls == []
    state["active"] = True
    settle()
    assert calls == ["begin"]                                      # once per recording, not per poll
    state["active"] = False
    settle()
    assert calls == ["begin", ("end", "token")]
    state["active"] = True
    settle()
    awake.stop()                                                   # stopping during a recording releases it too
    assert calls[-1] == ("end", "token") and calls.count("begin") == 2


def test_keep_awake_system_calls():
    """The real calls of this platform can be made and undone (no-op where there is nothing to do)."""
    held = desktop.KeepAwake._begin()
    desktop.KeepAwake._end(held)


class _FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self


def _fake_webview(on_start):
    """Stand-in for pywebview: records the window, calls `on_start(window)` instead of running a GUI loop."""
    wv = types.ModuleType("webview")
    wv.settings = {"ALLOW_DOWNLOADS": False, "OPEN_EXTERNAL_LINKS_IN_BROWSER": False}
    wv.calls = {}

    def create_window(title, url, **kw):
        win = types.SimpleNamespace(title=title, url=url, kw=kw, confirm_close=False,
                                    localization={"global.quitConfirmation": "Do you really want to quit?"},
                                    events=types.SimpleNamespace(closing=_FakeEvent()))
        wv.calls["window"] = win
        return win

    def start(**kw):
        wv.calls["start"] = kw
        on_start(wv.calls["window"])

    wv.create_window, wv.start = create_window, start
    return wv


def test_run_shows_window_and_stops_engine(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(config, "load_user", lambda: cfg)
    monkeypatch.delenv("VINYLCHECK_CONFIG", raising=False)
    seen = {}

    def on_start(win):
        seen["health"] = _health(win.url)                         # the engine serves while the window is open
        for h in win.events.closing.handlers:                    # idle: closing needs no confirmation
            h()
        seen["idle_confirm"] = win.confirm_close
        monkeypatch.setattr(desktop.Engine, "busy", lambda self: True)
        for h in win.events.closing.handlers:                    # busy: ask, in the interface's language
            h()
        seen["busy_confirm"] = win.confirm_close
        seen["text"] = win.localization["global.quitConfirmation"]

    wv = _fake_webview(on_start)
    monkeypatch.setitem(sys.modules, "webview", wv)
    monkeypatch.setattr(desktop, "setup_logging", lambda *a, **k: None)
    assert desktop.run() == 0
    win = wv.calls["window"]
    assert win.title == "vinyl-check" and win.url.startswith("http://127.0.0.1:")
    assert win.kw["min_size"] == desktop.MIN_SIZE
    assert wv.calls["start"]["private_mode"] is False
    assert wv.calls["start"]["storage_path"] == str(cfg.state_dir / "webview")
    assert wv.settings["ALLOW_DOWNLOADS"] is True
    assert seen["health"]["ok"] and seen["idle_confirm"] is False and seen["busy_confirm"] is True
    assert "recording or an analysis is running" in seen["text"]
    with pytest.raises(OSError):                                  # the window is closed: so is the engine
        _health(win.url)


def test_browser_fallback(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(config, "load_user", lambda: cfg)
    monkeypatch.delenv("VINYLCHECK_CONFIG", raising=False)
    monkeypatch.setattr(desktop, "setup_logging", lambda *a, **k: None)
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    monkeypatch.setattr(desktop, "_wait_until_stopped", lambda: opened.append(_health(opened[0])))
    assert desktop.run(browser=True) == 0
    assert opened[0].startswith("http://127.0.0.1:") and opened[1]["ok"]


def test_main_parses_arguments(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(desktop, "selftest", lambda output=None: calls.append(("selftest", output)) or 0)
    monkeypatch.setattr(desktop, "run", lambda browser=False: calls.append(("run", browser)) or 0)
    monkeypatch.setattr(desktop, "setup_logging", lambda *a, **k: None)

    def fake_exit(code):
        raise SystemExit(code)
    monkeypatch.setattr(desktop, "_exit", fake_exit)
    with pytest.raises(SystemExit) as e:
        desktop.main(["--selftest", "--output", str(tmp_path / "r.txt")])
    assert e.value.code == 0 and calls[-1] == ("selftest", str(tmp_path / "r.txt"))
    with pytest.raises(SystemExit):
        desktop.main(["--browser"])
    assert calls[-1] == ("run", True)


# ─── Shutting down ──────────────────────────────────────────────────────────

def _alive(pid: int) -> bool:
    return any(p.pid == pid for p in multiprocessing.active_children())


def test_worker_close_idle_and_busy(tmp_path):
    """An idle analysis process ends by itself within the timeout; under a running job it is ended at once and the
    job sees an interruption, not a crash. Nothing is started after close()."""
    cfg = _cfg(tmp_path)
    w = jobs.Worker(cfg, DB(cfg.db_path))
    pid = w.run_heavy(os.getpid)
    assert _alive(pid)
    t0 = time.monotonic()
    assert w.close(timeout=5.0) is True
    assert time.monotonic() - t0 < 5.0 and w.pool is None and not _alive(pid)
    assert w.close() is True                                       # a second close returns at once
    with pytest.raises(jobs.ShuttingDown):
        w.run_heavy(os.getpid)

    busy = jobs.Worker(cfg, DB(cfg.db_path))
    pid = busy.run_heavy(os.getpid)
    seen = {}

    def job():
        try:
            busy.run_heavy(time.sleep, 60)
        except Exception as e:  # noqa: BLE001
            seen["error"] = e
    t = threading.Thread(target=job, daemon=True)
    t.start()
    time.sleep(0.5)                                                # the job is in the child process now
    busy.current = {"kind": "analyze", "id": 1, "since": time.time()}
    t0 = time.monotonic()
    assert busy.close(timeout=5.0) is False                        # terminated, not ended by itself
    assert time.monotonic() - t0 < 5.0
    t.join(5)
    assert isinstance(seen.get("error"), jobs.ShuttingDown) and not _alive(pid)


def test_interrupted_job_runs_again_at_the_next_start(tmp_path):
    """A job cut off by quitting keeps its state, so the next start queues it again instead of reporting an error."""
    cfg = _cfg(tmp_path)
    db = DB(cfg.db_path)
    w = jobs.Worker(cfg, db)
    rid = db.add_record(artist="A", title="B")
    rec = db.add_recording(record_id=rid, side="A", kind="before", path=str(tmp_path / "a.wav"), status="recorded")

    def analyze(rec_id):
        db.update_recording(rec_id, status="analyzing")
        w.run_heavy(time.sleep, 60)
    w._analyze = analyze
    w.start()
    w.analyze(rec)
    deadline = time.monotonic() + 30
    while not (w.current and w.pool) and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)
    assert w.close(timeout=5.0) is False
    deadline = time.monotonic() + 5
    while w.current and time.monotonic() < deadline:
        time.sleep(0.05)
    r = db.recording(rec)
    assert r["status"] == "analyzing" and not r.get("error")


class _HangingEngine:
    def __init__(self, hang: float = 0.0):
        self.hang, self.stopped = hang, threading.Event()

    def stop(self):
        time.sleep(self.hang)
        self.stopped.set()


def test_shutdown_returns_within_its_timeout():
    eng = _HangingEngine()
    assert desktop.shutdown(eng, timeout=5.0) is True and eng.stopped.is_set()
    eng = _HangingEngine(hang=30.0)
    t0 = time.monotonic()
    assert desktop.shutdown(eng, timeout=0.3) is False             # a hang does not keep the app open
    assert time.monotonic() - t0 < 2.0 and not eng.stopped.is_set()


def test_engine_shutdown_closes_the_worker(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    eng = desktop.Engine(cfg, port=0)
    eng.start()
    worker = eng.app.state.worker
    pid = worker.run_heavy(os.getpid)
    closes = []
    real_close = worker.close
    monkeypatch.setattr(worker, "close", lambda *a, **k: closes.append(time.monotonic()) or real_close(*a, **k))
    assert desktop.shutdown(eng) is True
    assert closes and worker.pool is None and not _alive(pid) and not eng.server.running


@pytest.mark.skipif(sys.platform == "win32", reason="named semaphores and the resource tracker are POSIX only")
def test_quitting_leaves_no_semaphores(tmp_path):
    """The symptom itself: after an analysis process has run, quitting must not make multiprocessing's resource
    tracker warn about leaked semaphores."""
    code = (
        "import os, sys\n"
        "from dataclasses import replace\n"
        "from pathlib import Path\n"
        "from vinylcheck import desktop\n"
        "from vinylcheck.config import Config\n"
        "tmp = Path(sys.argv[1])\n"
        "cfg = replace(Config(), data_dir=tmp / 'data', state_dir=tmp / 'state', host='127.0.0.1', min_free_gb=0.0)\n"
        "eng = desktop.Engine(cfg, port=0)\n"
        "eng.start()\n"
        "eng.app.state.worker.run_heavy(os.getpid)\n"
        "print('stopped' if desktop.shutdown(eng) else 'hung', flush=True)\n"
        "desktop._exit(0)\n")
    r = subprocess.run([sys.executable, "-c", code, str(tmp_path)], cwd=ROOT, capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0 and "stopped" in r.stdout, r.stderr
    assert "leaked semaphore" not in r.stderr and "resource_tracker" not in r.stderr, r.stderr


def test_selftest(tmp_path, capsys):
    """The check CI runs on the built apps, here from the source tree (the window toolkit is skipped without it)."""
    pytest.importorskip("segno")
    out = tmp_path / "selftest.txt"
    assert desktop.selftest(str(out)) == 0
    text = out.read_text(encoding="utf-8")
    assert "self-test passed" in text and "ok   analysis" in text and "IBM Plex embedded" in text
    assert "self-test passed" in capsys.readouterr().out


# ─── Fonts ──────────────────────────────────────────────────────────────────

def test_bundled_fonts_are_preferred(monkeypatch):
    bundled = Path(report_pdf.__file__).parent / "fonts"
    assert report_pdf.FONT_DIRS[1] == str(bundled)
    for name in ("IBMPlexSans-Regular", "IBMPlexSans-Medium", "IBMPlexSans-SemiBold", "IBMPlexSans-Bold",
                 "IBMPlexSans-Italic", "IBMPlexMono-Regular"):
        assert (bundled / f"{name}.ttf").is_file(), name
    assert "SIL Open Font License" in (bundled / "LICENSE-IBM-Plex.txt").read_text(encoding="utf-8")
    monkeypatch.setattr(report_pdf, "FONT_DIRS", ["", str(bundled), "/nonexistent"])
    report_pdf._register_fonts()
    assert report_pdf._fonts["regular"] == "IBMPlexSans-Regular" and report_pdf._fonts["mono"] == "IBMPlexMono-Regular"

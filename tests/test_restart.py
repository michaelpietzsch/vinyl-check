"""Restart when idle: the flag file restart-when-idle (set by deploy.sh) triggers the restart only once nothing runs."""
import os
import threading
import time
from dataclasses import replace

from fastapi.testclient import TestClient

from vinylcheck.config import Config
from vinylcheck.web import create_app


def _cfg(tmp_path):
    return replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state", min_free_gb=0.0)


def test_restart_fires_when_idle(tmp_path):
    cfg = _cfg(tmp_path)
    app = create_app(cfg)
    fired = []
    app.state.restart["poll"] = 0.05
    app.state.restart["quiet"] = 0.0
    app.state.restart["do"] = lambda: fired.append(time.time())
    with TestClient(app) as c:
        assert c.get("/api/status").json()["restart_pending"] is False
        (cfg.state_dir / "restart-when-idle").touch()
        assert c.get("/api/status").json()["restart_pending"] is True
        deadline = time.time() + 3
        while not fired and time.time() < deadline:
            time.sleep(0.02)
        assert len(fired) == 1
        time.sleep(0.2)
        assert len(fired) == 1                                   # only once, even if the flag stays


def test_restart_waits_for_a_quiet_interface(tmp_path):
    """A change made in the interface (any write request) postpones the restart by the quiet spell; reading does
    not. The status names the start time of the process, which changes with every restart."""
    cfg = _cfg(tmp_path)
    app = create_app(cfg)
    fired = []
    app.state.restart["poll"] = 0.05
    app.state.restart["quiet"] = 0.6
    app.state.restart["do"] = lambda: fired.append(time.time())
    with TestClient(app) as c:
        started = c.get("/api/status").json()["started_at"]
        assert started and c.get("/api/status").json()["started_at"] == started
        assert app.state.restart["last_write"] == 0.0                          # reading does not count as working
        assert c.post("/api/records", json={"title": "x"}).status_code == 200     # someone is working here
        wrote = app.state.restart["last_write"]
        assert wrote > 0
        for _ in range(3):
            c.get("/api/status")
        assert app.state.restart["last_write"] == wrote
        (cfg.state_dir / "restart-when-idle").touch()
        deadline = time.time() + 5
        while not fired and time.time() < deadline:
            time.sleep(0.02)
        # the restart came, but not before the quiet spell after the change (independent of the machine's speed)
        assert len(fired) == 1 and fired[0] - wrote >= 0.6


def test_stale_flag_is_ignored(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.state_dir.mkdir(parents=True)
    flag = cfg.state_dir / "restart-when-idle"
    flag.touch()
    old = time.time() - 600
    os.utime(flag, (old, old))                                   # flag left over from an earlier process
    app = create_app(cfg)
    app.state.restart["poll"] = 0.05
    app.state.restart["do"] = lambda: (_ for _ in ()).throw(AssertionError("must not fire"))
    with TestClient(app) as c:
        assert c.get("/api/status").json()["restart_pending"] is False
        time.sleep(0.3)


def test_restart_waits_for_a_move_of_the_user_data_folder(tmp_path):
    cfg = _cfg(tmp_path)
    app = create_app(cfg)
    fired, moving = [], threading.Event()
    app.state.restart["poll"] = 0.05
    app.state.restart["quiet"] = 0.0
    app.state.restart["do"] = lambda: fired.append(time.time())
    app.state.mover._thread = threading.Thread(target=moving.wait, args=(10,), daemon=True)
    app.state.mover._thread.start()
    with TestClient(app):
        (cfg.state_dir / "restart-when-idle").touch()
        time.sleep(0.4)
        assert not fired                                         # the move runs: no restart
        moving.set()
        deadline = time.time() + 3
        while not fired and time.time() < deadline:
            time.sleep(0.02)
        assert len(fired) == 1

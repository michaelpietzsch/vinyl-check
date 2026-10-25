"""Restart when idle: the flag file restart-when-idle (set by deploy.sh) triggers the restart only once nothing runs."""
import os
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

"""Is there a newer version (updates.py): comparing versions, reading GitHub's answer, the switch, the status."""
import json
import time
import urllib.request
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from vinylcheck import __version__, updates
from vinylcheck.config import Config
from vinylcheck.db import DB
from vinylcheck.web import create_app


def test_versions():
    assert updates.parse("v1.10.2") == (1, 10, 2) and updates.parse("1.2") is None and updates.parse("v2.0.0-rc1") is None
    assert updates.newer("1.10.0", "1.9.9") and not updates.newer("1.2.0", "1.2.0") and not updates.newer("1.1.9", "1.2.0")
    assert not updates.newer("garbage", "1.0.0")


class Answer:
    def __init__(self, data):
        self.data = json.dumps(data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self.data


def test_fetch_reads_the_latest_release(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None, context=None):
        seen["url"], seen["ua"], seen["context"] = req.full_url, req.get_header("User-agent"), context
        return Answer({"tag_name": "v9.8.7", "html_url": "https://github.com/michaelpietzsch/vinyl-check/releases/tag/v9.8.7",
                       "published_at": "2026-11-01T10:00:00Z", "draft": False, "prerelease": False})
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    r = updates.fetch()
    assert r == {"latest": "9.8.7", "url": "https://github.com/michaelpietzsch/vinyl-check/releases/tag/v9.8.7",
                 "published_at": "2026-11-01T10:00:00Z"}
    assert seen["url"] == updates.LATEST_URL and seen["ua"] == f"vinyl-check/{__version__}" and seen["context"]


@pytest.mark.parametrize("data", [{"tag_name": "v9.0.0", "prerelease": True}, {"tag_name": "nightly"}, {}])
def test_fetch_refuses_what_is_no_release(monkeypatch, data):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None, context=None: Answer(data))
    with pytest.raises(ValueError):
        updates.fetch()


def test_a_foreign_link_is_replaced(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None, context=None:
                        Answer({"tag_name": "v9.0.0", "html_url": "https://evil.example/download"}))
    assert updates.fetch()["url"] == updates.RELEASES_PAGE


def test_checker_status_switch_and_memory(tmp_path):
    cfg = replace(Config(), data_dir=tmp_path / "d", state_dir=tmp_path / "s")
    cfg.state_dir.mkdir()
    db = DB(cfg.db_path)
    c = updates.Checker(cfg, db, fetch=lambda: {"latest": "99.0.0", "url": updates.RELEASES_PAGE, "published_at": None})
    assert c.status()["latest"] is None and not c.status()["available"]
    st = c.check()
    assert st["available"] and st["latest"] == "99.0.0" and st["checked_at"] <= time.time() and st["error"] is None
    assert updates.Checker(cfg, db).status()["latest"] == "99.0.0"          # kept across a restart
    object.__setattr__(cfg, "update_check", False)
    assert not c.status()["available"] and not c.status()["enabled"]

    def offline():
        raise OSError("no network")
    object.__setattr__(cfg, "update_check", True)
    st = updates.Checker(cfg, db, fetch=offline).check()
    assert st["error"] == "no network" and st["latest"] == "99.0.0"          # the last answer stays


def test_api_status_and_setting(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "fetch", lambda: {"latest": "99.0.0", "url": updates.RELEASES_PAGE, "published_at": None})
    cfg = replace(Config(), data_dir=tmp_path / "d", state_dir=tmp_path / "s", min_free_gb=0.0)
    app = create_app(cfg)
    app.state.updates._fetch = updates.fetch
    with TestClient(app) as c:
        assert c.get("/api/status").json()["update"]["available"] is False   # nothing asked yet
        assert c.post("/api/updates/check").json()["available"] is True
        assert c.get("/api/status").json()["update"]["latest"] == "99.0.0"
        assert c.put("/api/settings", json={"update_check": False}).status_code == 200
        assert c.get("/api/updates").json() | {"checked_at": None} == {
            "enabled": False, "current": __version__, "latest": "99.0.0", "available": False,
            "url": updates.RELEASES_PAGE, "published_at": None, "checked_at": None, "error": None}

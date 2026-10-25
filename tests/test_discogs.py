"""Discogs client: collection cache, search, writing the media condition back, the access token entered in the
settings (API faked)."""

import io
import json
import logging
import time
import urllib.error
import urllib.parse
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from vinylcheck import discogs, i18n
from vinylcheck.config import Config
from vinylcheck.web import create_app


def _item(rid, inst, artist, title, media, year=1988, catno="TL 001", added="2026-09-07T10:00:00-07:00"):
    return {"instance_id": inst, "folder_id": 1, "date_added": added,
            "basic_information": {"id": rid, "title": title, "year": year, "artists": [{"name": artist}],
                                  "labels": [{"name": "Test Label", "catno": catno}],
                                  "formats": [{"name": "Vinyl", "descriptions": ["LP", "Album"]}],
                                  "thumb": f"https://i.discogs.com/{rid}.jpg"},
            "notes": [{"field_id": 1, "value": media}, {"field_id": 2, "value": "Good (G)"},
                      {"field_id": 3, "value": "Test note"}]}


class FakeApi:
    """Stand-in for api.discogs.com: a collection on two pages, writing a field."""
    def __init__(self):
        self.calls = []
        self.media = {5605854: "Good (G)", 12345: "Near Mint (NM or M-)", 777: "Very Good (VG)"}

    def __call__(self, req, timeout=0):
        url = req.full_url
        self.calls.append((req.get_method(), url, dict(req.header_items()), req.data))
        assert dict(req.header_items()).get("Authorization") == "Discogs token=tok123"
        if "/collection/folders/0/releases" in url:
            page = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["page"][0])
            items = {1: [_item(5605854, 90001, "Test Artist", "First Album", self.media[5605854]),
                         _item(12345, 90002, "Test Artist", "Second Album", self.media[12345], 1979, "TL 002")],
                     2: [_item(777, 90003, "Ümlaut Bänd", "Third Album", self.media[777], 1980, "TL 003")]}[page]
            return io.BytesIO(json.dumps({"pagination": {"page": page, "pages": 2}, "releases": items}).encode())
        if "/fields/1" in url and req.get_method() == "POST":
            rid = int(url.split("/releases/")[1].split("/")[0])
            assert "/folders/1/" in url and f"/instances/{90001 if rid == 5605854 else 0}/" in url
            self.media[rid] = json.loads(req.data)["value"]
            return io.BytesIO(b"")
        raise AssertionError("unexpected URL " + url)


@pytest.fixture()
def api(monkeypatch):
    fake = FakeApi()
    monkeypatch.setattr(discogs.urllib.request, "urlopen", fake)
    return fake


def test_sync_search_entry_and_write(tmp_path, api):
    d = discogs.Discogs("tok123", "testuser", tmp_path / "coll.json", "test")
    assert d.enabled and d.stale() and d.status()["count"] == 0
    st = d.sync()
    assert st["count"] == 3 and not d.stale() and (tmp_path / "coll.json").exists()
    e = d.entry(5605854)
    assert e["media"] == "Good (G)" and e["instance_id"] == 90001
    assert e["catno"] == "TL 001" and e["format"] == "Vinyl (LP, Album)"
    assert d.entry(None, "test artist", "second album")["release_id"] == 12345
    hits = d.search("test artist first")
    assert hits[0]["release_id"] == 5605854 and hits[0]["media_condition"] == "Good (G)" and hits[0]["score"] == 1.0
    assert [h["release_id"] for h in d.search("umlaut band")] == [777]          # found without the umlauts
    assert d.search("") == [] and d.search("zzz") == []
    out = d.set_media_condition(5605854, "VG+")
    assert out == {"previous": "Good (G)", "current": "Very Good Plus (VG+)", "release_id": 5605854}
    assert api.media[5605854] == "Very Good Plus (VG+)" and d.entry(5605854)["media"] == "Very Good Plus (VG+)"
    with pytest.raises(discogs.DiscogsError):
        d.set_media_condition(999, "VG")
    with pytest.raises(discogs.DiscogsError) as ei:
        d.set_media_condition(5605854, "XX")
    assert ei.value.message == {"$t": "discogs.unknown_grade", "$p": {"grade": "XX"}}
    assert str(ei.value) == "Unknown grade 'XX'"
    d2 = discogs.Discogs("tok123", "testuser", tmp_path / "coll.json", "test")  # the cache survives a restart
    assert d2.status()["count"] == 3 and d2.entry(5605854)["media"] == "Very Good Plus (VG+)"
    assert discogs.condition_short("Near Mint (NM or M-)") == "NM" and discogs.condition_short(None) is None


def test_disabled_without_token(tmp_path):
    d = discogs.Discogs("", "x", tmp_path / "c.json", "t")
    assert not d.enabled and not d.stale() and d.sync_async() is False
    with pytest.raises(discogs.DiscogsError):
        d.sync()


def test_errors_are_messages_rendered_per_language(tmp_path, monkeypatch):
    """HTTP errors and network failures become i18n messages; the sync error is kept for the status."""
    def unauthorized(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"message": "no"}'))

    monkeypatch.setattr(discogs.urllib.request, "urlopen", unauthorized)
    d = discogs.Discogs("tok123", "testuser", tmp_path / "c.json", "t")
    with pytest.raises(discogs.DiscogsError) as ei:
        d.sync()
    assert ei.value.message["$t"] == "discogs.http_error" and ei.value.message["$p"]["code"] == 401
    assert str(ei.value) == "Discogs 401: token invalid or expired (Discogs → Settings → Developers)"
    with i18n.use_lang("de"):
        assert str(ei.value).startswith("Discogs 401: Token ungültig")
    assert d.status()["error"] == ei.value.message and not d.status()["syncing"]

    def offline(req, timeout=0):
        raise OSError("network down")

    monkeypatch.setattr(discogs.urllib.request, "urlopen", offline)
    with pytest.raises(discogs.DiscogsError) as ei:
        d.sync()
    assert str(ei.value) == "Discogs not reachable: network down"


def test_web_search_condition_and_writeback(tmp_path, api):
    cfg = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state", min_free_gb=0.0,
                  discogs_token="tok123", discogs_username="testuser")
    with TestClient(create_app(cfg)) as c:
        st = c.post("/api/discogs/sync").json()
        assert st["count"] == 3 and c.get("/api/status").json()["discogs_collection"]["count"] == 3
        hits = c.get("/api/discogs/search", params={"q": "second"}).json()
        assert hits[0]["release_id"] == 12345
        rid = c.post("/api/records", json={"artist": "Test Artist", "title": "First Album",
                                           "discogs_release_id": 5605854, "discogs_media_condition": "Good (G)"}).json()["id"]
        r = c.get(f"/api/records/{rid}/collection").json()
        assert r["media_condition"] == "Good (G)" and r["in_collection_since"].startswith("2026-09-07") and "sleeve" not in r
        assert c.get("/api/records/999/collection").status_code == 404
        s = c.get("/api/settings").json()["info"]["discogs"]
        assert "testuser" in s
    cfg2 = replace(cfg, discogs_token="")
    with TestClient(create_app(cfg2)) as c:
        assert c.get("/api/discogs/search", params={"q": "second"}).status_code == 404
        assert c.get("/api/discogs/status").json()["enabled"] is False
        assert c.get(f"/api/records/{rid}/collection").status_code == 404
        assert c.post(f"/api/records/{rid}/condition", json={"recording_id": 1}).status_code == 503


def test_oauth_flow_and_signed_requests(tmp_path, monkeypatch):
    """App keys → request token → authorization (verifier) → access token; then an OAuth header instead of a token."""
    seen = []

    def fake(req, timeout=0):
        url, auth = req.full_url, dict(req.header_items()).get("Authorization", "")
        seen.append((url, auth))
        if url.endswith("/oauth/request_token"):
            assert auth.startswith("OAuth ") and 'oauth_consumer_key="ck"' in auth and 'oauth_signature="cs%26"' in auth
            assert "oauth_callback=" in auth and 'oauth_signature_method="PLAINTEXT"' in auth
            return io.BytesIO(b"oauth_token=rt&oauth_token_secret=rs&oauth_callback_confirmed=true")
        if url.endswith("/oauth/access_token"):
            assert req.get_method() == "POST" and 'oauth_token="rt"' in auth and 'oauth_verifier="v123"' in auth and 'oauth_signature="cs%26rs"' in auth
            return io.BytesIO(b"oauth_token=at&oauth_token_secret=as")
        if url.endswith("/oauth/identity"):
            assert 'oauth_token="at"' in auth and 'oauth_signature="cs%26as"' in auth
            return io.BytesIO(json.dumps({"username": "testuser"}).encode())
        if "/collection/folders/0/releases" in url:
            assert 'oauth_token="at"' in auth
            return io.BytesIO(json.dumps({"pagination": {"pages": 1}, "releases": [_item(1, 2, "A", "B", "Good (G)")]}).encode())
        raise AssertionError(url)

    monkeypatch.setattr(discogs.urllib.request, "urlopen", fake)
    cfg = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state", min_free_gb=0.0,
                  discogs_consumer_key="ck", discogs_consumer_secret="cs")
    with TestClient(create_app(cfg)) as c:
        st = c.get("/api/discogs/status").json()
        assert st["enabled"] is False and st["oauth_ready"] is True
        r = c.post("/api/discogs/connect").json()
        assert r["authorize_url"].startswith("https://www.discogs.com/oauth/authorize?oauth_token=rt")
        bad = c.get("/api/discogs/callback", params={"oauth_token": "wrong", "oauth_verifier": "x"}, follow_redirects=False)
        assert bad.status_code == 400
        cb = c.get("/api/discogs/callback", params={"oauth_token": "rt", "oauth_verifier": "v123"}, follow_redirects=False)
        assert cb.status_code == 303 and cb.headers["location"] == "/#system"
        st = c.get("/api/discogs/status").json()
        assert st["enabled"] is True and st["username"] == "testuser" and st["via"] == "oauth"
        assert c.post("/api/discogs/sync").json()["count"] == 1
    with TestClient(create_app(cfg)) as c:                          # the authorization survives a restart
        assert c.get("/api/discogs/status").json()["enabled"] is True
        assert c.post("/api/discogs/disconnect").json()["enabled"] is False


# ─── Access token entered in the settings ───────────────────────────────────
UI_TOKEN = "UiTok0123456789abcdefghijklmnopqrstuvWXY"          # 40 characters, like the tokens Discogs issues
CFG_TOKEN = "CfgTok0123456789abcdefghijklmnopqrstuvwx"


class AccountApi:
    """Stand-in for api.discogs.com with two accounts: identity by token, a one-record collection per account and the
    public release data (cover)."""
    users = {UI_TOKEN: "testuser", CFG_TOKEN: "configuser"}

    def __init__(self):
        self.calls = []

    def __call__(self, req, timeout=0):
        url, auth = req.full_url, dict(req.header_items()).get("Authorization", "")
        self.calls.append((url, auth))
        if url.startswith("https://api.discogs.com/releases/"):
            return io.BytesIO(json.dumps({"images": [{"type": "primary", "uri": "https://i.discogs.com/c.jpg",
                                                      "uri150": "https://i.discogs.com/c150.jpg"}]}).encode())
        user = self.users.get(auth.removeprefix("Discogs token="))
        if user is None:
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(b'{"message": "authenticate"}'))
        if url.endswith("/oauth/identity"):
            return io.BytesIO(json.dumps({"id": 1, "username": user}).encode())
        if f"/users/{user}/collection/folders/0/releases" in url:
            item = _item(42 if user == "testuser" else 43, 9, user, "Album", "Good (G)")
            return io.BytesIO(json.dumps({"pagination": {"pages": 1}, "releases": [item]}).encode())
        raise AssertionError(url)


def _local(app):
    """The app as seen from this computer (the desktop app lets only the computer itself in without a code)."""
    async def wrapped(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = dict(scope, client=("127.0.0.1", 50000))
        await app(scope, receive, send)
    return wrapped


def _wait_synced(c, user):
    deadline = time.time() + 10
    while time.time() < deadline:
        st = c.get("/api/discogs/status").json()
        if not st["syncing"] and st["count"] and st["username"] == user:
            return st
        time.sleep(0.05)
    raise AssertionError(st)


def test_client_switches_accounts_at_run_time(tmp_path, monkeypatch):
    monkeypatch.setattr(discogs.urllib.request, "urlopen", AccountApi())
    d = discogs.Discogs("", "", tmp_path / "c.json", "t")
    assert d.identity(UI_TOKEN) == {"username": "testuser", "id": 1}       # a token that is not saved yet
    with pytest.raises(discogs.DiscogsError):
        d.identity()                                                      # no credentials of its own
    assert d.configure(UI_TOKEN, "testuser") and not d.configure(UI_TOKEN, "testuser")
    d.sync()
    assert d.status()["count"] == 1 and json.loads((tmp_path / "c.json").read_text())["owner"] == "testuser"
    assert d.configure(UI_TOKEN, "TestUser") and d.status()["count"] == 1   # the same account: names ignore case
    assert d.configure(CFG_TOKEN, "configuser") and d.status()["count"] == 0 and d.stale()   # another account
    assert discogs.mask(UI_TOKEN) == "••••" + UI_TOKEN[-4:] and discogs.mask("short") == "••••"
    assert discogs.mask("") == ""


def test_token_saved_in_the_settings_applies_at_once(tmp_path, monkeypatch, caplog):
    """Desktop users have no configuration file: token and user name are entered in the settings, apply without a
    restart and survive one, and the token never comes back in full (API responses, logs)."""
    api = AccountApi()
    monkeypatch.setattr(discogs.urllib.request, "urlopen", api)
    caplog.set_level(logging.DEBUG)
    cfg = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state", min_free_gb=0.0)
    with TestClient(create_app(cfg)) as c:
        st = c.get("/api/discogs/status").json()
        assert st["enabled"] is False and st["source"] is None and st["saved"] is None and st["token_hint"] == ""
        assert c.get("/api/settings").json()["info"]["discogs"] == "not set up"
        off = c.get("/api/discogs/search", params={"q": "album"})
        assert off.status_code == 404 and "Settings → Discogs" in off.json()["detail"]
        assert "configuration file" in off.json()["detail"]                # a service names its file second
        # the form is checked field by field, and a refused value is never echoed back
        bad = c.put("/api/discogs/account", json={"token": "has a space in it", "username": "two words"})
        assert bad.status_code == 422 and set(bad.json()["detail"]["fields"]) == {"token", "username"}
        assert "space in it" not in bad.text and "two words" not in bad.text
        assert c.put("/api/discogs/account", json={"token": "", "username": "x"}).json()["detail"]["fields"]["token"]
        # the test asks Discogs whom the token belongs to, without saving it
        t = c.post("/api/discogs/test", json={"token": UI_TOKEN}).json()
        assert t == {"ok": True, "username": "testuser", "expected": None, "matches": None}
        t = c.post("/api/discogs/test", json={"token": UI_TOKEN, "username": "someone"}).json()
        assert t["ok"] and t["matches"] is False and t["expected"] == "someone"
        t = c.post("/api/discogs/test", json={"token": "x" * 40}).json()
        assert t["ok"] is False and "token invalid or expired" in t["error"]
        assert c.post("/api/discogs/test", json={}).json()["ok"] is False       # nothing to test yet
        assert c.get("/api/discogs/status").json()["enabled"] is False
        # saving without a user name takes the one Discogs reports, and the collection is synced at once
        st = c.put("/api/discogs/account", json={"token": f"  {UI_TOKEN} ", "username": ""}).json()
        assert st["enabled"] and st["source"] == "ui" and st["username"] == "testuser"
        hint = "••••" + UI_TOKEN[-4:]
        assert st["token_hint"] == hint and st["saved"] == {"token_hint": hint, "username": "testuser"}
        assert _wait_synced(c, "testuser")["count"] == 1
        assert c.get("/api/discogs/search", params={"q": "album"}).json()[0]["release_id"] == 42
        info = c.get("/api/settings").json()["info"]["discogs"]
        assert info == "testuser (personal access token, saved in the settings)"
        assert c.post("/api/discogs/test", json={}).json()["username"] == "testuser"    # the saved credentials
        # saving again without a token keeps the saved one (the form never receives it back)
        assert c.put("/api/discogs/account", json={"username": "testuser"}).json()["source"] == "ui"
        # covers are fetched with the saved token as well
        rid = c.post("/api/records", json={"artist": "A", "title": "B", "discogs_release_id": 42}).json()["id"]
        assert c.get(f"/api/records/{rid}/cover").json()["url"] == "https://i.discogs.com/c.jpg"
        cover_auth = [a for u, a in api.calls if u == "https://api.discogs.com/releases/42"]
        assert cover_auth and set(cover_auth) == {f"Discogs token={UI_TOKEN}"}
        texts = [c.get(p).text for p in ("/api/discogs/status", "/api/settings", "/api/status")]
    assert not any(UI_TOKEN in t for t in texts) and UI_TOKEN not in caplog.text
    with TestClient(create_app(cfg)) as c:                                 # kept across a restart
        st = c.get("/api/discogs/status").json()
        assert st["enabled"] and st["source"] == "ui" and st["count"] == 1
        st = c.delete("/api/discogs/account").json()
        assert st["enabled"] is False and st["saved"] is None and st["count"] == 0     # its collection went too


def test_saved_token_wins_over_the_configuration_file(tmp_path, monkeypatch):
    monkeypatch.setattr(discogs.urllib.request, "urlopen", AccountApi())
    cfg = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state", min_free_gb=0.0,
                  discogs_token=CFG_TOKEN, discogs_username="configuser")
    with TestClient(create_app(cfg)) as c:
        st = _wait_synced(c, "configuser")
        assert st["source"] == "config" and st["saved"] is None and st["config_token"] is True
        assert st["token_hint"] == "••••" + CFG_TOKEN[-4:]
        c.put("/api/discogs/account", json={"token": UI_TOKEN, "username": "testuser"})
        st = _wait_synced(c, "testuser")
        assert st["source"] == "ui"
        # the other account's records are gone from the cache, not mixed in
        assert [h["release_id"] for h in c.get("/api/discogs/search", params={"q": "album"}).json()] == [42]
        st = c.delete("/api/discogs/account").json()
        assert st["source"] == "config" and st["username"] == "configuser"
        assert _wait_synced(c, "configuser")["count"] == 1


def test_not_set_up_texts_point_to_the_settings(tmp_path):
    """Desktop users are never sent to a file or asked to restart anything."""
    cfg = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state", min_free_gb=0.0)
    with TestClient(_local(create_app(cfg, desktop=True)), base_url="http://127.0.0.1:8123") as c:
        en = c.get("/api/discogs/search", params={"q": "x"}).json()["detail"]
        de = c.get("/api/discogs/search", params={"q": "x", "lang": "de"}).json()["detail"]
        assert c.get("/api/discogs/status").json()["desktop"] is True
    assert "Settings → Discogs" in en and "configuration" not in en and "restart" not in en
    assert "Einstellungen → Discogs" in de and "Konfiguration" not in de

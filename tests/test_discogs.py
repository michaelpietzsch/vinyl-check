"""Discogs client: collection cache, search, writing the media condition back (API faked)."""

import io
import json
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
    assert out == {"vorher": "Good (G)", "jetzt": "Very Good Plus (VG+)", "release_id": 5605854}
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

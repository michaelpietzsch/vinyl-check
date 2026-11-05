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


GENRES = {777: (["Electronic"], ["Synth-pop", "Electro"]), 12345: (["Classical"], ["Baroque"]),
          5605854: (["Rock"], ["Hard Rock"])}


def _item(rid, inst, artist, title, media, year=1988, catno="TL 001", added="2026-09-07T10:00:00-07:00"):
    g, st = GENRES.get(rid, ([], []))
    return {"instance_id": inst, "folder_id": 1, "date_added": added,
            "basic_information": {"id": rid, "title": title, "year": year, "artists": [{"name": artist}],
                                  "labels": [{"name": "Test Label", "catno": catno}],
                                  "formats": [{"name": "Vinyl", "descriptions": ["LP", "Album"]}],
                                  "thumb": f"https://i.discogs.com/{rid}.jpg", "genres": g, "styles": st},
            "notes": [{"field_id": 1, "value": media}, {"field_id": 2, "value": "Good (G)"},
                      {"field_id": 3, "value": "Test note"}]}


class FakeApi:
    """Stand-in for api.discogs.com: a collection on two pages, the copies of one release, writing a field.
    `twin` adds a second copy of release 12345 (another instance, another folder)."""
    def __init__(self):
        self.calls = []
        self.media = {90001: "Good (G)", 90002: "Near Mint (NM or M-)", 90003: "Very Good (VG)", 90004: "Very Good Plus (VG+)"}
        self.twin = False
        self.releases = 0
        self.tracklists: dict = {}

    def items(self):
        out = [_item(5605854, 90001, "Test Artist", "First Album", self.media[90001]),
               _item(12345, 90002, "Test Artist", "Second Album", self.media[90002], 1979, "TL 002"),
               _item(777, 90003, "Ümlaut Bänd", "Third Album", self.media[90003], 1980, "TL 003")]
        if self.twin:
            t = _item(12345, 90004, "Test Artist", "Second Album", self.media[90004], 1979, "TL 002",
                      added="2026-10-01T10:00:00-07:00")
            t["folder_id"] = 7
            out.append(t)
        return out

    def __call__(self, req, timeout=0, context=None):
        url = req.full_url
        self.calls.append((req.get_method(), url, dict(req.header_items()), req.data))
        assert dict(req.header_items()).get("Authorization") == "Discogs token=tok123"
        if "/collection/folders/0/releases" in url:
            page = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["page"][0])
            items = self.items()
            items = items[:2] if page == 1 else items[2:]
            return io.BytesIO(json.dumps({"pagination": {"page": page, "pages": 2}, "releases": items}).encode())
        if "/collection/releases/" in url:                              # the copies of one release, as they are now
            rid = int(urllib.parse.urlparse(url).path.rsplit("/", 1)[1])
            items = [it for it in self.items() if it["basic_information"]["id"] == rid]
            if not items:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b'{"message": "Not found"}'))
            return io.BytesIO(json.dumps({"pagination": {"page": 1, "pages": 1}, "releases": items}).encode())
        if "/fields/1" in url and req.get_method() == "POST":
            rid = int(url.split("/releases/")[1].split("/")[0])
            inst = int(url.split("/instances/")[1].split("/")[0])
            it = next(x for x in self.items() if x["instance_id"] == inst)
            assert it["basic_information"]["id"] == rid and f"/folders/{it['folder_id']}/" in url
            self.media[inst] = json.loads(req.data)["value"]
            return io.BytesIO(b"")
        if "/releases/" in url and "/collection/" not in url:           # a release itself (tracks, genres)
            rid = int(url.rsplit("/", 1)[1])
            g, st = (["Hip Hop"], ["Boom Bap"]) if rid == 4242 else GENRES.get(rid, ([], []))
            self.releases += rid == 4242
            return io.BytesIO(json.dumps({"id": rid, "genres": g, "styles": st,
                                          "tracklist": self.tracklists.get(rid, [])}).encode())
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
    assert out == {"previous": "Good (G)", "current": "Very Good Plus (VG+)", "release_id": 5605854, "instance_id": 90001}
    assert api.media[90001] == "Very Good Plus (VG+)" and d.entry(5605854)["media"] == "Very Good Plus (VG+)"
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
    def unauthorized(req, timeout=0, context=None):
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

    def offline(req, timeout=0, context=None):
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

    def fake(req, timeout=0, context=None):
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

    def __call__(self, req, timeout=0, context=None):
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


def test_genre_rule():
    assert discogs.genre_rule(["Electronic"], ["Synth-pop", "Electro"]) == "on"
    assert discogs.genre_rule(["Hip Hop"], []) == "on"
    assert discogs.genre_rule(["Electronic"], ["Ambient"]) is None                # no beat: the share decides
    assert discogs.genre_rule(["Rock", "Electronic"], ["New Wave"]) is None
    assert discogs.genre_rule(["Classical"], ["Baroque"]) == "off"
    assert discogs.genre_rule(["Classical", "Stage & Screen"], ["Score"]) == "off"
    assert discogs.genre_rule(["Electronic", "Classical"], ["Modern Classical"]) is None
    assert discogs.genre_rule(["Rock"], ["Hard Rock"]) is None and discogs.genre_rule(None, None) is None


def test_record_repeat_rule_sources():
    from vinylcheck import analysis
    g = json.dumps({"genres": ["Electronic"], "styles": ["Techno"]})
    assert analysis.record_repeat_rule({"genres": g})["mode"] == "on"
    assert analysis.record_repeat_rule({"genres": g})["source"] == "genre"
    assert analysis.record_repeat_rule({"genres": g, "repeat_rule": "off"}) == {
        "mode": "off", "source": "record", "genres": ["Electronic"], "styles": ["Techno"]}
    assert analysis.record_repeat_rule({"genres": "{}"}, "on") == {"mode": "on", "source": "default", "genres": [], "styles": []}
    assert analysis.record_repeat_rule(None)["mode"] == "auto"


def test_repeat_rule_default_from_the_discogs_genres(tmp_path, api):
    cfg = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state", min_free_gb=0.0,
                  discogs_token="tok123", discogs_username="testuser")
    (tmp_path / "state").mkdir(parents=True)
    with TestClient(create_app(cfg)) as c:
        # added before the collection was synced: the next sync brings its genres
        early = c.post("/api/records", json={"artist": "Ümlaut Bänd", "title": "Third Album", "discogs_release_id": 777}).json()["id"]
        c.post("/api/discogs/sync")
        eff = c.get(f"/api/records/{early}").json()["repeat_rule_effective"]
        assert eff == {"mode": "on", "source": "genre", "genres": ["Electronic"], "styles": ["Synth-pop", "Electro"]}
        classical = c.post("/api/records", json={"artist": "Test Artist", "title": "Second Album", "discogs_release_id": 12345}).json()["id"]
        assert c.get(f"/api/records/{classical}").json()["repeat_rule_effective"]["mode"] == "off"
        rock = c.post("/api/records", json={"artist": "Test Artist", "title": "First Album", "discogs_release_id": 5605854}).json()["id"]
        assert c.get(f"/api/records/{rock}").json()["repeat_rule_effective"] == {
            "mode": "auto", "source": "default", "genres": ["Rock"], "styles": ["Hard Rock"]}
        # not in the collection: the release itself is asked once
        other = c.post("/api/records", json={"artist": "X", "title": "Y", "discogs_release_id": 4242}).json()["id"]
        assert c.get(f"/api/records/{other}").json()["repeat_rule_effective"]["mode"] == "on"
        n = api.releases
        c.get(f"/api/records/{other}")
        assert api.releases == n                                         # kept on the record, not asked again
        # the record's own choice wins, also when it is "automatic"
        r = c.patch(f"/api/records/{early}", json={"repeat_rule": "auto"}).json()
        assert r["repeat_rule_effective"]["mode"] == "auto" and r["repeat_rule_effective"]["source"] == "record"
        # without a release no genres and no request
        plain = c.post("/api/records", json={"artist": "A", "title": "B"}).json()["id"]
        assert c.get(f"/api/records/{plain}").json()["repeat_rule_effective"]["source"] == "default"


def test_writing_asks_discogs_first_and_finds_the_right_copy(tmp_path, api):
    d = discogs.Discogs("tok123", "testuser", tmp_path / "coll.json", "test")
    d.sync()
    # changed on Discogs after the sync: "previous" is what Discogs held, not the stale cache
    api.media[90003] = "Mint (M)"
    assert d.set_media_condition(777, "VG")["previous"] == "Mint (M)"
    # a second copy of the release added on Discogs, in another folder: the write must say which one
    api.twin = True
    with pytest.raises(discogs.DiscogsError) as ei:
        d.set_media_condition(12345, "VG")
    assert ei.value.message["$t"] == "discogs.several_instances" and [e["instance_id"] for e in ei.value.instances] == [90002, 90004]
    assert len([e for e in d.releases() if e["release_id"] == 12345]) == 2               # the cache knows both now
    out = d.set_media_condition(12345, "G+", instance_id=90004)
    assert out["instance_id"] == 90004 and api.media[90004] == "Good Plus (G+)" and api.media[90002] == "Near Mint (NM or M-)"
    with pytest.raises(discogs.DiscogsError) as ei:
        d.set_media_condition(12345, "G+", instance_id=55555)                            # gone on Discogs
    assert ei.value.message["$t"] == "discogs.instance_gone" and len(ei.value.instances) == 2
    # removed from the collection on Discogs: an error, and the cache drops it
    with pytest.raises(discogs.DiscogsError) as ei:
        d.set_media_condition(999, "VG")
    assert ei.value.message["$t"] == "discogs.not_in_collection_now"
    # a sync that was already fetching when the condition was written keeps the written value
    api.twin = False
    real = api.__call__

    def slow(req, timeout=0, context=None):
        if "/collection/folders/0/releases" in req.full_url and "page=2" in req.full_url and not slow.done:
            slow.done = True
            api.media[90001] = "Fair (F)"                                # what Discogs answered before the write
            d._written[90001] = (time.time() + 1, "Very Good (VG)")       # the write lands while the sync runs
        return real(req, timeout, context)
    slow.done = False
    discogs.urllib.request.urlopen = slow
    d.sync()
    assert d.entry(5605854)["media"] == "Very Good (VG)"


def test_web_remembers_which_copy_is_which(tmp_path, api):
    cfg = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state", min_free_gb=0.0,
                  discogs_token="tok123", discogs_username="testuser")
    (tmp_path / "state").mkdir(parents=True)
    api.twin = True
    with TestClient(create_app(cfg)) as c:
        c.post("/api/discogs/sync")
        rid = c.post("/api/records", json={"artist": "Test Artist", "title": "Second Album", "discogs_release_id": 12345}).json()["id"]
        coll = c.get(f"/api/records/{rid}/collection", params={"copy": "Copy 2"}).json()
        assert [x["instance_id"] for x in coll["copies"]] == [90002, 90004] and coll["suggested"] is None
        api.media[90004] = "Fair (F)"
        live = c.get(f"/api/records/{rid}/collection", params={"live": True, "copy": "Copy 2"}).json()
        assert live["live"] and [x["media"] for x in live["copies"]] == ["Near Mint (NM or M-)", "Fair (F)"]
        assert c.post(f"/api/records/{rid}/condition", json={"copy": "Copy 2", "instance_id": 90004}).status_code == 409   # no grade yet
        from vinylcheck.db import DB
        db = DB(cfg.db_path)
        x = db.add_recording(record_id=rid, kind="before", side="A", copy_label="Copy 2", status="analyzed")
        db.save_analysis(x, {"metrics": {"summary": {}}, "grading": {"grade": "VG", "score": 60}, "warnings": [],
                             "overview": {}, "events": []}, "test", 1.0)
        r = c.post(f"/api/records/{rid}/condition", json={"copy": "Copy 2"})
        assert r.status_code == 409 and "2" in r.json()["detail"]                      # which of the two?
        r = c.post(f"/api/records/{rid}/condition", json={"copy": "Copy 2", "instance_id": 90004}).json()
        assert r["instance_id"] == 90004 and r["previous"] == "Fair (F)" and api.media[90004] == "Very Good (VG)"
        assert c.get(f"/api/records/{rid}/collection", params={"copy": "copy  2"}).json()["suggested"] == 90004
        api.media[90004] = "Poor (P)"
        r = c.post(f"/api/records/{rid}/condition", json={"copy": "Copy 2"}).json()        # remembered
        assert r["instance_id"] == 90004 and r["previous"] == "Poor (P)" and api.media[90002] == "Near Mint (NM or M-)"
        api.twin = False                                     # that copy was removed on Discogs: the one left is meant
        r = c.post(f"/api/records/{rid}/condition", json={"copy": "Copy 2"}).json()
        assert r["instance_id"] == 90002 and api.media[90002] == "Very Good (VG)"
        assert c.get(f"/api/records/{rid}").json()["discogs_media_condition"] == "Very Good (VG)"


def test_fetching_the_release_again(tmp_path, api):
    """Tracks, side lengths and genres are taken once; "fetch again" brings what was corrected on Discogs since."""
    cfg = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state", min_free_gb=0.0,
                  discogs_token="tok123", discogs_username="testuser")
    (tmp_path / "state").mkdir(parents=True)
    with TestClient(create_app(cfg)) as c:
        c.post("/api/discogs/sync")
        rid = c.post("/api/records", json={"artist": "Test Artist", "title": "First Album", "discogs_release_id": 5605854}).json()["id"]
        r = c.get(f"/api/records/{rid}").json()
        assert r["tracks"] == {} and r["repeat_rule_effective"]["mode"] == "auto"
        api.tracklists[5605854] = [{"position": "A1", "title": "One", "duration": "3:00"},
                                   {"position": "A2", "title": "Two", "duration": "4:30"}]
        GENRES[5605854] = (["Electronic"], ["Techno"])                  # reclassified on Discogs
        try:
            r = c.post(f"/api/records/{rid}/discogs-refresh").json()
        finally:
            GENRES[5605854] = (["Rock"], ["Hard Rock"])
        assert r["refreshed"] == {"tracks": True, "rule": True, "reanalyzing": 0}
        assert [t["title"] for t in r["tracks"]["A"]] == ["One", "Two"] and r["side_lengths"]["A"]["s"] == 450
        assert r["repeat_rule_effective"] == {"mode": "on", "source": "genre", "genres": ["Electronic"], "styles": ["Techno"]}
        r = c.post(f"/api/records/{rid}/discogs-refresh").json()
        assert r["refreshed"]["tracks"] is False
        plain = c.post("/api/records", json={"artist": "A", "title": "B"}).json()["id"]
        assert c.post(f"/api/records/{plain}/discogs-refresh").status_code == 409

"""Discogs client: read the user's collection, search it, write the measured condition back.

Discogs is the leading database. vinyl-check only keeps snapshots (release id, condition when the record was
added); what counts is the collection on Discogs. The collection is cached as JSON in `state_dir` and refreshed
when it is older than `ttl` (web.py sets it from `discogs_sync_minutes`) or on request, so search and display
do not hit the API on every click.

Two ways to authenticate: a personal access token plus user name (entered under Settings → Discogs and stored in the
database, or set in the configuration file), or a Discogs app (consumer key/secret in the configuration) with OAuth
1.0a: the web interface obtains the account's authorization once (request_token → authorize on Discogs →
access_token); token and secret are then stored in the database. Requests are signed with PLAINTEXT over HTTPS, as
Discogs specifies. Without credentials the Discogs features are off and the rest of the application works as usual.
`configure()` switches the credentials at run time, so a token entered in the settings applies without a restart.

A token is a password to the Discogs account: it is sent only to api.discogs.com, never logged, and shown to users
only as `mask()` (its last four characters).

Errors raise DiscogsError, which carries an i18n message.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import i18n, tls

API = "https://api.discogs.com"
FIELD_MEDIA, FIELD_SLEEVE, FIELD_NOTES = 1, 2, 3          # default fields of a Discogs collection
CONDITIONS = ["Mint (M)", "Near Mint (NM or M-)", "Very Good Plus (VG+)", "Very Good (VG)",
              "Good Plus (G+)", "Good (G)", "Fair (F)", "Poor (P)"]
GRADE_TO_CONDITION = {"M": CONDITIONS[0], "NM": CONDITIONS[1], "VG+": CONDITIONS[2], "VG": CONDITIONS[3],
                      "G+": CONDITIONS[4], "G": CONDITIONS[5], "F": CONDITIONS[6], "P": CONDITIONS[7]}


def mask(token: str | None) -> str:
    """A token as users may see it again: its last four characters ("••••1234"); short tokens show nothing of
    themselves. Empty for no token."""
    if not token:
        return ""
    return "••••" + (token[-4:] if len(token) >= 12 else "")


def condition_short(c: str | None) -> str | None:
    """Short grade of a Discogs condition: "Very Good Plus (VG+)" → "VG+", "Near Mint (NM or M-)" → "NM"."""
    if not c:
        return None
    m = re.search(r"\(([^)]+)\)", c)
    return m.group(1).split(" or ")[0].strip() if m else c


def _dur_s(txt: str) -> float | None:
    """Track duration in seconds: "5:16" or "1:02:03"; empty/unreadable → None."""
    parts = [p for p in str(txt or "").strip().split(":") if p != ""]
    if not parts or not all(p.isdigit() for p in parts):
        return None
    total = 0.0
    for p in parts:
        total = total * 60 + int(p)
    return total


def side_lengths(tracklist: list[dict]) -> dict[str, float]:
    """Side lengths from a Discogs tracklist: positions "A1", "B2" … summed per side. Sides with a track without
    duration are left out (better no number than a wrong one)."""
    sums: dict[str, float] = {}
    missing: set[str] = set()
    for t in tracklist or []:
        pos = str(t.get("position") or "").strip().upper()
        m = re.match(r"^([A-Z])\d*$", pos)
        if not m or t.get("type_", "track") not in ("track", None):
            continue
        side = m.group(1)
        d = _dur_s(t.get("duration"))
        if d is None:
            missing.add(side)
        else:
            sums[side] = sums.get(side, 0.0) + d
    return {k: round(v, 1) for k, v in sums.items() if k not in missing}


def tracks_by_side(tracklist: list[dict]) -> dict[str, list[dict]]:
    """Tracks per side from a Discogs tracklist: position, title, duration (None if not listed)."""
    out: dict[str, list[dict]] = {}
    for t in tracklist or []:
        pos = str(t.get("position") or "").strip().upper()
        m = re.match(r"^([A-Z])\d*$", pos)
        if not m or t.get("type_", "track") not in ("track", None):
            continue
        out.setdefault(m.group(1), []).append({"pos": pos, "title": str(t.get("title") or "").strip(),
                                               "dur_s": _dur_s(t.get("duration"))})
    return out


def _norm(s: str) -> str:
    """Search normalization: ASCII, lower case, words separated by single spaces
    ("Café Señor-Tango" → "cafe senor tango")."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


class DiscogsError(i18n.MessageError):
    """A Discogs failure the user should see (an i18n message, see i18n.MessageError)."""


class Discogs:
    """Collection cache and API access for one Discogs account."""

    def __init__(self, token: str, username: str, cache_file: Path, user_agent: str, ttl_s: float = 86400.0,
                 consumer_key: str = "", consumer_secret: str = ""):
        self.token, self.username, self.cache_file, self.ua, self.ttl = token or "", username or "", Path(cache_file), user_agent, ttl_s
        self.consumer_key, self.consumer_secret = consumer_key or "", consumer_secret or ""
        self.oauth_token, self.oauth_secret = "", ""
        self._lock = threading.Lock()
        self.state: dict = {"releases": [], "synced_at": None, "syncing": False, "error": None}
        self._owner = ""                    # account the cached collection belongs to ("" = unknown, older caches)
        try:
            d = json.loads(self.cache_file.read_text())
            self.state["releases"], self.state["synced_at"] = d.get("releases", []), d.get("synced_at")
            self._owner = d.get("owner") or ""
        except (OSError, ValueError):
            pass

    @property
    def oauth_ready(self) -> bool:
        """App keys are configured: the account's authorization can be requested."""
        return bool(self.consumer_key and self.consumer_secret)

    @property
    def enabled(self) -> bool:
        return bool(self.username) and (bool(self.token) or bool(self.oauth_ready and self.oauth_token and self.oauth_secret))

    def set_oauth(self, token: str, secret: str, username: str = "") -> None:
        self.oauth_token, self.oauth_secret = token or "", secret or ""
        if username:
            self.username = username

    def configure(self, token: str, username: str, oauth_token: str = "", oauth_secret: str = "") -> bool:
        """Switch to other credentials at run time (the settings page). A personal token wins over an OAuth
        authorization, as in _auth_header(). When the account changes, the cached collection of the previous one is
        dropped, so that search and conditions never show another account's records. Returns True if anything
        changed (the caller then syncs)."""
        token, username = token or "", username or ""
        with self._lock:
            new = (token, username, oauth_token or "", oauth_secret or "")
            if new == (self.token, self.username, self.oauth_token, self.oauth_secret):
                return False
            owner = self._owner or self.username         # caches written before the owner was stored
            self.token, self.username, self.oauth_token, self.oauth_secret = new
            self.state["error"] = None
            if self.state["releases"] and owner and owner.lower() != username.lower():   # names ignore case
                self.state["releases"], self.state["synced_at"], self._owner = [], None, ""
                self._save()
        return True

    # ─── HTTP ────────────────────────────────────────────────────────────────
    def _auth_header(self, extra: dict | None = None, token: str | None = None, token_secret: str | None = None) -> str:
        if self.token and token is None:
            return f"Discogs token={self.token}"
        tok = self.oauth_token if token is None else token
        sec = self.oauth_secret if token_secret is None else token_secret
        q = lambda s: urllib.parse.quote(str(s), safe="")
        params = {"oauth_consumer_key": self.consumer_key, "oauth_nonce": secrets.token_hex(16),
                  "oauth_signature": f"{q(self.consumer_secret)}&{q(sec)}", "oauth_signature_method": "PLAINTEXT",
                  "oauth_timestamp": str(int(time.time())), "oauth_version": "1.0"}
        if tok:
            params["oauth_token"] = tok
        params.update(extra or {})
        return "OAuth " + ", ".join(f'{k}="{q(v)}"' for k, v in sorted(params.items()))

    def auth_header(self) -> str:
        """Authorization header of the account's requests (for other requests to api.discogs.com)."""
        return self._auth_header()

    def _req(self, url: str, method: str = "GET", data: dict | None = None, auth: str | None = None) -> dict:
        """A request to the API; `auth` replaces the account's Authorization header (testing a token)."""
        req = urllib.request.Request(url, method=method, data=json.dumps(data).encode() if data is not None else None,
                                     headers={"Authorization": auth or self._auth_header(), "User-Agent": self.ua,
                                              "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20, context=tls.context()) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            try:
                reason = json.loads(e.read()).get("message") or e.reason
            except Exception:  # noqa: BLE001
                reason = e.reason
            if e.code == 401:
                reason = i18n.msg("discogs.token_invalid")
            elif e.code == 429:
                reason = i18n.msg("discogs.rate_limited")
            raise DiscogsError(i18n.msg("discogs.http_error", code=e.code, reason=reason)) from e
        except OSError as e:
            raise DiscogsError(i18n.msg("discogs.unreachable", error=str(e))) from e

    # ─── OAuth 1.0a (authorizing the app for the account) ────────────────────
    def _oauth_call(self, url: str, header: str, method: str = "GET") -> dict:
        req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None,
                                     headers={"Authorization": header, "User-Agent": self.ua,
                                              "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=20, context=tls.context()) as resp:
                return {k: v[0] for k, v in urllib.parse.parse_qs(resp.read().decode()).items()}
        except urllib.error.HTTPError as e:
            reason = e.read().decode(errors="replace")[:200] or e.reason
            raise DiscogsError(i18n.msg("discogs.http_error", code=e.code, reason=reason)) from e
        except OSError as e:
            raise DiscogsError(i18n.msg("discogs.unreachable", error=str(e))) from e

    def oauth_request_token(self, callback: str) -> dict:
        """Step 1: get a request token. Returns token, secret and authorize_url (where the browser goes next)."""
        if not self.oauth_ready:
            raise DiscogsError(i18n.msg("discogs.app_not_configured"))
        r = self._oauth_call(f"{API}/oauth/request_token", self._auth_header({"oauth_callback": callback}, token="", token_secret=""))
        if "oauth_token" not in r:
            raise DiscogsError(i18n.msg("discogs.no_request_token"))
        return {"token": r["oauth_token"], "secret": r.get("oauth_token_secret", ""),
                "authorize_url": f"https://www.discogs.com/oauth/authorize?oauth_token={urllib.parse.quote(r['oauth_token'])}"}

    def oauth_access_token(self, request_token: str, request_secret: str, verifier: str) -> dict:
        """Step 3: exchange the verifier (from the callback) for an access token and fetch the user name."""
        r = self._oauth_call(f"{API}/oauth/access_token", self._auth_header({"oauth_verifier": verifier}, token=request_token,
                                                                             token_secret=request_secret), method="POST")
        if "oauth_token" not in r:
            raise DiscogsError(i18n.msg("discogs.no_access_token"))
        self.set_oauth(r["oauth_token"], r.get("oauth_token_secret", ""))
        ident = self._req(f"{API}/oauth/identity")
        self.username = ident.get("username") or self.username
        return {"token": self.oauth_token, "secret": self.oauth_secret, "username": self.username}

    # ─── Account ─────────────────────────────────────────────────────────────
    def identity(self, token: str | None = None) -> dict:
        """Who the credentials belong to (GET /oauth/identity, which also answers personal tokens): user name and
        account id. `token`: check this personal access token instead of the client's own credentials (the settings
        form, before saving)."""
        if token is None and not (self.token or (self.oauth_ready and self.oauth_token and self.oauth_secret)):
            raise DiscogsError(i18n.msg("discogs.not_configured"))
        d = self._req(f"{API}/oauth/identity", auth=f"Discogs token={token}" if token else None)
        if not d.get("username"):
            raise DiscogsError(i18n.msg("discogs.no_identity"))
        return {"username": str(d["username"]), "id": d.get("id")}

    # ─── Collection ──────────────────────────────────────────────────────────
    @staticmethod
    def _entry(item: dict) -> dict:
        bi = item.get("basic_information") or {}
        fields = {n.get("field_id"): n.get("value") for n in (item.get("notes") or [])}
        labels = bi.get("labels") or []
        fmt = bi.get("formats") or []
        return {"release_id": bi.get("id") or item.get("id"), "instance_id": item.get("instance_id"),
                "folder_id": item.get("folder_id"), "artist": ", ".join(a.get("name", "") for a in (bi.get("artists") or [])).strip(),
                "title": bi.get("title", ""), "year": bi.get("year") or None,
                "label": ", ".join(dict.fromkeys(x.get("name", "") for x in labels)), "catno": ", ".join(dict.fromkeys(x.get("catno", "") for x in labels)),
                "format": ", ".join(f"{f.get('name', '')} ({', '.join(f.get('descriptions') or [])})" if f.get("descriptions") else f.get("name", "") for f in fmt),
                "media": fields.get(FIELD_MEDIA) or None, "sleeve": fields.get(FIELD_SLEEVE) or None, "notes": fields.get(FIELD_NOTES) or "",
                "date_added": item.get("date_added"), "thumb": bi.get("thumb") or "", "cover": bi.get("cover_image") or ""}

    def sync(self) -> dict:
        """Fetch the whole collection (folder 0 = all) and write the cache. Blocks; see sync_async()."""
        if not self.enabled:
            raise DiscogsError(i18n.msg("discogs.not_configured"))
        with self._lock:
            self.state["syncing"], self.state["error"] = True, None
            user = self.username
        try:
            out, page = [], 1
            while True:
                d = self._req(f"{API}/users/{urllib.parse.quote(user)}/collection/folders/0/releases?"
                              + urllib.parse.urlencode({"per_page": 100, "page": page, "sort": "artist"}))
                out.extend(self._entry(it) for it in d.get("releases", []))
                pages = (d.get("pagination") or {}).get("pages", 1)
                if page >= pages:
                    break
                page += 1
            with self._lock:
                if self.username == user:          # the account was not switched meanwhile (configure)
                    self.state["releases"], self.state["synced_at"], self._owner = out, time.time(), user
                    self._save()
        except DiscogsError as e:
            with self._lock:
                self.state["error"] = e.message            # rendered in the reader's language by the API
            raise
        finally:
            with self._lock:
                self.state["syncing"] = False
        return self.status()

    def sync_async(self) -> bool:
        """Start a sync in a background thread; False if Discogs is off or a sync is already running."""
        if not self.enabled or self.state["syncing"]:
            return False
        def run():
            try:
                self.sync()
            except DiscogsError:
                pass
        threading.Thread(target=run, daemon=True, name="vinylcheck-discogs").start()
        return True

    def stale(self) -> bool:
        return self.enabled and (self.state["synced_at"] is None or time.time() - self.state["synced_at"] > self.ttl)

    def _save(self) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(json.dumps({"releases": self.state["releases"],
                                                   "synced_at": self.state["synced_at"], "owner": self._owner}))
        except OSError:
            pass

    def status(self) -> dict:
        return {"enabled": self.enabled, "username": self.username if self.enabled else "", "count": len(self.state["releases"]),
                "synced_at": self.state["synced_at"], "syncing": self.state["syncing"], "error": self.state["error"]}

    def releases(self) -> list[dict]:
        return list(self.state["releases"])

    def entry(self, release_id: int | None, artist: str = "", title: str = "") -> dict | None:
        """Collection entry by release id, otherwise by artist + title (first match)."""
        if release_id:
            for e in self.state["releases"]:
                if e["release_id"] == int(release_id):
                    return e
            return None
        a, t = _norm(artist), _norm(title)
        for e in self.state["releases"]:
            if _norm(e["artist"]) == a and _norm(e["title"]) == t:
                return e
        return None

    def search(self, q: str, limit: int = 30) -> list[dict]:
        """Search the cached collection: an entry matches if at least half of the query words occur in artist,
        title, catalog number or year. Best matches first."""
        toks = [t for t in _norm(q).split() if t]
        if not toks:
            return []
        hits = []
        for e in self.state["releases"]:
            hay = _norm(f"{e['artist']} {e['title']} {e['catno']} {e['year'] or ''}")
            score = sum(1 for t in toks if t in hay) / len(toks)
            if score >= 0.5:
                hits.append((score, e))
        hits.sort(key=lambda x: (-x[0], x[1]["artist"], x[1]["title"]))
        return [{"artist": e["artist"], "title": e["title"], "year": e["year"], "label": e["label"], "catno": e["catno"],
                 "media_condition": e["media"], "release_id": e["release_id"], "instance_id": e["instance_id"],
                 "in_collection_since": e["date_added"], "thumb": e["thumb"], "score": round(s, 2)} for s, e in hits[:limit]]

    def release_side_lengths(self, release_id: int) -> dict[str, float]:
        """Side lengths of a release; if durations are missing, those of the master release (more often listed there).
        Also works without credentials (public release data), then with the lower rate limit."""
        rel = self._public_or_auth(f"{API}/releases/{int(release_id)}")
        out = side_lengths(rel.get("tracklist") or [])
        if not out and rel.get("master_id"):
            master = self._public_or_auth(f"{API}/masters/{int(rel['master_id'])}")
            out = side_lengths(master.get("tracklist") or [])
        return out

    def release_tracks(self, release_id: int) -> dict[str, list[dict]]:
        """Tracks per side of a release; missing durations come from the master release where possible
        (same position)."""
        rel = self._public_or_auth(f"{API}/releases/{int(release_id)}")
        tr = tracks_by_side(rel.get("tracklist") or [])
        missing = not tr or any(x["dur_s"] is None for side in tr.values() for x in side)
        if missing and rel.get("master_id"):
            master = tracks_by_side((self._public_or_auth(f"{API}/masters/{int(rel['master_id'])}")).get("tracklist") or [])
            if not tr:
                tr = master
            else:
                by_pos = {x["pos"]: x for side in master.values() for x in side}
                for side in tr.values():
                    for x in side:
                        if x["dur_s"] is None and by_pos.get(x["pos"], {}).get("dur_s") is not None:
                            x["dur_s"] = by_pos[x["pos"]]["dur_s"]
                for s, lst in master.items():            # sides the release does not list at all come from the master
                    if s not in tr:
                        tr[s] = lst
        return tr

    def _public_or_auth(self, url: str) -> dict:
        if self.enabled:
            return self._req(url)
        req = urllib.request.Request(url, headers={"User-Agent": self.ua, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10, context=tls.context()) as resp:
                return json.loads(resp.read() or b"{}")
        except (urllib.error.HTTPError, OSError, ValueError) as e:
            raise DiscogsError(i18n.msg("discogs.unreachable", error=str(e))) from e

    # ─── Writing ─────────────────────────────────────────────────────────────
    def set_media_condition(self, release_id: int, grade: str) -> dict:
        """Set the media condition (field 1) of the collection instance; sleeve condition and notes stay untouched.
        `grade` is a Goldmine grade ("VG+") or a full Discogs condition. Returns the condition before and after
        (keys "previous"/"current") and the release id."""
        cond = GRADE_TO_CONDITION.get(grade) or (grade if grade in CONDITIONS else None)
        if not cond:
            raise DiscogsError(i18n.msg("discogs.unknown_grade", grade=str(grade)))
        e = self.entry(release_id)
        if not e:
            raise DiscogsError(i18n.msg("discogs.not_in_collection"))
        if not e.get("instance_id") or e.get("folder_id") is None:
            raise DiscogsError(i18n.msg("discogs.instance_unknown"))
        url = (f"{API}/users/{urllib.parse.quote(self.username)}/collection/folders/{e['folder_id']}/releases/{int(release_id)}"
               f"/instances/{e['instance_id']}/fields/{FIELD_MEDIA}")
        self._req(url, method="POST", data={"value": cond})
        before = e.get("media")
        with self._lock:
            e["media"] = cond
            self._save()
        return {"previous": before, "current": cond, "release_id": int(release_id)}

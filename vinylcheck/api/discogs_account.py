"""The Discogs account of the station: access token or OAuth authorization, testing it, syncing and searching the
collection.

Part of the API of `vinylcheck.web`: `install` adds the routes to the app. What the areas share lives in the
`Station` (`vinylcheck/station.py`).
"""

from __future__ import annotations

import html
import json
import time
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from .. import discogs, i18n
from .common import DISCOGS_ACCOUNT, _discogs_field, log

if TYPE_CHECKING:
    from ..station import Station


def install(app: FastAPI, station: Station) -> None:
    cfg, db, dc, desktop = station.cfg, station.db, station.dc, station.desktop

    oauth_pending: dict = {}                              # request_token → secret (until Discogs redirects back)

    # ─── Discogs (collection search and sync, access token, OAuth) ─────────
    @app.get("/api/discogs/search")
    def discogs_search(q: str):
        if not dc.enabled:
            raise HTTPException(404, station.discogs_off())
        hits = dc.search(q)
        if not hits and len(q.strip()) >= 3 and (dc.state["synced_at"] is None or time.time() - dc.state["synced_at"] > 60):
            try:
                dc.sync()                                 # nothing found: sync now (1–2 s) and search again
            except discogs.DiscogsError:
                pass
            hits = dc.search(q)
        elif dc.stale():
            dc.sync_async()
        return hits

    @app.get("/api/discogs/status")
    def discogs_status():
        """Collection state and how the account is connected. The token only ever appears masked."""
        acct = station.discogs_account()
        saved = {"token_hint": discogs.mask(acct["token"]), "username": acct.get("username") or ""} if acct else None
        return dc.status() | {"oauth_ready": dc.oauth_ready,
                              "via": "token" if dc.token else ("oauth" if dc.oauth_token else None),
                              "source": station.discogs_source(), "token_hint": discogs.mask(dc.token), "saved": saved,
                              "config_token": bool(cfg.discogs_token), "desktop": desktop}

    def _discogs_form(body: dict) -> tuple[str, str]:
        """Token and user name from the form; HTTPException 422 with a message per field (never the value)."""
        out, errors = {}, {}
        for key in ("token", "username"):
            try:
                out[key] = _discogs_field(body, key)
            except ValueError as e:
                errors[key] = i18n.error_message(e)
        if errors:
            raise HTTPException(422, {"fields": errors})
        return out["token"], out["username"]

    @app.put("/api/discogs/account")
    def discogs_account_put(body: dict):
        """Save a personal access token and user name (Settings → Discogs). They apply at once and take precedence over
        the configuration file. An empty token keeps the saved one (the form never receives it back); an empty user
        name is asked from Discogs."""
        token, user = _discogs_form(body)
        token = token or station.discogs_account().get("token", "")
        if not token:
            raise HTTPException(422, {"fields": {"token": i18n.msg("api.discogs_token_missing")}})
        if not user:
            try:
                user = dc.identity(token)["username"]
            except discogs.DiscogsError as e:
                raise HTTPException(502, i18n.error_message(e)) from e
        db.set_setting(DISCOGS_ACCOUNT, json.dumps({"token": token, "username": user}))
        log.info("Discogs: access token for %s saved in the settings", user)
        if station.discogs_apply():
            dc.sync_async()
        return discogs_status()

    @app.delete("/api/discogs/account")
    def discogs_account_delete():
        """Remove the token saved in the settings; the configuration file's or an OAuth authorization applies again."""
        db.set_setting(DISCOGS_ACCOUNT, "")
        log.info("Discogs: access token removed from the settings")
        if station.discogs_apply():
            dc.sync_async()
        return discogs_status()

    @app.post("/api/discogs/test")
    def discogs_test(body: dict | None = None):
        """Ask Discogs whom a token belongs to (GET /oauth/identity): the form's token (not saved) or, without one, the
        credentials in use. Compares the answer with the form's user name, else the one in use."""
        token, user = _discogs_form(body or {})
        if not token and not (dc.token or dc.oauth_token):
            return {"ok": False, "error": i18n.msg("api.discogs_token_missing")}
        expected = user or (dc.username if not token else "")
        try:
            ident = dc.identity(token or None)
        except discogs.DiscogsError as e:
            return {"ok": False, "error": e.message}
        name = ident["username"]
        return {"ok": True, "username": name, "expected": expected or None,
                "matches": name.lower() == expected.lower() if expected else None}

    @app.post("/api/discogs/connect")
    def discogs_connect(request: Request):
        """OAuth step 1: get a request token; the browser then goes to Discogs' authorization page, which redirects back
        to /api/discogs/callback (at the address under which the browser reaches this server)."""
        if not dc.oauth_ready:
            raise HTTPException(409, i18n.msg("api.discogs_app_missing"))
        host = request.headers.get("host") or f"{cfg.host}:{cfg.port}"
        callback = f"{request.url.scheme}://{host}/api/discogs/callback"
        try:
            r = dc.oauth_request_token(callback)
        except discogs.DiscogsError as e:
            raise HTTPException(502, i18n.error_message(e)) from e
        oauth_pending.clear()
        oauth_pending[r["token"]] = r["secret"]
        return {"authorize_url": r["authorize_url"]}

    @app.get("/api/discogs/callback", include_in_schema=False)
    def discogs_callback(oauth_token: str = "", oauth_verifier: str = "", denied: str = ""):
        """OAuth step 2: Discogs redirects here after the user granted (or denied) access."""
        if denied or oauth_token not in oauth_pending:
            return Response(f"<p>{html.escape(i18n.t('api.oauth_denied'))}</p>",
                            media_type="text/html; charset=utf-8", status_code=400)
        try:
            r = dc.oauth_access_token(oauth_token, oauth_pending.pop(oauth_token), oauth_verifier)
        except discogs.DiscogsError as e:
            return Response(f"<p>{html.escape(i18n.t('api.oauth_failed', error=i18n.error_message(e)))}</p>",
                            media_type="text/html; charset=utf-8", status_code=502)
        db.set_setting("discogs_oauth_token", r["token"])
        db.set_setting("discogs_oauth_secret", r["secret"])
        db.set_setting("discogs_username", r["username"])
        station.discogs_apply()             # a personal token, if there is one, still takes precedence
        dc.sync_async()
        return RedirectResponse("/#system", status_code=303)

    @app.post("/api/discogs/disconnect")
    def discogs_disconnect():
        for k in ("discogs_oauth_token", "discogs_oauth_secret"):
            db.set_setting(k, "")
        station.discogs_apply()
        return discogs_status()

    @app.post("/api/discogs/sync")
    def discogs_sync():
        if not dc.enabled:
            raise HTTPException(404, station.discogs_off())
        try:
            return dc.sync()
        except discogs.DiscogsError as e:
            raise HTTPException(502, i18n.error_message(e)) from e

"""Web interface and REST API (FastAPI).

Serves the single-page web interface from ``static/`` and the JSON API behind it: records and recordings, starting and
stopping captures (live level meter, listen-in stream), analysis results, judgments of single events, before/after
comparisons, turntable profiles and alignment runs, master comparison and test pressing approval (engineer mode), PDF
reports, Discogs collection sync and settings. A WebSocket pushes the status (recorder, job queue, notifications) to
the browser.

This module builds the app (``create_app``): the ``Station`` (station.py) holds what all of it shares, the modules in
``vinylcheck/api/`` add the routes, one module per area, and ``api/common.py`` has the request bodies and names.

User-facing texts are i18n messages (``i18n.msg``), not finished strings: ``LocalizedJSONResponse`` renders them in the
language of the request (``LanguageMiddleware``: ``?lang=``, the interface's language cookie, Accept-Language), HTTP
error details and the WebSocket included. Server catalogs used here: ``api`` (messages) and ``labels`` (display names).

Intended for a trusted local network. Access from other devices can require a code (network.py: always in the
desktop app, on a service only if `access_code` is configured).
"""

from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import HTTPConnection
from fastapi.staticfiles import StaticFiles

from . import (__version__, api, audio_settings, i18n, licenses, network, onboarding, storage, testrecords, updates,
               vinylmind)
from .api.common import STATIC, _localize
from .config import Config, config_file
from .station import Station


class LocalizedJSONResponse(JSONResponse):
    """JSON response whose messages (i18n.msg) are rendered in the language of the request."""

    def render(self, content) -> bytes:
        return super().render(_localize(content))


class LanguageMiddleware:
    """Sets the language of each request (?lang=, cookie vc_lang, the configured default, Accept-Language only with
    language = "auto") for i18n.t and responses."""

    def __init__(self, app, default: str = "auto"):
        self.app = app
        self.default = default

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        conn = HTTPConnection(scope)
        lang = i18n.resolve(conn.query_params.get("lang"), conn.cookies.get(i18n.COOKIE),
                            conn.headers.get("accept-language"), self.default)
        with i18n.use_lang(lang):
            await self.app(scope, receive, send)


class _WriteClock:
    """Notes when a request last changed something (any method but GET/HEAD/OPTIONS) in `clock["last_write"]`: the
    restart when idle waits for a quiet spell after it."""

    def __init__(self, app, clock: dict):
        self.app, self.clock = app, clock

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("method") not in ("GET", "HEAD", "OPTIONS"):
            self.clock["last_write"] = time.time()
        await self.app(scope, receive, send)


def create_app(cfg: Config | None = None, source_factory=None, *, desktop: bool = False) -> FastAPI:
    """Build the app. `cfg` defaults to the configuration file; `source_factory(device, rate, channels, fmt)` replaces
    the audio source (tests). `desktop`: the app runs inside the desktop app (desktop.py), not as a service: no idle
    restart, network access only by setting and with the access code."""
    station = Station(cfg, source_factory, desktop)

    @asynccontextmanager
    async def lifespan(app):
        # recordings that were still running at the last shutdown are incomplete
        for r in station.db.q("SELECT id FROM recordings WHERE status = 'recording'"):
            interrupted = i18n.msg("api.recording_interrupted")
            station.db.update_recording(r["id"], status="error", error=i18n.dumps(interrupted))
            station.db.add_problem("recording", r["id"], interrupted)
        station.worker.start()
        station.regrade_all()                                      # grading scheme changed? then regrade everything
        threading.Thread(target=station.recheck_skips, daemon=True, name="vinylcheck-skips").start()
        if not station.desktop:
            threading.Thread(target=station.restart_loop, daemon=True, name="vinylcheck-restart").start()
        threading.Thread(target=station.learn_loop, daemon=True, name="vinylcheck-learning").start()
        station.vm.start()
        station.checker.start()
        yield
        station.checker.stop()
        station.vm.stop()
        station.worker.close()
        if station.recorder.active:
            station.recorder.stop()

    # No generated API documentation (/docs, /redoc, /openapi.json): the interface does not use it, and a service in
    # a network has no reason to describe itself to whoever asks
    app = FastAPI(title="vinyl-check", version=__version__, lifespan=lifespan, default_response_class=LocalizedJSONResponse,
                  docs_url=None, redoc_url=None, openapi_url=None)
    cfg, db = station.cfg, station.db
    network.install(app, cfg, db, station.desktop)  # access check, /access, /api/network; inside LanguageMiddleware
    licenses.install(app)                           # /licenses: own license and third-party notices
    vinylmind.install(app, station.vm)              # /api/vinylmind*: Settings → Vinyl-Mind
    storage.install(app, cfg, db, station.mover)    # /api/storage*: Settings → Storage, moving the user-data folder
    updates.install(app, station.checker)           # /api/updates*: is there a newer version
    testrecords.install(app, db)                    # /api/testrecords*: the test records the wizard knows
    onboarding.install(app, db)                     # /api/onboarding: the tutorial of a new station
    if cfg.language != "auto" and not i18n.supported(cfg.language):
        raise ValueError(f"Unknown language {cfg.language!r}: use 'auto' or one of {', '.join(i18n.languages())}")
    app.add_middleware(LanguageMiddleware, default=cfg.language)
    app.add_middleware(_WriteClock, clock=station.restart_cfg)
    app.state.desktop = station.desktop

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        # detail may be an i18n message (or contain some); it is rendered in the language of the request
        return LocalizedJSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    # what the desktop app and the tests reach for
    app.state.restart = station.restart_cfg
    app.state.cfg, app.state.db, app.state.worker, app.state.recorder = cfg, db, station.worker, station.recorder
    app.state.mover, app.state.updates = station.mover, station.checker
    audio_settings.install(app, station.aud, station.recorder, config_file=str(config_file()[0]),
                           active_profile=db.active_profile)

    # The areas of the API (vinylcheck/api/). No route of one area can match a request meant for another, so their
    # order does not matter; within an area the routes keep the order they are written in.
    for part in (api.system, api.records, api.masters, api.recorder, api.recordings, api.turntables,
                 api.discogs_account):
        part.install(app, station)

    # Never cache index.html itself and request app.js/style.css with the version: otherwise, after an update, the
    # browser could run the new page with the old script.
    index_html = (STATIC / "index.html").read_text(encoding="utf-8").replace("__VERSION__", __version__)

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    def index():
        # the page declares the language the interface will be shown in
        page = index_html.replace('<html lang="en">', f'<html lang="{i18n.current_lang()}">', 1)
        return Response(page, media_type="text/html", headers={"Cache-Control": "no-cache"})

    # Catalog of the web interface in the language of the request, loaded by index.html before app.js
    @app.get("/i18n/catalog.js", include_in_schema=False)
    def i18n_catalog():
        return Response(i18n.web_catalog_js(i18n.current_lang()), media_type="text/javascript; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app

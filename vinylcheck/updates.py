"""Is there a newer version? Once a day the station asks GitHub for the latest release of vinyl-check, and the
interface shows a note (header, System, Settings → Updates) when it is newer than the version running.

Nothing but the version is sent, in the User-Agent; GitHub sees the address the request comes from, as with any
download. `update_check = false` in the configuration file, or the switch under Settings → Updates, turns it off.
Drafts and pre-releases are never offered (GitHub's "latest release" leaves them out).

The note links to the release page. Installing an update from the interface is planned per platform (the wheel of
a service, the desktop installers) and is not part of this module.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request

from . import __version__, tls

log = logging.getLogger("vinylcheck.updates")

REPO = "michaelpietzsch/vinyl-check"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
EVERY_S = 24 * 3600                  # how often to ask
FIRST_AFTER_S = 60.0                 # the first question after a start waits a little (tests replace it)
S_STATE = "update_check_state"       # settings key: the last answer, kept across restarts


def parse(version: str) -> tuple[int, int, int] | None:
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (version or "").strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def newer(latest: str, current: str = __version__) -> bool:
    a, b = parse(latest), parse(current)
    return bool(a and b and a > b)


def fetch(timeout: float = 10.0) -> dict:
    """The latest release on GitHub: {latest, url, published_at}. Raises OSError or ValueError."""
    req = urllib.request.Request(LATEST_URL, headers={"Accept": "application/vnd.github+json",
                                                      "User-Agent": f"vinyl-check/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout, context=tls.context()) as r:
        data = json.loads(r.read(1_000_000))
    tag = str(data.get("tag_name") or "")
    if data.get("draft") or data.get("prerelease") or not parse(tag):
        raise ValueError(f"no release version in {tag!r}")
    url = str(data.get("html_url") or "")
    if not url.startswith(f"https://github.com/{REPO}/releases/"):    # shown as a link: only this repository's pages
        url = RELEASES_PAGE
    return {"latest": tag.lstrip("v"), "url": url, "published_at": data.get("published_at")}


class Checker:
    """Asks once a day while `cfg.update_check` is on; `status()` is what the interface shows."""

    def __init__(self, cfg, db, fetch=fetch):
        self.cfg, self.db, self._fetch = cfg, db, fetch
        self._stop = threading.Event()
        self._lock = threading.Lock()
        try:
            st = json.loads(db.setting(S_STATE) or "{}")
        except ValueError:
            st = {}
        self.state: dict = st if isinstance(st, dict) else {}

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="vinylcheck-updates").start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        if self._stop.wait(FIRST_AFTER_S):
            return
        while True:
            if self.cfg.update_check and time.time() - (self.state.get("checked_at") or 0) >= EVERY_S:
                self.check()
            if self._stop.wait(3600):                   # the switch and the time are looked at again every hour
                return

    def check(self) -> dict:
        with self._lock:
            try:
                found = self._fetch()
                self.state = {**found, "checked_at": time.time(), "error": None}
                if newer(found["latest"]):
                    log.info("vinyl-check %s is available (running %s): %s", found["latest"], __version__,
                             found["url"])
            except (OSError, ValueError, urllib.error.URLError) as e:
                self.state = {**self.state, "checked_at": time.time(), "error": str(e)[:200]}
                log.info("Looking for a new version failed: %s", e)
            self.db.set_setting(S_STATE, json.dumps(self.state))
        return self.status()

    def status(self) -> dict:
        st, on = self.state, bool(self.cfg.update_check)
        latest = st.get("latest")
        return {"enabled": on, "current": __version__, "latest": latest,
                "available": bool(on and latest and newer(latest)), "url": st.get("url") or RELEASES_PAGE,
                "published_at": st.get("published_at"), "checked_at": st.get("checked_at"), "error": st.get("error")}


def install(app, checker: Checker) -> None:
    """Routes: GET /api/updates, POST /api/updates/check (look now)."""

    @app.get("/api/updates")
    def updates_status():
        return checker.status()

    @app.post("/api/updates/check")
    def updates_check():
        return checker.check()

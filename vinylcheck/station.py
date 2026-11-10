"""The running station: what the web interface and the API share.

`Station` holds the parts of one installation (configuration, database, analysis worker, recorder, Discogs client,
Vinyl-Mind, the learned model) and the operations more than one area of the API needs: counting a stored analysis
again, a record's tracks, side lengths and genres, the Discogs credentials in effect, the masters' files, the target
maps, the restart when idle. `web.create_app` builds one and hands it to the modules in `vinylcheck/api/`, which add
their routes.

Building a Station starts what the installation needs from the first moment (the Discogs sync in the background);
the worker, the learning loop and Vinyl-Mind start with the app (`web.create_app`, its lifespan).
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from dataclasses import replace
from pathlib import Path

from fastapi import HTTPException

from . import (__version__, alignment, analysis, audio_settings, capture, discogs, grading, i18n, learning, skips,
               storage, updates, vinylmind)
from .config import Config, load
from .db import DB
from .jobs import Worker

from .api.common import (CHECK_KINDS, DISCOGS_ACCOUNT, KIND_LABEL, log, _setting_value, SETTINGS, TRIM_MARGIN_S,
                         TRIM_MIN_S)


class Station:
    """One installation of vinyl-check, as the web interface sees it. `cfg` defaults to the configuration file;
    `source_factory(device, rate, channels, fmt)` replaces the capture source (tests); `desktop`: the desktop app
    (loopback only, settings instead of a configuration file)."""

    def __init__(self, cfg: Config | None = None, source_factory=None, desktop: bool = False):
        self.source_factory, self.desktop = source_factory, desktop
        self.cfg = cfg or load()
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.db = DB(self.cfg.db_path)
        config_data_dir = storage.apply_saved(self.cfg, self.db)      # user-data folder chosen under Settings → Storage
        self.defaults = {k: getattr(self.cfg, k) for k in SETTINGS}
        for k, raw in self.db.get_settings().items():      # stored settings apply from startup
            if k in SETTINGS:
                try:
                    # Config is frozen; one shared instance
                    object.__setattr__(self.cfg, k, _setting_value(SETTINGS[k], raw))
                except ValueError:
                    pass
        self.worker = Worker(self.cfg, self.db)

        # arecord (ALSA) on the Pi, PortAudio on macOS/Windows: chosen once at startup (a typo in audio_backend stops
        # here)
        self.audio_backend = capture.resolve_backend(self.cfg.audio_backend)
        # Device, rate, bit depth and inputs as set under Settings → Audio interface, else as configured
        # (audio_settings.py); read whenever a stream opens, so changes apply from the next recording
        self.aud = audio_settings.AudioSettings(self.cfg, self.db, self.audio_backend)

        self.recorder = capture.Recorder(self.source_factory or self.default_factory)
        # Moving the user-data folder (storage.py): no recording, master upload or retention cleanup while it runs
        self.mover = storage.Mover(self.cfg, self.db, recording=lambda: self.recorder.active,
                                   analysing=lambda: bool(self.worker.status()["current"]), service=not self.desktop,
                                   config_dir=config_data_dir)
        self.worker.storage_busy = lambda: self.mover.active
        self.checker = updates.Checker(self.cfg, self.db)
        # Discogs: the collection is cached in the state directory, synced at startup (in the background) and
        # periodically. Credentials, in this order: a personal access token saved under Settings → Discogs (settings
        # row DISCOGS_ACCOUNT; desktop users have no configuration file to edit), the one in the configuration file,
        # an OAuth authorization.
        self.dc = discogs.Discogs(self.cfg.discogs_token, self.cfg.discogs_username,
                                  self.cfg.state_dir / "discogs-collection.json",
                                  f"vinyl-check/{__version__} +https://github.com/vinyl-check",
                                  consumer_key=self.cfg.discogs_consumer_key,
                                  consumer_secret=self.cfg.discogs_consumer_secret)

        self.discogs_apply()
        self.dc.ttl = self.cfg.discogs_sync_minutes * 60
        if self.dc.stale():
            self.dc.sync_async()
        threading.Thread(target=self._discogs_loop, daemon=True, name="vinylcheck-discogs-loop").start()

        # ─── Restart when idle (deploy) ──────────────────────────────────────────
        # deploy.sh stages new code in /opt/vinyl-check.staged and sets this flag while something is running. As soon
        # as neither a recording nor a job is running, the service stops cleanly (SIGTERM to itself), systemd starts it
        # again (Restart=always) and ExecStartPre swaps the code in. A flag older than this process does not count:
        # otherwise a failed delete would cause a restart loop. The file name is shared with deploy.sh; versions
        # before 1.0 used "restart-wenn-frei", and deploy.sh writes both names, because the service running during a
        # deploy may be old.
        self.restart_flags = [self.cfg.state_dir / "restart-when-idle", self.cfg.state_dir / "restart-wenn-frei"]
        self.started_at = time.time()

        # "quiet": seconds without a change made in the interface (any request but GET/HEAD/OPTIONS, noted by
        # _WriteClock) before the restart, so that it does not cut into someone working there; tests replace
        # do/poll/quiet
        self.restart_cfg = {"poll": 3.0, "do": self._restart_now, "requested": False, "quiet": 60.0, "last_write": 0.0}
        # latest notifications for the browser (push without external services)
        self.ui_events: list = []

        self.genres_missed: dict[int, float] = {}          # record id → when Discogs could not be asked (retry later)

        self.worker.on_master = lambda rec_id, res: self.push_event(
            "master", i18n.msg("api.event_master_done" if res.get("ok") else "api.event_master_mismatch", id=rec_id),
            rec_id)
        self.worker.on_analyzed = lambda rec_id, res: self.push_event(
            "analyzed", i18n.msg("api.event_analyzed_grade", id=rec_id, grade=res["grading"]["grade"])
            if (res.get("grading") or {}).get("grade") else i18n.msg("api.event_analyzed", id=rec_id), rec_id)

        self.app_started_at = time.time()

        # ─── Learning: model from master comparisons and judgments, estimated genuine clicks ─────────────────────────
        try:
            _lm = json.loads(self.db.setting("learn_model") or "null")
        except ValueError:
            _lm = None
        if _lm is not None and learning.model_version(_lm) != self.cfg.feature_version:
            # a model of another feature version computes other features: never apply it; the learning loop retrains
            log.info("Stored learned model is feature version %s, configured is %s: retraining",
                     learning.model_version(_lm), self.cfg.feature_version)
            _lm = None
        self.learn = {"model": _lm, "fp": None, "pending_fp": None, "lock": threading.Lock(), "last_error": None,
                      "applied_at": None, "applied": 0, "data": None}

        # Vinyl-Mind (vinylmind.py): contributions and the global model; nothing is sent while a recording runs
        self.vm = vinylmind.VinylMind(self.cfg, self.db, busy=lambda: self.recorder.active, retrain=self.learn_train,
                                      desktop=self.desktop)

        self.ref_cache: dict = {"key": None, "val": None}
        self.dc.on_synced = self.after_sync               # set last: everything it uses exists by now

    def capture_devices(self) -> list[dict]:
        return self.aud.devices()

    def default_factory(self, device, rate, channels, fmt):
        if self.cfg.fake_source:
            return capture.FileSource(self.cfg.fake_source, rate, channels, fmt, self.cfg.fake_speed)
        return capture.open_source(self.audio_backend, device, rate, channels, fmt, **self.aud.source_kwargs(device))

    def discogs_account(self) -> dict:
        """Token and user name saved in the settings ({} if none)."""
        try:
            d = json.loads(self.db.setting(DISCOGS_ACCOUNT) or "{}")
        except ValueError:
            return {}
        return d if isinstance(d, dict) and d.get("token") else {}

    def discogs_apply(self) -> bool:
        """Hand the credentials in effect to the client, so a change applies without a restart. True if they changed
        (the caller syncs)."""
        acct, st = self.discogs_account(), self.db.get_settings()
        otok, osec = st.get("discogs_oauth_token") or "", st.get("discogs_oauth_secret") or ""
        if acct:
            token, user = acct["token"], acct.get("username") or ""
        elif self.cfg.discogs_token:
            token, user = self.cfg.discogs_token, self.cfg.discogs_username
        else:                                   # OAuth: the name Discogs reported when the account was authorized
            token, user = "", (st.get("discogs_username") if otok and osec else "") or self.cfg.discogs_username
        return self.dc.configure(token, user, otok, osec)

    def discogs_source(self) -> str | None:
        """Where the credentials in use come from: "ui", "config", "oauth", or None (Discogs is off)."""
        if not self.dc.enabled:
            return None
        if self.dc.token:
            return "ui" if self.discogs_account() else "config"
        return "oauth"

    def discogs_off(self) -> dict:
        """Why a Discogs feature is unavailable, with where to set it up (the settings first; a service also has its
        configuration file)."""
        return i18n.msg("api.discogs_off_desktop" if self.desktop else "api.discogs_off_service")

    def _discogs_loop(self):
        """Re-sync the collection regularly (setting discogs_sync_minutes) so that records newly added on Discogs show
        up without any action."""
        while True:
            time.sleep(60)
            try:
                self.dc.ttl = self.cfg.discogs_sync_minutes * 60
                if self.dc.stale():
                    self.dc.sync_async()
            except Exception:  # noqa: BLE001
                pass

    def restart_pending(self) -> bool:
        if self.desktop:       # only a service is restarted (by systemd); the desktop app has no staged code
            return False
        for flag in self.restart_flags:
            try:
                if flag.stat().st_mtime >= self.started_at - 1:
                    return True
            except OSError:
                pass
        return False

    def _restart_now(self) -> None:
        for flag in self.restart_flags:
            try:
                flag.unlink()
            except OSError:
                pass
        os.kill(os.getpid(), signal.SIGTERM)
        threading.Timer(15, lambda: os._exit(0)).start()   # in case uvicorn hangs (open WebSockets)

    def restart_loop(self) -> None:
        while True:
            time.sleep(self.restart_cfg["poll"])
            if self.restart_cfg["requested"] or not self.restart_pending():
                continue
            ws = self.worker.status()
            if self.recorder.active or ws["current"] or ws["queued"] or self.mover.active:  # a move continues after a
                continue                                                           # restart, but from its start
            if time.time() - self.restart_cfg["last_write"] < self.restart_cfg["quiet"]:
                continue
            log.info("New code is staged and nothing is running: restarting the service")
            self.restart_cfg["requested"] = True
            self.restart_cfg["do"]()

    def push_event(self, kind: str, text, rec_id: int | None = None) -> None:
        """Queue a notification for the browser (toast / desktop notification); `text` is an i18n message."""
        self.ui_events.append({"id": int(time.time() * 1000), "kind": kind, "text": text, "recording_id": rec_id, "at": time.time()})
        del self.ui_events[:-20]

    def side_lengths(self, tracks: dict) -> dict:
        """Side length = sum of its tracks, only when every duration on that side is known."""
        return {s: round(sum(x["dur_s"] for x in lst), 1) for s, lst in tracks.items()
                if lst and all(x["dur_s"] is not None for x in lst)}

    def ensure_tracks(self, r: dict) -> dict:
        """Fetch the tracks per side from Discogs once and keep them on the record ({} = nothing found); also derive
        the side lengths when every duration of a side is known."""
        if r.get("tracks"):
            try:
                return json.loads(r["tracks"])
            except ValueError:
                pass
        if not r.get("discogs_release_id"):
            return {}
        try:
            tracks = self.dc.release_tracks(int(r["discogs_release_id"]))
        except discogs.DiscogsError:
            return {}
        sides = self.side_lengths(tracks)
        self.db.update_record(r["id"], tracks=json.dumps(tracks), sides=json.dumps(sides))
        r["tracks"], r["sides"] = json.dumps(tracks), json.dumps(sides)
        return tracks

    def ensure_sides(self, r: dict) -> dict:
        """Fetch the side lengths from Discogs once and keep them on the record ({} = nothing found)."""
        if r.get("sides"):
            try:
                return json.loads(r["sides"])
            except ValueError:
                pass
        if not r.get("discogs_release_id"):
            return {}
        self.ensure_tracks(r)
        if r.get("sides"):
            try:
                return json.loads(r["sides"])
            except ValueError:
                pass
        try:
            sides = self.dc.release_side_lengths(int(r["discogs_release_id"]))
        except discogs.DiscogsError:
            return {}
        self.db.update_record(r["id"], sides=json.dumps(sides))
        return sides

    def ensure_genres(self, r: dict) -> bool:
        """Fetch the Discogs genres and styles of a record once and keep them on it: they give the default of its
        repeat rule (analysis.record_repeat_rule). When that default changes the rule the record is counted with,
        its sides are counted and graded again; returns True then."""
        if r.get("genres") is not None or not r.get("discogs_release_id"):
            return False
        if time.time() - self.genres_missed.get(r["id"], 0) < 600:
            return False
        try:
            g = self.dc.release_genres(int(r["discogs_release_id"]))
        except Exception:  # noqa: BLE001 — Discogs unreachable or an odd answer: the rule stays as it was, retry later
            self.genres_missed[r["id"]] = time.time()
            return False
        before = analysis.record_repeat_rule(r, self.cfg.analysis.repeat_rule)["mode"]
        r["genres"] = json.dumps(g)
        self.db.update_record(r["id"], genres=r["genres"])
        if analysis.record_repeat_rule(r, self.cfg.analysis.repeat_rule)["mode"] == before:
            return False
        for rec in self.db.recordings(r["id"]):
            self.recount(rec["id"])
        self.ref_cache["key"] = None
        return True

    def after_sync(self) -> None:
        """After a Discogs sync: the condition snapshot of every record held once in the collection follows Discogs
        (the record list shows it), and records without genres and styles get them (from the synced collection, no
        further requests)."""
        for r in self.db.q("SELECT id, discogs_release_id, discogs_media_condition FROM records WHERE discogs_release_id IS NOT NULL"):
            cs = [e for e in self.dc.releases() if e.get("release_id") == int(r["discogs_release_id"])]
            if len(cs) == 1 and cs[0].get("media") and cs[0]["media"] != r["discogs_media_condition"]:
                self.db.update_record(r["id"], discogs_media_condition=cs[0]["media"])
        for r in self.db.q("SELECT * FROM records WHERE genres IS NULL AND discogs_release_id IS NOT NULL"):
            if "genres" in (self.dc.entry(r["discogs_release_id"]) or {}):
                try:
                    if self.ensure_genres(r):
                        log.info("Record %s: repeat rule from its Discogs genres, sides counted again", r["id"])
                except Exception:  # noqa: BLE001
                    log.exception("Genres of record %s", r["id"])

    def learn_model(self) -> dict | None:
        return self.learn["model"] if self.cfg.learn_enabled else None

    def learn_zp(self, rec: dict | None) -> dict | None:
        """The recording's zero point (learning.zero_point of its turntable profile) for a learned model of feature
        version 2; None for version 1, which does not use it (and then costs no lookup)."""
        model = self.learn_model()
        if not model or learning.model_version(model) == "1" or not rec:
            return None
        pid = rec.get("profile_id")
        return learning.zero_point(self.db.profile(pid) if pid else None)

    def acfg_for(self, rec_id: int) -> analysis.AnalysisConfig:
        """The analysis settings for counting a recording: the configuration, with the record's repeat rule (its own
        choice, else the default from its Discogs genres, see analysis.record_repeat_rule) and its mono switch."""
        row = self.db.one("SELECT rc.repeat_rule, rc.mono, rc.genres FROM recordings r JOIN records rc ON rc.id = r.record_id "
                          "WHERE r.id = ?", (rec_id,)) or {}
        rule = analysis.record_repeat_rule(row, self.cfg.analysis.repeat_rule)["mode"]
        return replace(self.cfg.analysis, repeat_rule=rule, mono=bool(row.get("mono")))

    def recount(self, rec_id: int, change=None, count: bool = True) -> tuple[dict, dict] | None:
        """Count and grade a stored analysis again from its events, with the verdicts on events and skips, the
        record's repeat rule and the learned model, and store it.

        This is the one place where a stored analysis changes after the worker wrote it. From reading it to writing
        it nothing else changes it (db.analysis_lock, which the worker holds as well when it stores a fresh
        analysis), so no change is lost to another one; and it is written in one statement, so whoever reads it
        sees it before or after, never new hits with the old count.

        `change(metrics)` alters the metrics before the count (returning False leaves the analysis as it is);
        `count=False` stores what `change` did without counting again. Returns (metrics, grading), or None if the
        recording has no analysis, or nothing in it to count and nothing changed."""
        with self.db.analysis_lock:
            r = self.db.recording(rec_id)
            a = (r or {}).get("analysis")
            if not a:
                return None
            metrics, grading_ = a.get("metrics") or {}, a.get("grading") or {}
            warnings = list(a.get("warnings") or [])
            if change is not None and change(metrics) is False:
                return None
            if count and metrics.get("counts"):
                metrics, grading_ = analysis.recount(metrics, self.db.events(rec_id), self.cfg.grading,
                                                     skip_labels=self.db.skip_labels(rec_id), warnings=warnings,
                                                     learned=self.learn_model(), learn_grade=self.cfg.learn_grade,
                                                     zero_point=self.learn_zp(r), acfg=self.acfg_for(rec_id))
            elif change is None:
                return None                           # analyses without counts (very old ones) are left alone
            self.db.update_analysis(rec_id, metrics, grading_, warnings)
            return metrics, grading_

    def learn_train(self) -> dict:
        """Retrain (takes a fraction of a second) and store the model. A Vinyl-Mind prior (an adopted global model or
        an imported package) is where the ridge penalty pulls the weights instead of towards zero. Features of the
        configured feature version; the prior is only used if it has the same version (vm.prior_info)."""
        with self.learn["lock"]:
            data, overview = learning.training_data(self.db, version=self.cfg.feature_version)
            prior = self.vm.prior_info()
            model = learning.train(data, prior=prior["kinds"] if prior else None, version=self.cfg.feature_version)
            model["data"] = overview
            if prior:
                model["prior"] = {k: prior[k] for k in ("source", "type", "ref")}
            self.learn["model"], self.learn["data"] = model, overview
            self.db.set_setting("learn_model", json.dumps(model))
            log.info("Learned model %s: %s", model["id"], {k: v.get("usable") for k, v in model["kinds"].items()})
        self.vm.note_retrained()                  # contributions may have changed: the next round looks
        return model

    def learn_apply(self, limit: int | None = None) -> int:
        """Recount every analysis whose estimate does not match the current model and switches."""
        mid = (self.learn_model() or {}).get("id")
        n = 0
        for r in self.db.q("SELECT recording_id, json_extract(metrics, '$.learned.model_id') AS mid, "
                           "json_extract(metrics, '$.learned.graded') AS gr, json_extract(metrics, '$.counts') AS c FROM analyses"):
            if not r["c"]:
                continue
            want_graded = bool(mid) and self.cfg.learn_grade
            if r["mid"] == mid and (bool(r["gr"]) == want_graded or not mid):
                continue
            try:
                self.recount(r["recording_id"])
                n += 1
            except Exception:  # noqa: BLE001
                log.exception("Recount with learned model failed: recording %s", r["recording_id"])
            if limit and n >= limit:
                break
        if n:
            self.ref_cache["key"] = None
            self.learn["applied_at"], self.learn["applied"] = time.time(), n
            log.info("Learned model %s applied to %d recordings", mid, n)
        return n

    def learn_fp(self):
        r = self.db.one("SELECT (SELECT COUNT(*) FROM event_labels) AS a, "
                        "(SELECT MAX(created_at) FROM event_labels) AS b, "
                        "(SELECT COUNT(*) FROM master_checks WHERE status = 'done') AS c, "
                        "(SELECT MAX(created_at) FROM master_checks WHERE status = 'done') AS d") or {}
        fp = (r.get("a"), r.get("b"), r.get("c"), r.get("d"))
        if self.cfg.feature_version != "1":
            # feature version 2 computes the features from the profiles: a newly accepted or re-measured profile
            # changes the training data as much as a new judgement
            fp += tuple((p["id"], p.get("accepted_at"), p.get("chain_db"), p.get("ref_dbfs"),
                         p.get("floor_clicks_per_min"), p.get("floor_ticks_per_min"))
                        for p in self.db.q("SELECT * FROM profiles ORDER BY id"))
        return fp

    def learn_loop(self) -> None:
        """Every 30 s: if judgments or master comparisons changed (and stayed so for one more round), retrain; then
        recount everything that does not match the model (freshly analyzed recordings too)."""
        while True:
            time.sleep(30)
            try:
                fp = self.learn_fp()
                if fp != self.learn["fp"]:
                    if self.learn["pending_fp"] == fp or self.learn["model"] is None:
                        self.learn_train()
                        self.learn["fp"], self.learn["pending_fp"] = fp, None
                    else:
                        self.learn["pending_fp"] = fp     # wait one more round in case several judgments are coming in
                        continue
                self.learn_apply()
                self.learn["last_error"] = None
            except Exception as e:  # noqa: BLE001
                self.learn["last_error"] = str(e)
                log.exception("Learning failed")

    def regrade_all(self, force: bool = False) -> int:
        """Regrade all stored analyses with the current grading scheme (without recomputing the audio): at startup
        when grading.VERSION changed, or on request. Analyses without counts (very old ones) are left alone."""
        if not force and self.db.setting("grading_version") == str(grading.VERSION):
            return 0
        n = 0
        for row in self.db.q("SELECT recording_id FROM analyses"):
            # from the stored events: a new scheme may also change the metrics (e.g. pops counted only in the music);
            # the audio is not recomputed
            try:
                n += bool(self.recount(row["recording_id"]))
            except ValueError:                                    # metrics that are no JSON: nothing to regrade
                continue
        self.db.set_setting("grading_version", str(grading.VERSION))
        self.ref_cache["key"] = None
        log.info("Grading scheme %s: %d analyses regraded", grading.VERSION, n)
        return n

    def recheck_skips(self) -> int:
        """Groove skips found before the waveform check (skips.VERSION) are judged again on the audio, once per
        analysis: a scratch that clicks every revolution was reported as a skip back and cost the side its grade.
        Only the excerpts around the hits are read; an analysis whose audio is gone keeps its hits. Returns the
        number of analyses that changed."""
        n = 0
        for row in self.db.q("SELECT a.recording_id AS id FROM analyses a WHERE "
                             "COALESCE(json_extract(a.metrics, '$.skips_v'), 1) < ?", (skips.VERSION,)):
            rid = row["id"]
            try:
                r = self.db.recording(rid)
                m = ((r or {}).get("analysis") or {}).get("metrics") or {}
                if not r or r["status"] != "analyzed" or "skips" not in m or self.mover.active:
                    continue
                todo = any(e.get("kind_auto", e.get("kind")) in ("backward", "uncertain") for e in m["skips"])
                if todo and (r.get("audio_deleted_at") or not r.get("path") or not os.path.exists(r["path"])):
                    continue
                changed = analysis.recheck_skips(r["path"], m) if todo else False

                def put(cur: dict, hits=m["skips"]):
                    if cur.get("skips_v", 1) >= skips.VERSION:
                        return False                  # analysed anew in the meantime: its hits are current
                    cur["skips"], cur["skips_v"] = hits, skips.VERSION      # only the skips: the rest stays
                # the hits, their count and the grade in one write; unchanged hits only get their mark
                self.recount(rid, change=put, count=bool(changed))
                n += changed
            except Exception:  # noqa: BLE001 — one unreadable file must not stop the others
                log.exception("Judging the groove skips of recording %s again failed", rid)
        if n:
            self.ref_cache["key"] = None
            log.info("Groove skips judged again with the waveform check: %d analyses changed", n)
        return n

    # ─── Master (engineer mode) ──────────────────────────────────────────────
    # The digital master per record side (e.g. the vinyl masters from the mastering studio as WAV). Stored in the data
    # directory as masters/<record>-<side>.<ext>; the retention period applies to recordings only, not to masters.
    def master_dir(self) -> Path:                        # follows a move of the user-data folder at run time
        return self.cfg.data_dir / "masters"

    def master_info(self, m: dict | None) -> dict | None:
        return None if not m else {k: m.get(k) for k in ("id", "side", "name", "samplerate", "channels", "subtype",
                                                           "duration_s", "bytes", "created_at")}

    def remove_master_file(self, m: dict | None) -> None:
        if not m or not m.get("path"):
            return
        f = Path(m["path"]).resolve()
        if self.master_dir().resolve() in f.parents and f.exists():
            f.unlink()

    def get_recording(self, rec_id: int):
        r = self.db.recording(rec_id)
        if not r:
            raise HTTPException(404, i18n.msg("api.recording_not_found"))
        r["kind_label"] = KIND_LABEL.get(r["kind"], r["kind"])
        if r["kind"] == "alignment" and r.get("analysis") and r["analysis"].get("metrics", {}).get("results"):
            _, targets = self.targets_for(r.get("profile_id"))
            r["analysis"]["metrics"]["results"] = alignment.apply_targets(r["analysis"]["metrics"]["results"], targets)
        r["file_exists"] = bool(r.get("path")) and Path(r["path"]).exists()
        r["audio_available"] = r["file_exists"] and not r.get("audio_deleted_at")
        r["overhang_s"] = self.overhang(r)
        return r

    def overhang(self, r: dict) -> float | None:
        """Seconds after the end of the groove (stylus up) that "Remove overhang" would cut, None below TRIM_MIN_S."""
        m = (r.get("analysis") or {}).get("metrics") or {}
        end = m.get("groove_end_s")
        if (end is None or not r.get("duration_s") or r["kind"] not in CHECK_KINDS or r["status"] != "analyzed"
                or not r.get("audio_available")):
            return None
        over = r["duration_s"] - (end + TRIM_MARGIN_S)
        return round(over, 1) if over >= TRIM_MIN_S else None

    def remove_audio(self, path: str | None):
        if not path:
            return
        p = Path(path).resolve()
        if self.cfg.rec_dir.resolve() in p.parents and p.exists():
            p.unlink()

    # ─── Alignment: target maps ─────────────────────────────────────────────
    # Built-in maps come from alignment.TARGET_MAPS; changes and custom maps are stored in the database (settings
    # target_maps, target_map_active). The map that applies: the profile's, else the global one.
    def maps(self) -> dict:
        maps = {k: {"id": k, "name": v["name"], "note": v["note"], "builtin": True, "modified": False, "targets": v["targets"]}
                for k, v in alignment.TARGET_MAPS.items()}
        try:
            custom = json.loads(self.db.get_settings().get("target_maps") or "{}")
        except ValueError:
            custom = {}
        for k, v in custom.items():
            base = maps.get(k)
            maps[k] = {"id": k, "name": v.get("name") or (base or {}).get("name") or k, "note": v.get("note", (base or {}).get("note", "")),
                       "builtin": bool(base), "modified": bool(base), "targets": {**((base or {}).get("targets") or {}), **v.get("targets", {})}}
        return maps

    def active_map_id(self) -> str:
        mid = self.db.get_settings().get("target_map_active") or "standard"
        return mid if mid in self.maps() else "standard"

    def targets_for(self, profile_id: int | None) -> tuple[str, dict]:
        maps = self.maps()
        mid = None
        if profile_id:
            p = self.db.profile(profile_id)
            mid = (p or {}).get("target_map")
        if not mid or mid not in maps:
            mid = self.active_map_id()
        return mid, maps[mid]["targets"]

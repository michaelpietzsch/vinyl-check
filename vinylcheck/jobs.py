"""Background worker: analyses, comparisons and master checks run one after another, so the web interface stays
responsive.

A single worker thread is enough: one LP side takes a few seconds to a few minutes on a Raspberry Pi 5 and needs
about 1–2 GB of RAM; two jobs in parallel would only compete for memory and CPU. The number crunching itself runs
in a low-priority child process (heavy.py, `run_heavy`).

Data integrity: the first time a recording is processed, the SHA-256 of its WAV file is stored. Before every
further analysis or comparison it is computed again. If it differs (bit errors, a file truncated after a storage
failure …), the job stops with a clear error instead of computing on corrupted data.

Retention: the recordings disk is only a buffer. Original audio is deleted after `retention_days`; measurements,
clicks, grades and comparisons stay in the database. Audio that is being recorded, analyzed or compared is never
deleted.

Errors a user should see are stored as i18n messages (see db.py), so they are shown in the reader's language.

Shutting down (`close`): the child process is ended and the pool's queues are released before the process exits, so
the multiprocessing resource tracker has no semaphores left to warn about. A job interrupted by the shutdown keeps
its state in the database and runs again at the next start.
"""

from __future__ import annotations

import collections
import concurrent.futures as cf
import gc
import hashlib
import json
import logging
import multiprocessing as mp
import os
import queue
import threading
import time
import traceback
from dataclasses import replace


from . import __version__, analysis, detect, grading, heavy, i18n
from .analysis import _py as _jsonable
from .config import Config
from .db import DB

log = logging.getLogger("vinylcheck.jobs")
# A calibration records the chain noise with the stylus lifted. Anything louder than this means the stylus was
# down; using that value would later make whole grooves count as "stylus up".
CALIBRATION_MAX_DBFS = -65.0
MASTER_KINDS = ("before", "after", "after_wet", "after_dry", "other")   # record checks, not calibration/alignment
RETENTION_CHECK_S = 3600
# An idle child process ends within a fraction of a second once the pool is shut down; a few seconds leave room for
# a slow disk or a busy computer before it is terminated
CLOSE_TIMEOUT_S = 3.0


class ShuttingDown(Exception):
    """The worker is closing: the job was interrupted, not failed, and keeps its state for the next start."""


def sha256_file(path: str) -> str:
    """SHA-256 of a file as hex string (read in 4 MB blocks)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Worker:
    """Job queue with one worker thread; also runs the retention clean-up."""

    def __init__(self, cfg: Config, db: DB):
        self.cfg, self.db = cfg, db
        self.storage_busy = lambda: False           # set by web.py: a move of the user-data folder runs
        self.q: queue.Queue = queue.Queue()
        self.current: dict | None = None
        self.completed = 0            # number of finished jobs (cache key for derived values)
        self.pool: cf.ProcessPoolExecutor | None = None               # child process for heavy.py, nice 10
        self.pending: list[tuple[str, int]] = []                      # queued jobs (for the "System" view)
        self.history: collections.deque = collections.deque(maxlen=30)   # recently finished jobs with duration
        self._t = threading.Thread(target=self._loop, daemon=True, name="vinylcheck-worker")
        self._closing = threading.Event()
        self._pool_lock = threading.Lock()                            # creating and taking away the pool
        self._close_lock = threading.Lock()                           # a second close() waits for the first

    def start(self) -> None:
        partial = self.db.reset_partial_checksums()
        if partial:
            log.info("Recording(s) %s were analysed while still being recorded: checked and analysed again", partial)
        # Re-queue jobs that were left unfinished by a restart
        for r in self.db.q("SELECT id FROM recordings WHERE status IN ('queued', 'analyzing')"):
            self.analyze(r["id"])
        for c in self.db.q("SELECT id, a_id, b_id FROM comparisons WHERE status IN ('queued', 'running')"):
            self.pending.append(("compare", c["id"]))
            self.q.put(("compare", c["id"]))
        for c in self.db.q("SELECT recording_id FROM master_checks WHERE status IN ('queued', 'running')"):
            self.pending.append(("master", c["recording_id"]))
            self.q.put(("master", c["recording_id"]))
        self._t.start()
        # always runs; cleanup() checks retention_days on every pass (the setting can change at run time)
        threading.Thread(target=self._retention_loop, daemon=True, name="vinylcheck-retention").start()

    def analyze(self, rec_id: int) -> None:
        """Queue the analysis of a recording (not while it is being recorded: the recorder queues it at the end)."""
        r = self.db.recording(rec_id)
        if r and r["status"] == "recording":
            return
        self.db.update_recording(rec_id, status="queued", error=None)
        self.pending.append(("analyze", rec_id))
        self.q.put(("analyze", rec_id))

    def compare(self, a_id: int, b_id: int) -> int:
        """Queue a before/after comparison of two recordings; returns the comparison id."""
        cid = self.db.add_comparison(a_id, b_id)
        self.pending.append(("compare", cid))
        self.q.put(("compare", cid))
        return cid

    def master_check(self, rec_id: int) -> None:
        """Queue measuring a recording against the master of its side (engineer mode, master.py)."""
        self.db.set_master_check(rec_id, status="queued", error=None)
        if ("master", rec_id) not in self.pending:
            self.pending.append(("master", rec_id))
            self.q.put(("master", rec_id))

    def run_heavy(self, fn, *args):
        """Run `fn(*args)` in the child process (low priority, separate memory). If it dies, start a new one next
        time and report the crash. Raises ShuttingDown once close() has begun."""
        with self._pool_lock:
            if self._closing.is_set():
                raise ShuttingDown()
            if self.pool is None:
                self.pool = cf.ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn"),
                                                   initializer=heavy.init_worker)
            pool = self.pool
            try:
                fut = pool.submit(fn, *args)
            except cf.process.BrokenProcessPool as e:
                self.pool = None
                raise RuntimeError(i18n.msg("jobs.worker_crashed", error=str(e))) from e
        try:
            return fut.result()
        except (cf.process.BrokenProcessPool, cf.CancelledError) as e:
            # close() ends the child under a running job: an interruption, not a crash worth reporting
            if self._closing.is_set():
                raise ShuttingDown() from e
            with self._pool_lock:
                if self.pool is pool:
                    self.pool = None
            raise RuntimeError(i18n.msg("jobs.worker_crashed", error=str(e))) from e

    def close(self, timeout: float = CLOSE_TIMEOUT_S) -> bool:
        """Stop taking jobs and end the child process: within `timeout` seconds when it is idle, at once when a job
        runs (the user chose to quit; the job runs again at the next start). Afterwards neither the process nor the
        pool's queues and semaphores are left. Returns True if the child ended by itself, False if it had to be
        terminated or did not end in time. Safe to call more than once and from several threads."""
        self._closing.set()
        with self._close_lock:
            with self._pool_lock:
                pool, self.pool = self.pool, None
            if pool is None:
                return True
            # shutdown() forgets the processes; keep them to terminate a child that does not end
            procs = list((getattr(pool, "_processes", None) or {}).values())
            # shutdown(wait=True) also joins the pool's manager thread, which holds the queues; in a helper thread, so
            # that waiting stays bounded
            stopper = threading.Thread(target=pool.shutdown, kwargs={"wait": True, "cancel_futures": True},
                                       daemon=True, name="vinylcheck-pool-stop")
            stopper.start()
            stopper.join(0 if self.current else timeout)
            clean = not stopper.is_alive()
            if not clean:
                for p in procs:
                    if p.is_alive():
                        p.terminate()
                stopper.join(timeout)
                if stopper.is_alive():
                    log.warning("The analysis process did not end within %.0f s", 2 * timeout)
                else:
                    log.info("Analysis process terminated; an interrupted job runs again at the next start")
            del pool, procs, stopper
            # The queues' semaphores are unlinked (and unregistered from the resource tracker) when collected; the
            # pool's objects may hold reference cycles, so collect now instead of at an exit that may never run
            # finalizers (os._exit in the desktop app)
            gc.collect()
            return clean

    def status(self) -> dict:
        return {"current": self.current, "queued": self.q.qsize(),
                "pending": [{"kind": k, "id": i} for k, i in self.pending], "history": list(self.history)}

    def _loop(self) -> None:
        while True:
            kind, ident = self.q.get()
            if self._closing.is_set():
                return                      # still "queued" in the database: queued again at the next start
            if (kind, ident) in self.pending:
                self.pending.remove((kind, ident))
            self.current = {"kind": kind, "id": ident, "since": time.time()}
            ok, err = True, None
            try:
                if kind == "analyze":
                    self._analyze(ident)
                elif kind == "master":
                    self._master(ident)
                else:
                    self._compare(ident)
            except ShuttingDown:
                # the state stays "analyzing"/"running", which start() queues again
                ok = False
                log.info("%s %s interrupted by shutting down; it runs again at the next start", kind, ident)
            except Exception as e:  # noqa: BLE001
                ok, err = False, i18n.error_message(e)       # stored as a message (db.py)
                log.error("%s %s failed: %s", kind, ident, traceback.format_exc())
                self.db.add_problem(kind, ident, err)                 # the station's error log (System tab)
                if kind == "analyze":
                    self.db.update_recording(ident, status="error", error=err)
                elif kind == "master":
                    self.db.set_master_check(ident, status="error", error=err)
                else:
                    self.db.update_comparison(ident, status="error", error=err)
            finally:
                self.history.appendleft({"kind": kind, "id": ident, "seconds": round(time.time() - self.current["since"], 1),
                                         "ok": ok, "error": err, "finished_at": time.time()})
                self.current = None
                self.completed += 1
                self.q.task_done()

    # ─── Integrity ───────────────────────────────────────────────────────────
    def verify_audio(self, rec: dict) -> None:
        """First time: store the checksum. After that it must match exactly, otherwise raise."""
        if rec.get("audio_deleted_at"):
            raise RuntimeError(i18n.msg("jobs.audio_deleted"))
        path = rec.get("path")
        if not path or not os.path.exists(path):
            raise RuntimeError(i18n.msg("jobs.audio_missing", path=str(path)))
        digest = sha256_file(path)
        if not rec.get("sha256"):
            self.db.update_recording(rec["id"], sha256=digest)
        elif digest != rec["sha256"]:
            raise RuntimeError(i18n.msg("jobs.integrity_error", id=rec["id"]))

    # ─── Retention ───────────────────────────────────────────────────────────
    def _retention_loop(self) -> None:
        time.sleep(60)
        while True:
            try:
                self.cleanup()
            except Exception:  # noqa: BLE001
                log.error("Retention run failed: %s", traceback.format_exc())
            time.sleep(RETENTION_CHECK_S)

    def cleanup(self, now: float | None = None) -> list[int]:
        """Delete original audio older than retention_days. Returns the ids of the affected recordings."""
        if self.cfg.retention_days <= 0 or self.storage_busy():
            return []                                  # nothing is deleted while the user-data folder moves
        cutoff = (now or time.time()) - self.cfg.retention_days * 86400
        busy = {self.current["id"]} if self.current and self.current["kind"] == "analyze" else set()
        done = []
        for r in self.db.retention_candidates(cutoff):
            if r["id"] in busy:
                continue
            try:
                if r["path"] and os.path.exists(r["path"]):
                    os.unlink(r["path"])
            except OSError as e:
                log.error("Retention: cannot delete %s: %s", r["path"], e)
                continue
            self.db.update_recording(r["id"], audio_deleted_at=time.time())
            done.append(r["id"])
        if done:
            log.info("Retention (%d days): deleted original audio of %d recording(s): %s",
                     self.cfg.retention_days, len(done), done)
        return done

    def _analyze(self, rec_id: int) -> None:
        rec = self.db.recording(rec_id)
        if not rec or not rec.get("path"):
            return
        if rec["status"] == "recording":
            # Still being recorded: its checksum and analysis would belong to an unfinished file. The recorder queues
            # the analysis itself when the side has ended.
            log.info("Recording %s is still being recorded: not analysed yet", rec_id)
            return
        self.db.update_recording(rec_id, status="analyzing")
        self.verify_audio(rec)
        t0 = time.time()
        if rec["kind"] == "calibration":
            chain = analysis.chain_noise_db(rec["path"])
            if chain > CALIBRATION_MAX_DBFS:
                # The stylus was evidently down: with this value whole grooves would later be discarded as
                # "stylus up", so do not take it over
                err = i18n.msg("jobs.calibration_too_loud", db=chain, max=CALIBRATION_MAX_DBFS)
                self.db.update_recording(rec_id, status="error", error=err)
                self.db.add_problem("analyze", rec_id, err)
                return
            self.db.update_recording(rec_id, chain_db=chain, status="analyzed")
            if rec.get("profile_id"):
                self.db.update_profile(rec["profile_id"], calibration_id=rec_id, chain_db=chain)
            log.info("Calibration %s: chain noise %.1f dBFS", rec_id, chain)
            return
        rec = self.db.recording(rec_id) or rec                      # verify_audio may have stored the checksum
        trim = self.profile_trim(rec)
        chain = trim["chain_db"]
        dp = detect.params_for(trim["sensitivity"], self.cfg.analysis.detect)
        if rec["kind"] == "alignment":
            res = self.run_heavy(heavy.alignment_job, rec["path"], self.cfg.alignment, chain, dp)
        else:
            acfg = replace(self.cfg.analysis, pop_dbfs=trim["pop_dbfs"], detect=dp,
                           clip_grace_s=float(self.cfg.clip_grace_s), clip_mention=int(self.cfg.clip_mention_samples))
            gcfg = replace(self.cfg.grading, clip_samples_max=int(self.cfg.clip_max_samples))
            res = self.run_heavy(heavy.analyze_job, rec["path"], acfg, gcfg, chain, trim["rpm"],
                                 trim["ref_dbfs"], trim["floor"], self.db.labels(rec_id), self.side_tracks(rec),
                                 self.db.skip_labels(rec_id), trim["fixed_gain"], self.db.so_cache(rec.get("sha256")))
            self.db.save_so_cache(rec.get("sha256"), res.pop("so_new", None) or {})
            res["metrics"]["profile"] = trim
        self.db.save_analysis(rec_id, res, __version__, time.time() - t0)
        self.db.update_recording(rec_id, status="analyzed", chain_db=chain)
        if rec["kind"] == "alignment":
            self._update_floor(rec, res)
        if getattr(self, "on_analyzed", None):
            try:
                self.on_analyzed(rec_id, res)
            except Exception:  # noqa: BLE001
                pass
        self.store_spectrogram(rec_id, rec["path"])
        if (self.cfg.engineer_mode and rec["kind"] in MASTER_KINDS and rec.get("record_id")
                and self.db.master(rec["record_id"], rec["side"])):
            self.master_check(rec_id)                      # engineer mode: measure against the master right away
        if rec["kind"] == "alignment":
            log.info("Alignment analysis %s done in %.1f s: %d bands", rec_id, time.time() - t0,
                     len(res["metrics"]["bands"]))
        else:
            log.info("Analysis %s done in %.1f s: grade %s", rec_id, time.time() - t0, res["grading"]["grade"])

    def _master(self, rec_id: int) -> None:
        rec = self.db.recording(rec_id)
        if not rec:
            return
        m = self.db.master(rec["record_id"], rec["side"]) if rec.get("record_id") else None
        if not m:
            raise RuntimeError(i18n.msg("jobs.no_master", side=str(rec["side"])))
        if not os.path.exists(m["path"]):
            raise RuntimeError(i18n.msg("jobs.master_file_missing", path=str(m["path"])))
        self.db.set_master_check(rec_id, status="running", error=None, master_id=m["id"])
        res = self.db.analysis_full(rec_id)
        if not res:
            raise RuntimeError(i18n.msg("jobs.analyze_first"))
        self.verify_audio(rec)
        trim = self.profile_trim(rec)
        others = {x["side"]: x["path"] for x in self.db.masters(rec["record_id"])
                  if x["id"] != m["id"] and os.path.exists(x["path"])}
        t0 = time.time()
        out = self.run_heavy(heavy.master_job, rec["path"], m["path"], res, trim["rpm"], trim.get("speed_pct"), others)
        out["master_file"] = {"id": m["id"], "name": m["name"], "side": m["side"]}
        if out.get("ok"):
            # Grade against the master: the same limits, but only what is provably on the vinyl (clicks and pops in
            # the music that the master does not explain). Gaps between tracks do not count (grading scheme 3).
            k, am = out.get("clicks") or {}, res.get("metrics") or {}
            out["grading"] = grading.grade({"clicks_music_per_min": k.get("vinyl_music_per_min"),
                                            "pops_per_min": k.get("vinyl_pops_music_per_min"),
                                            "noise_rel_db": am.get("noise_rel_db"), "clip_in_groove": am.get("clip_in_groove"),
                                            "skips_back": am.get("skips_back")}, self.cfg.grading)
        self.db.set_master_check(rec_id, status="done", result=_jsonable(out), version=__version__, error=None)
        log.info("Master comparison %s against master %s (side %s) in %.1f s: %s", rec_id, m["id"], m["side"],
                 time.time() - t0, "ok" if out.get("ok") else i18n.render(out.get("reason"), "en"))
        if getattr(self, "on_master", None):
            try:
                self.on_master(rec_id, out)
            except Exception:  # noqa: BLE001
                pass

    def side_tracks(self, rec: dict) -> list[dict]:
        """Tracks of this side from the Discogs data stored with the record (empty if never fetched)."""
        if not rec.get("record_id"):
            return []
        r = self.db.record(rec["record_id"]) or {}
        try:
            tracks = json.loads(r.get("tracks") or "{}")
        except ValueError:
            return []
        return tracks.get(str(rec.get("side") or "").strip().upper()[:1]) or []

    def _update_floor(self, rec: dict, res: dict) -> None:
        """Update the profile's turntable floor when the latest side 2 of the accepted calibration run has been
        (re)analyzed, e.g. after a sensitivity change or when all numbers are recomputed."""
        prof = self.db.profile(rec["profile_id"]) if rec.get("profile_id") else None
        if not prof or not prof.get("accepted_at") or not rec.get("setup_id") or prof.get("baseline_setup_id") != rec["setup_id"]:
            return
        latest = [r for r in self.db.setup_recordings(rec["setup_id"]) if str(r["side"]) == str(rec["side"])]
        if latest and latest[-1]["id"] != rec["id"]:
            return
        s = res.get("metrics", {}).get("summary") or {}
        if s.get("floor_clicks_per_min") is None:
            return
        self.db.update_profile(prof["id"], floor_clicks_per_min=s["floor_clicks_per_min"],
                               floor_ticks_per_min=s.get("floor_ticks_per_min"))
        log.info("Profile %s: turntable floor %.0f clicks, %.0f crackle per minute", prof["id"],
                 s["floor_clicks_per_min"], s.get("floor_ticks_per_min") or 0)

    def profile_trim(self, rec: dict) -> dict:
        """What the turntable profile changes in the evaluation of a recording.

        - Chain noise: calibration of this turntable (otherwise the most recent one of any).
        - Pop threshold: relative to the measured reference level instead of a fixed dBFS value. Turntables differ
          in output level by several dB (a USB turntable and a deck behind an analog mixer were 6 dB apart on the
          1 kHz reference tone); with a fixed threshold the louder one would report more clicks as pops.
        - Speed: the measured deviation, so the skip detector looks for the true revolution period.
        - Detector sensitivity (0…1, 0.5 = calibrated point): recording before profile before default.
        - Turntable floor: clicks/crackle in the silent groove of the test record; the quiet-passage metrics are
          rated net of it (only for an accepted profile with a measured floor).
        """
        prof = self.db.profile(rec["profile_id"]) if rec.get("profile_id") else None
        cal = (self.db.latest_calibration(prof["id"]) if prof else None) or self.db.latest_calibration()
        chain = (prof or {}).get("chain_db") or (cal["chain_db"] if cal else None)
        rpm = rec.get("rpm") or 100 / 3
        pop = self.cfg.analysis.pop_dbfs
        speed = None
        if prof and prof.get("accepted_at"):
            if prof.get("ref_dbfs") is not None:
                pop = round(prof["ref_dbfs"] + (self.cfg.analysis.pop_dbfs - self.cfg.alignment.target_ref_dbfs), 2)
            if prof.get("speed_pct") is not None:
                speed = prof["speed_pct"]
                rpm = rpm * (1 + speed / 100)
        sens = rec.get("sensitivity")
        if sens is None and prof:
            sens = prof.get("sensitivity")
        sens = 0.5 if sens is None else float(sens)
        floor = None
        if prof and prof.get("accepted_at") and prof.get("floor_clicks_per_min") is not None:
            # "source" is stored with the analysis: where the floor was measured (label: labels.side_source.*)
            floor = {"clicks_per_min": prof["floor_clicks_per_min"], "ticks_per_min": prof.get("floor_ticks_per_min"),
                     "source": "test_record_silent_groove"}
        return {"id": prof["id"] if prof else None, "name": prof["name"] if prof else None,
                "accepted": bool(prof and prof.get("accepted_at")), "chain_db": chain,
                "fixed_gain": bool(prof and prof.get("fixed_gain")),
                "pop_dbfs": pop, "rpm": round(rpm, 4), "speed_pct": speed,
                "ref_dbfs": prof.get("ref_dbfs") if prof and prof.get("accepted_at") else None,
                "sensitivity": sens, "floor": floor}

    def store_spectrogram(self, rec_id: int, path: str) -> dict | None:
        """Compute the overview image and store it in the database. Not fatal: without a stored image the web
        interface computes it from the audio when needed."""
        try:
            t0 = time.time()
            meta, img = self.run_heavy(heavy.spectro_job, path)
            self.db.save_spectrogram(rec_id, meta, img)
            log.info("Spectrogram %s: %d×%d in %.1f s", rec_id, meta["rows"], meta["cols"], time.time() - t0)
            return meta
        except ShuttingDown:
            raise
        except Exception:  # noqa: BLE001
            log.error("Spectrogram %s failed: %s", rec_id, traceback.format_exc())
            return None

    def _compare(self, cid: int) -> None:
        c = self.db.comparison(cid)
        self.db.update_comparison(cid, status="running")
        ra, rb = self.db.recording(c["a_id"]), self.db.recording(c["b_id"])
        res_a, res_b = self.db.analysis_full(c["a_id"]), self.db.analysis_full(c["b_id"])
        if not (res_a and res_b):
            raise RuntimeError(i18n.msg("jobs.both_analyze_first"))
        self.verify_audio(ra)
        self.verify_audio(rb)
        res = self.run_heavy(heavy.compare_job, ra["path"], rb["path"], res_a, res_b, self.profile_trim(ra)["rpm"])
        res["kinds"] = [ra["kind"], rb["kind"]]
        res["copies"] = [ra.get("copy_label") or "", rb.get("copy_label") or ""]
        same_copy = res["copies"][0] == res["copies"][1]
        res["repeatability"] = ra["kind"] == rb["kind"] == "before" and same_copy
        if not same_copy:
            unnamed = i18n.msg("jobs.copy_unnamed")
            res["copy_note"] = i18n.msg("jobs.copy_note", a=res["copies"][0] or unnamed, b=res["copies"][1] or unnamed)
        pa, pb = ra.get("profile_id"), rb.get("profile_id")
        if pa != pb:
            names = [(self.db.profile(x) or {}).get("name") or i18n.msg("jobs.no_profile") for x in (pa, pb)]
            res["profile_note"] = i18n.msg("jobs.profile_note", a=names[0], b=names[1])
        self.db.update_comparison(cid, status="done", result=_jsonable(res))

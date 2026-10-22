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
"""

from __future__ import annotations

import collections
import concurrent.futures as cf
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


def sha256_file(path: str) -> str:
    """SHA-256 of a file as hex string (read in 4 MB blocks)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def error_message(e: BaseException):
    """What to store and show for a failed job: the i18n message an exception carries (as `.message` or as its
    first argument), otherwise its text."""
    for v in (getattr(e, "message", None), e.args[0] if e.args else None):
        if i18n.is_msg(v):
            return v
    return str(e)


class Worker:
    """Job queue with one worker thread; also runs the retention clean-up."""

    def __init__(self, cfg: Config, db: DB):
        self.cfg, self.db = cfg, db
        self.q: queue.Queue = queue.Queue()
        self.current: dict | None = None
        self.completed = 0            # number of finished jobs (cache key for derived values)
        self.pool: cf.ProcessPoolExecutor | None = None               # child process for heavy.py, nice 10
        self.pending: list[tuple[str, int]] = []                      # queued jobs (for the "System" view)
        self.history: collections.deque = collections.deque(maxlen=30)   # recently finished jobs with duration
        self._t = threading.Thread(target=self._loop, daemon=True, name="vinylcheck-worker")

    def start(self) -> None:
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
        """Queue the analysis of a recording."""
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
        time and report the crash."""
        if self.pool is None:
            self.pool = cf.ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn"),
                                               initializer=heavy.init_worker)
        try:
            return self.pool.submit(fn, *args).result()
        except cf.process.BrokenProcessPool as e:
            self.pool = None
            raise RuntimeError(i18n.msg("jobs.worker_crashed", error=str(e))) from e

    def close(self) -> None:
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)
            self.pool = None

    def status(self) -> dict:
        return {"current": self.current, "queued": self.q.qsize(),
                "pending": [{"kind": k, "id": i} for k, i in self.pending], "history": list(self.history)}

    def _loop(self) -> None:
        while True:
            kind, ident = self.q.get()
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
            except Exception as e:  # noqa: BLE001
                ok, err = False, error_message(e)
                log.error("%s %s failed: %s", kind, ident, traceback.format_exc())
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
        if self.cfg.retention_days <= 0:
            return []
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
        self.db.update_recording(rec_id, status="analyzing")
        self.verify_audio(rec)
        t0 = time.time()
        if rec["kind"] == "calibration":
            chain = analysis.chain_noise_db(rec["path"])
            if chain > CALIBRATION_MAX_DBFS:
                # The stylus was evidently down: with this value whole grooves would later be discarded as
                # "stylus up", so do not take it over
                self.db.update_recording(rec_id, status="error", error=i18n.msg(
                    "jobs.calibration_too_loud", db=chain, max=CALIBRATION_MAX_DBFS))
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
            res = self.run_heavy(heavy.alignment_job, rec["path"], self.cfg.justage, chain, dp)
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
                pop = round(prof["ref_dbfs"] + (self.cfg.analysis.pop_dbfs - self.cfg.justage.target_ref_dbfs), 2)
            if prof.get("speed_pct") is not None:
                speed = prof["speed_pct"]
                rpm = rpm * (1 + speed / 100)
        sens = rec.get("sensitivity")
        if sens is None and prof:
            sens = prof.get("sensitivity")
        sens = 0.5 if sens is None else float(sens)
        floor = None
        if prof and prof.get("accepted_at") and prof.get("floor_clicks_per_min") is not None:
            # "source" is stored with the analysis; the value is kept as is for compatibility with existing data
            floor = {"clicks_per_min": prof["floor_clicks_per_min"], "ticks_per_min": prof.get("floor_ticks_per_min"),
                     "source": "stille Rille der Testplatte"}
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

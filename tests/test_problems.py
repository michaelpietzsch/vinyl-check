"""The error log, explanations of known errors, and no analysis of a recording that is still being recorded."""

import time
from dataclasses import replace

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from vinylcheck import i18n
from vinylcheck.config import Config
from vinylcheck.db import DB, error_key
from vinylcheck.jobs import Worker
from vinylcheck.web import create_app


def _cfg(tmp_path):
    cfg = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state", min_free_gb=0.0)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _wav(path, seconds=2.0):
    sf.write(path, np.zeros((int(48000 * seconds), 2), dtype=np.float32), 48000, subtype="PCM_16")
    return str(path)


def _analysis(db, rid, analyzed_at):
    db.save_analysis(rid, {"metrics": {"summary": {}}, "grading": {"grade": "VG", "score": 60}, "warnings": [],
                           "overview": {}, "events": []}, "test", 1.0)
    db.x("UPDATE analyses SET analyzed_at = ? WHERE recording_id = ?", (analyzed_at, rid))


def test_a_recording_in_progress_is_not_analysed(tmp_path):
    cfg = _cfg(tmp_path)
    db = DB(cfg.db_path)
    rid = db.add_recording(kind="before", side="A", path=_wav(tmp_path / "a.wav"), status="recording")
    w = Worker(cfg, db)
    w.analyze(rid)                                                     # e.g. ↻ pressed while the side is recorded
    assert db.recording(rid)["status"] == "recording" and not w.pending
    w._analyze(rid)                                                    # a job queued before the guard: ignored
    r = db.recording(rid)
    assert r["status"] == "recording" and r["sha256"] is None and r.get("analysis") is None
    db.conn.close()
    with TestClient(create_app(cfg)) as c:
        # a recording still "recording" at a start was interrupted by the shutdown: an error, and in the log
        r = c.get(f"/api/recordings/{rid}").json()
        assert r["status"] == "error" and r["error_key"] == "api.recording_interrupted"
        assert [(p["source"], p["key"]) for p in c.get("/api/problems").json()] == [("recording", "api.recording_interrupted")]
        running = DB(cfg.db_path)
        rid2 = running.add_recording(kind="before", side="B", path=_wav(tmp_path / "b.wav"), status="recording")
        res = c.post(f"/api/recordings/{rid2}/analyze")
        assert res.status_code == 409 and f"#{rid2}" in res.json()["detail"]
        assert running.recording(rid2)["status"] == "recording"


def test_checksums_taken_during_the_recording_are_taken_again(tmp_path):
    cfg = _cfg(tmp_path)
    db = DB(cfg.db_path)
    t0 = time.time() - 3600
    bad = db.add_recording(kind="after_wet", side="D", path=_wav(tmp_path / "d.wav"), status="error")
    db.update_recording(bad, sha256="partial", duration_s=1245.4, created_at=t0,
                        error=i18n.msg("jobs.integrity_error", id=bad))
    _analysis(db, bad, t0 + 61)                                        # analysed a minute after the start
    good = db.add_recording(kind="before", side="A", path=_wav(tmp_path / "a.wav"), status="analyzed")
    db.update_recording(good, sha256="whole", duration_s=600.0, created_at=t0)
    _analysis(db, good, t0 + 640)                                      # analysed after the end
    assert db.reset_partial_checksums() == [bad]
    r = db.recording(bad)
    assert r["sha256"] is None and r["status"] == "queued" and r["error"] is None
    assert db.recording(good)["sha256"] == "whole" and db.reset_partial_checksums() == []


def test_failed_jobs_land_in_the_error_log(tmp_path):
    cfg = _cfg(tmp_path)
    db = DB(cfg.db_path)
    rec = db.add_record("Artist", "Title")
    rid = db.add_recording(record_id=rec, kind="before", side="B", path=_wav(tmp_path / "b.wav"), status="analyzed")
    db.update_recording(rid, sha256="not-the-checksum-of-this-file")
    db.conn.close()
    with TestClient(create_app(cfg)) as c:
        assert c.get("/api/problems").json() == []
        assert c.post(f"/api/recordings/{rid}/analyze").status_code == 200
        deadline = time.time() + 30
        while c.get(f"/api/recordings/{rid}").json()["status"] != "error" and time.time() < deadline:
            time.sleep(0.1)
        r = c.get(f"/api/recordings/{rid}").json()
        assert r["status"] == "error" and r["error_key"] == "jobs.integrity_error"
        listed = [x for x in c.get(f"/api/records/{rec}").json()["recordings"] if x["id"] == rid][0]
        assert listed["error_key"] == "jobs.integrity_error"                # the record page explains it on hover
        p = c.get("/api/problems").json()
        assert len(p) == 1 and p[0]["source"] == "analyze" and p[0]["key"] == "jobs.integrity_error"
        assert p[0]["recording_id"] == rid and p[0]["record_id"] == rec and "Artist" in p[0]["label"]
        assert f"#{rid}" in p[0]["message"]
        hist = c.get("/api/system").json()["worker"]["history"]
        assert hist[0]["ok"] is False and hist[0]["error_key"] == "jobs.integrity_error"
        assert c.delete("/api/problems").json() == [] and c.get("/api/problems").json() == []


def test_the_log_keeps_the_newest(tmp_path):
    db = DB(_cfg(tmp_path).db_path)
    db.PROBLEMS_KEPT = 5
    for i in range(8):
        db.add_problem("recording", i, i18n.msg("api.recording_too_short") if i % 2 else f"plain text {i}")
    p = db.problems()
    assert [x["ref_id"] for x in p] == [7, 6, 5, 4, 3]
    assert p[0]["key"] == "api.recording_too_short" and p[1]["key"] is None and p[1]["message"] == "plain text 6"
    assert error_key(i18n.msg("jobs.audio_missing", path="x")) == "jobs.audio_missing" and error_key("text") is None


@pytest.mark.parametrize("key", ["generic", "jobs.integrity_error", "jobs.audio_missing", "jobs.audio_deleted",
                                 "jobs.calibration_too_loud", "jobs.worker_crashed", "api.recording_too_short",
                                 "api.recording_interrupted", "audio.device_stalled", "audio.device_not_found"])
def test_known_errors_are_explained(key):
    for lang in i18n.languages():
        cat = i18n.catalog(lang, web=True)
        assert f"problems.{key}.what" in cat and f"problems.{key}.todo" in cat, (lang, key)
    if key != "generic":
        assert key in i18n.catalog("en")                                    # it explains a message that exists

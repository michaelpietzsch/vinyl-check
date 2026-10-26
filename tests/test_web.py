"""End to end through the API: create a record, (test) recording, analysis, overview, snippet, comparison, and the
other endpoints of web.py. Requests choose no language, so texts come back in English (the default)."""

import json
import re
import time
from pathlib import Path
from dataclasses import replace

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from vinylcheck import i18n, synth
from vinylcheck.config import Config
from vinylcheck.web import create_app

SR = 48000


@pytest.fixture()
def client(tmp_path):
    audio, _ = synth.make_side(duration_s=40, seed=5, clicks_per_min=30)
    wav = tmp_path / "side.wav"
    sf.write(wav, audio, SR, subtype="PCM_16")
    cfg = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state",
                  fake_source=str(wav), fake_speed=200.0, min_free_gb=0.0)
    with TestClient(create_app(cfg)) as c:
        yield c


def _wait(client, rec_id, status="analyzed", timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = client.get(f"/api/recordings/{rec_id}").json()
        if r["status"] in (status, "error"):
            return r
        time.sleep(0.2)
    raise AssertionError(f"timeout, status {r['status']}")


def _record(client, record_id, kind, side="A"):
    r = client.post("/api/recordings/start", json={"record_id": record_id, "side": side, "kind": kind})
    assert r.status_code == 200, r.text
    rec_id = r.json()["recording_id"]
    # the test file plays at 200× real time and ends by itself
    deadline = time.time() + 30
    while client.get("/api/status").json()["recorder"]["active"] and time.time() < deadline:
        time.sleep(0.1)
    return _wait(client, rec_id)


def test_full_flow(client):
    assert client.get("/api/health").json()["ok"]
    assert client.get("/").status_code == 200
    rid = client.post("/api/records", json={"artist": "Test", "title": "Synth"}).json()["id"]

    a = _record(client, rid, "before")
    assert a["status"] == "analyzed", a.get("error")
    assert a["analysis"]["grading"]["grade"]
    assert a["duration_s"] == pytest.approx(40, abs=0.5)
    assert a["kind_label"] == "uncleaned"

    ov = client.get(f"/api/recordings/{a['id']}/overview").json()
    assert ov["events"] and ov["quiet"]
    ev = client.get(f"/api/recordings/{a['id']}/events?limit=5").json()
    assert len(ev) == 5 and ev[0]["strength"] >= ev[-1]["strength"]
    snip = client.get(f"/api/recordings/{a['id']}/snippet", params={"t": ev[0]["t"]})
    assert snip.status_code == 200 and snip.content[:4] == b"RIFF"
    diff = client.get(f"/api/recordings/{a['id']}/clicks.wav")                       # difference track: clicks only
    assert diff.status_code == 200 and diff.content[:4] == b"RIFF" and int(diff.headers["x-events"]) > 0
    ranged = client.get(f"/api/recordings/{a['id']}/clicks.wav", headers={"Range": "bytes=0-3, 8-11"})
    assert ranged.status_code == 200 and ranged.content == diff.content             # whole file, no range requests
    import io as _io
    d, _ = sf.read(_io.BytesIO(diff.content), dtype="int16", always_2d=True)
    assert len(d) == int(a["duration_s"] * SR) and np.mean(d != 0) < 0.05 and np.abs(d).max() > 0
    bad = client.patch(f"/api/recordings/{a['id']}", json={"sensitivity": 1.5})
    assert bad.status_code == 422 and bad.json()["detail"].startswith("Sensitivity must be between 0 and 1")
    top = client.get(f"/api/recordings/{a['id']}/events?order=strength&limit=3").json()
    assert "gc" in top[0] and "label" in top[0]
    lab = client.put(f"/api/recordings/{a['id']}/events/label", json={"t": top[0]["t"], "label": "music"}).json()
    assert lab["counts"]["labeled_music"] == 1
    bad_label = {"t": top[0]["t"], "label": "nonsense"}
    assert client.put(f"/api/recordings/{a['id']}/events/label", json=bad_label).status_code == 422
    assert client.put(f"/api/recordings/{a['id']}/events/label", json={"t": 9999.0, "label": "music"}).status_code == 404
    rec_m = client.get(f"/api/recordings/{a['id']}").json()["analysis"]["metrics"]
    assert rec_m["counts"]["labeled_music"] == 1
    exp = client.get("/api/audit/export").json()
    assert exp["count"] == 1 and exp["events"][0]["label"] == "music" and exp["events"][0]["recording_id"] == a["id"]
    assert client.patch(f"/api/recordings/{a['id']}", json={"copy_label": " Version 1 "}).json()["copy_label"] == " Version 1 "
    assert client.get(f"/api/records/{rid}").json()["recordings"][0]["copy_label"] == " Version 1 "
    r2 = client.post("/api/recordings/start", json={"record_id": rid, "side": "B", "kind": "after_wet", "copy_label": " Version 2 "})
    assert r2.status_code == 200 and client.get(f"/api/recordings/{r2.json()['recording_id']}").json()["copy_label"] == "Version 2"
    assert client.get(f"/api/recordings/{r2.json()['recording_id']}").json()["kind"] == "after_wet"
    bad_kind = client.post("/api/recordings/start", json={"record_id": rid, "side": "B", "kind": "nonsense"})
    assert bad_kind.status_code == 422 and bad_kind.json()["detail"].startswith("kind must be one of")
    assert client.post("/api/recordings/stop").status_code in (200, 409)
    _wait(client, r2.json()["recording_id"])
    assert client.delete(f"/api/recordings/{r2.json()['recording_id']}").json()["ok"]   # counting below stays
    r = client.patch(f"/api/recordings/{a['id']}", json={"sensitivity": 0.7})
    assert r.status_code == 200 and r.json()["sensitivity"] == 0.7 and r.json()["reanalyzing"]
    a = _wait(client, a["id"])
    pdf = client.get(f"/api/recordings/{a['id']}/report.pdf")                      # PDF report of the recording
    assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-" and "attachment" in pdf.headers["content-disposition"], pdf.text[:300]
    assert "Report_Test_Synth_side_A" in pdf.headers["content-disposition"]
    assert a["analysis"]["metrics"]["profile"]["sensitivity"] == 0.7 and a["analysis"]["metrics"]["detect"]["stripe_db"] < 12.0
    assert a["analysis"]["metrics"]["counts"]["labeled_music"] == 1                  # the judgment survives recomputing
    r = client.patch(f"/api/recordings/{a['id']}", json={"sensitivity": None})
    assert r.json()["sensitivity"] is None
    a = _wait(client, a["id"])
    orig_side, orig_kind = a["side"], a["kind"]
    assert client.patch(f"/api/recordings/{a['id']}", json={"side": "  "}).status_code == 422          # empty: no
    r = client.patch(f"/api/recordings/{a['id']}", json={"side": " c "})
    assert r.status_code == 200 and r.json()["side"] == "c" and r.json()["reanalyzing"]      # other side → recompute
    a = _wait(client, a["id"])
    r = client.patch(f"/api/recordings/{a['id']}", json={"side": "C", "kind": "after_dry"})
    assert r.json()["kind"] == "after_dry" and not r.json().get("reanalyzing")               # case only: no recompute
    client.patch(f"/api/recordings/{a['id']}", json={"side": orig_side, "kind": orig_kind})  # back for the comparison
    a = _wait(client, a["id"])
    a = _wait(client, a["id"])
    assert a["analysis"]["metrics"]["detect"]["stripe_db"] == 12.0
    sg = client.get(f"/api/recordings/{a['id']}/spectrogram")                    # overview: stored by the worker
    assert sg.status_code == 200 and sg.headers["x-stored"] == "1"
    assert len(sg.content) == int(sg.headers["x-rows"]) * int(sg.headers["x-cols"]) == 160 * 1600
    assert float(sg.headers["x-t1"]) == pytest.approx(40, abs=0.5)
    sg2 = client.get(f"/api/recordings/{a['id']}/spectrogram", params={"t0": 5, "t1": 15, "top": float(sg.headers["x-top-db"]) - 3})
    assert sg2.status_code == 200 and sg2.headers["x-stored"] == "0"            # zoom: computed from the audio
    assert float(sg2.headers["x-top-db"]) == pytest.approx(float(sg.headers["x-top-db"]) - 3)
    sg3 = client.get(f"/api/recordings/{a['id']}/spectrogram", params={"top": -10})   # overview, another scale
    assert sg3.headers["x-stored"] == "1" and sg3.headers["x-top-db"] == "-10.0" and sg3.headers["x-floor-db"] == "-90.0"

    b = _record(client, rid, "after")
    cid = client.post("/api/comparisons", json={"a_id": a["id"], "b_id": b["id"]}).json()["id"]
    for _ in range(300):
        c = client.get(f"/api/comparisons/{cid}").json()
        if c["status"] in ("done", "error"):
            break
        time.sleep(0.2)
    assert c["status"] == "done", c.get("error")
    # the same file twice → everything "persistent", nothing removed or new
    cnt = c["result"]["match"]["counts"]
    assert cnt["removed"] == 0 and cnt["new"] == 0 and cnt["persistent"] > 0

    rec = client.get(f"/api/records/{rid}").json()
    assert len(rec["recordings"]) == 2 and len(rec["comparisons"]) == 1
    assert client.delete(f"/api/records/{rid}").json()["ok"]


def test_whatif_recalculates_without_storing(client):
    rid = client.post("/api/records", json={"artist": "Test", "title": "What If"}).json()["id"]
    a = _record(client, rid, "before")
    base = a["analysis"]["grading"]
    top = client.get(f"/api/recordings/{a['id']}/events?order=strength&limit=8").json()
    ts = [e["t"] for e in top if e["region"] != "excluded"]
    r = client.post(f"/api/recordings/{a['id']}/whatif", json={"ignore_events": ts})
    assert r.status_code == 200, r.text
    r = r.json()
    assert r["ignored"] == {"events": len(ts), "skips": 0} and r["base"]["grade"] == base["grade"]
    assert r["record"]["decisive"] and r["record"]["grade"] == r["grade"] and r["record"]["base"] == base["grade"]
    assert r["metrics"]["clicks_music_per_min"] + r["metrics"]["clicks_quiet_per_min"] < \
        r["base"]["metrics"]["clicks_music_per_min"] + r["base"]["metrics"]["clicks_quiet_per_min"]
    after = client.get(f"/api/recordings/{a['id']}").json()["analysis"]                 # nothing stored
    assert after["grading"]["grade"] == base["grade"] and after["grading"]["score"] == base["score"]
    assert after["metrics"]["counts"] == a["analysis"]["metrics"]["counts"]
    assert all(e["label"] is None for e in client.get(f"/api/recordings/{a['id']}/events?order=strength&limit=8").json())
    assert client.get("/api/audit/export").json()["count"] == 0
    assert client.post("/api/recordings/999999/whatif", json={}).status_code == 404
    assert client.post(f"/api/recordings/{a['id']}/whatif", json={"ignore_skips": [1.0] * 501}).status_code == 422


def test_status_exposes_grading_scheme(client):
    st = client.get("/api/status").json()
    g = st["grading"]
    assert g["grades"] == ["NM", "VG+", "VG", "G+", "G", "F"] and len(g["bands"]) == 6 and g["bands"][0] == [100, 90]
    assert "clicks_music_per_min" in g["graded"] and "pops_per_min" in g["graded"]
    assert g["secondary"] == [] and g["secondary_gap"] == 2                     # gaps are information only
    assert set(g["labels"]) == set(st["grading_limits"]) and g["version"] >= 3


def test_start_requires_record(client):
    r = client.post("/api/recordings/start", json={"kind": "before"})
    assert r.status_code == 422 and r.json()["detail"] == "Please select a record for this recording first"


def test_storage_guard(tmp_path):
    cfg = replace(Config(), data_dir=tmp_path / "d", state_dir=tmp_path / "s",
                  require_mount=str(tmp_path / "not-mounted"), fake_source="x.wav")
    with TestClient(create_app(cfg)) as c:
        st = c.get("/api/status").json()["storage"]
        assert not st["ok"] and st["reason"].endswith("is not mounted (data drive missing).")
        rid = c.post("/api/records", json={"artist": "a", "title": "b"}).json()["id"]
        r = c.post("/api/recordings/start", json={"record_id": rid, "kind": "before"})
        assert r.status_code == 409 and "is not mounted" in r.json()["detail"]


def test_calibration_rejects_needle_down(client):
    # the test source is a whole record side → far too loud for "stylus up"
    r = client.post("/api/recordings/start", json={"kind": "calibration", "side": "-"})
    assert r.status_code == 200
    rec = _wait(client, r.json()["recording_id"])
    assert rec["status"] == "error" and "dBFS" in rec["error"]                 # message from jobs.py names the level
    assert client.get("/api/status").json()["calibration"] is None


def test_calibration_needle_up(tmp_path):
    rng = np.random.default_rng(1)
    wav = tmp_path / "chain.wav"
    sf.write(wav, (rng.standard_normal((SR * 10, 2)) * 10 ** (-90 / 20)).astype(np.float32), SR, subtype="PCM_16")
    cfg = replace(Config(), data_dir=tmp_path / "d", state_dir=tmp_path / "s", fake_source=str(wav),
                  fake_speed=100.0, min_free_gb=0.0)
    with TestClient(create_app(cfg)) as c:
        r = c.post("/api/recordings/start", json={"kind": "calibration", "side": "-"})
        rec = _wait(c, r.json()["recording_id"])
        assert rec["status"] == "analyzed", rec.get("error")
        assert -95 < rec["chain_db"] < -80
        assert c.get("/api/status").json()["calibration"]["chain_db"] == rec["chain_db"]


def test_retention_deletes_audio_keeps_metrics(client):
    """The data drive is a buffer: after retention_days the audio is gone, the measurements stay."""
    rid = client.post("/api/records", json={"artist": "Old", "title": "Buffer"}).json()["id"]
    old = _record(client, rid, "before")
    new = _record(client, rid, "after")
    assert old["sha256"] and len(old["sha256"]) == 64          # checksum stored on the first analysis
    db = client.app.state.db
    db.update_recording(old["id"], created_at=time.time() - 31 * 86400)

    res = client.post("/api/retention/run").json()
    assert res["deleted"] == [old["id"]]                       # only the old one, not the fresh one
    r = client.get(f"/api/recordings/{old['id']}").json()
    assert r["audio_deleted_at"] and not r["audio_available"] and not r["file_exists"]
    assert r["analysis"]["grading"]["grade"]                   # measurements still there
    assert client.get(f"/api/recordings/{old['id']}/overview").status_code == 200
    assert client.get(f"/api/recordings/{old['id']}/events").json()
    assert client.get(f"/api/recordings/{old['id']}/snippet", params={"t": 5}).status_code == 410
    assert client.get(f"/api/recordings/{old['id']}/spectrogram").status_code == 200           # the overview stays
    assert client.get(f"/api/recordings/{old['id']}/spectrogram", params={"t0": 1, "t1": 5}).status_code == 410
    assert client.post(f"/api/recordings/{old['id']}/analyze").status_code == 409
    cmp_ = client.post("/api/comparisons", json={"a_id": old["id"], "b_id": new["id"]})
    assert cmp_.status_code == 409 and "after 30 days of retention" in cmp_.json()["detail"]
    assert client.get(f"/api/recordings/{new['id']}").json()["audio_available"]
    assert client.post("/api/retention/run").json()["deleted"] == []   # idempotent


def test_integrity_check_detects_modified_audio(client):
    rid = client.post("/api/records", json={"artist": "Bit", "title": "Flip"}).json()["id"]
    rec = _record(client, rid, "before")
    assert rec["status"] == "analyzed" and rec["sha256"]
    with open(rec["path"], "r+b") as f:                         # flip one byte in the middle of the audio
        f.seek(100_000)
        b = f.read(1)
        f.seek(100_000)
        f.write(bytes([b[0] ^ 0xFF]))
    assert client.post(f"/api/recordings/{rec['id']}/analyze").status_code == 200
    r = _wait(client, rec["id"])
    # the message comes from jobs.py (English, or German in databases from before 1.0)
    assert r["status"] == "error" and any(w in r["error"].lower() for w in ("integrity", "integrität"))


# ─── Alignment ──────────────────────────────────────────────────────────────
@pytest.fixture()
def jclient(tmp_path):
    audio, _ = synth.make_test_side(2, synth.Rig(clip_l_db=7.0), seed=2)
    wav = tmp_path / "test-record-s2.wav"
    sf.write(wav, audio, SR, subtype="PCM_24")
    cfg = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state",
                  fake_source=str(wav), fake_speed=300.0, min_free_gb=0.0, sample_format="S24_3LE")
    with TestClient(create_app(cfg)) as c:
        yield c


def _record_alignment(client, setup_id, side="2"):
    r = client.post("/api/recordings/start", json={"kind": "alignment", "setup_id": setup_id, "side": side})
    assert r.status_code == 200, r.text
    rec_id = r.json()["recording_id"]
    deadline = time.time() + 30
    while client.get("/api/status").json()["recorder"]["active"] and time.time() < deadline:
        time.sleep(0.1)
    return _wait(client, rec_id)


def test_alignment_flow(jclient):
    c = jclient
    assert c.post("/api/recordings/start", json={"kind": "alignment", "side": "2"}).status_code == 422
    a = c.post("/api/setups", json={"tracking_force_g": 1.88, "bias": "1", "needle_hours": 83}).json()["id"]
    rec = _record_alignment(c, a)
    assert rec["status"] == "analyzed", rec.get("error")
    assert rec["kind"] == "alignment" and rec["record_id"] is None
    m = rec["analysis"]["metrics"]
    assert m["kind"] == "alignment" and m["summary"]["antiskate_break_l_db"] is not None
    assert {b.get("role") for b in m["bands"]} >= {"antiskating", "resonance", "vert_1k", "silent_groove"}

    s = c.get(f"/api/setups/{a}").json()
    assert s["recordings"][0]["bands"] >= 5 and "metrics" not in s["recordings"][0]
    assert {it["key"] for it in s["results"]} >= {"antiskating", "resonance_hz"}
    assert "ref_level" in {it["key"] for it in s["results"]}             # input level via the vertical 1 kHz tone
    lt = c.get("/api/status").json()["level_target"]
    assert lt["ref_dbfs"] == -20.0 and lt["last"] is None              # the lateral reference tone is on side 1 only

    b = c.post("/api/setups", json={"tracking_force_g": 1.95, "bias": "0.8"}).json()["id"]
    _record_alignment(c, b)
    cmp_ = c.get("/api/setups/compare", params={"a": a, "b": b}).json()
    rows = {r["key"]: r for r in cmp_["rows"]}
    assert rows["resonance_hz"]["delta"] == pytest.approx(0, abs=0.01)   # same test file
    assert not cmp_["notes"]
    assert c.patch(f"/api/setups/{b}", json={"bias": "0.7"}).json()["bias"] == "0.7"
    cleared = c.patch(f"/api/setups/{b}", json={"needle_hours": None, "vta": None}).json()   # clear fields
    assert cleared["needle_hours"] is None and cleared["vta"] == "" and cleared["bias"] == "0.7"
    lst = c.get("/api/setups").json()
    assert len(lst) == 2 and lst[0]["status_counts"]["warn"] >= 1

    # alignment recordings are not part of the before/after comparison
    assert c.post("/api/comparisons", json={"a_id": rec["id"], "b_id": rec["id"]}).status_code == 409
    path = rec["path"]
    assert c.delete(f"/api/setups/{a}").json()["ok"]
    assert c.get(f"/api/setups/{a}").status_code == 404
    assert c.get(f"/api/recordings/{rec['id']}").status_code == 404
    assert not Path(path).exists()


def test_live_meter_in_status(jclient):
    assert jclient.post("/api/monitor/start").status_code == 200
    deadline = time.time() + 30
    while jclient.get("/api/status").json()["recorder"]["active"] and time.time() < deadline:
        time.sleep(0.1)
    live = jclient.get("/api/status").json()["recorder"]["live"]
    assert live["seq"] > 10 and "tonal" in live


OLD_SCHEMA_0_2 = """
CREATE TABLE records (id INTEGER PRIMARY KEY, artist TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',
    discogs_release_id INTEGER, discogs_media_condition TEXT, notes TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE TABLE recordings (id INTEGER PRIMARY KEY, record_id INTEGER REFERENCES records(id) ON DELETE CASCADE,
    side TEXT NOT NULL DEFAULT 'A', kind TEXT NOT NULL CHECK (kind IN ('calibration', 'before', 'after', 'other')),
    cleaning_method TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', path TEXT,
    device TEXT, samplerate INTEGER, channels INTEGER, format TEXT,
    duration_s REAL, clip_samples INTEGER DEFAULT 0, xruns INTEGER DEFAULT 0, chain_db REAL,
    sha256 TEXT, audio_deleted_at REAL, status TEXT NOT NULL DEFAULT 'recording', error TEXT, created_at REAL NOT NULL);
CREATE TABLE analyses (recording_id INTEGER PRIMARY KEY REFERENCES recordings(id) ON DELETE CASCADE,
    version TEXT NOT NULL, metrics TEXT NOT NULL, grading TEXT NOT NULL, warnings TEXT NOT NULL, overview TEXT NOT NULL,
    grade TEXT, score REAL, duration_s REAL, analyzed_at REAL NOT NULL);
CREATE TABLE events (recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    t REAL NOT NULL, dur_ms REAL, strength REAL, amp_dbfs REAL, ch INTEGER, kind TEXT, region TEXT);
CREATE INDEX events_rec ON events(recording_id, t);
CREATE TABLE comparisons (id INTEGER PRIMARY KEY, a_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    b_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE, status TEXT NOT NULL DEFAULT 'queued',
    result TEXT, error TEXT, created_at REAL NOT NULL);
INSERT INTO records VALUES (1, 'TEST', 'Realtime', NULL, NULL, '', 1759500000);
INSERT INTO recordings (id, record_id, side, kind, path, status, sha256, created_at)
    VALUES (1, 1, 'A', 'before', '/mnt/data/x.wav', 'analyzed', 'ab', 1759500000);
INSERT INTO recordings (id, record_id, side, kind, status, chain_db, created_at)
    VALUES (2, NULL, '-', 'calibration', 'analyzed', -91.5, 1759400000);
INSERT INTO analyses VALUES (1, '0.2.0', '{}', '{"grade": "VG"}', '[]', '{}', 'VG', 70, 51, 1759500100);
INSERT INTO events VALUES (1, 1.5, 1, 30, -20, 3, 'click', 'quiet');
INSERT INTO comparisons (id, a_id, b_id, status, created_at) VALUES (1, 1, 1, 'done', 1759500200);
"""


def _backups(tmp_path, version):
    """Backups db.py writes before a schema migration (vinyl-check.<prefix>-<version>-<time>.sqlite)."""
    return list(tmp_path.glob(f"vinyl-check.*{version}-*.sqlite"))


def test_migration_from_0_2_0(tmp_path):
    import sqlite3

    from vinylcheck.db import DB
    path = tmp_path / "vinyl-check.sqlite"
    con = sqlite3.connect(path)
    con.executescript(OLD_SCHEMA_0_2)
    con.close()
    db = DB(path)
    assert _backups(tmp_path, "0.3.0"), "no backup before the migration"
    r = db.recording(1)
    assert r["sha256"] == "ab" and r["analysis"]["grade"] == "VG" and r["setup_id"] is None
    assert db.latest_calibration()["chain_db"] == -91.5
    sid = db.add_setup(tracking_force_g=1.88, bias="1")
    rid = db.add_recording(setup_id=sid, side="2", kind="alignment", rpm=33.333, status="recording")
    assert db.recording(rid)["kind"] == "alignment"
    db.delete_record(1)                                   # cascade through the rebuilt table
    assert db.recording(1) is None and db.events(1) == [] and db.comparison(1) is None
    assert db.delete_setup(sid) == [] and db.recording(rid) is None
    DB(path)                                              # second start: no second migration
    assert len(_backups(tmp_path, "0.3.0")) == 1


OLD_RECORDINGS_0_7_0 = """
CREATE TABLE recordings (
    id INTEGER PRIMARY KEY,
    record_id INTEGER REFERENCES records(id) ON DELETE CASCADE,
    setup_id INTEGER REFERENCES setups(id) ON DELETE CASCADE,
    side TEXT NOT NULL DEFAULT 'A',
    kind TEXT NOT NULL CHECK (kind IN ('calibration', 'before', 'after', 'other', 'alignment')),
    rpm REAL, cleaning_method TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', path TEXT,
    device TEXT, samplerate INTEGER, channels INTEGER, format TEXT,
    duration_s REAL, clip_samples INTEGER DEFAULT 0, xruns INTEGER DEFAULT 0,
    chain_db REAL, sha256 TEXT, audio_deleted_at REAL,
    status TEXT NOT NULL DEFAULT 'recording', error TEXT, created_at REAL NOT NULL
, profile_id INTEGER REFERENCES profiles(id) ON DELETE SET NULL, sensitivity REAL, copy_label TEXT NOT NULL DEFAULT '');
INSERT INTO profiles (id, name, device, created_at) VALUES (1, 'Turntable A', 'hw:CARD=CODEC', 1);
INSERT INTO recordings (id, side, kind, status, created_at, profile_id, copy_label, sensitivity)
    VALUES (1, 'A', 'after', 'recorded', 1759500000, 1, 'Copy 1', 0.6);
"""


def test_migration_keeps_columns_added_by_alter(tmp_path):
    """A database in the field had profile_id (with a foreign key), sensitivity and copy_label appended to
    `recordings` by ALTER. Rebuilding the table for the after_wet/after_dry CHECK used the base schema without
    profile_id and failed while copying; the service ended up in a restart loop."""
    import sqlite3

    from vinylcheck.db import DB
    path = tmp_path / "vinyl-check.sqlite"
    DB(path).conn.close()                                 # create the full schema …
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("DROP TABLE recordings")
    con.executescript(OLD_RECORDINGS_0_7_0)               # … and replace recordings with the 0.7.0 layout
    con.commit(); con.close()
    db = DB(path)
    assert len(_backups(tmp_path, "0.7.0")) == 1
    r = db.recording(1)
    assert r["profile_id"] == 1 and r["copy_label"] == "Copy 1" and r["sensitivity"] == 0.6 and r["findings"] == []
    fks = {row[3]: (row[2], row[6]) for row in db.conn.execute("PRAGMA foreign_key_list(recordings)")}
    assert fks["profile_id"] == ("profiles", "SET NULL")
    rid = db.add_recording(record_id=None, side="B", kind="after_wet", status="recording")
    assert db.recording(rid)["kind"] == "after_wet"
    db.conn.execute("DELETE FROM profiles WHERE id = 1"); db.conn.commit()
    assert db.recording(1)["profile_id"] is None         # ON DELETE SET NULL still works
    DB(path)                                              # second start: no second migration
    assert len(_backups(tmp_path, "0.7.0")) == 1


def test_migration_merges_findings_per_copy_and_side(tmp_path):
    """Ticks that used to be stored per recording are merged per copy and side."""
    from vinylcheck.db import DB
    path = tmp_path / "vinyl-check.sqlite"
    db = DB(path)
    rid = db.add_record("A", "B", None, None, "")
    a = db.add_recording(record_id=rid, side="A", kind="before", copy_label="Copy 1", status="recorded")
    b = db.add_recording(record_id=rid, side="a", kind="after", copy_label="copy  1", status="recorded")
    c = db.add_recording(record_id=rid, side="B", kind="before", copy_label="Copy 1", status="recorded")
    db.conn.execute("UPDATE recordings SET findings = ? WHERE id = ?", ('["scratch"]', a))
    db.conn.execute("UPDATE recordings SET findings = ? WHERE id = ?", ('["warp"]', b))
    db.conn.execute("DELETE FROM copy_findings")
    db.conn.close()
    db = DB(path)
    assert db.recording(a)["findings"] == ["scratch", "warp"] == db.recording(b)["findings"]
    assert db.recording(c)["findings"] == []
    assert [(f["copy_key"], f["side"]) for f in db.copy_findings(rid)] == [("copy 1", "A")]


# ─── Browser cache after updates ─────────────────────────────────────────────
def test_index_has_versioned_assets_and_no_cache(client):
    """index.html is never cached and requests app.js/style.css with the version,
    otherwise the new page could run with the old script after an update."""
    from vinylcheck import __version__
    r = client.get("/")
    assert r.status_code == 200
    assert f"app.js?v={__version__}" in r.text and f"style.css?v={__version__}" in r.text
    assert "__VERSION__" not in r.text
    assert r.headers["cache-control"] == "no-cache"
    assert client.get("/app.js").status_code == 200


# ─── Record covers from Discogs ──────────────────────────────────────────────
def test_record_cover_is_fetched_once_and_cached(client, monkeypatch):
    import io
    from vinylcheck import web
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append(req.full_url)
        body = json.dumps({"images": [{"type": "secondary", "uri": "https://i.discogs.com/b.jpg", "uri150": "https://i.discogs.com/b150.jpg"},
                                      {"type": "primary", "uri": "https://i.discogs.com/a.jpg", "uri150": "https://i.discogs.com/a150.jpg"}]})
        return io.BytesIO(body.encode())

    monkeypatch.setattr(web.urllib.request, "urlopen", fake_urlopen)
    rid = client.post("/api/records", json={"artist": "Test Artist", "title": "Test Album",
                                            "discogs_release_id": 1234567}).json()["id"]
    for _ in range(2):
        r = client.get(f"/api/records/{rid}/cover")
        assert r.status_code == 200 and r.json()["url"] == "https://i.discogs.com/a.jpg"
        assert r.json()["thumb"] == "https://i.discogs.com/a150.jpg"
    assert calls == ["https://api.discogs.com/releases/1234567"]          # asked only once
    no_rel = client.post("/api/records", json={"artist": "X", "title": "Y"}).json()["id"]
    r = client.get(f"/api/records/{no_rel}/cover")
    assert r.status_code == 404 and r.json()["detail"] == "No Discogs release ID"


def test_no_api_docs_and_whole_file_downloads(client):
    """No generated API documentation; cover images are sent whole (no range requests); masters only in engineer
    mode."""
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path
    rid = client.post("/api/records", json={"title": "Whole files"}).json()["id"]
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8
    assert client.put(f"/api/records/{rid}/cover", content=png).status_code == 200
    url = client.get(f"/api/records/{rid}/cover").json()["url"]
    r = client.get(url, headers={"Range": "bytes=0-9, 20-29, 40-49"})
    assert r.status_code == 200 and r.content == png and "content-range" not in r.headers
    r = client.put(f"/api/records/{rid}/masters/A", content=b"RIFF")
    assert r.status_code == 403 and "engineer mode" in r.json()["detail"]


def test_own_cover_and_tracklist_without_discogs(client):
    """A test pressing without a Discogs entry: upload a custom cover, enter the track list by hand, side lengths
    follow from it."""
    rid = client.post("/api/records", json={"artist": "Test Artist", "title": "Test Pressing"}).json()["id"]
    assert client.put(f"/api/records/{rid}/cover", content=b"not an image").status_code == 415
    jpg = b"\xff\xd8\xff\xe0" + b"\0" * 64
    assert client.put(f"/api/records/{rid}/cover", content=jpg, headers={"Content-Type": "image/jpeg"}).json()["type"] == "image/jpeg"
    c = client.get(f"/api/records/{rid}/cover").json()
    assert c["local"] and c["url"].startswith(f"/api/records/{rid}/cover/image?v=")
    img = client.get(c["url"])
    assert img.status_code == 200 and img.content == jpg and img.headers["content-type"] == "image/jpeg"
    assert next(r for r in client.get("/api/records").json() if r["id"] == rid)["cover_local"] is True
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 64                       # replacing: the old JPEG disappears
    client.put(f"/api/records/{rid}/cover", content=png)
    assert client.get(client.get(f"/api/records/{rid}/cover").json()["url"]).content == png

    tracks = {"a": [{"title": "Track One", "dur_s": 564.6}, {"title": "Track Two", "dur_s": 347.12}],
              "B": [{"pos": "B1", "title": "Track Three", "dur_s": None}]}
    r = client.patch(f"/api/records/{rid}", json={"tracks": tracks}).json()
    assert [x["pos"] for x in r["tracks"]["A"]] == ["A1", "A2"] and r["tracks"]["B"][0]["dur_s"] is None
    assert r["side_lengths"] == {"A": {"s": 911.7, "source": "track_list", "source_label": "track list"}}            # B without durations → no length
    assert r["cover_local"] is True and r["title"] == "Test Pressing"
    for bad in ("Side 1", "1", ""):
        assert client.patch(f"/api/records/{rid}", json={"tracks": {bad: []}}).status_code == 422

    client.delete(f"/api/records/{rid}/cover")
    assert client.get(f"/api/records/{rid}/cover").status_code == 404
    client.put(f"/api/records/{rid}/cover", content=jpg)
    client.delete(f"/api/records/{rid}")                                                # record gone → cover gone
    assert client.get(f"/api/records/{rid}/cover/image").status_code == 404


def test_live_pcm_stream_while_monitoring(tmp_path):
    """Listen in: /api/live/pcm delivers 16-bit PCM of the running level check without disturbing it."""
    audio, _ = synth.make_side(duration_s=20, seed=7, clicks_per_min=10)
    wav = tmp_path / "side.wav"
    sf.write(wav, audio, SR, subtype="PCM_16")
    cfg = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state",
                  fake_source=str(wav), fake_speed=4.0, min_free_gb=0.0)
    with TestClient(create_app(cfg)) as c:
        assert c.get("/api/live/pcm").status_code == 409                    # nothing running
        assert c.post("/api/monitor/start").status_code == 200
        got = b""
        with c.stream("GET", "/api/live/pcm") as r:
            assert r.status_code == 200
            assert r.headers["x-rate"] == str(SR) and r.headers["x-channels"] == "2"
            # The TestClient buffers the stream until it ends (browsers play it continuously):
            # it ends by itself when the test file is through
            for chunk in r.iter_bytes():
                got += chunk
        assert c.get("/api/live/pcm").status_code == 409                    # ended cleanly
    assert len(got) % 4 == 0 and len(got) >= SR * 2 * 2 // 2                 # whole frames, ≥ 0.5 s of audio
    pcm = np.frombuffer(got, dtype="<i2")
    assert int(np.abs(pcm).max()) > 1000


def test_live_pcm_rewind_streams_the_file_being_written(tmp_path):
    """Rewind: ?start=… delivers the audio from that second out of the WAV being written, then on to the end,
    bit-exact like the finished file."""
    audio, _ = synth.make_side(duration_s=20, seed=8, clicks_per_min=10)
    wav = tmp_path / "side.wav"
    sf.write(wav, audio, SR, subtype="PCM_16")
    cfg = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state",
                  fake_source=str(wav), fake_speed=4.0, min_free_gb=0.0)
    with TestClient(create_app(cfg)) as c:
        rid = c.post("/api/records", json={"artist": "Test", "title": "Rewind"}).json()["id"]
        r = c.post("/api/recordings/start", json={"record_id": rid, "kind": "before"})
        assert r.status_code == 200, r.text
        rec_id = r.json()["recording_id"]
        time.sleep(0.8)                                                      # ≈ 3 s of recording are written
        got = b""
        with c.stream("GET", "/api/live/pcm", params={"start": 1.0}) as s:
            assert s.status_code == 200
            for chunk in s.iter_bytes():
                got += chunk
        rec = _wait(c, rec_id, timeout=60)                      # "recorded" lasts only briefly, then "analyzed"
        ref = sf.read(rec["path"], dtype="int16")[0]
    assert len(got) == (len(ref) - SR) * 4                                   # from second 1 to the end
    assert got == ref[SR:].tobytes()


def test_wav_data_offset_and_pcm16(tmp_path):
    from vinylcheck import capture
    x = (np.arange(8, dtype=np.int16).reshape(-1, 2) * 1000)
    p = tmp_path / "a.wav"
    sf.write(p, x, SR, subtype="PCM_16")
    off = capture.wav_data_offset(p)
    assert off == 44 and p.read_bytes()[off:] == x.tobytes()
    raw = (np.arange(4, dtype="<i4") << 20).tobytes()
    assert capture.pcm16(raw, "S32_LE", 2) == (np.arange(4, dtype="<i4") << 4).astype("<i2").tobytes()


def test_settings_are_validated_saved_and_survive_restart(tmp_path):
    cfg = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state", min_free_gb=0.0)
    with TestClient(create_app(cfg)) as c:
        g = c.get("/api/settings").json()
        assert g["values"]["retention_days"] == 30 and g["values"]["autostop_end_groove"] is True
        assert g["spec"]["retention_days"]["unit"] == "days" and "device" in g["info"]
        assert g["spec"]["retention_days"]["label"] == "Keep original audio for" and g["spec"]["engineer_mode"]["help"]
        bad = c.put("/api/settings", json={"retention_days": -1, "max_record_minutes": "abc", "nope": 1})
        assert bad.status_code == 422
        f = bad.json()["detail"]["fields"]
        assert f == {"retention_days": "expected between 0 and 3650 days", "max_record_minutes": "number expected",
                     "nope": "unknown setting"}
        ok = c.put("/api/settings", json={"retention_days": 90, "autostop_end_groove": False, "min_free_gb": 3.5, "discogs_sync_minutes": 5,
                                           "clip_grace_s": 2.5, "clip_mention_samples": 50, "clip_max_samples": 900})
        assert ok.status_code == 200 and ok.json()["values"]["retention_days"] == 90 and ok.json()["values"]["discogs_sync_minutes"] == 5
        assert ok.json()["values"]["clip_grace_s"] == 2.5 and c.get("/api/status").json()["clip_limits"] == {"mention": 50, "max": 900, "grace_s": 2.5}
        assert c.put("/api/settings", json={"discogs_sync_minutes": 0}).status_code == 422
        assert c.get("/api/status").json()["retention_days"] == 90                 # effective immediately
    cfg2 = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state", min_free_gb=0.0)
    with TestClient(create_app(cfg2)) as c:                                        # "restart"
        v = c.get("/api/settings").json()["values"]
        assert v["retention_days"] == 90 and v["autostop_end_groove"] is False and v["min_free_gb"] == 3.5


# ─── Languages ────────────────────────────────────────────────────────────────
def test_error_details_follow_the_request_language(tmp_path):
    """HTTP error details are i18n messages: English by default, German when the request asks for it."""
    cfg = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state", min_free_gb=0.0)
    with TestClient(create_app(cfg)) as c:
        assert c.get("/api/records/999999").json() == {"detail": "Record not found"}
        de = c.get("/api/records/999999", cookies={"vc_lang": "de"})
        assert de.status_code == 404 and de.json() == {"detail": "Platte nicht gefunden"}
        assert c.get("/api/records/999999", params={"lang": "de"}).json()["detail"] == "Platte nicht gefunden"
        bad = c.put("/api/settings", json={"nope": 1, "min_free_gb": 500}, params={"lang": "de"})
        assert bad.json()["detail"]["fields"] == {"nope": "unbekannte Einstellung",
                                                  "min_free_gb": "zwischen 0,0 und 200,0 GB erwartet"}
        spec = c.get("/api/settings", params={"lang": "de"}).json()["spec"]["retention_days"]
        assert spec["unit"] == "Tage" and spec["label"] == "Original-Audio aufbewahren"


def test_interrupted_recording_error_is_rendered_per_reader(tmp_path):
    """A recording still running at shutdown is marked as failed with a stored message, shown in the reader's
    language."""
    from vinylcheck.db import DB
    cfg = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state", min_free_gb=0.0)
    cfg.state_dir.mkdir(parents=True)
    db = DB(cfg.db_path)
    rid = db.add_recording(record_id=None, side="A", kind="other", status="recording")
    db.conn.close()
    with TestClient(create_app(cfg)) as c:
        r = c.get(f"/api/recordings/{rid}").json()
        assert r["status"] == "error" and r["error"] == "The service stopped during the recording"
        de = c.get(f"/api/recordings/{rid}", params={"lang": "de"}).json()
        assert de["error"] == "Dienst während der Aufnahme beendet" and de["kind_label"] == "sonstige"


def test_dynamic_label_keys_exist():
    """Label keys built at runtime (kinds, findings, settings, polarity, verdicts, approval checks, technical data)
    are in the catalog; test_i18n only checks literal keys."""
    from vinylcheck import web
    en = i18n.catalog("en")
    keys = [m["$t"] for m in (*web.KIND_LABEL.values(), *web.FINDINGS_LABEL.values(), *web.POLARITY_LABEL.values(),
                              *web.VERDICT_TEXT.values())]
    keys += [web.SETTINGS[k][p]["$t"] for k in web.SETTINGS for p in ("label", "help")]
    src = Path(web.__file__).read_text(encoding="utf-8")
    keys += [f"labels.approval_check.{k}" for k in re.findall(r'check\("([a-z_]+)"', src)]
    keys += [f"labels.tech.{k}" for k in re.findall(r'lab\("([a-z_]+)"\)', src)]
    assert len(keys) > 50 and [k for k in keys if k not in en] == []


# ─── Turntable profiles ───────────────────────────────────────────────────────
def test_profiles_migration_groups_existing_data_by_device(tmp_path):
    import sqlite3

    from vinylcheck import db as dbmod
    path = tmp_path / "vinyl-check.sqlite"
    con = sqlite3.connect(path)
    con.executescript(dbmod.SCHEMA)                       # schema of 0.3.x: tables without profile_id
    con.executescript("""
        INSERT INTO setups (id, label, created_at) VALUES (1, 'Run turntable A', 1), (2, 'Run turntable B', 5);
        INSERT INTO recordings (id, setup_id, kind, side, device, chain_db, status, created_at) VALUES
          (1, NULL, 'calibration', '-', 'hw:CARD=Interface,DEV=0', -91.0, 'analyzed', 1),
          (2, 1, 'alignment', '1', 'hw:CARD=Interface,DEV=0', NULL, 'analyzed', 2),
          (3, 2, 'alignment', '1', 'hw:CARD=CODEC,DEV=0', NULL, 'analyzed', 6),
          (4, NULL, 'before', 'A', 'hw:CARD=CODEC,DEV=0', NULL, 'analyzed', 7);""")
    con.close()
    db = dbmod.DB(path)
    assert _backups(tmp_path, "0.4.0"), "no backup before the migration"
    profs = {p["device"]: p for p in db.profiles()}
    assert set(profs) == {"hw:CARD=Interface,DEV=0", "hw:CARD=CODEC,DEV=0"}
    interface, usb = profs["hw:CARD=Interface,DEV=0"], profs["hw:CARD=CODEC,DEV=0"]
    assert usb["active"] == 1 and interface["active"] == 0                # the latest recording came from the USB deck
    assert interface["chain_db"] == -91.0 and interface["calibration_id"] == 1
    assert db.setup(1)["profile_id"] == interface["id"] and db.setup(2)["profile_id"] == usb["id"]
    assert db.recording(4)["profile_id"] == usb["id"]
    dbmod.DB(path)                                                        # second start: nothing duplicated
    assert len(db.profiles()) == 2 and len(_backups(tmp_path, "0.4.0")) == 1


def test_profile_wizard_flow_and_recordings_carry_profile(jclient):
    c = jclient
    p = c.post("/api/profiles", json={"name": "Turntable A", "cartridge": "AT3600L", "device": "hw:CARD=CODEC,DEV=0",
                                      "tracking_force_g": 3.0, "antiskating": "3", "weight": "none"}).json()
    assert p["active"] == 1 and p["connected"] and p["progress"]["next"] == "calibration"
    assert c.get("/api/status").json()["profile"]["name"] == "Turntable A"
    assert c.post("/api/profiles", json={"name": " "}).status_code == 422
    assert c.post(f"/api/profiles/{p['id']}/accept", json={}).status_code == 409            # no run yet
    run = c.post(f"/api/profiles/{p['id']}/run").json()["id"]
    assert c.post(f"/api/profiles/{p['id']}/run").json()["id"] == run                        # resumable
    s = c.get(f"/api/setups/{run}").json()
    assert s["profile_id"] == p["id"] and s["tracking_force_g"] == 3.0 and s["bias"] == "3"
    assert s["label"] == "Calibration Turntable A" and s["notes"] == "Record weight: none"
    rec = _record_alignment(c, run, side="2")
    assert rec["status"] == "analyzed" and rec["profile_id"] == p["id"]
    prog = c.get(f"/api/profiles/{p['id']}").json()["progress"]
    assert prog["side2"]["status"] == "analyzed" and prog["run_id"] == run
    r = c.post(f"/api/profiles/{p['id']}/accept", json={})
    assert r.status_code == 409 and "side 1" in r.json()["detail"]                       # reference tone missing
    q = c.post("/api/profiles", json={"name": "Turntable B"}).json()
    assert q["active"] == 0
    assert c.delete(f"/api/profiles/{p['id']}").status_code == 409                          # active
    assert c.post(f"/api/profiles/{q['id']}/activate").json()["active"] == 1
    assert c.get("/api/status").json()["profile"]["id"] == q["id"]
    assert [x["id"] for x in c.get("/api/setups", params={"profile_id": p["id"]}).json()] == [run]
    assert c.delete(f"/api/profiles/{p['id']}").json()["ok"]
    assert c.get(f"/api/recordings/{rec['id']}").json()["profile_id"] is None              # the recording stays


def test_profile_trim_pop_threshold_and_speed(tmp_path):
    from vinylcheck.db import DB
    from vinylcheck.jobs import Worker
    cfg = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state")
    db = DB(cfg.db_path)
    w = Worker(cfg, db)
    pid = db.add_profile(name="Turntable A", ref_dbfs=-13.5, speed_pct=0.69, chain_db=-88.0, accepted_at=1.0,
                         floor_clicks_per_min=678.0, floor_ticks_per_min=900.0, sensitivity=0.6)
    t = w.profile_trim({"profile_id": pid, "rpm": 100 / 3})
    assert t["pop_dbfs"] == pytest.approx(-13.5) and t["chain_db"] == -88.0               # pop = reference level
    assert t["rpm"] == pytest.approx(100 / 3 * 1.0069, abs=1e-3)
    assert t["floor"]["clicks_per_min"] == 678.0 and t["floor"]["ticks_per_min"] == 900.0 and t["floor"]["source"]
    assert t["sensitivity"] == 0.6
    # the recording's sensitivity wins over the profile's
    assert w.profile_trim({"profile_id": pid, "rpm": 100 / 3, "sensitivity": 0.3})["sensitivity"] == 0.3
    raw = db.add_profile(name="new", ref_dbfs=-13.5, speed_pct=0.69, floor_clicks_per_min=10.0)   # not accepted
    t2 = w.profile_trim({"profile_id": raw, "rpm": 100 / 3})
    assert t2["pop_dbfs"] == cfg.analysis.pop_dbfs and t2["rpm"] == pytest.approx(100 / 3, abs=1e-4)
    assert t2["floor"] is None and t2["sensitivity"] == 0.5
    assert w.profile_trim({"rpm": 45})["id"] is None
    fixed = db.add_profile(name="USB", fixed_gain=True, accepted_at=1.0)
    assert w.profile_trim({"profile_id": fixed, "rpm": 100 / 3})["fixed_gain"] is True and t["fixed_gain"] is False


def test_targets_api_and_profile_override(jclient):
    from vinylcheck import alignment
    c = jclient
    t = c.get("/api/targets").json()
    assert t["active"] == "standard" and {m["id"] for m in t["maps"]} >= {"standard", "din45500", "lab", "at3600l", "uccello"}
    assert "balance_db" in t["fields"]
    # built-in maps (standard, manufacturers) are read-only; custom maps are created as copies
    std = next(m for m in t["maps"] if m["id"] == "standard")["targets"]
    body = {"targets": {**std, "balance_db": {"ok": 0.2, "warn": 0.4}}}
    r = c.put("/api/targets/standard", json=body)
    assert r.status_code == 409 and "is built in and read-only" in r.json()["detail"]
    assert next(m for m in c.get("/api/targets").json()["maps"] if m["id"] == "standard")["modified"] is False
    wrong = {"name": "Mine", "note": "x", "targets": {**std, "balance_db": {"ok": 2, "warn": 1}}}   # warn inside ok
    assert c.put("/api/targets/mine", json=wrong).status_code == 422
    assert c.put("/api/targets/New!", json=body).status_code == 422
    incomplete = c.put("/api/targets/mine", json={"name": "Mine", "targets": {"balance_db": {"ok": 1, "warn": 2}}})
    assert incomplete.status_code == 422 and incomplete.json()["detail"].startswith("A new map is missing: ")
    r = c.put("/api/targets/mine", json={"name": "Mine", "note": "Test", "targets": std}).json()
    assert next(m for m in r["maps"] if m["id"] == "mine")["builtin"] is False
    assert c.post("/api/targets/active", json={"id": "mine"}).json()["active"] == "mine"
    assert c.get("/api/status").json()["targets"] == "mine"
    assert c.delete("/api/targets/standard").status_code == 404                     # nothing to delete, unchanged
    # the findings of a run use the map of the profile
    p = c.post("/api/profiles", json={"name": "Turntable A", "target_map": "din45500"}).json()
    din_name = i18n.render(alignment.TARGET_MAPS["din45500"]["name"], "en")                    # text from alignment.py
    assert p["targets_used"] == {"id": "din45500", "name": din_name, "own": True}
    run = c.post(f"/api/profiles/{p['id']}/run").json()["id"]
    rec = _record_alignment(c, run, side="2")
    assert rec["status"] == "analyzed"
    s = c.get(f"/api/setups/{run}").json()
    mc = next(it for it in s["results"] if it["key"] == "mono_cancel_db")
    assert "20 dB" in mc["target"]                                                         # DIN map: ≤ −20 dB
    c.patch(f"/api/profiles/{p['id']}", json={"target_map": "lab"})
    s = c.get(f"/api/setups/{run}").json()
    assert "25 dB" in next(it for it in s["results"] if it["key"] == "mono_cancel_db")["target"]
    res_target = next(it for it in s["results"] if it["key"] == "resonance_hz")["target"]
    assert res_target.startswith("8") and res_target.endswith("11 Hz")
    r = c.get(f"/api/recordings/{rec['id']}").json()
    assert "25 dB" in next(it for it in r["analysis"]["metrics"]["results"] if it["key"] == "mono_cancel_db")["target"]
    c.patch(f"/api/profiles/{p['id']}", json={"target_map": None})
    assert c.get(f"/api/profiles/{p['id']}").json()["targets_used"]["id"] == "mine"    # back to the global map
    assert c.delete("/api/targets/mine").json()["active"] == "standard"


def test_measured_grade_can_be_written_to_collection(tmp_path, monkeypatch):
    import io
    from vinylcheck import discogs, web
    audio, _ = synth.make_side(duration_s=40, seed=5, clicks_per_min=30)
    wav = tmp_path / "side.wav"
    sf.write(wav, audio, SR, subtype="PCM_16")
    written = {}

    def fake_urlopen(req, timeout=0):
        url = req.full_url
        if "/collection/folders/0/releases" in url:
            item = {"instance_id": 42, "folder_id": 1, "date_added": "2026-09-07T10:00:00-07:00",
                    "basic_information": {"id": 1234567, "title": "Test Album", "year": 1988,
                                          "artists": [{"name": "Test Artist"}], "labels": [], "formats": []},
                    "notes": [{"field_id": 1, "value": "Good (G)"}]}
            return io.BytesIO(json.dumps({"pagination": {"pages": 1}, "releases": [item]}).encode())
        if "/fields/1" in url:
            written["url"] = url; written["value"] = json.loads(req.data)["value"]
            return io.BytesIO(b"")
        if "/releases/1234567" in url:                                # side lengths at the start of a recording
            return io.BytesIO(json.dumps({"id": 1234567, "tracklist": []}).encode())
        raise AssertionError(url)

    monkeypatch.setattr(discogs.urllib.request, "urlopen", fake_urlopen)
    cfg = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state", fake_source=str(wav),
                  fake_speed=200.0, min_free_gb=0.0, discogs_token="t", discogs_username="testuser")
    with TestClient(create_app(cfg)) as c:
        c.post("/api/discogs/sync")
        assert c.get("/api/status").json()["discogs_write"] is True
        info = c.get("/api/settings").json()["info"]["discogs"]
        assert info == "testuser (personal access token from the configuration file)"
        rid = c.post("/api/records", json={"artist": "Test Artist", "title": "Test Album",
                                           "discogs_release_id": 1234567,
                                           "discogs_media_condition": "Good (G)"}).json()["id"]
        rec = _record(c, rid, "after")
        grade = rec["analysis"]["grading"]["grade"]
        assert grade
        r = c.post(f"/api/records/{rid}/condition", json={"recording_id": rec["id"]})
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "grade": grade, "previous": "Good (G)", "current": web.GRADE_TO_DISCOGS[grade]}
        assert "/folders/1/releases/1234567/instances/42/fields/1" in written["url"]
        assert written["value"] == web.GRADE_TO_DISCOGS[grade]
        assert c.get(f"/api/records/{rid}").json()["discogs_media_condition"] == web.GRADE_TO_DISCOGS[grade]
        assert c.get(f"/api/records/{rid}/collection").json()["media_condition"] == web.GRADE_TO_DISCOGS[grade]
        r = c.post(f"/api/records/{rid}/condition", json={"copy": ""})            # record grade of the copy
        assert r.status_code == 200 and r.json()["grade"] == grade, r.text
        assert c.post(f"/api/records/{rid}/condition", json={"copy": "Copy 9"}).status_code == 409
        other = c.post("/api/records", json={"artist": "X", "title": "Y"}).json()["id"]
        r = c.post(f"/api/records/{other}/condition", json={"recording_id": rec["id"]})
        assert r.status_code == 409 and r.json()["detail"].startswith("The record has no Discogs release ID")


def test_system_endpoint_lists_jobs_and_load(client):
    rid = client.post("/api/records", json={"artist": "Test", "title": "Synth"}).json()["id"]
    a = _record(client, rid, "before")
    for _ in range(150):                          # the spectrogram still runs in the child process after "analyzed"
        s = client.get("/api/system").json()
        if s["worker"]["current"] is None:
            break
        time.sleep(0.2)
    assert "load" in s and "cores" in s["load"] and s["service"]["version"]
    assert s["worker"]["current"] is None and s["worker"]["pending"] == []
    h = s["worker"]["history"]
    assert h and h[0]["kind"] == "analyze" and h[0]["id"] == a["id"] and h[0]["ok"] and h[0]["seconds"] >= 0
    assert h[0]["label"] == f"Recording #{a['id']}: Test – Synth, side A, uncleaned"
    de = client.get("/api/system", params={"lang": "de"}).json()["worker"]["history"][0]["label"]
    assert de == f"Aufnahme #{a['id']}: Test – Synth, Seite A, ungereinigt"
    s2 = client.get("/api/system").json()              # second sample: CPU percent is there (Linux) or stays None
    assert s2["load"]["cpu_percent"] is None or 0 <= s2["load"]["cpu_percent"] <= 100
    m = client.get(f"/api/recordings/{a['id']}").json()["analysis"]["metrics"]
    assert "second_opinion" in m and "so_new" not in m              # cached values are not part of the metrics


def test_records_list_carries_grades_per_copy(client):
    rid = client.post("/api/records", json={"artist": "Test", "title": "Two Copies"}).json()["id"]
    a = client.post("/api/recordings/start", json={"record_id": rid, "side": "A", "kind": "before", "copy_label": "Version 1"})
    assert a.status_code == 200
    deadline = time.time() + 30
    while client.get("/api/status").json()["recorder"]["active"] and time.time() < deadline:
        time.sleep(0.1)
    _wait(client, a.json()["recording_id"])
    b = client.post("/api/recordings/start", json={"record_id": rid, "side": "A", "kind": "before", "copy_label": "Version 2"})
    while client.get("/api/status").json()["recorder"]["active"] and time.time() < deadline + 30:
        time.sleep(0.1)
    _wait(client, b.json()["recording_id"])
    r = next(x for x in client.get("/api/records").json() if x["id"] == rid)
    g = json.loads(r["grades_by_copy"])
    assert sorted(x["copy"] for x in g) == ["Version 1", "Version 2"] and all(x["grade"] for x in g)
    assert all([s["side"] for s in x["sides"]] == ["A"] for x in g)                 # the sides per copy
    det = client.get(f"/api/records/{rid}").json()
    assert det["copies"] == g and det["copies"][0]["grade"] == det["copies"][0]["sides"][0]["grade"]


def test_findings_and_notes_on_recordings(client):
    rid = client.post("/api/records", json={"artist": "Test", "title": "Findings"}).json()["id"]
    r = client.post("/api/recordings/start", json={"record_id": rid, "side": "A", "kind": "before",
                                                   "notes": "Scratch near the edge", "findings": ["warp", "scratch"]})
    assert r.status_code == 200
    rec_id = r.json()["recording_id"]
    deadline = time.time() + 30
    while client.get("/api/status").json()["recorder"]["active"] and time.time() < deadline:
        time.sleep(0.1)
    x = _wait(client, rec_id)
    assert x["findings"] == ["scratch", "warp"] and x["notes"] == "Scratch near the edge"   # in list order, as a list
    bad = client.patch(f"/api/recordings/{rec_id}", json={"findings": ["nonsense"]})
    assert bad.status_code == 422
    assert bad.json()["detail"].startswith("Unknown finding: nonsense; allowed are scratch, ")
    # findings belong to the copy and side: a second recording of the same side inherits them, side B does not
    r2 = client.post("/api/recordings/start", json={"record_id": rid, "side": "a ", "kind": "after_wet"})
    assert r2.status_code == 200
    while client.get("/api/status").json()["recorder"]["active"] and time.time() < deadline + 30:
        time.sleep(0.1)
    assert client.get(f"/api/recordings/{r2.json()['recording_id']}").json()["findings"] == ["scratch", "warp"]
    r3 = client.post("/api/recordings/start", json={"record_id": rid, "side": "B", "kind": "before"})
    while client.get("/api/status").json()["recorder"]["active"] and time.time() < deadline + 60:
        time.sleep(0.1)
    assert client.get(f"/api/recordings/{r3.json()['recording_id']}").json()["findings"] == []
    det = client.get(f"/api/records/{rid}").json()
    assert [(f["copy_key"], f["side"], f["findings"]) for f in det["findings"]] == [("", "A", ["scratch", "warp"])]
    # a change in one recording applies to all recordings of the copy's side
    assert client.patch(f"/api/recordings/{rec_id}", json={"findings": [], "notes": ""}).json()["findings"] == []
    assert all(x["findings"] == [] for x in client.get(f"/api/records/{rid}").json()["recordings"])
    assert client.patch(f"/api/recordings/{r2.json()['recording_id']}", json={"findings": ["warp"]}).json()["findings"] == ["warp"]
    assert client.get(f"/api/recordings/{rec_id}").json()["findings"] == ["warp"]
    unknown = {"record_id": rid, "side": "A", "kind": "before", "findings": ["nothing"]}
    assert client.post("/api/recordings/start", json=unknown).status_code == 422


def test_pressing_grade_over_copies_without_outliers(client):
    """Grade of the pressing: average over the sides of all copies, from 4 sides on without the best and the worst;
    with a single copy there is no overall grade (the tile needs several copies)."""
    rid = client.post("/api/records", json={"artist": "Test Artist", "title": "Test Pressings"}).json()["id"]
    for i in range(1, 5):
        r = client.post("/api/recordings/start", json={"record_id": rid, "side": "A", "kind": "before",
                                                       "copy_label": f"Copy {i}"})
        assert r.status_code == 200, r.text
        rec_id = r.json()["recording_id"]
        deadline = time.time() + 30
        while client.get("/api/status").json()["recorder"]["active"] and time.time() < deadline:
            time.sleep(0.1)
        assert _wait(client, rec_id)["status"] == "analyzed"
    p = client.get(f"/api/records/{rid}").json()["pressing"]
    assert p["copies"] == 4 and p["sides"] == 4 and p["counted"] == 2 and len(p["dropped"]) == 2
    assert all(d["side"].startswith("Copy ") and d["side"].endswith(" A") for d in p["dropped"])
    grades = {c["copy"]: c["grade"] for c in client.get(f"/api/records/{rid}").json()["copies"]}
    assert p["grade"] in grades.values() and "ear" not in p                    # same file: all equal, no master


def test_learn_api_status_and_retrain(client):
    """/api/learn shows the state, POST retrains at once; without examples no kind is usable."""
    s = client.get("/api/learn").json()
    assert s["enabled"] is True and s["grade"] is False
    r = client.post("/api/learn").json()
    assert r["model_id"] and set(r["kinds"]) == {"click", "tick", "pop"}
    assert not any(v["usable"] for v in r["kinds"].values())
    st = client.get("/api/status").json()["learn"]
    assert st["model_id"] == r["model_id"] and st["usable"] == []
    assert client.put("/api/settings", json={"learn_grade": True}).json()["values"]["learn_grade"] is True

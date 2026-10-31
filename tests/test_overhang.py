"""The end of a side: the recording stops once the stylus has been lifted after the music, and the time with the
stylus up that a recording still has can be cut off afterwards ("Remove overhang")."""

import time
from dataclasses import replace

import numpy as np
import soundfile as sf
from fastapi.testclient import TestClient

from vinylcheck import capture
from vinylcheck.config import Config
from vinylcheck.db import DB
from vinylcheck.web import create_app

SR = 48000


def test_stylus_up_watch():
    w = capture.StylusUpWatch(-84.0, hold_s=15.0, after_music_s=30.0)
    feed = lambda lvl, t, start: w.feed(lvl, 0.1, t, start)          # noqa: E731
    assert not any(feed(-95.0, i / 10, None) for i in range(400))     # before the music the stylus is up anyway
    assert not any(feed(-95.0, 20 + i / 10, 5.0) for i in range(100))  # too soon after the start of the music
    assert not any(feed(-30.0, 40 + i / 10, 5.0) for i in range(100))  # music
    assert not any(feed(-95.0, 50 + i / 10, 5.0) for i in range(140))  # 14 s stylus up …
    assert not feed(-60.0, 64.0, 5.0)                                   # … a sound (groove, a hand on the plinth)
    hits = [feed(-95.0, 64.1 + i / 10, 5.0) for i in range(160)]
    assert hits.index(True) == 149                                     # 15 s in a row from there


class _Src:
    def __init__(self, data, frames):
        self.chunks = [data[i:i + frames] for i in range(0, len(data), frames)]
        self.xruns, self.errors = 0, []

    def read(self, frames):
        return self.chunks.pop(0) if self.chunks else None

    def close(self):
        pass


def _side(music_s, up_s, seed=0):
    """Int16 stereo: groove with music (about −20 dBFS), then the stylus up (about −96 dBFS of noise)."""
    rng = np.random.default_rng(seed)
    music = rng.normal(0, 0.1, (int(music_s * SR), 2))
    up = rng.normal(0, 0.000015, (int(up_s * SR), 2))
    return (np.concatenate([music, up]) * 32767).astype(np.int64)


def test_recording_stops_when_the_stylus_is_lifted(tmp_path):
    data = _side(45, 60)
    rec = capture.Recorder(lambda dev, rate, ch, fmt: _Src(data, int(SR * capture.CHUNK_S)))
    done = []
    rec.start(device="x", rate=SR, channels=2, fmt="S16_LE", path=tmp_path / "a.wav", max_s=600,
              music_thr_dbfs=-40.0, stylus_up_dbfs=-84.0, on_done=done.append)
    deadline = time.time() + 30
    while not done and time.time() < deadline:
        time.sleep(0.05)
    st = done[0]
    assert st["stopped_reason"] == {"$t": "capture.stopped_stylus_up"}
    assert 45 + 14 <= st["duration_s"] <= 45 + 16                     # 15 s after the stylus went up
    # without a threshold (no calibrated chain) it records to the end of the source
    done.clear()
    rec2 = capture.Recorder(lambda dev, rate, ch, fmt: _Src(data, int(SR * capture.CHUNK_S)))
    rec2.start(device="x", rate=SR, channels=2, fmt="S16_LE", path=tmp_path / "b.wav", max_s=600,
               music_thr_dbfs=-40.0, on_done=done.append)
    while not done and time.time() < deadline + 30:
        time.sleep(0.05)
    assert done[0]["duration_s"] == 105.0 and not done[0].get("stopped_reason")


def _wait(c, rid, want=("analyzed", "error"), timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = c.get(f"/api/recordings/{rid}").json()
        if r["status"] in want:
            return r
        time.sleep(0.2)
    raise AssertionError(r["status"])


def test_remove_overhang(tmp_path):
    cfg = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state", min_free_gb=0.0)
    cfg.state_dir.mkdir(parents=True)
    path = tmp_path / "side.wav"
    sf.write(path, (_side(40, 70, seed=1) / 32767).astype(np.float32), SR, subtype="PCM_16")
    db = DB(cfg.db_path)
    rec = db.add_record("Artist", "Title")
    rid = db.add_recording(record_id=rec, kind="before", side="A", path=str(path), status="recorded")
    db.update_recording(rid, duration_s=110.0)                         # as the recorder stores it at the end
    db.conn.close()
    with TestClient(create_app(cfg)) as c:
        assert c.post(f"/api/recordings/{rid}/analyze").status_code == 200
        r = _wait(c, rid)
        assert r["status"] == "analyzed", r.get("error")
        end = r["analysis"]["metrics"]["groove_end_s"]
        assert 39 <= end <= 41 and r["overhang_s"] == round(110.0 - end - 2.0, 1)
        out = c.post(f"/api/recordings/{rid}/trim").json()
        assert out["duration_s"] == round(end + 2.0, 2) and out["removed_s"] == round(110.0 - out["duration_s"], 1)
        with sf.SoundFile(path) as f:
            assert abs(f.frames / SR - out["duration_s"]) < 0.01 and f.subtype == "PCM_16" and f.channels == 2
        r = _wait(c, rid)                                                # analysed again, new checksum
        assert r["status"] == "analyzed" and r["sha256"] and r["duration_s"] == out["duration_s"]
        assert r["overhang_s"] is None                                    # nothing left to remove
        res = c.post(f"/api/recordings/{rid}/trim")
        assert res.status_code == 409 and "30" in res.json()["detail"]
        # never while it is being recorded
        live = DB(cfg.db_path)
        rid2 = live.add_recording(record_id=rec, kind="before", side="B", path=str(path), status="recording")
        assert c.post(f"/api/recordings/{rid2}/trim").status_code == 409

"""Side length from Discogs, live start of the music, expected end and browser events."""

import io
import json
import time
from dataclasses import replace

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from vinylcheck import capture, discogs, synth
from vinylcheck.config import Config
from vinylcheck.web import create_app

SR = 48000


def test_side_lengths_from_tracklist():
    tl = [{"position": "A1", "duration": "11:54"}, {"position": "A2", "duration": "5:27"},
          {"position": "B1", "duration": "9:28"}, {"position": "B2", "duration": ""},
          {"position": "C1", "duration": "1:02:03"}, {"position": "", "type_": "heading", "duration": ""}]
    out = discogs.side_lengths(tl)
    assert out == {"A": 1041.0, "C": 3723.0}                     # B lacks a duration → no number
    assert discogs.side_lengths([{"position": "1-1", "duration": "3:00"}]) == {}   # master positions without a side
    assert discogs._dur_s("abc") is None and discogs._dur_s("0:33") == 33.0


class _Src:
    def __init__(self, chunks):
        self.chunks, self.xruns, self.errors = list(chunks), 0, []

    def read(self, frames):
        return self.chunks.pop(0) if self.chunks else None

    def close(self):
        pass


def test_music_start_and_expected_end_events():
    rate, n = 48000, int(48000 * capture.CHUNK_S)
    quiet = (np.random.default_rng(0).standard_normal((n, 2)) * 10 ** (-60 / 20) * 32768).astype(np.int64)
    loud = (np.sin(2 * np.pi * 1000 * np.arange(n) / rate)[:, None] * np.ones((1, 2)) * 0.1 * 32768).astype(np.int64)
    chunks = [quiet] * 20 + [loud] * 60                             # 2 s of quiet groove, then 6 s of music
    got = []
    rec = capture.Recorder(lambda dev, rate_, ch, fmt: _Src(chunks))
    rec.start(device="x", rate=rate, channels=2, fmt="S16_LE", path=None, max_s=30,
              music_thr_dbfs=-40.0, side_length_s=3.0, on_event=lambda name, st: got.append((name, round(st["frames"] / rate, 1))))
    rec._thread.join(20)                                            # the source runs dry by itself
    st = rec.status()
    assert st["music_start_s"] == pytest.approx(2.0, abs=0.15)
    assert st["expected_end_s"] == pytest.approx(5.0, abs=0.15)
    assert got and got[0][0] == "side_end" and got[0][1] == pytest.approx(5.0, abs=0.2)
    assert st["side_end_notified"] is True


def test_recording_uses_side_length_and_reports_remaining(tmp_path, monkeypatch):
    audio, _ = synth.make_side(duration_s=30, seed=9, clicks_per_min=5)
    wav = tmp_path / "record.wav"
    sf.write(wav, audio, SR, subtype="PCM_16")

    def fake(req, timeout=0):
        url = req.full_url
        if "/releases/777" in url:
            return io.BytesIO(json.dumps({"id": 777, "master_id": 9, "tracklist": [{"position": "A1", "duration": ""}]}).encode())
        if "/masters/9" in url:
            return io.BytesIO(json.dumps({"tracklist": [{"position": "A1", "duration": "0:20"}, {"position": "B1", "duration": "0:25"}]}).encode())
        raise AssertionError(url)

    monkeypatch.setattr(discogs.urllib.request, "urlopen", fake)
    cfg = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state", fake_source=str(wav),
                  fake_speed=3.0, min_free_gb=0.0)
    with TestClient(create_app(cfg)) as c:
        rid = c.post("/api/records", json={"artist": "Test Artist", "title": "Test Album",
                                           "discogs_release_id": 777}).json()["id"]
        rec = c.get(f"/api/records/{rid}").json()
        assert rec["side_lengths"] == {"A": {"s": 20.0, "source": "Discogs"}, "B": {"s": 25.0, "source": "Discogs"}}
        r = c.post("/api/recordings/start", json={"record_id": rid, "side": "A", "kind": "before"})
        assert r.status_code == 200, r.text
        seen_end = seen_remaining = False
        deadline = time.time() + 40
        while time.time() < deadline:
            st = c.get("/api/status").json()
            rs = st["recorder"]
            if not rs["active"]:
                break
            if rs.get("expected_end_s"):
                seen_remaining = True
                assert rs["meta"]["side_length_source"] == "Discogs"
            if any(e["kind"] == "side_end" for e in st["events"]):
                seen_end = True
            time.sleep(0.2)
        assert seen_remaining and seen_end
        st = c.get("/api/status").json()
        kinds = [e["kind"] for e in st["events"]]
        assert "side_end" in kinds

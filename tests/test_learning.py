"""Learning from master comparisons and verdicts: estimate of the real clicks, cross-check, use in the counts."""
import numpy as np

from vinylcheck import analysis, i18n, learning


def _events(rng, n_real, n_music, ref=-15.0, t0=0.0):
    """Synthetic events of one recording: real clicks quiet and short, music false alarms as loud as the music."""
    ev = []
    for i in range(n_real):
        ev.append({"t": t0 + i * 1.7 + rng.uniform(0, 0.2), "kind": "click", "region": "music", "ch": 2,
                   "strength": float(rng.uniform(40, 200)), "amp_dbfs": float(ref - rng.uniform(20, 35)), "dur_ms": 0.4,
                   "gc": float(rng.uniform(0.6, 1.0)), "label": None, "_y": 1.0})
    for i in range(n_music):
        ev.append({"t": t0 + 0.8 + i * 2.3 + rng.uniform(0, 0.2), "kind": "click", "region": "music", "ch": 3,
                   "strength": float(rng.uniform(30, 120)), "amp_dbfs": float(ref + rng.uniform(-4, 2)), "dur_ms": 0.9,
                   "gc": float(rng.uniform(0.3, 0.9)), "label": None, "_y": 0.0})
    return ev


def _data(recs):
    """recs: list of (recording, record, events) → data for learning.train as training_data() builds it."""
    X, y, w, rec, record, man, mas = [], [], [], [], [], [], []
    raw_n = {}
    for rid, plate, ev in recs:
        F = learning.features(ev, -15.0)
        raw_n[rid] = len(ev)
        for i, e in enumerate(ev):
            X.append(F[i]); y.append(e["_y"]); w.append(1.0); rec.append(rid); record.append(plate); man.append(False); mas.append(True)
    return {"click": {"X": np.array(X), "y": np.array(y), "w": np.array(w), "rec": np.array(rec), "record": np.array(record),
                      "manual": np.array(man), "master": np.array(mas), "raw_n": raw_n}}


def test_learns_to_estimate_real_clicks():
    rng = np.random.default_rng(1)
    recs = [(i, 1, _events(rng, 40 + 5 * i, 20 + 3 * i)) for i in range(1, 6)]
    model = learning.train(_data(recs))
    k = model["kinds"]["click"]
    assert k["usable"], i18n.render(k["reason"], "en")
    assert k["reason"]["$t"] == "learning.usable" and "estimates the real clicks" in i18n.render(k["reason"], "en")
    assert k["cv_err_est"] < k["cv_err_raw"] and k["cv_err_est"] <= learning.MAX_EST_ERROR
    pop = model["kinds"]["pop"]
    assert pop["usable"] is False and pop["reason"]["$t"] == "learning.no_examples"
    assert i18n.render(pop["label"], "en") == "Pops"
    assert i18n.render(model["kinds"]["tick"]["label"], "de") == "Knistern"
    # new recording: the estimate is close to the truth, the raw count clearly above it
    ev = _events(rng, 50, 30)
    wts, p = learning.estimate(ev, model, -15.0)
    assert abs(wts.sum() - 50) < 0.2 * 50 and len(ev) == 80
    assert np.nanmean(p[:50]) > 0.7 and np.nanmean(p[50:]) < 0.3


def test_manual_labels_always_win():
    rng = np.random.default_rng(2)
    model = learning.train(_data([(i, 1, _events(rng, 40, 25)) for i in range(1, 4)]))
    ev = _events(rng, 3, 3)
    ev[0]["label"] = "music"          # looks real, but judged a false alarm by hand
    ev[3]["label"] = "defect"         # looks like music, but judged real by hand
    ev[4]["label"] = "unsure"
    wts, _ = learning.estimate(ev, model, -15.0)
    assert wts[0] == 0.0 and wts[3] == 1.0 and wts[4] == 1.0


def test_not_usable_without_enough_master_recordings():
    rng = np.random.default_rng(3)
    model = learning.train(_data([(1, 1, _events(rng, 60, 40))]))
    k = model["kinds"]["click"]
    assert not k["usable"] and k.get("model") and k["reason"]["$t"] == "learning.not_verifiable_master"
    assert "not verifiable yet" in i18n.render(k["reason"], "en")
    wts, p = learning.estimate(_events(rng, 5, 5), model, -15.0)
    assert np.all(wts == 1.0) and not np.isnan(p).any()       # probabilities available, but the count stays raw


def test_recount_shows_estimate_and_grades_only_when_asked():
    rng = np.random.default_rng(4)
    model = learning.train(_data([(i, 1, _events(rng, 40, 30)) for i in range(1, 5)]))
    ev = _events(rng, 30, 30)
    for e in ev:
        e.pop("_y")
    metrics = {"quiet_s": 60.0, "groove_s": 660.0, "music_ref_dbfs": -15.0, "floor": None, "counts": {}}
    m_raw, g_raw = analysis.recount(metrics, ev)
    assert m_raw["learned"] is None and m_raw["clicks_music_per_min"] == 6.0          # 60 events in 10 min of music
    m, g = analysis.recount(metrics, ev, learned=model)
    assert m["learned"]["model_id"] == model["id"] and not m["learned"]["graded"]
    assert 2.0 < m["learned"]["clicks_music_per_min"] < 4.5 and m["clicks_music_per_min"] == 6.0
    assert g["grade"] == g_raw["grade"]                                                # shown only
    m2, g2 = analysis.recount(metrics, ev, learned=model, learn_grade=True)
    assert m2["learned"]["graded"]
    assert g2["per_metric"]["clicks_music_per_min"]["value"] == m2["learned"]["clicks_music_per_min"]

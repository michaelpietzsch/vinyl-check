"""Learning from master comparisons and verdicts: estimate of the real clicks, cross-check, use in the counts."""
import hashlib
import json

import numpy as np
import pytest

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


def _flipped(rng, n_real, n_music):
    """The opposite world: real clicks as loud as the music, false alarms quiet. A prior learned there is wrong here."""
    ev = _events(rng, n_real, n_music)
    for e in ev:
        e["_y"] = 1.0 - e["_y"]
    return ev


def _dist(a: dict, b: dict) -> float:
    return float(np.linalg.norm(np.array(a["weights"]) - np.array(b["weights"])))


def test_fixed_scaling_is_the_same_function():
    rng = np.random.default_rng(5)
    model = learning.train(_data([(i, 1, _events(rng, 40, 25)) for i in range(1, 4)]))
    m = model["kinds"]["click"]["model"]
    fixed = learning.to_fixed(m)
    assert set(learning.FIXED_SCALING) == set(learning.FEATURES) and len(fixed["weights"]) == len(learning.FEATURES)
    X = learning.features(_events(rng, 20, 20), -15.0)
    assert np.allclose(learning.predict_fixed(fixed, X), learning._predict(m, X), atol=1e-4)


def test_without_prior_nothing_changes():
    rng = np.random.default_rng(6)
    data = _data([(i, 1, _events(rng, 40, 25)) for i in range(1, 4)])
    a, b = learning.train(data, now=1.0), learning.train(data, now=1.0, prior=None)
    assert a == b and "prior_kinds" not in a
    assert a["kinds"]["click"]["cv_auc"] > 0.9


def test_prior_pulls_with_few_examples_and_fades_with_many():
    rng = np.random.default_rng(7)
    prior_model = learning.train(_data([(i, 1, _flipped(rng, 60, 60)) for i in range(1, 4)]))
    prior = {"click": learning.to_fixed(prior_model["kinds"]["click"]["model"])}

    few = _data([(1, 1, _events(rng, 12, 12))])
    own = learning.to_fixed(learning.train(few)["kinds"]["click"]["model"])
    pulled = learning.train(few, prior=prior)
    assert pulled["prior_kinds"] == ["click"]
    got = learning.to_fixed(pulled["kinds"]["click"]["model"])
    assert _dist(got, prior["click"]) < _dist(got, own)                # few examples: close to the prior

    many = _data([(i, 1, _events(rng, 400, 300)) for i in range(1, 6)])
    own = learning.to_fixed(learning.train(many)["kinds"]["click"]["model"])
    got = learning.to_fixed(learning.train(many, prior=prior)["kinds"]["click"]["model"])
    assert _dist(got, own) < _dist(got, prior["click"])
    X = learning.features(_events(rng, 50, 50), -15.0)                 # many examples: the own data win
    p_got, p_own, p_prior = (learning.predict_fixed(f, X) for f in (got, own, prior["click"]))
    assert np.mean(np.abs(p_got - p_own)) < 0.1 and np.mean(np.abs(p_got - p_prior)) > 0.8
    # the cross-check uses the prior as well, and a wrong prior does not stop a well-fed model from being usable
    assert learning.train(many, prior=prior)["kinds"]["click"]["usable"]


def test_training_data_per_profile(tmp_path):
    from vinylcheck.db import DB
    db = DB(tmp_path / "t.sqlite")
    rid = db.add_record("A", "B")
    p1, p2 = db.add_profile(name="one"), db.add_profile(name="two")
    rng = np.random.default_rng(8)
    for pid in (p1, p1, p2):
        rec = db.add_recording(record_id=rid, profile_id=pid, side="A", kind="before", status="analyzed")
        ev = _events(rng, 15, 10)
        db.save_analysis(rec, {"metrics": {"music_ref_dbfs": -15.0}, "grading": {"grade": None, "score": None},
                               "warnings": [], "overview": {},
                               "events": [{k: e.get(k) for k in ("t", "dur_ms", "strength", "amp_dbfs", "ch", "kind",
                                                                 "region", "gc")} for e in ev]}, "test", 1.0)
        db.set_master_check(rec, status="done", result={"clicks": {"events": [[round(e["t"], 3), "v" if e["_y"] else "m"]
                                                                              for e in ev]}})
    n = lambda d: len(d["click"]["y"])
    assert n(learning.training_data(db)[0]) == 75
    assert n(learning.training_data(db, p1)[0]) == 50 and n(learning.training_data(db, p2)[0]) == 25


# ─── Feature versions ────────────────────────────────────────────────────────

def _features_v1_reference(events, music_ref_dbfs):
    """Feature version 1 exactly as it was defined before feature versions existed (frozen copy): version 1 must stay
    bit-for-bit the same, or every stored weight and contribution of version 1 would silently change meaning."""
    n = len(events)
    if not n:
        return np.zeros((0, 10))
    t = np.array([float(e["t"]) for e in events])
    order = np.argsort(t)
    ts = t[order]
    lo = np.searchsorted(ts, ts - 0.5, "left")
    hi = np.searchsorted(ts, ts + 0.5, "right")
    dens = np.empty(n)
    dens[order] = hi - lo - 1
    strength = np.array([max(float(e.get("strength") or 0.0), 0.0) for e in events])
    amp = np.array([float(e["amp_dbfs"]) if e.get("amp_dbfs") is not None else -60.0 for e in events])
    amp_rel = amp - (float(music_ref_dbfs) if music_ref_dbfs is not None else -15.0)
    dur = np.array([max(float(e.get("dur_ms") or 0.0), 0.0) for e in events])
    gcv = [e.get("gc") for e in events]
    gc = np.array([0.5 if g is None else float(g) for g in gcv])
    gc_missing = np.array([1.0 if g is None else 0.0 for g in gcv])
    music = np.array([1.0 if e.get("region") == "music" else 0.0 for e in events])
    both = np.array([1.0 if e.get("ch") == 3 else 0.0 for e in events])
    ls = np.log1p(strength)
    return np.column_stack([ls, amp_rel, np.log1p(dur), gc, gc_missing, music, both, np.log1p(dens),
                            amp_rel ** 2 / 100.0, ls * amp_rel / 10.0])


def _random_events(rng, n):
    return [{"t": float(rng.uniform(0, 30)), "kind": "click", "ch": int(rng.integers(1, 4)),
             "strength": float(rng.uniform(5, 300)) if rng.random() > 0.1 else None,
             "amp_dbfs": float(rng.uniform(-80, -5)) if rng.random() > 0.1 else None,
             "dur_ms": float(rng.uniform(0, 3)), "gc": None if rng.random() < 0.5 else float(rng.random()),
             "region": str(rng.choice(["music", "quiet"]))} for _ in range(n)]


def test_feature_version_1_unchanged_bit_for_bit():
    rng = np.random.default_rng(11)
    zp = {"chain_db": -90.0, "ref_dbfs": -18.0, "floor_per_min": 60.0}
    for trial in range(25):
        ev = _random_events(rng, int(rng.integers(0, 80)))
        ref = None if trial % 5 == 0 else float(rng.uniform(-25, -10))
        want = _features_v1_reference(ev, ref)
        for got in (learning.features(ev, ref), learning.features(ev, ref, "1"),
                    learning.features(ev, ref, "1", zp)):                 # version 1 ignores the profile
            assert got.shape == want.shape and got.tobytes() == want.tobytes()
    assert learning.FEATURE_VERSION == "1" and learning.feature_names("1") == learning.FEATURES
    # the model and its id are those of version 1 before versions existed (no recount of every analysis on update)
    data = _data([(i, 1, _events(rng, 40, 25)) for i in range(1, 4)])
    model = learning.train(data, now=1.0)
    assert model == learning.train(data, now=1.0, version="1") and model["feature_version"] == "1"
    core = {k: {kk: v.get(kk) for kk in ("usable", "model")} for k, v in model["kinds"].items()}
    assert model["id"] == hashlib.sha1(json.dumps(core, sort_keys=True).encode()).hexdigest()[:12]
    assert learning.model_version({"kinds": {}}) == "1"                   # stored before versions existed


def test_feature_version_2_as_defined():
    zp = {"chain_db": -90.0, "ref_dbfs": -18.0, "floor_per_min": 120.0}   # λ = 2 neighbours within ±0.5 s
    ev = [{"t": 1.0 + 0.1 * i, "strength": 30.0, "amp_dbfs": a, "dur_ms": 0.5, "gc": None, "region": "music", "ch": 3}
          for i, a in enumerate((-40.0, -75.0, -95.0, -20.0))]
    ev.append({"t": 5.0, "strength": 9.0, "amp_dbfs": -60.0, "dur_ms": 0.2, "gc": 0.7, "region": "quiet", "ch": 1})
    X1 = learning.features(ev, -21.0)
    X2 = learning.features(ev, -21.0, "2", zp)
    names = list(learning.FEATURES_V2)
    assert X2.shape == (5, 12) and names == ["log_strength", "amp_rel", "log_dur", "gc", "gc_missing", "music",
                                             "both_ch", "log_density_net", "amp_rel2", "strength_x_amp", "amp_ref",
                                             "chain_margin"]
    same = [i for i, f in enumerate(learning.FEATURES) if f in names]
    assert np.array_equal(X2[:, same], X1[:, same])                     # chain-independent features unchanged
    dn = X2[:, names.index("log_density_net")]
    assert np.allclose(dn[:4], np.log1p(3 - 2)) and dn[4] == 0.0        # 3 neighbours minus the chain's 2
    assert np.allclose(X1[:4, learning.FEATURES.index("log_density")], np.log1p(3))
    assert np.allclose(X2[:, names.index("amp_ref")], [-22.0, -57.0, -77.0, -2.0, -42.0])
    assert np.allclose(X2[:, names.index("chain_margin")], [4.0, 1.5, 0.0, 4.0, 3.0])    # (amp + 90) / 10 in 0 … 4
    assert set(learning.FIXED_SCALING_V2) == set(names)
    # features constant on many profiles are centred at that value, so their intercepts stay comparable
    assert learning.FIXED_SCALING_V2["chain_margin"][0] == 4.0 and learning.FIXED_SCALING_V2["gc_missing"][0] == 1.0
    assert all(learning.FIXED_SCALING_V2[f] == learning.FIXED_SCALING[f] for f in names
               if f in learning.FIXED_SCALING and f != "gc_missing")


def test_zero_point_and_fallback():
    assert learning.zero_point(None) == {"chain_db": None, "ref_dbfs": None, "floor_per_min": None}
    measured = {"accepted_at": 1.0, "chain_db": -92.0, "ref_dbfs": -19.5, "floor_clicks_per_min": 20.0,
                "floor_ticks_per_min": 100.0}
    assert learning.zero_point(measured) == {"chain_db": -92.0, "ref_dbfs": -19.5, "floor_per_min": 120.0}
    assert learning.zero_point_missing(learning.zero_point(measured)) == []
    # not accepted: the calibration's chain noise counts, reference and floor only with an accepted run (profile_trim)
    pending = learning.zero_point(dict(measured, accepted_at=None))
    assert pending == {"chain_db": -92.0, "ref_dbfs": None, "floor_per_min": None}
    assert learning.zero_point_missing(pending) == ["ref_dbfs", "floor_per_min"]
    # the fallback adds no profile information: amp_ref = amp_rel − 5 dB, margin saturated, density as in version 1
    rng = np.random.default_rng(12)
    ev = _random_events(rng, 40)
    X1, X2 = learning.features(ev, -20.0), learning.features(ev, -20.0, "2", None)
    names = list(learning.FEATURES_V2)
    assert np.allclose(X2[:, names.index("amp_ref")], X1[:, 1] - learning.REF_ABOVE_MUSIC_DB)
    assert np.all(X2[:, names.index("chain_margin")] == learning.CHAIN_MARGIN_MAX_DB / 10)
    assert np.array_equal(X2[:, names.index("log_density_net")], X1[:, learning.FEATURES.index("log_density")])


def _seed_profiles(db, rng):
    """Three recordings with a master: one with a measured profile, one with a profile not accepted, one without."""
    rid = db.add_record("A", "B")
    good = db.add_profile(name="measured", chain_db=-92.0)
    db.update_profile(good, accepted_at=1.0, ref_dbfs=-19.0, floor_clicks_per_min=10.0, floor_ticks_per_min=50.0)
    pending = db.add_profile(name="pending", chain_db=-80.0)
    recs = {}
    for name, pid in (("good", good), ("pending", pending), ("none", None)):
        rec = db.add_recording(record_id=rid, profile_id=pid, side="A", kind="before", status="analyzed")
        ev = _events(rng, 15, 10)
        db.save_analysis(rec, {"metrics": {"music_ref_dbfs": -15.0}, "grading": {"grade": None, "score": None},
                               "warnings": [], "overview": {},
                               "events": [{k: e.get(k) for k in ("t", "dur_ms", "strength", "amp_dbfs", "ch", "kind",
                                                                 "region", "gc")} for e in ev]}, "test", 1.0)
        classified = [[round(e["t"], 3), "v" if e["_y"] else "m"] for e in ev]
        db.set_master_check(rec, status="done", result={"clicks": {"events": classified}})
        recs[name] = rec
    return good, pending, recs


def test_training_data_flags_fallback(tmp_path):
    from vinylcheck.db import DB
    db = DB(tmp_path / "t.sqlite")
    good, pending, recs = _seed_profiles(db, np.random.default_rng(13))
    d1, o1 = learning.training_data(db)
    assert "recordings_fallback" not in o1 and not d1["click"]["fallback"].any()     # version 1: no zero point
    d2, o2 = learning.training_data(db, version="2")
    c = d2["click"]
    assert c["X"].shape == (75, 12) and o2["recordings_fallback"] == 2
    assert set(c["rec"][c["fallback"]].tolist()) == {recs["pending"], recs["none"]}
    kept = learning.drop_fallback(d2)["click"]
    assert set(kept["rec"].tolist()) == {recs["good"]} and set(kept["raw_n"]) == {recs["good"]}
    assert len(kept["y"]) == len(kept["X"]) == len(kept["w"]) == 25
    # the measured profile is the zero point of its recording: amp_ref against its reference level
    row = c["X"][c["rec"] == recs["good"]][0]
    assert np.isclose(row[10], row[1] - 15.0 + 19.0)                       # amp − (−19) = amp_rel + (−15) + 19
    only = learning.training_data(db, good, version="2")[0]["click"]
    assert not only["fallback"].any() and len(only["y"]) == 25


def test_versions_are_never_mixed():
    rng = np.random.default_rng(14)
    data1 = _data([(i, 1, _events(rng, 40, 25)) for i in range(1, 4)])
    with pytest.raises(ValueError):
        learning.train(data1, version="2")                                 # 10 features are not version 2
    with pytest.raises(ValueError):
        learning.features([], None, "3")
    m1 = learning.train(data1)
    fixed1 = learning.to_fixed(m1["kinds"]["click"]["model"])
    with pytest.raises(ValueError):
        learning.to_fixed(m1["kinds"]["click"]["model"], "2")
    ev = _events(rng, 10, 10)
    with pytest.raises(ValueError):
        learning.predict_fixed(fixed1, learning.features(ev, -15.0, "2"), "2")
    # a version-2 model trains on version-2 data, refuses a version-1 prior and estimates with its own features
    c1 = data1["click"]
    data2 = {"click": dict(c1, X=np.column_stack([c1["X"], np.zeros((len(c1["y"]), 2))]))}
    with pytest.raises(ValueError):
        learning.train(data2, version="2", prior={"click": fixed1})
    m2 = learning.train(data2, version="2")
    assert m2["feature_version"] == "2" and m2["features"] == list(learning.FEATURES_V2) and m2["id"] != m1["id"]
    assert len(learning.to_fixed(m2["kinds"]["click"]["model"], "2")["weights"]) == 12
    w, p = learning.estimate(ev, m2, -15.0, {"chain_db": -90.0, "ref_dbfs": -18.0, "floor_per_min": 0.0})
    assert len(p) == 20 and not np.isnan(p).any()
    assert learning.summary(m2)["feature_version"] == "2" and learning.summary(m1)["feature_version"] == "1"


def test_feature_version_setting(tmp_path):
    from vinylcheck import config
    f = tmp_path / "config.toml"
    assert config.load(str(f)).feature_version == "1"                     # default: version 1 stays active
    f.write_text("feature_version = 2\n")
    assert config.load(str(f)).feature_version == "2"
    f.write_text('feature_version = "3"\n')
    with pytest.raises(ValueError, match="feature_version"):
        config.load(str(f))

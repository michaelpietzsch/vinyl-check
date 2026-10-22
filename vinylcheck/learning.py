"""Learning from master comparisons and verdicts: estimate how many of the reported events are real clicks.

Examples: every master comparison classifies each event of an analysis as "v" (only on the vinyl, a real click)
or "m" (also in the master, i.e. music: a false alarm). On top of that come the verdicts given by hand in the
audit view ("knackser" = real, "musik" = false alarm); they count MANUAL_WEIGHT times and take precedence over the
master's classification ("unsicher" = unsure is left out).

Model: per event kind (click, crackle = "tick", pop) a ridge-regularized logistic regression on features the
analysis already stores (strength, level relative to the recording's music reference, duration, second-opinion
score, region, both channels, event density within ±DENSITY_S), plus two curvature terms. Pure numpy; training
takes milliseconds even on a Raspberry Pi.

**Estimate rather than filter.** The model cannot separate individual events reliably enough (crackle: at best
about 80 % correct when used as a filter, even with a high-frequency feature). Its probabilities are well
calibrated, though: their sum over all events estimates the number of real clicks. In a leave-one-recording-out
cross-check on ten recordings with a master (music passages), the raw count was off by +15 % for clicks, +52 % for
crackle and +208 % for pops; the estimate by ±4 %, ±5 % and ±23 %. A kind counts as usable when its estimate is
off by at most MAX_EST_ERROR per recording in the cross-check and beats the raw count. Verdicts given by hand count
with fixed weights in the estimate (real and "unsure" 1, false alarm 0).

Status texts in the model (`label`, `reason`, `check`) are i18n messages; the model is stored as JSON.
"""
from __future__ import annotations

import hashlib
import json
import time

import numpy as np

from . import i18n

VERSION = 1
KINDS = ("click", "tick", "pop")
KIND_LABEL = {k: i18n.msg(f"learning.kind_{k}") for k in KINDS}       # column label ("Clicks")
KIND_NOUN = {k: i18n.msg(f"learning.noun_{k}") for k in KINDS}        # inside a sentence ("clicks")
FEATURES = ("log_strength", "amp_rel", "log_dur", "gc", "gc_missing", "music", "both_ch", "log_density",
            "amp_rel2", "strength_x_amp")
MIN_PER_CLASS = 10         # examples per class and kind before training at all
MIN_MASTER_RECORDINGS = 2  # recordings with a master (fully classified) on which the estimate is cross-checked
MANUAL_WEIGHT = 5.0        # one verdict by hand counts like five master classifications
MAX_EST_ERROR = 0.30       # largest mean deviation of the estimate per recording in the cross-check
L2 = 1.0                   # ridge penalty on the standardized features
DENSITY_S = 0.5            # window (±s) for the event density feature


def features(events: list[dict], music_ref_dbfs: float | None) -> np.ndarray:
    """Feature matrix (n × len(FEATURES)) for the events of one recording. The density needs all events of the
    recording, not just a selection. Levels are taken relative to the music reference (−15 dBFS if unknown)."""
    n = len(events)
    if not n:
        return np.zeros((0, len(FEATURES)))
    t = np.array([float(e["t"]) for e in events])
    order = np.argsort(t)
    ts = t[order]
    lo = np.searchsorted(ts, ts - DENSITY_S, "left")
    hi = np.searchsorted(ts, ts + DENSITY_S, "right")
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


def _fit(X: np.ndarray, y: np.ndarray, w: np.ndarray, l2: float = L2, iters: int = 60) -> dict:
    """Weighted logistic regression with ridge penalty (Newton's method) on standardized features."""
    mu = X.mean(axis=0)
    sd = X.std(axis=0)
    sd[sd < 1e-9] = 1.0
    Z = np.column_stack([np.ones(len(X)), (X - mu) / sd])
    b = np.zeros(Z.shape[1])
    R = np.eye(Z.shape[1]) * l2
    R[0, 0] = 0.0                                   # no penalty on the intercept
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(Z @ b, -30, 30)))
        g = Z.T @ (w * (y - p)) - R @ b
        H = (Z * (w * p * (1 - p) + 1e-9)[:, None]).T @ Z + R
        step = np.linalg.solve(H, g)
        b += step
        if np.max(np.abs(step)) < 1e-7:
            break
    return {"mu": mu.round(6).tolist(), "sd": sd.round(6).tolist(), "b": b.round(6).tolist()}


def _predict(m: dict, X: np.ndarray) -> np.ndarray:
    Z = (X - np.array(m["mu"])) / np.array(m["sd"])
    return 1.0 / (1.0 + np.exp(-np.clip(m["b"][0] + Z @ np.array(m["b"][1:]), -30, 30)))


def _auc(p: np.ndarray, y: np.ndarray) -> float | None:
    """Area under the ROC curve (rank statistic); None without both classes."""
    pos, neg = p[y == 1], p[y == 0]
    if not len(pos) or not len(neg):
        return None
    r = np.argsort(np.argsort(np.concatenate([pos, neg]))) + 1.0
    return float((r[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def train(data: dict, now: float | None = None) -> dict:
    """Train one model per kind and cross-check its estimate.

    data[kind] = {"X", "y" (1 real, 0 music), "w" (weight), "rec" (recording per row), "record" (record per row),
    "manual" (bool), "master" (row comes from a fully classified recording), "raw_n" {recording: events}}.
    Returns the model with a status per kind (JSON-serializable).
    """
    model = {"version": VERSION, "trained_at": now or time.time(), "features": list(FEATURES), "kinds": {}}
    for kind in KINDS:
        d = data.get(kind)
        info: dict = {"label": KIND_LABEL[kind], "usable": False}
        if d is None or not len(d["y"]):
            info.update(n_real=0, n_false=0, recordings=0, records=0, manual=0, reason=i18n.msg("learning.no_examples"))
            model["kinds"][kind] = info
            continue
        X, y, w = d["X"], d["y"].astype(float), d["w"].astype(float)
        rec, record, master = np.asarray(d["rec"]), np.asarray(d["record"]), np.asarray(d["master"], bool)
        mrecs = sorted(set(rec[master].tolist()))
        info.update(n_real=int(np.sum(y == 1)), n_false=int(np.sum(y == 0)), recordings=int(len(set(rec.tolist()))),
                    records=int(len(set(record.tolist()))), manual=int(np.sum(d["manual"])), master_recordings=len(mrecs),
                    master_records=int(len(set(record[master].tolist()))))
        if info["n_real"] < MIN_PER_CLASS or info["n_false"] < MIN_PER_CLASS:
            info["reason"] = i18n.msg("learning.too_few_examples", real=info["n_real"], false_alarms=info["n_false"],
                                      min=MIN_PER_CLASS)
            model["kinds"][kind] = info
            continue
        info["model"] = _fit(X, y, w)
        if len(mrecs) < MIN_MASTER_RECORDINGS:
            info["reason"] = i18n.msg("learning.not_verifiable_master", min=MIN_MASTER_RECORDINGS, n=len(mrecs))
            model["kinds"][kind] = info
            continue
        # Cross-check: leave out each master recording once, train on the rest, estimate its real clicks
        errs_raw, errs_est, rows = [], [], []
        for g in mrecs:
            te = (rec == g) & master
            tr = rec != g
            if np.sum(y[tr] == 1) < 5 or np.sum(y[tr] == 0) < 5 or not te.any():
                continue
            p = _predict(_fit(X[tr], y[tr], w[tr]), X[te])
            true, raw, est = float(y[te].sum()), float(d["raw_n"].get(int(g), int(te.sum()))), float(p.sum())
            if true < 3:                                    # relative errors of tiny counts say nothing
                continue
            errs_raw.append((raw - true) / true)
            errs_est.append((est - true) / true)
            rows.append({"recording": int(g), "true": int(true), "raw": int(raw), "est": round(est, 1)})
        if not errs_est:
            info["reason"] = i18n.msg("learning.not_verifiable_clicks")
            model["kinds"][kind] = info
            continue
        er, ee = float(np.mean(np.abs(errs_raw))), float(np.mean(np.abs(errs_est)))
        info.update(cv_err_raw=round(er, 3), cv_err_est=round(ee, 3), cv_bias_est=round(float(np.mean(errs_est)), 3),
                    cv=rows, check=i18n.msg("learning.check_one_record" if info["master_records"] < 2
                                            else "learning.check_per_recording"))
        if ee <= MAX_EST_ERROR and ee < er:
            info["usable"] = True
            info["reason"] = i18n.msg("learning.usable", kind=KIND_NOUN[kind], est=round(ee * 100), raw=round(er * 100))
        else:
            info["reason"] = i18n.msg("learning.too_inaccurate", est=round(ee * 100), raw=round(er * 100),
                                      max=round(MAX_EST_ERROR * 100))
        model["kinds"][kind] = info
    core = {k: {kk: v.get(kk) for kk in ("usable", "model")} for k, v in model["kinds"].items()}
    model["id"] = hashlib.sha1(json.dumps(core, sort_keys=True).encode()).hexdigest()[:12]
    return model


def estimate(events: list[dict], model: dict | None, music_ref_dbfs: float | None) -> tuple[np.ndarray, np.ndarray]:
    """(weight, p) per event. p = probability "real click" (NaN where the kind has no model); weight = what the event
    contributes to the estimated count: verdict "musik" 0, "knackser"/"unsicher" 1, otherwise p (usable kinds only),
    and 1 as in the raw count where there is no usable model."""
    n = len(events)
    p = np.full(n, np.nan)
    weight = np.ones(n)
    if not n:
        return weight, p
    if model:
        X = features(events, music_ref_dbfs)
        kinds = np.array([e.get("kind") for e in events], dtype=object)
        for kind, km in (model.get("kinds") or {}).items():
            sel = kinds == kind
            if not km.get("model") or not sel.any():
                continue
            p[sel] = _predict(km["model"], X[sel])
            if km.get("usable"):
                weight[sel] = p[sel]
    for i, e in enumerate(events):
        lab = e.get("label")
        if lab == "musik":
            weight[i] = 0.0
        elif lab in ("knackser", "unsicher"):
            weight[i] = 1.0
    return weight, p


def training_data(db) -> tuple[dict, dict]:
    """Examples from the database: all recordings with a master comparison (classification per event) or with
    verdicts given by hand. Returns (data per kind for train(), overview counts)."""
    rows = db.q("""SELECT a.recording_id, r.record_id, a.metrics,
                          (SELECT m.result FROM master_checks m WHERE m.recording_id = a.recording_id AND m.status = 'done') AS master,
                          (SELECT COUNT(*) FROM event_labels l WHERE l.recording_id = a.recording_id) AS n_labels
                     FROM analyses a JOIN recordings r ON r.id = a.recording_id
                    WHERE r.kind NOT IN ('calibration', 'alignment')""")
    acc = {k: {"X": [], "y": [], "w": [], "rec": [], "record": [], "manual": [], "master": [], "raw_n": {}} for k in KINDS}
    overview = {"recordings_master": 0, "recordings_manual": 0, "events_master": 0, "events_manual": 0}
    for r in rows:
        if not r["master"] and not r["n_labels"]:
            continue
        mlab = {}
        if r["master"]:
            try:
                res = json.loads(r["master"])
            except ValueError:
                res = {}
            for ev in ((res.get("clicks") or {}).get("events") or []):
                if len(ev) >= 2 and ev[1] in ("v", "m"):
                    mlab[round(float(ev[0]), 3)] = 1.0 if ev[1] == "v" else 0.0
            if mlab:
                overview["recordings_master"] += 1
        if r["n_labels"]:
            overview["recordings_manual"] += 1
        try:
            ref = (json.loads(r["metrics"] or "{}") or {}).get("music_ref_dbfs")
        except ValueError:
            ref = None
        events = db.events(r["recording_id"])
        X = features(events, ref)
        for i, e in enumerate(events):
            kind = e.get("kind")
            if kind not in acc or e.get("region") == "excluded":
                continue
            if mlab:
                acc[kind]["raw_n"][int(r["recording_id"])] = acc[kind]["raw_n"].get(int(r["recording_id"]), 0) + 1
            lab = e.get("label")
            if lab in ("knackser", "musik"):
                y, w, man = (1.0 if lab == "knackser" else 0.0), MANUAL_WEIGHT, True
                overview["events_manual"] += 1
            elif lab is None and round(float(e["t"]), 3) in mlab:
                y, w, man = mlab[round(float(e["t"]), 3)], 1.0, False
                overview["events_master"] += 1
            else:
                continue
            a = acc[kind]
            a["X"].append(X[i]); a["y"].append(y); a["w"].append(w)
            a["rec"].append(r["recording_id"]); a["record"].append(r["record_id"]); a["manual"].append(man)
            a["master"].append(bool(mlab))
    data = {k: {"X": np.array(v["X"]).reshape(-1, len(FEATURES)), "y": np.array(v["y"]), "w": np.array(v["w"]),
                "rec": np.array(v["rec"]), "record": np.array(v["record"]), "manual": np.array(v["manual"], bool),
                "master": np.array(v["master"], bool), "raw_n": v["raw_n"]}
            for k, v in acc.items()}
    return data, overview


def summary(model: dict | None) -> dict:
    """Model status without the weights, for the web interface and the API."""
    if not model:
        return {"trained": False}
    return {"trained": True, "id": model.get("id"), "trained_at": model.get("trained_at"), "data": model.get("data"),
            "kinds": {k: {kk: vv for kk, vv in v.items() if kk != "model"} for k, v in (model.get("kinds") or {}).items()}}

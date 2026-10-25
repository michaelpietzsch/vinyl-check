"""Learning from master comparisons and verdicts: estimate how many of the reported events are real clicks.

Examples: every master comparison classifies each event of an analysis as "v" (only on the vinyl, a real click)
or "m" (also in the master, i.e. music: a false alarm). On top of that come the verdicts given by hand in the
audit view ("defect" = real, "music" = false alarm); they count MANUAL_WEIGHT times and take precedence over the
master's classification ("unsure" is left out).

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

**Shared scaling and prior (Vinyl-Mind).** A fitted model standardizes the features with its own mean and spread, so
its weights only mean something together with that normalisation. To compare or combine weights across turntable
profiles and stations, they are re-expressed in a fixed common scaling (`FIXED_SCALING`, tied to `FEATURE_VERSION`;
for version 1 the values of the hub's specification, `spec/v1/features.json`): `to_fixed` and `predict_fixed`.
`train(..., prior=...)` takes weights in that scaling, fits in it and lets the ridge penalty pull every coefficient,
the intercept included, towards them instead of towards zero (docs/vinyl-mind.md, docs/architecture.md).

**Feature versions.** Version "1" (`FEATURES`) is relative to the music reference of each recording. Version "2"
(`FEATURES_V2`, experimental, opt-in with the configuration key `feature_version`) additionally takes the turntable
profile as the zero point (`zero_point`): density net of the chain's click floor, level relative to the profile's
reference tone, and how close an event comes to the chain's own noise. Every function that computes or exchanges
features takes the version; models, priors and contributions of different versions are never combined.
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

# Version of the feature definitions above (names, order, how they are computed). Weights computed on different
# versions must never be combined, so any change to `features()` or FEATURES needs a new version. Version "1" is
# relative to the music reference of each recording, not yet to the turntable profile. It is the default and stays
# active until version "2" (below) has been evaluated on real stations.
FEATURE_VERSION = "1"
# Fixed common scaling of feature version "1": (centre, scale) per feature. These are the values of the hub's
# specification (Vinyl-Mind, spec/v1/features.json, "stable" there): a contribution does not carry the scaling and is
# only comparable with other stations' if every station uses exactly the same numbers, so they are copied here
# verbatim and a test compares them with the specification. They are a convention, not statistics of any data set,
# and never change within the feature version; another scaling is another FEATURE_VERSION.
FIXED_SCALING = {
    "log_strength": (2.7, 0.7),     # log1p of the detector peak; ~2.4 (strength 10) … 6.2 (strength 500)
    "amp_rel": (-15.0, 10.0),       # dB relative to the music reference; real clicks −40 … −15, music −5 … +5
    "log_dur": (0.7, 0.5),          # log1p of the duration in ms; ~0.1 … 1.5
    "gc": (0.5, 0.3),               # second-opinion score 0 … 1 (0.5 when missing)
    "gc_missing": (1.0, 0.5),       # 0/1; centred at "no second opinion", the common case (see FIXED_SCALING_V2)
    "music": (0.5, 0.5),            # 0/1
    "both_ch": (0.5, 0.5),          # 0/1
    "log_density": (0.5, 0.6),      # log1p of the neighbours within ±DENSITY_S; 0 … ~3
    "amp_rel2": (2.5, 2.0),         # amp_rel² / 100; 0 … ~16
    "strength_x_amp": (-4.0, 3.0),  # log_strength × amp_rel / 10; ~−25 … +3
}

# ─── Feature version "2": the turntable profile as the zero point (stage 0, experimental) ───────────────────────
# The same model and kinds; the chain-dependent features are re-expressed against what the profile measured, the
# chain-independent ones (second opinion, region, channels, duration) stay as in version 1. What a profile stores and
# can serve as the zero point (db.py, profiles; jobs.Worker.profile_trim):
#   chain_db              chain noise with the stylus raised (mean power, dBFS), from the calibration recording
#   ref_dbfs              level of the test record's 1 kHz reference tone (sine amplitude, dBFS), from the accepted run
#   floor_clicks/ticks    detector hits per minute in the test record's silent groove, measured with the profile's own
#                         detector setting: what stylus, cartridge and electronics produce before any record does
# The profile stores how many hits the chain produces, not how strong they are, so the click floor (a rate) can only
# normalise the density (a rate); the chain noise (a level) is what the event levels are related to.
#
# - log_density_net replaces log_density: ln(1 + max(0, n − λ)) with λ = the floor's hits per second × 2·DENSITY_S,
#   the number of neighbours the chain alone puts into the window. Subtracted, not divided, as grading does with the
#   quiet-passage counts (`…_net_per_min`): a crackling record shows the same excess over a noisy chain as over a
#   quiet one, while the chain's own hits no longer look like crackle. The floor is measured in the silent groove, so
#   in loud music, which masks part of the chain's hits, λ over-corrects somewhat; the density there is dominated by
#   the music's own transients anyway.
# - amp_ref (new): event level relative to the profile's reference tone, in dB. amp_rel stays: it says how much the
#   music masks the event. amp_ref adds the click's physical size, independent of the interface gain and of how loud
#   the record was cut. Not collinear with amp_rel, because the music reference varies from record to record.
# - chain_margin (new): how far the event rises above the chain's own noise, (amp − chain_db) / 10, clipped to
#   0 … CHAIN_MARGIN_MAX_DB / 10. The detector's strength is the peak over the local residual level, and that level
#   never falls below the chain's noise, so strength already is "relative to the chain's noise" wherever that noise
#   dominates; re-expressing it would change nothing (and any per-profile offset is absorbed by the intercept). What
#   strength cannot tell is whether an event sits near the level the chain itself produces: a hit just over the
#   detector threshold (8σ ≈ 18 dB) above the chain's noise is the chain's kind of hit on every chain, the same low
#   value everywhere. From 40 dB on the chain plays no role, so the value saturates; within one profile it then only
#   varies for events close to the chain's noise and does not compete with the level features.
FEATURES_V2 = ("log_strength", "amp_rel", "log_dur", "gc", "gc_missing", "music", "both_ch", "log_density_net",
               "amp_rel2", "strength_x_amp", "amp_ref", "chain_margin")
CHAIN_MARGIN_MAX_DB = 40.0
# Fallback where the profile lacks a value (recording without a profile, profile not measured or not accepted). The
# fallback makes the feature carry no profile information instead of a wrong one, and the recording is flagged
# (`zero_point_missing`, training_data's "fallback") so that it never goes into a contribution:
#   ref_dbfs  → music reference + REF_ABOVE_MUSIC_DB (music lies 0–10 dB below the reference tone, see the level check
#               in analysis.py), so amp_ref = amp_rel − 5 dB adds nothing beyond amp_rel;
#   floor     → λ = 0, the density of version 1;
#   chain_db  → chain_margin saturated, as on a chain whose noise plays no role.
REF_ABOVE_MUSIC_DB = 5.0
# Fixed common scaling of version 2. A weight whose feature did not vary in a profile's data is sent as uninformative
# and left out of the hub's average, and that is only exact if the feature sat at its centre: otherwise the profile's
# intercept contains the effect of the constant value, and averaging it with intercepts of profiles where the feature
# varied biases the global model (in the synthetic evaluation, centre 3 for chain_margin made the averaged click model
# several times worse than version 1). So a feature that is constant on many profiles has its centre at that value:
# chain_margin at its saturation (the chain plays no role, as on most chains), and gc_missing at 1 as in version 1
# (most stations run without the optional second-opinion network, and crackle never gets a score). The other centres
# are version 1's.
FIXED_SCALING_V2 = {
    **{k: FIXED_SCALING[k] for k in FEATURES_V2 if k in FIXED_SCALING},
    "log_density_net": FIXED_SCALING["log_density"],  # like log_density; the chain's own neighbours removed
    "amp_ref": (-20.0, 10.0),        # dB relative to the reference tone; real clicks −50 … −15, music −15 … +5
    "chain_margin": (CHAIN_MARGIN_MAX_DB / 10, 1.0),  # 0 … 4; detected events ≳ 1.8, most at the saturation 4
}
FEATURE_VERSIONS = ("1", "2")
FEATURES_BY_VERSION = {"1": FEATURES, "2": FEATURES_V2}
FIXED_SCALING_BY_VERSION = {"1": FIXED_SCALING, "2": FIXED_SCALING_V2}
ZERO_POINT_KEYS = ("chain_db", "ref_dbfs", "floor_per_min")

# Ridge penalty towards a prior (a global model or an imported package) instead of L2 towards zero. With a prior the
# model is fitted in the fixed common scaling and every coefficient is pulled towards the prior's, the intercept
# included: in the hub's simulation (Vinyl-Mind, docs/aggregation.md) a free intercept adapted to the few records a
# new profile had seen and gave most of the benefit away, and a strength of about 100 worked best for profiles with
# few judgements. One example with p(1 − p) ≈ 0.2 adds about 0.2 to the curvature of a weight on a feature of unit
# spread, so 100 lets the prior hold like roughly five hundred master-classified events (a hundred judgements by
# hand): a new profile stays close to it, one with thousands of events follows its own data. Whether the prior is
# used at all is decided by the station's own check on its master recordings (vinylmind.check_model), not here.
PRIOR_L2 = 100.0


def feature_names(version: str = FEATURE_VERSION) -> tuple[str, ...]:
    """The feature names of a feature version, in order; ValueError for an unknown version."""
    try:
        return FEATURES_BY_VERSION[str(version)]
    except KeyError:
        raise ValueError(f"unknown feature version {version!r}; known: {', '.join(FEATURE_VERSIONS)}") from None


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) else None


def zero_point(profile: dict | None) -> dict:
    """The zero point of feature version 2 from a turntable profile (a row of the profiles table, or None for a
    recording without a profile): {"chain_db", "ref_dbfs", "floor_per_min"}, None where the profile has no value.

    As in jobs.Worker.profile_trim, the reference level and the click floor only count for an accepted profile (the
    alignment run taken over as its reference); the chain noise is the profile's own calibration and counts as soon as
    it was measured. The floor is clicks plus crackle per minute: the density counts events of every kind."""
    p = profile or {}
    accepted = bool(p.get("accepted_at"))
    clicks = _num(p.get("floor_clicks_per_min")) if accepted else None
    floor = None if clicks is None else clicks + (_num(p.get("floor_ticks_per_min")) or 0.0)
    return {"chain_db": _num(p.get("chain_db")), "ref_dbfs": _num(p.get("ref_dbfs")) if accepted else None,
            "floor_per_min": floor}


def zero_point_missing(zp: dict | None) -> list[str]:
    """The values of a zero point that are missing and replaced by the fallback (see REF_ABOVE_MUSIC_DB)."""
    return [k for k in ZERO_POINT_KEYS if (zp or {}).get(k) is None]


def features(events: list[dict], music_ref_dbfs: float | None, version: str = FEATURE_VERSION,
             profile: dict | None = None) -> np.ndarray:
    """Feature matrix (n × number of features of `version`) for the events of one recording. The density needs all
    events of the recording, not just a selection. Levels are taken relative to the music reference (−15 dBFS if
    unknown). `profile` is the recording's zero point (`zero_point`), used by version 2 only; missing values fall
    back as described at REF_ABOVE_MUSIC_DB."""
    names = feature_names(version)
    n = len(events)
    if not n:
        return np.zeros((0, len(names)))
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
    if names is FEATURES:
        return np.column_stack([ls, amp_rel, np.log1p(dur), gc, gc_missing, music, both, np.log1p(dens),
                                amp_rel ** 2 / 100.0, ls * amp_rel / 10.0])
    # version 2: see FEATURES_V2 for why each feature is defined this way, REF_ABOVE_MUSIC_DB for the fallbacks
    zp = profile or {}
    floor = zp.get("floor_per_min")
    lam = 0.0 if floor is None else float(floor) / 60.0 * 2 * DENSITY_S
    ref = zp.get("ref_dbfs")
    if ref is None:
        ref = (float(music_ref_dbfs) if music_ref_dbfs is not None else -15.0) + REF_ABOVE_MUSIC_DB
    chain = zp.get("chain_db")
    top = CHAIN_MARGIN_MAX_DB / 10.0
    margin = np.full(n, top) if chain is None else np.clip((amp - float(chain)) / 10.0, 0.0, top)
    return np.column_stack([ls, amp_rel, np.log1p(dur), gc, gc_missing, music, both,
                            np.log1p(np.maximum(dens - lam, 0.0)), amp_rel ** 2 / 100.0, ls * amp_rel / 10.0,
                            amp - float(ref), margin])


def _scaling(version: str = FEATURE_VERSION) -> tuple[np.ndarray, np.ndarray]:
    """Centres and scales of the fixed scaling of `version`, in the order of its features."""
    names, sc = feature_names(version), FIXED_SCALING_BY_VERSION[str(version)]
    return np.array([sc[f][0] for f in names]), np.array([sc[f][1] for f in names])


def _check_weights(fixed: dict, version: str) -> np.ndarray:
    """Weights in the fixed scaling as an array; ValueError if they belong to another feature version (another
    number of features): weights of different versions are never combined, not even by accident."""
    v = np.asarray(fixed["weights"], float)
    if v.shape != (len(feature_names(version)),):
        raise ValueError(f"{v.size} weights do not fit feature version {version} "
                         f"({len(feature_names(version))} features)")
    return v


def _fit(X: np.ndarray, y: np.ndarray, w: np.ndarray, l2: float = L2, iters: int = 60, prior: dict | None = None,
         prior_l2: float = PRIOR_L2, version: str = FEATURE_VERSION) -> dict:
    """Weighted logistic regression with ridge penalty (Newton's method).

    Without a prior: on the data's own standardisation; the penalty `l2` pulls the weights towards zero and leaves the
    intercept free. With `prior` (weights in the fixed scaling of `version`): on the fixed scaling itself, and the
    penalty `prior_l2` pulls every coefficient, the intercept included, towards the prior's (see PRIOR_L2). Either way
    the result is {"mu", "sd", "b"} for _predict; with a prior, mu and sd are the fixed centres and scales.

    The fit always starts at zero: started at a prior that disagrees with the data, every probability is saturated at
    the wrong end and Newton's first step overshoots."""
    if prior is None:
        mu = X.mean(axis=0)
        sd = X.std(axis=0)
        sd[sd < 1e-9] = 1.0
    else:
        mu, sd = _scaling(version)
    Z = np.column_stack([np.ones(len(X)), (X - mu) / sd])
    if prior is None:
        b0 = np.zeros(Z.shape[1])
        R = np.eye(Z.shape[1]) * l2
        R[0, 0] = 0.0                               # no penalty on the intercept
    else:
        b0 = np.concatenate([[float(prior["intercept"])], _check_weights(prior, version)])
        R = np.eye(Z.shape[1]) * prior_l2
    b = np.zeros(Z.shape[1])
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(Z @ b, -30, 30)))
        g = Z.T @ (w * (y - p)) - R @ (b - b0)
        H = (Z * (w * p * (1 - p) + 1e-9)[:, None]).T @ Z + R
        step = np.linalg.solve(H, g)
        b += step
        if np.max(np.abs(step)) < 1e-7:
            break
    return {"mu": np.asarray(mu, float).round(6).tolist(), "sd": np.asarray(sd, float).round(6).tolist(),
            "b": b.round(6).tolist()}


def to_fixed(m: dict, version: str = FEATURE_VERSION) -> dict:
    """A fitted model ({"mu", "sd", "b"}) in the fixed common scaling of `version`: {"intercept", "weights"}, the same
    function of the features. Weights of features without spread in the training data are 0 (they carried no
    information)."""
    c, s = _scaling(version)
    mu, sd, b = np.array(m["mu"]), np.array(m["sd"]), np.array(m["b"])
    if mu.shape != c.shape:
        raise ValueError(f"a model with {mu.size} features is not of feature version {version}")
    v = b[1:] * s / sd
    a = b[0] - float(np.sum(b[1:] * (mu - c) / sd))
    return {"intercept": round(float(a), 6), "weights": [round(float(x), 6) for x in v]}


def to_coef(m: dict, version: str = FEATURE_VERSION) -> list[float]:
    """A fitted model as the coefficient list of the hub's specification: [intercept, weight per feature] in the fixed
    scaling of `version` (spec/README.md: coef[i + 1] = b[i + 1] · scale[i] / sd[i], coef[0] = b[0] + Σ b[i + 1] /
    sd[i] · (centre[i] − mu[i]))."""
    f = to_fixed(m, version)
    return [f["intercept"], *f["weights"]]


def mask_uninformative(coef: list, X: np.ndarray, prior: dict | None = None,
                       version: str = FEATURE_VERSION) -> tuple[list[float], list[bool]]:
    """Coefficients as a contribution reports them (the hub's spec/README.md, "Features that never vary"), and per
    feature whether it was informative.

    A feature that took one single value x0 in all rows of `X` (the examples the kind was fitted on; on a station
    without a second opinion `gc` is always 0.5 and `gc_missing` always 1) says nothing about its weight: in the fit it
    only trades off against the intercept. Its weight is replaced by the prior's (`prior` in the fixed scaling, the
    model the profile trained towards; 0 without one), and the intercept takes the difference at x0, so that every
    prediction on these examples stays exactly the same."""
    c, s = _scaling(version)
    out = [float(x) for x in coef]
    pw = _check_weights(prior, version) if prior else np.zeros(len(c))
    X = np.asarray(X, float)
    informative = []
    for i in range(len(c)):
        col = X[:, i] if X.ndim == 2 and len(X) else np.zeros(0)
        if len(col) and np.all(col == col[0]):
            out[0] += (out[i + 1] - float(pw[i])) * (float(col[0]) - float(c[i])) / float(s[i])
            out[i + 1] = float(pw[i])
            informative.append(False)
        else:
            informative.append(True)
    return [round(x, 6) for x in out], informative


def from_coef(coef: list, version: str = FEATURE_VERSION) -> dict:
    """The coefficient list of the specification as {"intercept", "weights"} (a prior for train()); ValueError if its
    length does not fit `version`."""
    fixed = {"intercept": float(coef[0]), "weights": [float(x) for x in coef[1:]]}
    _check_weights(fixed, version)
    return fixed


def predict_fixed(fixed: dict, X: np.ndarray, version: str = FEATURE_VERSION) -> np.ndarray:
    """Probability "real click" from weights in the fixed scaling of `version` (a contribution, a global model, a
    prior)."""
    c, s = _scaling(version)
    z = float(fixed["intercept"]) + ((X - c) / s) @ _check_weights(fixed, version)
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


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


def train(data: dict, now: float | None = None, prior: dict | None = None, version: str = FEATURE_VERSION) -> dict:
    """Train one model per kind and cross-check its estimate.

    data[kind] = {"X", "y" (1 real, 0 music), "w" (weight), "rec" (recording per row), "record" (record per row),
    "manual" (bool), "master" (row comes from a fully classified recording), "raw_n" {recording: events}}, with X
    computed for feature version `version` (training_data(..., version=...)).
    prior: optional {kind: {"intercept", "weights"}} in the fixed scaling of the same feature version; for the kinds
    it covers, the ridge penalty pulls towards it instead of towards zero, in the fit and in the cross-check.
    Returns the model with a status per kind (JSON-serializable); with a prior, `prior_kinds` lists the kinds it
    applied to. ValueError if the data or the prior belong to another feature version.
    """
    names = feature_names(version)
    model = {"version": VERSION, "trained_at": now or time.time(), "features": list(names),
             "feature_version": str(version), "kinds": {}}
    for kind, d in data.items():
        if kind in KINDS and d is not None and np.asarray(d["X"]).ndim == 2 and len(d["y"]) \
                and np.asarray(d["X"]).shape[1] != len(names):
            raise ValueError(f"training data of {kind} has {np.asarray(d['X']).shape[1]} features, "
                             f"feature version {version} has {len(names)}")
    for kind, kp in (prior or {}).items():
        if kind in KINDS:
            _check_weights(kp, version)
    if prior:
        model["prior_kinds"] = sorted(k for k in prior if k in KINDS)
    for kind in KINDS:
        kp = (prior or {}).get(kind)
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
        info["model"] = _fit(X, y, w, prior=kp, version=version)
        if len(mrecs) < MIN_MASTER_RECORDINGS:
            info["reason"] = i18n.msg("learning.not_verifiable_master", min=MIN_MASTER_RECORDINGS, n=len(mrecs))
            model["kinds"][kind] = info
            continue
        # Cross-check: leave out each master recording once, train on the rest, estimate its real clicks
        errs_raw, errs_est, rows, oof_p, oof_y = [], [], [], [], []
        for g in mrecs:
            te = (rec == g) & master
            tr = rec != g
            if np.sum(y[tr] == 1) < 5 or np.sum(y[tr] == 0) < 5 or not te.any():
                continue
            p = _predict(_fit(X[tr], y[tr], w[tr], prior=kp, version=version), X[te])
            oof_p.append(p); oof_y.append(y[te])
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
        # separation of the held-out events (all folds together): a quality figure for Vinyl-Mind contributions
        auc = _auc(np.concatenate(oof_p), np.concatenate(oof_y))
        info.update(cv_err_raw=round(er, 3), cv_err_est=round(ee, 3), cv_bias_est=round(float(np.mean(errs_est)), 3),
                    cv_auc=None if auc is None else round(auc, 3), cv=rows,
                    check=i18n.msg("learning.check_one_record" if info["master_records"] < 2
                                   else "learning.check_per_recording"))
        if ee <= MAX_EST_ERROR and ee < er:
            info["usable"] = True
            info["reason"] = i18n.msg("learning.usable", kind=KIND_NOUN[kind], est=round(ee * 100), raw=round(er * 100))
        else:
            info["reason"] = i18n.msg("learning.too_inaccurate", est=round(ee * 100), raw=round(er * 100),
                                      max=round(MAX_EST_ERROR * 100))
        model["kinds"][kind] = info
    core = {k: {kk: v.get(kk) for kk in ("usable", "model")} for k, v in model["kinds"].items()}
    if str(version) != "1":
        # a model of another feature version is another model even with equal numbers; version 1 keeps the ids it
        # had before versions existed, so updating does not recount every analysis
        core["feature_version"] = str(version)
    model["id"] = hashlib.sha1(json.dumps(core, sort_keys=True).encode()).hexdigest()[:12]
    return model


def estimate(events: list[dict], model: dict | None, music_ref_dbfs: float | None,
             profile: dict | None = None) -> tuple[np.ndarray, np.ndarray]:
    """(weight, p) per event. p = probability "real click" (NaN where the kind has no model); weight = what the event
    contributes to the estimated count: verdict "music" 0, "defect"/"unsure" 1, otherwise p (usable kinds only),
    and 1 as in the raw count where there is no usable model. The features are those of the model's feature version
    (version 1 for models stored before versions existed); `profile` is the recording's zero point for version 2."""
    n = len(events)
    p = np.full(n, np.nan)
    weight = np.ones(n)
    if not n:
        return weight, p
    if model:
        X = features(events, music_ref_dbfs, model_version(model), profile)
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
        if lab == "music":
            weight[i] = 0.0
        elif lab in ("defect", "unsure"):
            weight[i] = 1.0
    return weight, p


def model_version(model: dict | None) -> str:
    """The feature version a stored model was trained on; models from before versions existed are version 1."""
    return str((model or {}).get("feature_version") or "1")


def training_data(db, profile_id: int | None = None, version: str = FEATURE_VERSION) -> tuple[dict, dict]:
    """Examples from the database: all recordings with a master comparison (classification per event) or with
    verdicts given by hand; with `profile_id` only the recordings made with that turntable profile. Features of
    feature version `version`; for version 2 each recording's own profile is its zero point (`zero_point`), and rows
    whose profile lacked a value carry "fallback" = True (see `drop_fallback`). Returns (data per kind for train(),
    overview counts)."""
    names = feature_names(version)
    profiles = {}
    if names is not FEATURES:
        profiles = {int(p["id"]): p for p in db.q("SELECT * FROM profiles")}
    where, args = ("AND r.profile_id = ?", (profile_id,)) if profile_id is not None else ("", ())
    rows = db.q(f"""SELECT a.recording_id, r.record_id, r.profile_id, a.metrics,
                          (SELECT m.result FROM master_checks m WHERE m.recording_id = a.recording_id AND m.status = 'done') AS master,
                          (SELECT COUNT(*) FROM event_labels l WHERE l.recording_id = a.recording_id) AS n_labels
                     FROM analyses a JOIN recordings r ON r.id = a.recording_id
                    WHERE r.kind NOT IN ('calibration', 'alignment') {where}""", args)
    acc = {k: {"X": [], "y": [], "w": [], "rec": [], "record": [], "manual": [], "master": [], "fallback": [],
               "raw_n": {}} for k in KINDS}
    overview = {"recordings_master": 0, "recordings_manual": 0, "events_master": 0, "events_manual": 0}
    if names is not FEATURES:
        overview["recordings_fallback"] = 0
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
        zp, fallback = None, False
        if names is not FEATURES:
            zp = zero_point(profiles.get(int(r["profile_id"])) if r["profile_id"] is not None else None)
            fallback = bool(zero_point_missing(zp))
            overview["recordings_fallback"] += fallback
        X = features(events, ref, version, zp)
        for i, e in enumerate(events):
            kind = e.get("kind")
            if kind not in acc or e.get("region") == "excluded":
                continue
            if mlab:
                acc[kind]["raw_n"][int(r["recording_id"])] = acc[kind]["raw_n"].get(int(r["recording_id"]), 0) + 1
            lab = e.get("label")
            if lab in ("defect", "music"):
                y, w, man = (1.0 if lab == "defect" else 0.0), MANUAL_WEIGHT, True
                overview["events_manual"] += 1
            elif lab is None and round(float(e["t"]), 3) in mlab:
                y, w, man = mlab[round(float(e["t"]), 3)], 1.0, False
                overview["events_master"] += 1
            else:
                continue
            a = acc[kind]
            a["X"].append(X[i]); a["y"].append(y); a["w"].append(w)
            a["rec"].append(r["recording_id"]); a["record"].append(r["record_id"]); a["manual"].append(man)
            a["master"].append(bool(mlab)); a["fallback"].append(fallback)
    data = {k: {"X": np.array(v["X"]).reshape(-1, len(names)), "y": np.array(v["y"]), "w": np.array(v["w"]),
                "rec": np.array(v["rec"]), "record": np.array(v["record"]), "manual": np.array(v["manual"], bool),
                "master": np.array(v["master"], bool), "fallback": np.array(v["fallback"], bool), "raw_n": v["raw_n"]}
            for k, v in acc.items()}
    return data, overview


def drop_fallback(data: dict) -> dict:
    """The training data without the rows whose zero point fell back (training_data's "fallback"): what may go into
    a contribution of feature version 2, where every weight must mean the same on every station."""
    out = {}
    for kind, d in data.items():
        fb = np.asarray(d.get("fallback", np.zeros(len(d["y"]), bool)), bool)
        gone = {int(g) for g in np.asarray(d["rec"])[fb].tolist()}
        out[kind] = {k: ({g: c for g, c in v.items() if int(g) not in gone} if k == "raw_n" else np.asarray(v)[~fb])
                     for k, v in d.items()}
    return out


def summary(model: dict | None) -> dict:
    """Model status without the weights, for the web interface and the API."""
    if not model:
        return {"trained": False}
    return {"trained": True, "id": model.get("id"), "trained_at": model.get("trained_at"), "data": model.get("data"),
            "feature_version": model_version(model),
            "kinds": {k: {kk: vv for kk, vv in v.items() if kk != "model"} for k, v in (model.get("kinds") or {}).items()}}

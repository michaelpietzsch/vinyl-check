"""Compare the feature versions of the learning model: on synthetic chains, or on a copy of a station database.

    python tools/eval_features.py synthetic [--recordings 12] [--seconds 60] [--chains budget,mid,high] [--seed 1]
                                            [--repeat 3] [--details]
    python tools/eval_features.py real path/to/a-copy-of/vinyl-check.sqlite

The default synthetic run takes about two and a half minutes (three seeds, 36 analysed sides each).

For every feature version (learning.FEATURE_VERSIONS) and event kind it measures:

- **local**: the leave-one-recording-out estimate error of each chain (synthetic) or turntable profile (real) on its
  own recordings, exactly learning.train's cross-check; "raw" is the same for the raw count;
- **pooled**: the same for one model over all chains together, as a station with several turntables trains it;
- **transfer A→B**: the weights trained on all of A's recordings, in the fixed common scaling of the version, applied
  unchanged to B's recordings;
- **federated →C**: the mean of the other chains' weights applied to C, what a Vinyl-Mind hub would hand back (each
  weight averaged only over the chains where its feature varied, as the protocol tells the hub);
- **similarity**: the cosine of two chains' weight vectors (features informative in both), their distance relative
  to their mean length, and the gap between their intercepts.

An error is the mean relative deviation of the estimated from the true number of real events per recording, over the
recordings with at least 3 real events (as in learning.train). Lower is better; for the cosine, higher is better.

**synthetic**: the same synthetic records played through simulated chains (a budget USB turntable with a high noise
floor and many hits of its own, a mid-range deck behind an interface, an aligned high-end chain). Each chain's profile
is measured the way a station measures it (chain noise with the stylus up, the reference level, the click floor in the
silent groove of a test record with alignment.click_floor), each side is analysed by vinyl-check's own analysis with
that profile, and every event is judged by the synthetic ground truth the way a master comparison would judge it.

**real**: the same metrics on a station database. It is only read (SQLite immutable for a checkpointed copy, mode=ro
when a -wal file lies next to it, always query_only); run it on a copy anyway. Feature version 2 is compared on the
profiles with a complete zero point only (an accepted alignment run and a chain noise measurement), and version 1 on
exactly the same recordings, so that the two are compared on equal terms.

Only the standard library, numpy and vinyl-check itself.
"""
from __future__ import annotations

import argparse
import itertools
import sqlite3
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

if __package__ in (None, ""):                         # run as a script from a checkout: use that checkout's package
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vinylcheck import learning, legacy  # noqa: E402

KINDS = learning.KINDS
VERSIONS = learning.FEATURE_VERSIONS
MIN_TRUE = 3                                          # recordings with fewer real events say nothing (learning.train)
SR = 48000
NOMINAL_REF_DBFS = -20.0                              # reference tone at the interface for a gain of 0 dB


# ─── Metrics, shared by both parts ───────────────────────────────────────────

def _rows(d: dict | None) -> int:
    return 0 if d is None else len(d["y"])


def direct_error(fixed: dict, d: dict | None, version: str) -> float | None:
    """Estimate error of weights applied without any training (learning.predict_fixed) on the recordings of `d`."""
    if not _rows(d):
        return None
    errs = []
    for g in sorted(set(np.asarray(d["rec"]).tolist())):
        te = np.asarray(d["rec"]) == g
        true = float(np.sum(d["y"][te]))
        if true < MIN_TRUE:
            continue
        errs.append(abs(float(learning.predict_fixed(fixed, d["X"][te], version).sum()) - true) / true)
    return float(np.mean(errs)) if errs else None


def _informative(d: dict) -> np.ndarray:
    return np.asarray(d["X"]).std(axis=0) >= 1e-9


def federated_mean(fits: list[tuple[dict, np.ndarray]]) -> dict:
    """Plain federated averaging: the mean intercept, and each weight averaged over the fits where its feature varied
    (a weight without spread carried no information, docs/vinyl-mind-protocol.md); 0 where it varied nowhere."""
    a = float(np.mean([f["intercept"] for f, _ in fits]))
    W = np.array([f["weights"] for f, _ in fits], float)
    M = np.array([inf for _, inf in fits], bool)
    n = M.sum(axis=0)
    w = np.where(n > 0, (W * M).sum(axis=0) / np.maximum(n, 1), 0.0)
    return {"intercept": a, "weights": w.tolist()}


def similarity(fa: tuple[dict, np.ndarray], fb: tuple[dict, np.ndarray]) -> dict:
    both = fa[1] & fb[1]
    va, vb = np.asarray(fa[0]["weights"])[both], np.asarray(fb[0]["weights"])[both]
    na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
    cos = float(va @ vb / (na * nb)) if na > 0 and nb > 0 else None
    dist = float(np.linalg.norm(va - vb) / ((na + nb) / 2)) if na + nb > 0 else None
    return {"cos": cos, "dist": dist, "intercept_gap": abs(float(fa[0]["intercept"]) - float(fb[0]["intercept"]))}


def _merge(parts: list[dict]) -> dict:
    keys = ("X", "y", "w", "rec", "record", "manual", "master")
    out = {k: np.concatenate([np.asarray(p[k]) for p in parts]) for k in keys}
    out["X"] = out["X"].reshape(-1, parts[0]["X"].shape[1])
    out["raw_n"] = {g: c for p in parts for g, c in p["raw_n"].items()}
    return out


def evaluate(groups: dict[str, dict[str, dict]], version: str) -> dict:
    """All metrics of one feature version. groups[name][kind] = training data as learning.training_data builds it
    (recording ids unique across groups). Returns {kind: {...}} with None wherever there was too little data."""
    models = {g: learning.train(data, now=0.0, version=version) for g, data in groups.items()}
    out = {}
    for kind in KINDS:
        res: dict = {"local": {}, "transfer": {}, "federated": {}, "similarity": {}, "pooled": None}
        fits = {}
        for g, m in models.items():
            info = m["kinds"][kind]
            res["local"][g] = {"est": info.get("cv_err_est"), "raw": info.get("cv_err_raw"), "auc": info.get("cv_auc"),
                               "rows": _rows(groups[g].get(kind))}
            if info.get("model"):
                fits[g] = (learning.to_fixed(info["model"], version), _informative(groups[g][kind]))
        parts = [groups[g][kind] for g in groups if _rows(groups[g].get(kind))]
        if parts:
            pm = learning.train({kind: _merge(parts)}, now=0.0, version=version)["kinds"][kind]
            res["pooled"] = {"est": pm.get("cv_err_est"), "raw": pm.get("cv_err_raw"), "auc": pm.get("cv_auc")}
        for a, b in itertools.permutations(groups, 2):
            if a in fits:
                res["transfer"][(a, b)] = direct_error(fits[a][0], groups[b].get(kind), version)
        for c in groups:
            others = [fits[g] for g in groups if g != c and g in fits]
            if others:
                res["federated"][c] = direct_error(federated_mean(others), groups[c].get(kind), version)
        for a, b in itertools.combinations(groups, 2):
            if a in fits and b in fits:
                res["similarity"][(a, b)] = similarity(fits[a], fits[b])
        out[kind] = res
    return out


def _mean(xs) -> float | None:
    xs = [x for x in xs if x is not None]
    return float(np.mean(xs)) if xs else None


def headline(res: dict) -> dict:
    """The means that the summary table and the recommendation use, for one version and kind."""
    sims = list(res["similarity"].values())
    return {"local": _mean(v["est"] for v in res["local"].values()),
            "raw": _mean(v["raw"] for v in res["local"].values()),
            "pooled": (res["pooled"] or {}).get("est"),
            "transfer": _mean(res["transfer"].values()),
            "federated": _mean(res["federated"].values()),
            "cos": _mean(s["cos"] for s in sims),
            "intercept_gap": _mean(s["intercept_gap"] for s in sims)}


def heads(results: dict[str, dict]) -> dict[str, dict]:
    """headline() for every version and kind: {version: {kind: {...}}}."""
    return {v: {kind: headline(results[v][kind]) for kind in KINDS} for v in VERSIONS}


def mean_heads(runs: list[dict[str, dict]]) -> dict[str, dict]:
    """The mean of several runs' heads (different seeds), key by key over the runs that have a value."""
    return {v: {kind: {key: _mean(r[v][kind][key] for r in runs) for key in runs[0][v][kind]} for kind in KINDS}
            for v in VERSIONS}


# Thresholds of the recommendation: an improvement must be clear on both scales, a regression only needs to be real
CLEAR_ABS, CLEAR_REL, WORSE_ABS = 0.02, 0.15, 0.02


def recommend(results: dict[str, dict]) -> tuple[str, list[str]]:
    """recommend_heads() for the results of one run."""
    return recommend_heads(heads(results))


def recommend_heads(h: dict[str, dict]) -> tuple[str, list[str]]:
    """("v2", "v1" or "unclear", reasons) from the comparison of version 2 with version 1, per kind: version 2 must
    estimate clearly better across chains (federated, direct transfer, or one model over several chains) without
    estimating clearly worse on any chain's own recordings."""
    better, worse, reasons = [], [], []
    largest = None                                      # the largest improvement across chains, for context
    for kind in KINDS:
        h1, h2 = h["1"][kind], h["2"][kind]
        for key in ("federated", "transfer", "pooled", "local"):
            a, b = h1[key], h2[key]
            if a is None or b is None:
                continue
            if key != "local" and a > 0 and (largest is None or (a - b) / a > largest[0]):
                largest = ((a - b) / a, f"{kind} {key} {a:.3f} → {b:.3f}")
            if key != "local" and a - b >= CLEAR_ABS and (a - b) >= CLEAR_REL * a:
                better.append((kind, key))
                reasons.append(f"{kind}: {key} error {a:.3f} → {b:.3f}, better with version 2")
            elif b - a >= WORSE_ABS:
                worse.append((kind, key))
                reasons.append(f"{kind}: {key} error {a:.3f} → {b:.3f}, worse with version 2")
    if not reasons:
        reasons.append(f"no kind is better with version 2 by at least {CLEAR_ABS:.2f} and {CLEAR_REL:.0%} across "
                       f"chains, or worse by {WORSE_ABS:.2f}; or there was too little data")
        if largest is not None and largest[0] > 0:
            reasons.append(f"largest improvement across chains: {largest[1]} ({largest[0]:.0%})")
    if better and not worse:
        return "v2", reasons
    if worse and not better:
        return "v1", reasons
    return "unclear", reasons


def _f(x, digits: int = 3) -> str:
    return "–" if x is None else f"{x:.{digits}f}"


SUMMARY_ROWS = (("local", "local estimate error (mean of the chains)"),
                ("raw", "raw count error (mean of the chains)"),
                ("pooled", "pooled station model, estimate error"),
                ("transfer", "transfer A→B, estimate error (mean)"),
                ("federated", "federated →C, estimate error (mean)"),
                ("cos", "cosine of the weights (mean of pairs)"),
                ("intercept_gap", "intercept gap (mean of pairs)"))


def summary(h: dict[str, dict], kind: str, per_run: list[dict[str, dict]] | None = None) -> list[str]:
    """The summary table of one kind; with `per_run` (several seeds) also each run's difference v2 − v1."""
    if all(x is None for v in VERSIONS for x in h[v][kind].values()):
        return [f"{kind}: too few examples in every chain"]
    head = f"{kind:<44}" + "".join(f"{'v' + v:>9}" for v in VERSIONS) + f"{'v2 − v1':>10}"
    lines = [head + ("   v2 − v1 per seed" if per_run else "")]
    for key, label in SUMMARY_ROWS:
        vals = [h[v][kind][key] for v in VERSIONS]
        delta = None if None in vals else vals[-1] - vals[0]
        digits = 2 if key in ("cos", "intercept_gap") else 3
        ln = f"  {label:<42}" + "".join(f"{_f(x, digits):>9}" for x in vals) + f"{_f(delta, digits):>10}"
        if per_run:
            ds = [None if None in (r["1"][kind][key], r["2"][kind][key]) else r["2"][kind][key] - r["1"][kind][key]
                  for r in per_run]
            ln += "   " + " ".join(f"{_f(d, digits):>7}" for d in ds)
        lines.append(ln)
    return lines


def verdict_lines(h: dict[str, dict]) -> list[str]:
    verdict, reasons = recommend_heads(h)
    text = {"v2": "Version 2 estimates clearly better across chains without estimating worse locally: worth trying "
                  "as the default once real stations confirm it.",
            "v1": "Version 2 is worse here: keep version 1.",
            "unclear": "No clear difference: keep version 1 as the default and decide on real data."}[verdict]
    return ["Recommendation: " + text] + [f"  - {r}" for r in reasons]


def report(results: dict[str, dict], groups: list[str], title: str) -> str:
    """The printed comparison of one run: a summary per kind, the details, and the recommendation."""
    lines = [title, ""]
    h = heads(results)
    for kind in KINDS:
        lines += summary(h, kind)
        if all(x is None for v in VERSIONS for x in h[v][kind].values()):
            lines.append("")
            continue
        lines.append("  details (v1 / v2):")
        for g in groups:
            loc = [results[v][kind]["local"].get(g) or {} for v in VERSIONS]
            lines.append(f"    local {g:<22} est {_f(loc[0].get('est'))} / {_f(loc[-1].get('est'))}   "
                         f"raw {_f(loc[0].get('raw'))}   auc {_f(loc[0].get('auc'), 2)} / {_f(loc[-1].get('auc'), 2)}"
                         f"   events {loc[0].get('rows', 0)}")
        for a, b in itertools.permutations(groups, 2):
            t = [results[v][kind]["transfer"].get((a, b)) for v in VERSIONS]
            if any(x is not None for x in t):
                lines.append(f"    transfer {a} → {b}".ljust(36) + f" {_f(t[0])} / {_f(t[-1])}")
        for c in groups:
            t = [results[v][kind]["federated"].get(c) for v in VERSIONS]
            if any(x is not None for x in t):
                lines.append(f"    federated → {c}".ljust(36) + f" {_f(t[0])} / {_f(t[-1])}")
        for a, b in itertools.combinations(groups, 2):
            s = [results[v][kind]["similarity"].get((a, b)) or {} for v in VERSIONS]
            if any(s):
                lines.append(f"    similarity {a} ~ {b}".ljust(36)
                             + f" cos {_f(s[0].get('cos'), 2)} / {_f(s[-1].get('cos'), 2)}"
                             + f"   dist {_f(s[0].get('dist'), 2)} / {_f(s[-1].get('dist'), 2)}"
                             + f"   intercept gap {_f(s[0].get('intercept_gap'), 2)} / "
                             + f"{_f(s[-1].get('intercept_gap'), 2)}")
        lines.append("")
    return "\n".join(lines + verdict_lines(h))


def report_repeated(runs: list[dict[str, dict]], title: str) -> str:
    """The printed comparison over several seeds: the means, each seed's difference, the recommendation on the
    means."""
    per_run = [heads(r) for r in runs]
    h = mean_heads(per_run)
    lines = [title, ""]
    for kind in KINDS:
        lines += summary(h, kind, per_run) + [""]
    return "\n".join(lines + verdict_lines(h))


# ─── Synthetic chains ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Chain:
    """A simulated playback chain. Levels in dB relative to the reference tone, except where noted.

    gain_db: reference tone at NOMINAL_REF_DBFS + gain_db (a USB turntable with a fixed, hot gain sits higher);
    hiss_db: the chain's own broadband noise (preamp, converter), present with the stylus up as well;
    ticks_per_min, tick_db: hits the chain itself produces in the groove (stylus, cartridge, electronics), the source of
    a high click floor; a master comparison and a listener both count them as real, not as music;
    lowpass_hz: high-frequency roll-off of the cartridge (second order), something the profile does not capture."""
    label: str
    gain_db: float
    hiss_db: float
    ticks_per_min: float
    tick_db: tuple[float, float]
    lowpass_hz: float | None

    @property
    def ref_dbfs(self) -> float:
        return NOMINAL_REF_DBFS + self.gain_db


CHAINS = {
    "budget": Chain("budget USB turntable", 8.0, -62.0, 500.0, (-38.0, -20.0), 12000.0),
    "mid": Chain("mid-range deck, interface", 0.0, -72.0, 100.0, (-38.0, -20.0), 16000.0),
    "high": Chain("aligned high-end chain", 2.0, -82.0, 15.0, (-38.0, -20.0), None),
}


def _music_transients(audio: np.ndarray, music: list[tuple[float, float]], rng, per_min: float) -> list[float]:
    """Short, sharp percussive hits in the music (claves, rim shots): click-like, but part of the music, so a master
    comparison explains them. These are the false alarms the model has to tell from clicks."""
    times = []
    for a, b in music:
        for _ in range(int(per_min * (b - a) / 60)):
            t = float(rng.uniform(a + 0.2, b - 0.2))
            n = int(rng.uniform(0.001, 0.004) * SR)
            k = np.arange(n)
            burst = rng.standard_normal(n) * np.exp(-k / max(1.0, rng.uniform(0.0003, 0.0012) * SR))
            burst *= 10 ** (rng.uniform(-34, -18) / 20)
            pan = rng.uniform(0.2, 0.8)
            i = int(t * SR)
            audio[i:i + n, 0] += burst * pan / max(pan, 1 - pan)
            audio[i:i + n, 1] += burst * (1 - pan) / max(pan, 1 - pan)
            times.append(t)
    return times


def make_record(seed: int, seconds: float):
    """One synthetic record side at the nominal gain: groove noise, music with drums and sharp transients, clicks,
    crackle and pops. Returns (audio float64, times of the record's defects)."""
    from vinylcheck import synth
    rng = np.random.default_rng(1000 + seed)
    edge = min(4.0, 0.08 * seconds)
    lay = synth.Layout(lead_in_s=edge, tracks=3 if seconds >= 40 else 2, gap_s=min(3.0, 0.05 * seconds),
                       lead_out_s=edge)
    audio, truth = synth.make_side(seconds, SR, seed=seed, layout=lay, noise_dbfs=float(rng.uniform(-64, -56)),
                                   music_dbfs=float(rng.uniform(-26, -19)), clicks_per_min=float(rng.uniform(8, 50)),
                                   pops=int(rng.integers(0, 4)), amp_range=(-48.0, -14.0))
    audio = audio.astype(np.float64)
    defects = [c["t"] for c in truth.clicks]
    for _ in range(int(rng.uniform(0, 150) * seconds / 60)):              # crackle: many faint clicks
        t = float(rng.uniform(0.6, seconds - 0.6))
        synth.add_click(audio, SR, t, float(rng.uniform(-62, -44)), int(rng.choice([1, 2, 3])), rng)
        defects.append(t)
    _music_transients(audio, truth.music, rng, float(rng.uniform(20, 120)))
    return audio, np.sort(np.array(defects))


def through_chain(audio: np.ndarray, chain: Chain, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """Play a side recorded at the nominal gain through `chain`: gain, roll-off, hiss, the chain's own hits. Returns
    (float32 audio, times of the chain's hits)."""
    from scipy import signal

    from vinylcheck import synth
    rng = np.random.default_rng(seed)
    y = audio * 10 ** (chain.gain_db / 20)
    if chain.lowpass_hz:
        b, a = signal.butter(2, chain.lowpass_hz, fs=SR)
        y = signal.lfilter(b, a, y, axis=0)
    y = y + rng.standard_normal(y.shape) * 10 ** ((chain.ref_dbfs + chain.hiss_db) / 20)
    ticks = []
    for _ in range(int(chain.ticks_per_min * len(y) / SR / 60)):
        t = float(rng.uniform(0.3, len(y) / SR - 0.3))
        synth.add_click(y, SR, t, chain.ref_dbfs + float(rng.uniform(*chain.tick_db)),
                        int(rng.choice([1, 2, 3], p=[0.4, 0.4, 0.2])), rng)
        ticks.append(t)
    return np.clip(y, -1, 1).astype(np.float32), np.sort(np.array(ticks))


def measure_profile(chain: Chain, seed: int, floor_seconds: float = 40.0) -> dict:
    """The chain's turntable profile, measured as a station does: chain noise from a stylus-up recording
    (analysis.chain_noise_dbfs), the reference level (the simulated chain's exact value; the alignment reads the 1 kHz
    band to ±0.1 dB on synthetic test records), the click floor in the silent groove of the same test record for every
    chain (alignment.click_floor). Returned as a profiles row of an accepted profile."""
    from vinylcheck import alignment, analysis, synth
    rng = np.random.default_rng(seed)
    up = rng.standard_normal((3 * SR, 2)) * 10 ** ((chain.ref_dbfs + chain.hiss_db) / 20)
    trng = np.random.default_rng(4242)                                   # the test record: the same for all chains
    groove = synth._surface_noise(int(floor_seconds * SR), trng, NOMINAL_REF_DBFS - 50.0)
    for _ in range(int(6 * floor_seconds / 60)):
        synth.add_click(groove, SR, float(trng.uniform(1, floor_seconds - 1)),
                        NOMINAL_REF_DBFS + float(trng.uniform(-45, -20)), 3, trng)
    played, _ = through_chain(groove, chain, seed + 1)
    fl = alignment.click_floor(played, SR, 0.0, floor_seconds, chain.ref_dbfs, alignment.AlignmentConfig())
    return {"accepted_at": 1.0, "chain_db": analysis.chain_noise_dbfs(up, SR), "ref_dbfs": chain.ref_dbfs,
            "floor_clicks_per_min": fl.get("clicks_per_min"), "floor_ticks_per_min": fl.get("ticks_per_min")}


def judge(events: list[dict], impulses: np.ndarray, tol_s: float = 0.003) -> np.ndarray:
    """The synthetic counterpart of a master comparison (master.py): real (1) where a record defect or a hit of the
    chain lies within ±3 ms (the master has neither) or where the event is not in the music (where the master is
    silent, everything is on the vinyl); music (0) otherwise, that is a music event the master explains."""
    y = np.zeros(len(events))
    for i, e in enumerate(events):
        j = int(np.searchsorted(impulses, e["t"]))
        near = any(0 <= k < len(impulses) and abs(impulses[k] - e["t"]) <= tol_s for k in (j - 1, j))
        y[i] = 1.0 if near or e.get("region") != "music" else 0.0
    return y


def _group_data(recs: list[tuple[int, int, list[dict], np.ndarray, float | None]], version: str, zp: dict) -> dict:
    """training_data()'s structure from judged synthetic events: recs = [(recording, record, events, y, ref)]."""
    acc = {k: {"X": [], "y": [], "rec": [], "record": [], "raw_n": {}} for k in KINDS}
    for rec, record, events, y, ref in recs:
        X = learning.features(events, ref, version, zp)
        for i, e in enumerate(events):
            a = acc.get(e.get("kind"))
            if a is None:
                continue
            a["X"].append(X[i]); a["y"].append(y[i]); a["rec"].append(rec); a["record"].append(record)
            a["raw_n"][rec] = a["raw_n"].get(rec, 0) + 1
    n_feat = len(learning.feature_names(version))
    out = {}
    for k, a in acc.items():
        n = len(a["y"])
        out[k] = {"X": np.array(a["X"]).reshape(-1, n_feat), "y": np.array(a["y"]), "w": np.ones(n),
                  "rec": np.array(a["rec"]), "record": np.array(a["record"]), "manual": np.zeros(n, bool),
                  "master": np.ones(n, bool), "fallback": np.zeros(n, bool), "raw_n": a["raw_n"]}
    return out


def run_synthetic(recordings: int = 12, seconds: float = 60.0, chains: list[str] | None = None, seed: int = 1,
                  floor_seconds: float = 40.0, out=print, repeat: int = 1, details: bool = False) -> dict:
    """Simulate, analyse, judge and compare, `repeat` times with the seeds seed, seed + 1, … (other records, other
    noise), because a single run of a dozen records moves the errors by about ±0.01. Returns {"runs" [one dict per
    seed: "seed", "profiles", "results" {version: evaluate()}], "profiles" and "results" of the first run, "report",
    "recommendation", "reasons"}; with several runs the report and the recommendation use the means over the runs."""
    t0 = time.time()
    names = chains or list(CHAINS)
    runs = []
    for s in range(seed, seed + max(1, repeat)):
        if repeat > 1:
            out(f"seed {s}:")
        runs.append(_synthetic_once(recordings, seconds, names, s, floor_seconds, out))
    what = f"{recordings} records of {seconds:.0f} s through {len(names)} chains"
    if len(runs) == 1:
        text = report(runs[0]["results"], names, f"Synthetic: {what}, seed {seed} ({time.time() - t0:.0f} s)")
        verdict, reasons = recommend(runs[0]["results"])
    else:
        title = (f"Synthetic: {what}, seeds {seed}–{seed + len(runs) - 1}, means over the seeds "
                 f"({time.time() - t0:.0f} s)")
        text = report_repeated([r["results"] for r in runs], title)
        if details:
            for r in runs:
                text += "\n\n" + report(r["results"], names, "Details of seed " + str(r["seed"]))
        verdict, reasons = recommend_heads(mean_heads([heads(r["results"]) for r in runs]))
    return {"runs": runs, "profiles": runs[0]["profiles"], "results": runs[0]["results"], "report": text,
            "recommendation": verdict, "reasons": reasons}


def _synthetic_once(recordings: int, seconds: float, names: list[str], seed: int, floor_seconds: float, out) -> dict:
    from vinylcheck import alignment, analysis
    cfg = replace(analysis.AnalysisConfig(), second_opinion=False)
    target = alignment.AlignmentConfig().target_ref_dbfs
    records = [make_record(seed * 100 + r, seconds) for r in range(recordings)]
    profiles, judged = {}, {}
    for ci, name in enumerate(names):
        chain = CHAINS[name]
        prof = measure_profile(chain, seed * 10 + ci, floor_seconds)
        profiles[name] = prof
        # pop threshold relative to the reference, as jobs.Worker.profile_trim sets it for an accepted profile
        acfg = replace(cfg, pop_dbfs=round(prof["ref_dbfs"] + (cfg.pop_dbfs - target), 2))
        floor = {"clicks_per_min": prof["floor_clicks_per_min"], "ticks_per_min": prof["floor_ticks_per_min"]}
        judged[name] = []
        for ri, (audio, defects) in enumerate(records):
            played, ticks = through_chain(audio, chain, seed * 1000 + 10 * ri + ci)
            res = analysis.analyze(played, SR, acfg, chain_db=prof["chain_db"], ref_dbfs=prof["ref_dbfs"], floor=floor)
            events = [e for e in res["events"] if e.get("region") != "excluded"]
            y = judge(events, np.sort(np.concatenate([defects, ticks])))
            judged[name].append((1000 * ci + ri, ri, events, y, res["metrics"].get("music_ref_dbfs")))
        out(f"  {name:<8} {chain.label:<28} chain noise {prof['chain_db']:6.1f} dBFS, reference "
            f"{prof['ref_dbfs']:5.1f} dBFS, click floor {prof['floor_clicks_per_min']:5.1f} + "
            f"{prof['floor_ticks_per_min']:5.1f} /min, {sum(len(r[2]) for r in judged[name])} events "
            f"({np.mean(np.concatenate([r[3] for r in judged[name]])):.0%} real)")
    results = {}
    for v in VERSIONS:
        groups = {name: _group_data(judged[name], v, learning.zero_point(profiles[name])) for name in names}
        results[v] = evaluate(groups, v)
    return {"seed": seed, "profiles": profiles, "results": results}


# ─── A copy of a station database ───────────────────────────────────────────

class ReadOnlyDB:
    """The two queries learning.training_data needs (q, events), on a database opened read-only. Judgements stored by
    versions before 1.0 under their German names are read as the current ones (legacy.EVENT_LABELS)."""

    def __init__(self, path: str | Path):
        p = Path(path).resolve()
        if not p.is_file():
            raise FileNotFoundError(p)
        wal = p.with_name(p.name + "-wal")
        self.note = None
        if wal.is_file() and wal.stat().st_size:
            # the copy still has changes in its write-ahead log: reading them needs SQLite's shared-memory index, which
            # SQLite creates next to the file if it is missing (an index, no data of the database is changed)
            self.conn = self._open(p, immutable=False)
            self.note = "the -wal file next to the copy is read; SQLite may add a -shm index file there"
        else:
            # a checkpointed copy: immutable reads the file as it is and creates nothing next to it
            self.conn = self._open(p, immutable=True)
        self.user_version = self.conn.execute("PRAGMA user_version").fetchone()[0]

    @staticmethod
    def _open(p: Path, immutable: bool) -> sqlite3.Connection:
        conn = sqlite3.connect(p.as_uri() + ("?mode=ro&immutable=1" if immutable else "?mode=ro"), uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        return conn

    def q(self, sql: str, args=()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def events(self, rec_id: int) -> list[dict]:
        rows = self.q("SELECT t, dur_ms, strength, amp_dbfs, ch, kind, region, gc, label FROM events "
                      "WHERE recording_id = ? ORDER BY t", (rec_id,))
        for r in rows:
            r["label"] = legacy.EVENT_LABELS.get(r["label"], r["label"])
        return rows


def _keep_recordings(data: dict, recs: set[int]) -> dict:
    out = {}
    for kind, d in data.items():
        keep = np.isin(np.asarray(d["rec"]), list(recs))
        out[kind] = {k: ({g: c for g, c in v.items() if int(g) in recs} if k == "raw_n" else np.asarray(v)[keep])
                     for k, v in d.items()}
    return out


def run_real(path: str | Path, out=print) -> dict:
    """The comparison on a copy of a station database (read-only). Returns like run_synthetic, plus "profiles" with
    the number of recordings and the missing zero-point values per profile."""
    db = ReadOnlyDB(path)
    if db.note:
        out(f"  note: {db.note}")
    profiles = db.q("SELECT * FROM profiles ORDER BY id")
    info, measured = {}, []
    for p in profiles:
        missing = learning.zero_point_missing(learning.zero_point(p))
        d1, ov = learning.training_data(db, p["id"], version="1")
        rows = sum(len(d["y"]) for d in d1.values())
        recs = {int(g) for d in d1.values() for g in np.asarray(d["rec"]).tolist()}
        info[p["id"]] = {"missing": missing, "recordings": len(recs), "examples": rows,
                         "master": ov["recordings_master"], "manual": ov["recordings_manual"]}
        out(f"  profile {p['id']}: {len(recs)} recordings with examples ({ov['recordings_master']} master, "
            f"{ov['recordings_manual']} with judgements), {rows} examples, zero point "
            + ("complete" if not missing else "missing " + ", ".join(missing)))
        if not missing and rows:
            measured.append(p["id"])
    _, ov_all = learning.training_data(db, version="2")
    out(f"  all recordings: {ov_all['recordings_master']} with a master, {ov_all['recordings_manual']} with "
        f"judgements, {ov_all['recordings_fallback']} without a complete zero point (version 2 falls back there)")
    if not measured:
        msg = ("No profile with a complete zero point and examples: feature version 2 cannot be compared here. "
               "Accept an alignment run (with the silent groove of the test record) and a calibration for the "
               "turntable profile, then judge or master-compare some recordings made with it.")
        return {"profiles": info, "results": None, "report": msg, "recommendation": "unclear", "reasons": [msg]}
    results = {}
    for v in VERSIONS:
        groups = {}
        for pid in measured:
            data, _ = learning.training_data(db, pid, version=v)
            groups[f"profile {pid}"] = learning.drop_fallback(data)
        results[v] = evaluate(groups, v)
    title = (f"Station database (data version {db.user_version}): {len(measured)} profile(s) with a complete zero "
             f"point; transfer, federated and similarity need at least two")
    text = report(results, [f"profile {pid}" for pid in measured], title)
    verdict, reasons = recommend(results)
    return {"profiles": info, "results": results, "report": text, "recommendation": verdict, "reasons": reasons}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("synthetic", help="simulated chains, the same records through each")
    s.add_argument("--recordings", type=int, default=12, help="records (one side each) per chain")
    s.add_argument("--seconds", type=float, default=60.0, help="length of each side")
    s.add_argument("--chains", default=",".join(CHAINS), help=f"comma-separated, from {', '.join(CHAINS)}")
    s.add_argument("--seed", type=int, default=1, help="first seed")
    s.add_argument("--repeat", type=int, default=3, help="runs with the seeds seed, seed + 1, …; the means decide")
    s.add_argument("--details", action="store_true", help="with several runs, also each run's details")
    r = sub.add_parser("real", help="a copy of a station database, read-only")
    r.add_argument("database", help="path to the copy of vinyl-check.sqlite")
    a = ap.parse_args(argv)
    if a.cmd == "synthetic":
        chains = [c.strip() for c in a.chains.split(",") if c.strip()]
        unknown = [c for c in chains if c not in CHAINS]
        if unknown or len(chains) < 2:
            ap.error(f"--chains needs at least two of {', '.join(CHAINS)}")
        res = run_synthetic(a.recordings, a.seconds, chains, a.seed, repeat=a.repeat, details=a.details)
    else:
        res = run_real(a.database)
    print()
    print(res["report"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Turntable alignment: measure the set-up of a turntable with a test record.

The second test method next to the record check (analysis.py). Here the record is not judged, the arm and
cartridge are: anti-skating, tracking headroom, channel balance, azimuth, arm/cartridge resonance, speed and
wow & flutter, arm height (via intermodulation), frequency response and rumble.

The test record is recorded side by side like an ordinary record. The analysis
 1. splits the recording into measurement bands **from the signal** (pauses, changes of channel image or
    frequency), not from time stamps, because band lengths are not printed on any sleeve,
 2. determines the type of each band (tone, sweep, level ramp, two-tone, noise, silent groove), its
    frequency and channel image (lateral = L and R in phase, vertical = out of phase, left only, right only),
 3. assigns each band its task (band sequence of the Analogue Productions AAPT 1, see assign_roles) and
    measures it,
 4. compares the results with the target values and derives advice.

Channel assignment of stereo grooves (45/45): the inner groove wall carries the left channel, the outer wall
the right one. Too much anti-skating pushes the stylus outwards against the outer wall, the left (inner)
wall loses tracking force → **left distorts first**. Too little anti-skating → right distorts first.

Levels are in dB relative to full scale (sine amplitude) or relative to the side's reference tone.

All user-facing texts (labels, findings, advice, warnings) are i18n messages: they are stored with the
results and rendered in the reader's language when displayed. Role, type and channel-image values ("ton",
"links", "vertikal", "stille_rille", …) are stored data and stay as they are.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage, signal, stats

from . import detect, i18n, measure, skips
from .analysis import _py, a_weighting_sos
from .i18n import msg, msg_join
from .measure import runs as _runs

HOP_S = 0.1             # time grid of the band search
WIN_S = 0.4             # analysis window per grid step (2.5 Hz resolution)
MIN_BAND_S = 1.5
MIN_SILENT_S = 8.0
MIN_SILENT_GROOVE_S = 30.0  # AAPT 1: ≈ 120 s; anything shorter is run-out before the locked groove (side 1: 9–14 s)
LV_HP_HZ = 30.0         # band search: level only from here up (record warps at 10–17 Hz are not a test tone)
EDGE_S = 0.3            # margin at each band edge that is not measured (fade in/out)
MIN_SWEEP_S = 4.0       # anything shorter is not a sweep (AAPT: 30–50 s) but e.g. rumble in the lead-in
TONAL = 0.6             # share of the peak in the band power from which a section counts as a tone
NOMINAL_WF = {3150.0: 100 / 3, round(3150 * 45 / (100 / 3), 1): 45.0}   # flutter test tone → speed (rpm)


@dataclass(frozen=True)
class AlignmentConfig:
    # A channel on the anti-skating ramp counts as distorted from this THD (harmonics only, without hum/noise) …
    thd_limit_pct: float = 3.0
    knee_db: float = 6.0                # … or from a jump above the trend of the lower half of the ramp
    same_db: float = 1.0                # both channels breaking up within this span = "simultaneously"
    balance_db: float = 1.0             # channel balance tolerance (typical cartridge spec: 1 dB)
    crosstalk_ok_db: float = -25.0      # good MM cartridges: 25–30 dB channel separation
    crosstalk_warn_db: float = -20.0
    azimuth_asym_db: float = 3.0        # difference between crosstalk L→R and R→L
    resonance_hz: tuple = (8.0, 12.0)   # target band of the arm/cartridge resonance
    speed_ok_pct: float = 0.5
    speed_warn_pct: float = 1.0
    wf_ok_pct: float = 0.1              # weighted, 2σ
    wf_warn_pct: float = 0.2            # DIN 45500
    imd_ok_pct: float = 3.0
    response_ok_db: float = 2.0         # frequency response deviation
    target_ref_dbfs: float = -20.0      # input level: reference tone (1 kHz, 0 dB) at the interface
    target_ref_tol_db: float = 4.0
    headroom_db: float = 12.0           # loudest band of the AAPT 1 (anti-skating ramp) above the reference


# Display names of the band roles and band types (keys are stored values, see assign_roles/classify)
ROLE_LABEL = {
    "ref_1k": msg("alignment.band.ref_1k"), "xt_links": msg("alignment.band.crosstalk_left"),
    "xt_rechts": msg("alignment.band.crosstalk_right"), "ref_1k_m20": msg("alignment.band.ref_1k_minus20"),
    "hf_10k": msg("alignment.band.hf_10k"), "sweep_hoch": msg("alignment.band.sweep_up"),
    "sweep_tief": msg("alignment.band.sweep_down"), "lf_100": msg("alignment.band.lf_100"),
    "imd": msg("alignment.band.imd"), "gleichlauf": msg("alignment.band.flutter"),
    "antiskating": msg("alignment.band.antiskating"), "rosa_lateral": msg("alignment.band.pink_lateral"),
    "rosa_vertikal": msg("alignment.band.pink_vertical"), "vert_1k": msg("alignment.band.vertical_1k"),
    "resonanz": msg("alignment.band.resonance"), "stille_rille": msg("alignment.band.silent_groove"),
}
TYPE_LABEL = {"ton": msg("alignment.type.tone"), "sweep": msg("alignment.type.sweep"),
              "rampe": msg("alignment.type.ramp"), "zweiton": msg("alignment.type.two_tone"),
              "rauschen": msg("alignment.type.noise"), "stille": msg("alignment.type.silent"),
              "unbekannt": msg("alignment.type.unknown")}

# ─── Targets ──────────────────────────────────────────────────────────────────
# A target map states per metric when a value is "ok", "check" (warn) or "bad".
# Rule kinds: abs (|value| ≤ limit), max (value ≤ limit), min (value ≥ limit), range ([low, high]).
# Maps can be chosen per turntable profile and edited in the settings; the findings are re-judged against the
# selected map when they are displayed (apply_targets), not during the analysis. The analysis itself judges
# with the standard map, so stored results are usable without a map.
# TARGET_FIELDS: metric → (label, unit, rule kind, hint); label and hint are i18n messages.
TARGET_FIELDS = {
    "pegel": (msg("alignment.field.ref_level"), "dBFS", "range", msg("alignment.field.ref_level_hint")),
    "balance_db": (msg("alignment.field.balance"), "dB", "abs", msg("alignment.field.balance_hint")),
    "crosstalk_lr_db": (msg("alignment.field.crosstalk_lr"), "dB", "max", msg("alignment.field.crosstalk_lr_hint")),
    "crosstalk_rl_db": (msg("alignment.field.crosstalk_rl"), "dB", "max", msg("alignment.field.crosstalk_rl_hint")),
    "azimuth_asym_db": (msg("alignment.field.azimuth"), "dB", "abs", msg("alignment.field.azimuth_hint")),
    "tracking_reserve_db": (msg("alignment.field.tracking_reserve"), "dB", "min",
                            msg("alignment.field.tracking_reserve_hint")),
    "mono_cancel_db": (msg("alignment.field.mono_cancel"), "dB", "max", msg("alignment.field.mono_cancel_hint")),
    "resonance_hz": (msg("alignment.field.resonance"), "Hz", "range", msg("alignment.field.resonance_hint")),
    "speed_pct": (msg("alignment.field.speed"), "%", "abs", msg("alignment.field.speed_hint")),
    "wf_weighted_pct": (msg("alignment.field.wf_weighted"), "%", "max", msg("alignment.field.wf_weighted_hint")),
    "imd_pct": (msg("alignment.field.imd"), "%", "max", msg("alignment.field.imd_hint")),
    "hf_10k_db": (msg("alignment.field.hf_10k"), "dB", "abs", msg("alignment.field.hf_10k_hint")),
    "sweep_hf_dev_db": (msg("alignment.field.sweep_hf"), "dB", "abs", msg("alignment.field.sweep_hf_hint")),
    "sweep_lf_dev_db": (msg("alignment.field.sweep_lf"), "dB", "abs", msg("alignment.field.sweep_lf_hint")),
    "lf_100_db": (msg("alignment.field.lf_100"), "dB", "abs", msg("alignment.field.lf_100_hint")),
    "rumble_db": (msg("alignment.field.rumble"), "dB", "max", msg("alignment.field.rumble_hint")),
}

# Built-in maps. The ids are stored in profiles and settings and must not change.
TARGET_MAPS = {
    "standard": {"name": msg("alignment.map.standard.name"), "note": msg("alignment.map.standard.note"), "targets": {
        "pegel": {"ok": [-24, -16], "warn": [-32, -6]}, "balance_db": {"ok": 1, "warn": 2},
        "crosstalk_lr_db": {"ok": -25, "warn": -20}, "crosstalk_rl_db": {"ok": -25, "warn": -20},
        "azimuth_asym_db": {"ok": 3, "warn": 8}, "tracking_reserve_db": {"ok": 11, "warn": 6},
        "mono_cancel_db": {"ok": -20, "warn": -14}, "resonance_hz": {"ok": [8, 12], "warn": [6, 15]},
        "speed_pct": {"ok": 0.5, "warn": 1.0}, "wf_weighted_pct": {"ok": 0.1, "warn": 0.2}, "imd_pct": {"ok": 3, "warn": 6},
        "hf_10k_db": {"ok": 2, "warn": 4}, "sweep_hf_dev_db": {"ok": 2, "warn": 4}, "sweep_lf_dev_db": {"ok": 2, "warn": 4},
        "lf_100_db": {"ok": 1, "warn": 2}, "rumble_db": {"ok": -55, "warn": -45}}},
    "din45500": {"name": msg("alignment.map.din45500.name"), "note": msg("alignment.map.din45500.note"), "targets": {
        "pegel": {"ok": [-24, -16], "warn": [-32, -6]}, "balance_db": {"ok": 2, "warn": 3},
        "crosstalk_lr_db": {"ok": -15, "warn": -12}, "crosstalk_rl_db": {"ok": -15, "warn": -12},
        "azimuth_asym_db": {"ok": 3, "warn": 8}, "tracking_reserve_db": {"ok": 11, "warn": 6},
        "mono_cancel_db": {"ok": -20, "warn": -14}, "resonance_hz": {"ok": [8, 12], "warn": [6, 15]},
        "speed_pct": {"ok": 1.5, "warn": 2.0}, "wf_weighted_pct": {"ok": 0.2, "warn": 0.3}, "imd_pct": {"ok": 3, "warn": 6},
        "hf_10k_db": {"ok": 3, "warn": 5}, "sweep_hf_dev_db": {"ok": 3, "warn": 5}, "sweep_lf_dev_db": {"ok": 3, "warn": 5},
        "lf_100_db": {"ok": 2, "warn": 3}, "rumble_db": {"ok": -35, "warn": -30}}},
    "labor": {"name": msg("alignment.map.lab.name"), "note": msg("alignment.map.lab.note"), "targets": {
        "pegel": {"ok": [-24, -16], "warn": [-32, -6]}, "balance_db": {"ok": 0.5, "warn": 1},
        "crosstalk_lr_db": {"ok": -28, "warn": -24}, "crosstalk_rl_db": {"ok": -28, "warn": -24},
        "azimuth_asym_db": {"ok": 1.5, "warn": 3}, "tracking_reserve_db": {"ok": 11, "warn": 10},
        "mono_cancel_db": {"ok": -25, "warn": -20}, "resonance_hz": {"ok": [8, 11], "warn": [7, 13]},
        "speed_pct": {"ok": 0.1, "warn": 0.3}, "wf_weighted_pct": {"ok": 0.05, "warn": 0.1}, "imd_pct": {"ok": 2, "warn": 4},
        "hf_10k_db": {"ok": 1.5, "warn": 3}, "sweep_hf_dev_db": {"ok": 1.5, "warn": 3}, "sweep_lf_dev_db": {"ok": 1.5, "warn": 3},
        "lf_100_db": {"ok": 0.5, "warn": 1}, "rumble_db": {"ok": -60, "warn": -52}}},
    "at3600l": {"name": msg("alignment.map.at3600l.name"), "note": msg("alignment.map.at3600l.note"), "targets": {
        "pegel": {"ok": [-24, -10], "warn": [-32, -4]}, "balance_db": {"ok": 2.5, "warn": 3.5},
        "crosstalk_lr_db": {"ok": -18, "warn": -15}, "crosstalk_rl_db": {"ok": -18, "warn": -15},
        "azimuth_asym_db": {"ok": 3, "warn": 8}, "tracking_reserve_db": {"ok": 11, "warn": 6},
        "mono_cancel_db": {"ok": -20, "warn": -14}, "resonance_hz": {"ok": [8, 12], "warn": [6, 15]},
        "speed_pct": {"ok": 0.5, "warn": 1.0}, "wf_weighted_pct": {"ok": 0.15, "warn": 0.25}, "imd_pct": {"ok": 3, "warn": 6},
        "hf_10k_db": {"ok": 3, "warn": 5}, "sweep_hf_dev_db": {"ok": 3, "warn": 5}, "sweep_lf_dev_db": {"ok": 2, "warn": 4},
        "lf_100_db": {"ok": 1, "warn": 2}, "rumble_db": {"ok": -50, "warn": -42}}},
    "uccello": {"name": msg("alignment.map.uccello.name"), "note": msg("alignment.map.uccello.note"), "targets": {
        "pegel": {"ok": [-24, -16], "warn": [-32, -6]}, "balance_db": {"ok": 1, "warn": 2},
        "crosstalk_lr_db": {"ok": -25, "warn": -20}, "crosstalk_rl_db": {"ok": -25, "warn": -20},
        "azimuth_asym_db": {"ok": 2, "warn": 4}, "tracking_reserve_db": {"ok": 11, "warn": 6},
        "mono_cancel_db": {"ok": -20, "warn": -14}, "resonance_hz": {"ok": [8, 12], "warn": [6, 15]},
        "speed_pct": {"ok": 0.5, "warn": 1.0}, "wf_weighted_pct": {"ok": 0.1, "warn": 0.2}, "imd_pct": {"ok": 3, "warn": 6},
        "hf_10k_db": {"ok": 2, "warn": 4}, "sweep_hf_dev_db": {"ok": 2, "warn": 4}, "sweep_lf_dev_db": {"ok": 2, "warn": 4},
        "lf_100_db": {"ok": 1, "warn": 2}, "rumble_db": {"ok": -55, "warn": -45}}},
}
STANDARD_TARGETS = TARGET_MAPS["standard"]["targets"]


class TargetMapError(ValueError):
    """An invalid target map. `msg` is the translatable message; str() renders it in the current language."""

    def __init__(self, message: dict):
        super().__init__(message)
        self.msg = message

    def __str__(self) -> str:
        return str(i18n.render(self.msg))


def judge(key: str, value, targets: dict | None = None) -> str | None:
    """Status "ok" / "warn" / "bad" of a value under the map's rule (None: no rule or no numeric value)."""
    rule = (targets or STANDARD_TARGETS).get(key)
    if rule is None or value is None or not isinstance(value, (int, float)):
        return None
    kind = TARGET_FIELDS[key][2]
    v, ok, warn = float(value), rule["ok"], rule["warn"]
    if kind == "abs":
        return "ok" if abs(v) <= ok else ("warn" if abs(v) <= warn else "bad")
    if kind == "max":
        return "ok" if v <= ok else ("warn" if v <= warn else "bad")
    if kind == "min":
        return "ok" if v >= ok else ("warn" if v >= warn else "bad")
    return "ok" if ok[0] <= v <= ok[1] else ("warn" if warn[0] <= v <= warn[1] else "bad")


def _target_value(x: float) -> dict:
    """A limit for display: 2 decimals below 1, else 1, no trailing zeros, typographic minus sign."""
    x = round(float(x), 2 if abs(x) < 1 else 1)
    return msg("alignment.target.value_negative", v=-x) if x < 0 else msg("alignment.target.value", v=x)


def target_text(key: str, targets: dict | None = None) -> dict | str:
    """The "ok" range of a metric as a message, e.g. "±1 dB", "≤ −25 dB", "8 to 12 Hz" ("" without a rule)."""
    rule = (targets or STANDARD_TARGETS).get(key)
    if rule is None:
        return ""
    unit, kind = TARGET_FIELDS[key][1], TARGET_FIELDS[key][2]
    ok = rule["ok"]
    if kind == "range":
        return msg("alignment.target.range", lo=_target_value(ok[0]), hi=_target_value(ok[1]), unit=unit)
    return msg(f"alignment.target.{kind}", v=_target_value(ok), unit=unit)


def validate_targets(t: dict) -> dict:
    """Check a map coming from the web interface: known metrics only, numbers, "warn" at least as wide as "ok".

    Returns the cleaned map (floats); raises TargetMapError (a ValueError) with a translatable message."""
    out = {}
    for key, rule in t.items():
        if key not in TARGET_FIELDS:
            raise TargetMapError(msg("alignment.error.unknown_metric", metric=key))
        kind = TARGET_FIELDS[key][2]
        try:
            if kind == "range":
                ok = [float(rule["ok"][0]), float(rule["ok"][1])]
                warn = [float(rule["warn"][0]), float(rule["warn"][1])]
                if ok[0] > ok[1] or warn[0] > ok[0] or warn[1] < ok[1]:
                    raise ValueError
            else:
                ok, warn = float(rule["ok"]), float(rule["warn"])
                if (kind == "abs" and (ok < 0 or warn < ok)) or (kind == "max" and warn < ok) or (kind == "min" and warn > ok):
                    raise ValueError
        except (KeyError, TypeError, ValueError, IndexError):
            raise TargetMapError(msg("alignment.error.warn_narrower_than_ok", label=TARGET_FIELDS[key][0])) from None
        out[key] = {"ok": ok, "warn": warn}
    return out


def _without_advice(text, advice):
    """The neutral part of a finding's text, if the advice was appended to it earlier."""
    if isinstance(text, dict) and text.get("$join") and text["$join"][-1] == advice:
        rest = text["$join"][:-1]
        return rest[0] if len(rest) == 1 else msg_join(rest, text.get("$sep", " "))
    if isinstance(text, str) and isinstance(advice, str) and text.rstrip().endswith(advice):
        return text.rstrip()[:-len(advice)].rstrip()      # plain-text findings stored by older versions
    return text


def _with_advice(text, advice):
    if isinstance(text, str) and isinstance(advice, str):
        return (text.rstrip() + " " + advice).strip()
    return msg_join([text, advice])


def apply_targets(items: list[dict], targets: dict) -> list[dict]:
    """Re-judge findings against a target map: status and target text of every metric with a rule.

    The neutral text stays; the advice is appended only when the status is not "ok", so switching maps never
    leaves a wrong recommendation behind. Idempotent: findings that already carry their advice (stored
    results) get it exactly once. Metrics without a rule stay as they are."""
    out = []
    for it in items:
        it = dict(it)
        st = judge(it["key"], it.get("value"), targets)
        if st is not None and it.get("status") != "info":
            it["status"] = st
            it["soll"] = target_text(it["key"], targets)
        advice = it.get("advice")
        if advice:
            base = _without_advice(it.get("text"), advice)
            it["text"] = base if it["status"] == "ok" else _with_advice(base, advice)
        out.append(it)
    return sorted(out, key=_result_key)


# Metrics for comparing runs: label, unit, what counts as better ("lower", "higher", "abs" = closer to 0,
# ("target", x) = closer to x, None = no judgement)
SUMMARY_SPEC = {
    "ref_level_dbfs": (msg("alignment.summary.ref_level_dbfs"), "dBFS", None),
    "balance_db": (msg("alignment.summary.balance_db"), "dB", "abs"),
    "crosstalk_lr_db": (msg("alignment.summary.crosstalk_lr_db"), "dB", "lower"),
    "crosstalk_rl_db": (msg("alignment.summary.crosstalk_rl_db"), "dB", "lower"),
    "azimuth_asym_db": (msg("alignment.summary.azimuth_asym_db"), "dB", "abs"),
    "antiskate_break_l_db": (msg("alignment.summary.antiskate_break_l_db"), "dB", "higher"),
    "antiskate_break_r_db": (msg("alignment.summary.antiskate_break_r_db"), "dB", "higher"),
    "antiskate_diff_db": (msg("alignment.summary.antiskate_diff_db"), "dB", "abs"),
    "tracking_reserve_db": (msg("alignment.summary.tracking_reserve_db"), "dB", "higher"),
    "thd_top_l_pct": (msg("alignment.summary.thd_top_l_pct"), "%", "lower"),
    "thd_top_r_pct": (msg("alignment.summary.thd_top_r_pct"), "%", "lower"),
    "thdn_top_l_pct": (msg("alignment.summary.thdn_top_l_pct"), "%", "lower"),
    "thdn_top_r_pct": (msg("alignment.summary.thdn_top_r_pct"), "%", "lower"),
    "resonance_hz": (msg("alignment.summary.resonance_hz"), "Hz", ("target", 10.0)),
    "resonance_peak_db": (msg("alignment.summary.resonance_peak_db"), "dB", "lower"),
    "speed_pct": (msg("alignment.summary.speed_pct"), "%", "abs"),
    "wf_weighted_pct": (msg("alignment.summary.wf_weighted_pct"), "%", "lower"),
    "wf_drive_pct": (msg("alignment.summary.wf_drive_pct"), "%", "lower"),
    "eccentricity_pct": (msg("alignment.summary.eccentricity_pct"), "%", "lower"),
    "imd_pct": (msg("alignment.summary.imd_pct"), "%", "lower"),
    "mono_cancel_db": (msg("alignment.summary.mono_cancel_db"), "dB", "lower"),
    "hf_10k_db": (msg("alignment.summary.hf_10k_db"), "dB", "abs"),
    "sweep_hf_dev_db": (msg("alignment.summary.sweep_hf_dev_db"), "dB", "abs"),
    "sweep_lf_dev_db": (msg("alignment.summary.sweep_lf_dev_db"), "dB", "abs"),
    "lf_100_db": (msg("alignment.summary.lf_100_db"), "dB", "abs"),
    "rumble_db": (msg("alignment.summary.rumble_db"), "dB", "lower"),
    "hum_db": (msg("alignment.summary.hum_db"), "dB", "lower"),
    "noise_a_db": (msg("alignment.summary.noise_a_db"), "dB", "lower"),
    "floor_clicks_per_min": (msg("alignment.summary.floor_clicks_per_min"), "/min", "lower"),
    "floor_ticks_per_min": (msg("alignment.summary.floor_ticks_per_min"), "/min", "lower"),
}


# ─── Framing ──────────────────────────────────────────────────────────────────
def _frames(audio: np.ndarray, sr: int) -> dict:
    """Features per 0.1 s: power of L/R/M/S, peak frequency and tonality of M and S."""
    hop, win = int(sr * HOP_S), int(sr * WIN_S)
    n = len(audio)
    nf = max(0, (n - win) // hop + 1)
    w = signal.windows.blackmanharris(win, sym=False).astype(np.float32)
    w2 = float(np.sum(w.astype(np.float64) ** 2))
    fr = np.fft.rfftfreq(win, 1 / sr)
    band = (fr >= 4) & (fr <= 0.45 * sr)
    b0 = int(np.argmax(band))
    df = fr[1]
    hp = fr[band] >= LV_HP_HZ
    out = {k: np.zeros(nf) for k in ("pl", "pr", "pm", "ps", "fm", "fs", "tm", "ts", "php")}
    lv = np.lib.stride_tricks.sliding_window_view
    vl, vr = lv(audio[:, 0], win)[::hop][:nf], lv(audio[:, 1], win)[::hop][:nf]
    for s in range(0, nf, 256):
        L = vl[s:s + 256].astype(np.float32)
        R = vr[s:s + 256].astype(np.float32)
        M, S = (L + R) / 2, (L - R) / 2
        sl = slice(s, s + len(L))
        out["pl"][sl], out["pr"][sl] = np.mean(L.astype(np.float64) ** 2, 1), np.mean(R.astype(np.float64) ** 2, 1)
        out["pm"][sl], out["ps"][sl] = np.mean(M.astype(np.float64) ** 2, 1), np.mean(S.astype(np.float64) ** 2, 1)
        for comp, fk, tk in ((M, "fm", "tm"), (S, "fs", "ts")):
            p = np.abs(np.fft.rfft(comp * w, axis=1)) ** 2 * (2 / (win * w2))
            pb = p[:, band]
            out["php"][sl] += pb[:, hp].sum(axis=1)
            k = np.argmax(pb, axis=1)
            idx = np.arange(len(k))
            # parabolic interpolation of the peak bin on log power
            a = np.log(pb[idx, np.maximum(k - 1, 0)] + 1e-30)
            b = np.log(pb[idx, k] + 1e-30)
            c = np.log(pb[idx, np.minimum(k + 1, pb.shape[1] - 1)] + 1e-30)
            den = a - 2 * b + c
            off = np.where(den < 0, 0.5 * (a - c) / np.where(den < 0, den, -1), 0.0)
            out[fk][sl] = (b0 + k + np.clip(off, -0.5, 0.5)) * df
            # Tonality: share of the power around the peak (±6 %, at least ±5 bins). Fairly wide, so that a
            # fast sweep that moves a few percent within the window still counts as a tone
            cs = np.cumsum(np.pad(pb, ((0, 0), (1, 0))), axis=1)
            half = np.maximum(5, np.ceil(0.06 * (b0 + k))).astype(int)
            lo, hi = np.maximum(k - half, 0), np.minimum(k + half + 1, pb.shape[1])
            out[tk][sl] = (cs[idx, hi] - cs[idx, lo]) / np.maximum(cs[:, -1], 1e-30)
    out["lv"] = measure.db((out["pl"] + out["pr"]) / 2)
    out["lv_hp"] = measure.db(out["php"])            # M² + S² = (L² + R²) / 2, only from LV_HP_HZ up
    out["t"] = (np.arange(nf) * hop + win / 2) / sr
    return out


def _frame_mode(F: dict, i: slice) -> np.ndarray:
    """Channel image per frame as codes of MODE_NAMES (same thresholds as measure.channel_mode)."""
    pl, pr, pm, ps = F["pl"][i], F["pr"][i], F["pm"][i], F["ps"][i]
    r = measure.db(pl) - measure.db(pr)
    corr = (pm - ps) / np.maximum(pm + ps, 1e-30)
    return np.select([r > 12, r < -12, corr > 0.6, corr < -0.6], [1, 2, 3, 4], 0)


def _split(F: dict, a: int, b: int) -> list[tuple[int, int]]:
    """An active section without a pause can contain several bands: split it where the channel image
    changes or the frequency jumps (but not along sweeps)."""
    per_s = int(round(1 / HOP_S))
    mode = ndimage.median_filter(_frame_mode(F, slice(a, b)), size=9, mode="nearest")
    vert = F["ps"][a:b] > F["pm"][a:b]
    f = np.where(vert, F["fs"][a:b], F["fm"][a:b])
    tonality = np.where(vert, F["ts"][a:b], F["tm"][a:b])
    lf = ndimage.median_filter(np.log2(np.maximum(f, 1.0)), size=5, mode="nearest")
    cuts = set()
    for (s1, e1), (s2, e2) in zip(_runs_of(mode)[:-1], _runs_of(mode)[1:]):
        # Don't split noise bands at changes of the channel image: record warps tip it to "mixed" now and
        # then, and pink noise would fall apart into pieces of which only the first gets its role
        if (e1 - s1 >= per_s and e2 - s2 >= per_s
                and max(np.median(tonality[s1:e1]), np.median(tonality[s2:e2])) > TONAL):
            cuts.add(s2)
    jumps = np.flatnonzero(np.abs(np.diff(lf)) > 0.25) + 1          # > ¼ octave from one frame to the next
    for j in jumps:
        if j >= 3 and j + 3 <= len(tonality) and tonality[j - 3:j].min() > TONAL and tonality[j:j + 3].min() > TONAL:
            cuts.add(int(j))
    bounds = [0] + sorted(cuts) + [b - a]
    out, start = [], 0
    for c in bounds[1:]:
        if c - start >= MIN_BAND_S * per_s or c == b - a:
            out.append((a + start, a + c))
            start = c
    return [(s, e) for s, e in out if e - s >= MIN_BAND_S * per_s]


def _runs_of(labels: np.ndarray) -> list[tuple[int, int]]:
    """Runs of equal labels as (start, end_exclusive)."""
    if len(labels) == 0:
        return []
    cut = np.flatnonzero(np.diff(labels)) + 1
    starts = np.concatenate([[0], cut])
    ends = np.concatenate([cut, [len(labels)]])
    return list(zip(starts, ends))


def _sweep_tail(F: dict, s: int, e: int, lv_full: np.ndarray, thr: float) -> int:
    """End of a falling sweep below LV_HP_HZ: follow it as long as the frequency stays on the line (log f over t)
    of the last 3 s and the full-band level is above the threshold. Returns the new end (frame)."""
    per_s = int(round(1 / HOP_S))
    a = max(s, e - 3 * per_s)
    if e - a < per_s or e >= len(lv_full):
        return e
    vert = F["ps"] > F["pm"]
    lf = np.log2(np.maximum(np.where(vert, F["fs"], F["fm"]), 1.0))
    t = np.arange(len(lf))
    if float(np.median(lf[e - per_s:e])) > np.log2(4 * LV_HP_HZ):
        return e
    slope, icpt = np.polyfit(t[a:e], lf[a:e], 1)
    if slope >= -0.05 / per_s:                       # not falling (at least 0.05 octaves per second)
        return e
    k, miss = e, 0
    while k < min(len(lf), e + 20 * per_s) and lv_full[k] > thr and miss < 3:
        ok = abs(lf[k] - (slope * k + icpt)) < 0.35 and lf[k] >= 2.0
        miss = 0 if ok else miss + 1
        k += 1
    return k - miss


def find_bands(audio: np.ndarray, sr: int, chain_db: float | None = None, F: dict | None = None) -> dict:
    """Measurement bands, silent sections and the groove area (stylus down) of a test record side.

    chain_db: noise of the recording chain with the stylus up (from the calibration), if known.
    Returns frame indices: bands and silent sections as (start, end) lists, groove as (start, end)."""
    F = F or _frames(audio, sr)
    # Level without record warps: some turntables pass warps into the silent groove at −46 dBFS, only 40 dB
    # below the loudest bands. With them, the silent groove fell apart into dozens of "bands" and the section
    # after the stylus lift was measured instead (rumble −76.5 instead of −40.4 dB). Bass and resonance sweeps
    # down to 10 Hz are followed further by _sweep_tail on the full-band level.
    lv = ndimage.median_filter(F["lv_hp"], size=5, mode="nearest")
    lv_full = ndimage.median_filter(F["lv"], size=5, mode="nearest")
    nf = len(lv)
    per_s = int(round(1 / HOP_S))
    if nf == 0:
        return {"bands": [], "silent": [], "groove": (0.0, 0.0), "groove_noise_dbfs": None, "frames": F}
    top = float(np.percentile(lv, 95))
    thr = max(top - 40.0, float(np.percentile(lv, 1)) + 10.0)

    def scan(thr):
        active = lv > thr
        # bridge short dips (< 0.3 s)
        for s, e in _runs(~active):
            if e - s < 3 and s > 0 and e < nf:
                active[s:e] = True
        for s, e in _runs(active):
            k = _sweep_tail(F, s, e, lv_full, thr)
            active[e:k] = True
        runs = [(s, e) for s, e in _runs(active) if e - s >= MIN_BAND_S * per_s]
        bands = [part for s, e in runs for part in _split(F, s, e)]
        gaps = np.zeros(nf, bool)
        if bands:
            gaps[bands[0][0]:bands[-1][1]] = ~active[bands[0][0]:bands[-1][1]]
        # Only count pause frames down to 20 dB below the loudest pauses: silence with the stylus raised is
        # ≈ 40 dB lower and must not set the groove noise (seen in practice: 126 s at −102 dBFS between the
        # bands, groove noise −60 dBFS)
        g = lv[gaps]
        if g.size < per_s:
            return bands, None
        g = g[g >= float(np.percentile(g, 95)) - 20.0]
        return bands, float(np.median(g))

    bands, groove_noise = scan(thr)
    # Second pass: threshold at least 6 dB above the groove noise of the pauses. With hum and rumble the groove
    # noise otherwise sat right at the threshold, and the silent groove fell apart into many short "bands"
    if groove_noise is not None and groove_noise + 6.0 > thr:
        thr = groove_noise + 6.0
        bands, groove_noise = scan(thr)
    in_band = np.zeros(nf, bool)
    for s, e in bands:
        in_band[s:e] = True

    # Groove area: at the start/end only "stylus up" when clearly quieter than the groove noise in the pauses
    # between the bands (or than the chain noise + 6 dB from the calibration)
    if chain_db is not None:
        up_thr = chain_db + 6.0
    elif groove_noise is not None:
        up_thr = groove_noise - 8.0
    else:
        up_thr = -95.0
    up = lv < up_thr
    gs, ge = 0, nf
    first = bands[0][0] if bands else nf
    last = bands[-1][1] if bands else 0
    k = 0
    while k < min(first, nf) and up[k]:
        k += 1
    gs = k + (int(0.5 * per_s) if k > 0 else 0)
    k = nf
    while k > max(last, 0) and up[k - 1]:
        k -= 1
    ge = k - (int(0.5 * per_s) if k < nf else 0)
    # Silence = in no band (single clicks don't cut up the silent groove), inside the groove area, and not as
    # quiet as "stylus up": the stylus can be raised in the middle of a recording too (e.g. two minutes at
    # −102 dBFS while the record is turned over must not count as silent groove)
    silent = [(max(s, gs), min(e, ge)) for s, e in _runs(~in_band)]
    silent = [(s, e) for s, e in silent
              if e - s >= MIN_SILENT_S * per_s and float(np.median(lv[s:e])) >= up_thr]
    return {"bands": bands, "silent": silent, "groove": (gs, ge), "groove_noise_dbfs": groove_noise,
            "frames": F}


# ─── Classification ───────────────────────────────────────────────────────────
# Codes of _frame_mode → channel image (stored values, as returned by measure.channel_mode)
MODE_NAMES = {1: "links", 2: "rechts", 3: "lateral", 4: "vertikal", 0: "gemischt"}


def classify(F: dict, s: int, e: int, audio: np.ndarray, sr: int) -> dict:
    """Type, channel image and frequency of the band between frames s and e.

    Types: "ton" (steady tone), "rampe" (tone with rising level), "zweiton" (two tones, e.g. IMD),
    "sweep" (frequency moving along a line in log f), "rauschen" (steady noise), "unbekannt"."""
    trim = int(EDGE_S / HOP_S)
    i = slice(s + trim, max(s + trim + 1, e - trim))
    pl, pr, pm, ps = (float(np.mean(F[k][i])) for k in ("pl", "pr", "pm", "ps"))
    corr = (pm - ps) / max(pm + ps, 1e-30)
    mode = measure.channel_mode(pl, pr, corr)
    vert = mode == "vertikal"
    f = F["fs"][i] if vert else F["fm"][i]
    tonality = F["ts"][i] if vert else F["tm"][i]
    pc = F["ps"][i] if vert else F["pm"][i]
    lvc = measure.db(pc)
    t = F["t"][i]
    out = {"t0": round(float(F["t"][s] - WIN_S / 2 + HOP_S / 2), 2), "t1": round(float(F["t"][e - 1] + WIN_S / 2 - HOP_S / 2), 2),
           "mode": mode, "corr": round(corr, 3), "level_db": round(float(measure.db(pc.mean())), 2),
           "tonality": round(float(np.median(tonality)), 3)}
    out["dur_s"] = round(out["t1"] - out["t0"], 2)
    if np.median(tonality) >= TONAL:
        # Only frames whose level belongs to the signal: when a sweep runs all the way to 20 kHz, the band
        # contains a few quiet frames after its end in which the frequency search finds rumble at 10–13 Hz;
        # that pulled the sweep's rank correlation down from 0.93 to 0.58
        tonal = (tonality >= 0.5) & (lvc >= float(np.percentile(lvc, 90)) - 20.0)
        # In a rising sweep (1 → 20 kHz), frames below 20 Hz are record warps/rumble after its end. Some
        # turntables pass them almost as loud as the −20 dB tracks (−42…−52 dBFS at 11–15 Hz), so the level
        # mask alone is not enough. In falling sweeps (bass, resonance down to 10 Hz) they are signal.
        fq = f[tonal]
        q = max(1, fq.size // 4)
        rising = fq.size >= 8 and float(np.median(fq[-q:])) > 1.5 * float(np.median(fq[:q]))
        if rising:
            tonal &= f >= 20.0
        lf = np.log2(np.maximum(f[tonal], 1.0))
        spread = float(np.percentile(lf, 95) - np.percentile(lf, 5)) if lf.size else 0.0
        if spread < 0.06:                            # < 0.06 octaves: one frequency
            fmed = float(2 ** np.median(lf))
            out |= {"type": "ton", "freq_hz": round(fmed, 2)}
            n = len(lvc)
            q = max(1, n // 7)
            rise = float(np.median(lvc[-q:]) - np.median(lvc[:q]))
            rho = stats.spearmanr(t, lvc).statistic if n > 5 else 0.0
            if rise > 4 and rho > 0.8:               # level rises steadily by more than 4 dB
                out |= {"type": "rampe", "rise_db": round(rise, 2)}
            else:
                second = _second_tone(audio, sr, out["t0"] + EDGE_S, out["t1"] - EDGE_S, fmed, vert)
                if second:
                    out |= {"type": "zweiton", "freq2_hz": round(second[0], 1), "ratio_db": round(second[1], 1)}
        elif spread >= 0.5 and lf.size > 5 and out["dur_s"] >= MIN_SWEEP_S:
            rho = stats.spearmanr(t[tonal], lf).statistic
            if abs(rho) > 0.9:                       # frequency moves monotonically: a sweep
                fit = _traj_fit(t[tonal], lf)
                ta, tb = out["t0"] + EDGE_S, out["t1"] - EDGE_S
                if rising:
                    # The sweep ends before the band does (2–3 s of rumble afterwards) → measuring range up to
                    # the last frame on the curve, otherwise the line extrapolates beyond 20 kHz (points at
                    # 24 and 30 kHz)
                    on = t[tonal][np.abs(lf - np.polyval(fit, t[tonal])) < 0.1]
                    if on.size:
                        ta, tb = max(ta, float(on[0]) - WIN_S / 2), min(tb, float(on[-1]) + WIN_S / 2)
                out |= {"type": "sweep", "direction": "auf" if rho > 0 else "ab",
                        "f_start_hz": round(float(2 ** np.polyval(fit, ta)), 1),
                        "f_end_hz": round(float(2 ** np.polyval(fit, tb)), 1),
                        "span": [round(ta, 2), round(tb, 2)], "fit": [float(c) for c in fit]}
            else:
                out["type"] = "unbekannt"
        else:
            out["type"] = "unbekannt"
    else:
        steady = float(np.std(lvc)) < 2.0
        out["type"] = "rauschen" if steady else "unbekannt"
    return out


def _traj_fit(t: np.ndarray, lf: np.ndarray) -> np.ndarray:
    """log2 frequency over time: a line (logarithmic sweep), otherwise a parabola. Outliers are dropped."""
    keep = np.ones(len(t), bool)
    deg = 1
    for _ in range(3):
        fit = np.polyfit(t[keep], lf[keep], deg)
        res = np.abs(lf - np.polyval(fit, t))
        if deg == 1 and np.sqrt(np.mean(res[keep] ** 2)) > 0.08:
            deg = 2
            continue
        new = res < max(0.05, 3 * np.median(res[keep]))
        if new.sum() < 5 or np.array_equal(new, keep):
            break
        keep = new
    fit = np.polyfit(t[keep], lf[keep], deg)
    if deg == 2 and abs(fit[0]) > 1e-12 and t.min() < -fit[1] / (2 * fit[0]) < t.max():
        # vertex inside the band: the frequency would go there and back, which is no sweep → line
        fit = np.polyfit(t[keep], lf[keep], 1)
    return fit


def _comp(audio: np.ndarray, which: str) -> np.ndarray:
    """One component of a stereo signal: "L", "R", "M" (mid, lateral) or "S" (side, vertical)."""
    if which == "M":
        return (audio[:, 0] + audio[:, 1]) / 2
    if which == "S":
        return (audio[:, 0] - audio[:, 1]) / 2
    return audio[:, {"L": 0, "R": 1}[which]]


def _seg(audio: np.ndarray, sr: int, t0: float, t1: float) -> np.ndarray:
    return audio[max(0, int(t0 * sr)):max(int(t0 * sr) + 1, int(t1 * sr))].astype(np.float64)


def _second_tone(audio, sr, t0, t1, f1, vert) -> tuple[float, float] | None:
    """Two-tone signal (e.g. IMD 60 Hz + 4 kHz): the strongest peak that is not a harmonic of f1.
    Returns (frequency, level relative to f1 in dB), or None if nothing reaches −20 dB."""
    x = _comp(_seg(audio, sr, t0, t1), "S" if vert else "M")
    if len(x) < sr // 4:
        return None
    f, p = measure.spectrum(x, sr, 0.5)
    p = p[:, 0]
    p1 = p[np.argmin(np.abs(f - f1))]
    ok = (f > 5 * f1) & (f < 0.45 * sr)
    order = np.round(f / f1)
    harm = (order <= 10) & (np.abs(f - order * f1) < 3 * (f[1] - f[0]) + 0.005 * f)
    cand = ok & ~harm
    if not cand.any():
        return None
    k = int(np.flatnonzero(cand)[np.argmax(p[cand])])
    ratio = float(measure.db(p[k]) - measure.db(p1))
    return (float(f[k]), ratio) if ratio > -20 else None


def assign_roles(bands: list[dict]) -> None:
    """Give each band its task (band sequence of the AAPT 1, matched by features instead of position).

    Sets b["role"] in place; every role is given at most once. Role values are stored and used by the web
    interface: ref_1k, ref_1k_m20, xt_links, xt_rechts, vert_1k, hf_10k, lf_100, gleichlauf (wow & flutter),
    antiskating, sweep_hoch (rising sweep), sweep_tief (falling bass sweep), resonanz (vertical sweep), imd,
    rosa_lateral, rosa_vertikal (pink noise), stille_rille (silent groove)."""
    taken = set()

    def give(b, role):
        if role not in taken:
            b["role"] = role
            taken.add(role)

    near = lambda f, x, tol=0.05: f is not None and abs(f / x - 1) <= tol   # noqa: E731
    lat1k = [b for b in bands if b.get("type") == "ton" and b["mode"] == "lateral" and near(b.get("freq_hz"), 1000)]
    if lat1k:
        ref = max(lat1k, key=lambda b: b["level_db"])
        give(ref, "ref_1k")
        for b in lat1k:
            if b is not ref and b["level_db"] <= ref["level_db"] - 14:
                give(b, "ref_1k_m20")
    for b in bands:
        if b.get("role"):
            continue
        typ, f, mode = b.get("type"), b.get("freq_hz"), b["mode"]
        if typ == "ton":
            if near(f, 1000) and mode in ("links", "rechts"):
                give(b, "xt_" + mode)
            elif near(f, 1000) and mode == "vertikal":
                give(b, "vert_1k")
            elif near(f, 10000) and mode == "lateral":
                give(b, "hf_10k")
            elif near(f, 100) and mode == "lateral":
                give(b, "lf_100")
            elif any(near(f, x, 0.04) for x in NOMINAL_WF):
                give(b, "gleichlauf")
        elif typ == "rampe" and f and 250 <= f <= 400:
            give(b, "antiskating")
        elif typ == "sweep":
            if b["direction"] == "auf" and b["f_end_hz"] >= 8000:
                give(b, "sweep_hoch")
            elif b["direction"] == "ab" and b["f_end_hz"] <= 40 and b["f_start_hz"] >= 200:
                give(b, "resonanz" if mode == "vertikal" else "sweep_tief")
        elif typ == "zweiton" and f and 40 <= f <= 80 and 3000 <= b.get("freq2_hz", 0) <= 6000:
            give(b, "imd")
        elif typ == "rauschen" and mode in ("lateral", "vertikal"):
            give(b, "rosa_" + mode)
        elif typ == "stille":
            give(b, "stille_rille")


# ─── Measurements per band ────────────────────────────────────────────────────
def _tone_band(audio, sr, b, comps=("L", "R")) -> dict:
    """Level and distortion of a steady tone band (0.5 s margins), per component."""
    x = np.column_stack([_comp(_seg(audio, sr, b["t0"] + 0.5, b["t1"] - 0.5), c) for c in comps])
    return measure.tone(x, sr, f_hint=b.get("freq_hz"), frame_s=0.5)


def sweep_response(audio, sr, b, comps=("L", "R"), per_octave: int = 12) -> dict:
    """Frequency response along a sweep: at each measuring point a window matched to the momentary frequency.

    The sweep trajectory (log f over t, from classify) tells when the sweep passes each target frequency. The
    window is ~10 periods long (40 ms … 1.2 s); the level is the power summed over the window's main lobe,
    widened by the distance the sweep travels within the window. Returns {"comps", "points": [[f, dB, …]]}."""
    fit = np.array(b["fit"])
    t0, t1 = b.get("span") or (b["t0"] + EDGE_S, b["t1"] - EDGE_S)
    tt = np.linspace(t0, t1, 2000)
    lf = np.polyval(fit, tt)
    if lf[-1] < lf[0]:
        tt, lf = tt[::-1], lf[::-1]
    if np.any(np.diff(lf) <= 0):                      # np.interp needs a monotonic frequency axis
        return {"comps": list(comps), "points": []}
    lo, hi = lf[0], lf[-1]
    targets = np.arange(np.ceil(lo * per_octave), np.floor(hi * per_octave) + 1) / per_octave
    xs = [_comp(audio, c) for c in comps]
    rate = np.abs(np.polyval(np.polyder(fit), tt))        # octaves per second
    pts = []
    for lt in targets:
        te = float(np.interp(lt, lf, tt))
        fe = 2 ** lt
        tw = float(np.clip(10 / fe, 0.04, 1.2))
        half = int(tw * sr / 2)
        i0, i1 = int(te * sr) - half, int(te * sr) + half
        if i0 < int(t0 * sr) - half // 2 or i1 > int(t1 * sr) + half // 2 or i0 < 0 or i1 > len(audio):
            continue
        n = i1 - i0
        w = signal.windows.blackmanharris(n, sym=False)
        w2 = np.sum(w ** 2)
        df = sr / n
        oct_s = float(np.interp(te, tt, rate)) if tt[0] < tt[-1] else float(np.interp(te, tt[::-1], rate[::-1]))
        extra = int(np.ceil(fe * oct_s * np.log(2) * tw / df))     # bins the sweep moves through in the window
        hb = measure.BH_LOBE + 1 + extra
        row = [round(fe, 2)]
        ok = True
        for x in xs:
            p = np.abs(np.fft.rfft(x[i0:i1] * w)) ** 2 * (2 / (n * w2))
            k = int(round(fe / df))
            lo_k, hi_k = max(1, int(fe * 0.88 / df)), min(len(p) - 2, int(np.ceil(fe * 1.12 / df)))
            if hi_k <= lo_k:
                ok = False
                break
            k = lo_k + int(np.argmax(p[lo_k:hi_k + 1]))
            row.append(round(float(measure.db(2 * p[max(0, k - hb):k + hb + 1].sum())), 2))
        if ok:
            pts.append(row)
    return {"comps": list(comps), "points": pts}


def _rel_curve(resp: dict, ref_lo: float, ref_hi: float) -> np.ndarray:
    """Response points relative to their median level between ref_lo and ref_hi (Hz)."""
    pts = np.array(resp["points"], dtype=float)
    if not len(pts):
        return pts
    m = (pts[:, 0] >= ref_lo) & (pts[:, 0] <= ref_hi)
    ref = np.median(pts[m, 1:], axis=0) if m.any() else pts[0, 1:]
    out = pts.copy()
    out[:, 1:] -= ref
    return out


def fit_resonance(f: np.ndarray, rel_db: np.ndarray) -> dict | None:
    """Fit the arm/cartridge resonance to a vertical sweep response.

    Model: second-order high-pass (below the resonance arm and cartridge move together, so the stylus tracks
    less and less; at f0 the response peaks by an amount set by Q). Grid search over f0 and Q with a free gain,
    using only points up to 150 Hz. Returns f0_hz, q, peak_db, fit_rms_db, gain_db, or None if the sweep
    does not reach low enough."""
    m = (f <= 150) & np.isfinite(rel_db)
    if m.sum() < 6 or f[m].min() > 18:
        return None
    ff, yy = f[m], rel_db[m]
    f0s = np.arange(4.0, 30.0, 0.05)
    qs = np.arange(0.4, 6.01, 0.05)
    u = (ff[None, None, :] / f0s[:, None, None]) ** 2
    model = 20 * np.log10(u / np.sqrt((1 - u) ** 2 + u / qs[None, :, None] ** 2))
    g = np.mean(yy - model, axis=2, keepdims=True)
    err = np.sqrt(np.mean((yy - model - g) ** 2, axis=2))
    i, j = np.unravel_index(np.argmin(err), err.shape)
    f0, q = float(f0s[i]), float(qs[j])
    # peak of a 2nd-order high-pass above its asymptote; no peak for Q ≤ 1/√2
    peak_db = float(20 * np.log10(q / np.sqrt(1 - 1 / (4 * q * q)))) if q > 0.7072 else 0.0
    return {"f0_hz": round(f0, 2), "q": round(q, 2), "peak_db": round(peak_db, 2),
            "fit_rms_db": round(float(err[i, j]), 2), "gain_db": round(float(g[i, j, 0]), 2)}


def ramp_measure(audio, sr, b, cfg: AlignmentConfig) -> dict:
    """Anti-skating ramp: level and distortion per channel along the ramp, break-up point per channel.

    The ramp (AAPT 1: 315 Hz from 0 to +12 dB) is measured in 0.2 s windows. A channel "breaks up" where its
    distortion exceeds cfg.thd_limit_pct, or where it jumps by more than cfg.knee_db above the trend of the
    lower half of the ramp, for at least three windows in a row. break_db is the level (dB above the start of
    the ramp) of the break-up, None if the channel stays clean.

    Distortion is judged from the harmonics (THD), not THD+N: THD+N contains hum and groove noise, which at
    low levels fake a "break-up from +0 dB" (seen on a real run: 50 Hz hum 38 dB below the tone gave
    THD+N 2.5–3.4 % at the start of the ramp, while the harmonics were at 0.2 %)."""
    f0 = b["freq_hz"]
    x = _seg(audio, sr, b["t0"] + EDGE_S, b["t1"] - EDGE_S)
    win, hop = int(0.2 * sr), int(0.1 * sr)
    rows = []
    for i in range(0, len(x) - win, hop):
        r = measure.tone(x[i:i + win], sr, f_hint=f0, search=0.08, frame_s=None)
        rows.append(r["level_db"] + r["thd_pct"] + r["thdn_pct"])
    a = np.array(rows)
    if len(a):
        # The band can still contain lead-in groove before the ramp (after set-down: 5 s of record warps at
        # 10–17 Hz, the tone at −78 dBFS) → measure only from the tone on, otherwise the baseline is wrong
        on = np.flatnonzero(np.all(a[:, :2] >= np.median(a[:, :2], axis=0) - 20.0, axis=1))
        if on.size:
            a = a[on[0]:on[-1] + 1]
    if len(a) < 10:
        return {"ok": False}
    lev, thd, thdn = a[:, :2], a[:, 2:4], a[:, 4:6]
    n0 = max(3, int(1.5 / 0.1))
    rel = lev - np.median(lev[:n0], axis=0)
    xpos = ndimage.median_filter(np.max(rel, axis=1), size=5, mode="nearest")
    thd_s = ndimage.median_filter(thd, size=(3, 1), mode="nearest")
    thdn_s = ndimage.median_filter(thdn, size=(3, 1), mode="nearest")
    top = float(np.max(xpos))
    lower = xpos <= top / 2
    breaks, base = [], []
    for c in range(2):
        b0 = float(np.median(thd_s[:n0, c]))
        base.append(b0)
        # Trend of the lower half of the ramp (tracing distortion grows with the level) → a knee above it.
        # Tracing distortion (k2) grows in proportion to the amplitude, i.e. by at least 1 dB per dB of level.
        # If the lower half sits on the noise floor, the fitted trend would be flat and the normal rise at the
        # top would count as a knee (seen on a real run: "right from +12.8 dB" at 1.4 %).
        lt = 20 * np.log10(np.maximum(thd_s[:, c], 1e-4))
        if lower.sum() > 5:
            slope = max(float(np.polyfit(xpos[lower], lt[lower], 1)[0]), 1.0)
            trend = slope * xpos + float(np.median(lt[lower] - slope * xpos[lower]))
        else:
            trend = np.full_like(lt, lt[:n0].mean())
        bad = (thd_s[:, c] > cfg.thd_limit_pct) | ((lt - trend > cfg.knee_db) & (thd_s[:, c] > 1.0))
        hit = None
        for k in range(n0, len(bad) - 2):
            if bad[k] and bad[k + 1] and bad[k + 2]:
                hit = k
                break
        breaks.append(None if hit is None else round(float(xpos[hit]), 2))
    step = max(1, len(a) // 300)
    curve = [[round(float(xpos[k]), 2), round(float(thd_s[k, 0]), 3), round(float(thd_s[k, 1]), 3)]
             for k in range(0, len(a), step)]
    return {"ok": True, "freq_hz": round(f0, 1), "top_db": round(top, 2), "break_db": breaks,
            "limit_pct": cfg.thd_limit_pct,
            "baseline_thd_pct": [round(v, 3) for v in base],
            "top_thd_pct": [round(float(np.median(thd_s[-10:, c])), 3) for c in range(2)],
            "baseline_thdn_pct": [round(float(np.median(thdn_s[:n0, c])), 3) for c in range(2)],
            "top_thdn_pct": [round(float(np.median(thdn_s[-10:, c])), 3) for c in range(2)],
            "start_level_db": [round(float(v), 2) for v in np.median(lev[:n0], axis=0)],
            "peak_level_db": [round(float(v), 2) for v in np.max(lev, axis=0)],
            "curve": curve}


def imd_measure(audio, sr, b) -> dict:
    """Intermodulation per SMPTE: band-pass the 4 kHz carrier, take its envelope and measure how strongly the
    60 Hz tone modulates it (RMS of the envelope's low-frequency part / mean envelope, in %). Tracing errors
    from a wrong vertical tracking angle (arm height) show up as such modulation."""
    f_lo, f_hi = b["freq_hz"], b["freq2_hz"]
    x = _seg(audio, sr, b["t0"] + 0.5, b["t1"] - 0.5)
    sos = signal.butter(4, [f_hi - 5 * f_lo, f_hi + 5 * f_lo], btype="band", fs=sr, output="sos")
    lp = signal.butter(4, 5 * f_lo, fs=sr, output="sos")
    out = []
    for c in range(2):
        y = signal.sosfiltfilt(sos, x[:, c])
        env = np.abs(signal.hilbert(y))
        cut = int(0.2 * sr)
        env = env[cut:-cut] if len(env) > 3 * cut else env
        ac = signal.sosfiltfilt(lp, env - env.mean())
        # Median over 2 s blocks: the tone fading in at the band edge (0.3 s from 0 to full level) raised the
        # RMS over 120 s from 2.2 to 5.5 % when the band was detected 0.8 s early
        blk = 2 * sr
        if len(env) >= 2 * blk:
            v = [np.sqrt(np.mean(ac[i:i + blk] ** 2)) / max(float(np.mean(env[i:i + blk])), 1e-12)
                 for i in range(0, len(env) - blk + 1, blk)]
            out.append(round(float(100 * np.median(v)), 3))
        else:
            out.append(round(float(100 * np.sqrt(np.mean(ac ** 2)) / max(env.mean(), 1e-12)), 3))
    return {"imd_pct": out, "f_lo_hz": round(f_lo, 1), "f_hi_hz": round(f_hi, 1)}


def wf_measure(audio, sr, b) -> dict:
    """Speed and wow & flutter from the 3150 Hz band (measure.flutter), plus a decimated trace for plotting."""
    fmed = b["freq_hz"]
    nominal = min(NOMINAL_WF, key=lambda x: abs(fmed / x - 1))
    x = _comp(_seg(audio, sr, b["t0"] + 0.5, b["t1"] - 0.5), "M")
    fi, rate = measure.inst_freq(x, sr, fmed)
    cut = int(0.5 * rate)
    fi = fi[cut:-cut] if len(fi) > 3 * cut else fi
    res = measure.flutter(fi, rate, nominal, NOMINAL_WF[nominal])
    step = max(1, int(rate / 20))
    dev = (fi[::step] / np.mean(fi) - 1) * 100
    res["trace"] = {"rate": rate / step, "dev_pct": [round(float(v), 4) for v in dev[:2400]]}
    res["rpm"] = round(NOMINAL_WF[nominal], 3)
    return res


def silent_measure(audio, sr, t0: float, t1: float, ref_db: float | None) -> dict:
    """Silent groove: rumble, mains hum, A-weighted noise, low-frequency peaks (arm resonance) and the most
    prominent spectral line. Values in dBFS and, given the side's reference level ref_db, relative to it."""
    x = _seg(audio, sr, t0 + 1.0, t1 - 1.0)
    if len(x) < 4 * sr:
        return {"ok": False}
    # Drive rumble 20–100 Hz. Below that are the record warps that excite the arm at its resonance; they depend
    # on how the record lies (two runs: 10–100 Hz −41/−32 dB, 20–100 Hz −42.9/−42.7 dB). The warps are reported
    # separately in lf_peaks.
    # Median over 2 s blocks instead of the mean: knocks from the run-out spiral or the stylus lift that are
    # still inside the section don't count (mean −33.9 dB, without the run-out −42.7 dB).
    # Mains hum (50/100 Hz ±1.5 Hz) separately: it is electrical, not mechanical (hum −45.0 dB in two runs,
    # rumble without the lines −48.9/−47.7 dB, both together −43 dB)
    blk = int(2.0 * sr)
    mech, hum = [], []
    for i in range(0, len(x) - blk + 1, blk):
        fr, pb = measure.spectrum(x[i:i + blk], sr, 2.0)
        band = (fr >= 20) & (fr <= 100)
        lines = (np.abs(fr - 50) <= 1.5) | (np.abs(fr - 100) <= 1.5)
        mech.append(pb[band & ~lines].sum(axis=0))
        hum.append(pb[band & lines].sum(axis=0))
    rumble = float(np.mean(measure.db(np.median(np.array(mech), axis=0))))
    hum_lvl = float(np.mean(measure.db(np.median(np.array(hum), axis=0))))
    wide = float(np.mean(measure.db(measure.band_power(x, sr, 20, 20000, 1.0))))
    aw = a_weighting_sos(sr)
    hp = signal.butter(2, 20, btype="high", fs=sr, output="sos")
    a_pow = [float(np.mean(signal.sosfilt(aw, signal.sosfilt(hp, x[:, c]))[sr // 2:] ** 2)) for c in range(2)]
    noise_a = float(np.mean(measure.db(np.array(a_pow))))
    ms = np.column_stack([_comp(x, "M"), _comp(x, "S")])
    f, p = measure.spectrum(ms, sr, 4.0)
    m = (f >= 3) & (f <= 30)
    peaks = {}
    for c, name in enumerate(("lateral", "vertikal")):
        pd = measure.db(p[m, c])
        k = int(np.argmax(pd))
        peaks[name] = {"freq_hz": round(float(f[m][k]), 2), "prominence_db": round(float(pd[k] - np.median(pd)), 1)}
    # narrow line (hum, motor): prominence over its ±10 Hz surroundings, not over the whole band
    f2, p2 = measure.spectrum(x[:, 0] + x[:, 1], sr, 2.0)
    pd2 = measure.db(p2[:, 0])
    local = ndimage.median_filter(pd2, size=int(20 / (f2[1] - f2[0])) | 1, mode="nearest")
    m2 = (f2 >= 30) & (f2 <= 400)
    prom = (pd2 - local)[m2]
    k2 = int(np.argmax(prom))
    line = {"freq_hz": round(float(f2[m2][k2]), 1), "prominence_db": round(float(prom[k2]), 1)}
    line["hum"] = bool(abs(line["freq_hz"] / 50 - round(line["freq_hz"] / 50)) * 50 < 1.5)
    # ref_db is the amplitude of the reference tone, rumble and noise are RMS values (mean power).
    # RMS against RMS: reference − 3.01 dB.
    rel = (lambda v: None if ref_db is None else round(v - (ref_db - 3.01), 2))
    return {"ok": True, "rumble_dbfs": round(rumble, 2), "wide_dbfs": round(wide, 2),
            "noise_a_dbfs": round(noise_a, 2), "rumble_db": rel(rumble), "noise_a_db": rel(noise_a),
            "hum_dbfs": round(hum_lvl, 2), "hum_db": rel(hum_lvl),
            "lf_peaks": peaks, "line": line, "ref_db": ref_db}


def click_floor(audio, sr, t0: float, t1: float, ref_db: float | None, cfg: AlignmentConfig,
                dp: detect.DetectParams | None = None) -> dict:
    """Click floor: clicks and crackle per minute that the detector finds in the silent groove of the test record.

    This is what stylus, cartridge and record of this chain bring along before a record side contributes
    anything (two real chains differed by a factor of 30: 678 vs. 22 per minute). Classified as in the record
    check: a click from strength `click_strength` or from the pop threshold relative to the reference tone,
    the rest is crackle."""
    from .analysis import AnalysisConfig
    x = _seg(audio, sr, t0 + 1.0, t1 - 1.0)
    if len(x) < 10 * sr:
        return {"ok": False}
    dp = dp or detect.DetectParams()
    acfg = AnalysisConfig()
    ev = detect.merge_channels([detect.detect_channel(x[:, c], sr, dp) for c in range(x.shape[1])], dp.merge_ms)
    pop_thr = acfg.pop_dbfs if ref_db is None else ref_db + (acfg.pop_dbfs - cfg.target_ref_dbfs)
    strong = (ev["strength"] >= acfg.click_strength) | (ev["amp_dbfs"] >= pop_thr)
    minutes = len(x) / sr / 60
    return {"ok": True, "dur_s": round(len(x) / sr, 1), "events": int(len(ev)),
            "clicks_per_min": round(float(strong.sum()) / minutes, 1),
            "ticks_per_min": round(float((~strong).sum()) / minutes, 1),
            "threshold": dp.threshold, "stripe_db": dp.stripe_db}


# ─── Evaluation ───────────────────────────────────────────────────────────────
def _item(key, label, value, unit, status, text, band=None, soll=None, advice=None) -> dict:
    """One finding. `text` describes neutrally; `advice` is the recommendation, shown only for warn/bad
    (apply_targets appends it to the text), so that switching the target map never leaves a wrong sentence.
    label, text, advice and soll (the target) are i18n messages; soll defaults to the standard map's target."""
    return {"key": key, "label": label, "value": value, "unit": unit, "status": status, "text": text,
            "band": band, "soll": soll if soll is not None else target_text(key), "advice": advice}


def evaluate(bands: list[dict], silent: dict | None, cfg: AlignmentConfig) -> tuple[list[dict], dict]:
    """Measurements → findings (target, status, advice) and the summary metrics for comparing runs.

    Bands whose measurement failed (m = {"ok": False, …}) are left out; the others are still evaluated.
    The status is judged with the standard map; the display re-judges against the selected map."""
    by = {b["role"]: b for b in bands
          if b.get("role") and isinstance(b.get("m"), dict) and b["m"].get("ok", True) and not b["m"].get("error")}
    items: list[dict] = []
    summ: dict = {}

    def put(key, v):
        if v is not None:
            summ[key] = v

    # Anti-skating + tracking headroom: the most important part, it decides whether the stylus stays in the groove
    if "antiskating" in by and by["antiskating"]["m"].get("ok"):
        b = by["antiskating"]
        m = b["m"]
        bl, br = m["break_db"]
        top = m["top_db"]
        put("antiskate_break_l_db", bl)
        put("antiskate_break_r_db", br)
        put("thd_top_l_pct", m["top_thd_pct"][0])
        put("thd_top_r_pct", m["top_thd_pct"][1])
        put("thdn_top_l_pct", m["top_thdn_pct"][0])
        put("thdn_top_r_pct", m["top_thdn_pct"][1])
        reserve = min(v for v in (bl, br, top) if v is not None)
        put("tracking_reserve_db", round(reserve, 2))
        clean = [v is None or v >= top - 0.5 for v in (bl, br)]   # a break-up only in the last ½ dB doesn't count

        def other(v):        # how the channel that did not break up first behaves
            return (msg("alignment.result.antiskating.other_clean", db=top) if v is None
                    else msg("alignment.result.antiskating.other_from", db=v))

        # Verdict values are stored: "ok", "zu stark" (too strong), "zu schwach" (too weak), "ausgeglichen" (balanced)
        if all(clean):
            st, verdict = "ok", "ok"
            txt = msg("alignment.result.antiskating.clean", top=top)
        elif br is None or (bl is not None and br - bl > cfg.same_db):
            # left (inner wall) breaks up first: too much anti-skating force pushes the stylus outwards
            st, verdict = "warn", "zu stark"
            txt = msg("alignment.result.antiskating.too_strong", left=bl, right=other(br))
        elif bl is None or (br is not None and bl - br > cfg.same_db):
            st, verdict = "warn", "zu schwach"
            txt = msg("alignment.result.antiskating.too_weak", right=br, left=other(bl))
        else:
            # both break up together: the bias is right, but the tracking force is too low for the ramp
            st, verdict = ("warn" if reserve >= 6 else "bad"), "ausgeglichen"
            txt = msg("alignment.result.antiskating.balanced", left=bl, right=br, reserve=reserve)
        put("antiskate_diff_db", None if (bl is None or br is None) else round(bl - br, 2))
        items.append(_item("antiskating", msg("alignment.result.antiskating.label"), verdict, "", st, txt, b["i"],
                           msg("alignment.result.antiskating.target")))
        items.append(_item("tracking_reserve_db", msg("alignment.result.tracking_reserve.label"), round(reserve, 1),
                           "dB", "ok" if reserve >= top - 0.5 else (judge("tracking_reserve_db", reserve) or "warn"),
                           msg("alignment.result.tracking_reserve.text", thd_l=m["top_thd_pct"][0],
                               thd_r=m["top_thd_pct"][1], limit=cfg.thd_limit_pct,
                               thdn_l=m["top_thdn_pct"][0], thdn_r=m["top_thdn_pct"][1]),
                           b["i"]))

    ref_level = None
    if "ref_1k" in by:
        b = by["ref_1k"]
        lv = b["m"]["level_db"]
        ref_level = float(np.mean(lv))
        bal = round(lv[0] - lv[1], 2)
        put("ref_level_dbfs", round(ref_level, 2))
        put("balance_db", bal)
        st = judge("balance_db", bal)
        if abs(bal) < 0.3:
            txt = msg("alignment.result.balance.equal")
        else:
            txt = msg("alignment.result.balance.left_louder" if bal > 0 else "alignment.result.balance.right_louder",
                      db=abs(bal))
        items.append(_item("balance_db", msg("alignment.result.balance.label"), bal, "dB", st, txt,
                           b["i"], advice=msg("alignment.result.balance.advice")))
    elif "vert_1k" in by:
        ref_level = float(np.mean(by["vert_1k"]["m"]["level_db"]))

    if ref_level is not None:
        # Input level: the reference tone should leave room for the ramp's +12 dB below full scale
        peak = ref_level + cfg.headroom_db
        if "antiskating" in by and by["antiskating"]["m"].get("ok"):
            peak = max(by["antiskating"]["m"]["peak_level_db"])
        st = judge("pegel", ref_level)
        if peak >= -1 and st == "ok":
            st = "warn"
        txt = msg("alignment.result.ref_level.text", ref=ref_level, peak=peak)
        if peak >= -1:
            adv = msg("alignment.result.ref_level.advice_hot")
        elif ref_level < STANDARD_TARGETS["pegel"]["ok"][0]:
            adv = msg("alignment.result.ref_level.advice_quiet")
        else:
            adv = msg("alignment.result.ref_level.advice_loud")
        items.append(_item("pegel", msg("alignment.result.ref_level.label"), round(ref_level, 1), "dBFS", st, txt,
                           by.get("ref_1k", by.get("vert_1k"))["i"], advice=adv))

    xl = by.get("xt_links", {}).get("m")
    xr = by.get("xt_rechts", {}).get("m")
    for key, mm, label in (("crosstalk_lr_db", xl, msg("alignment.result.crosstalk_lr.label")),
                           ("crosstalk_rl_db", xr, msg("alignment.result.crosstalk_rl.label"))):
        if mm:
            v = mm["crosstalk_db"]
            put(key, v)
            st = judge(key, v)
            items.append(_item(key, label, v, "dB", st, msg("alignment.result.crosstalk.text", db=-v),
                               by["xt_links" if mm is xl else "xt_rechts"]["i"],
                               advice=msg("alignment.result.crosstalk.advice")))
    if xl and xr:
        # Azimuth: a cartridge twisted around the groove axis raises the crosstalk in one direction only
        asym = round(xl["crosstalk_db"] - xr["crosstalk_db"], 2)
        put("azimuth_asym_db", asym)
        st = judge("azimuth_asym_db", asym)
        if abs(asym) < 0.5:
            txt = msg("alignment.result.azimuth.equal")
        else:
            txt = msg("alignment.result.azimuth.lr_stronger" if asym > 0 else "alignment.result.azimuth.rl_stronger",
                      db=abs(asym))
        items.append(_item("azimuth_asym_db", msg("alignment.result.azimuth.label"), asym, "dB", st, txt,
                           None, advice=msg("alignment.result.azimuth.advice")))

    if "vert_1k" in by:
        # vertical (out-of-phase) tone summed to mono should cancel; what remains is channel mismatch
        mc = by["vert_1k"]["m"]["mono_cancel_db"]
        put("mono_cancel_db", mc)
        st = judge("mono_cancel_db", mc)
        items.append(_item("mono_cancel_db", msg("alignment.result.mono_cancel.label"), mc, "dB", st,
                           msg("alignment.result.mono_cancel.text", db=mc),
                           by["vert_1k"]["i"], advice=msg("alignment.result.mono_cancel.advice")))

    if "resonanz" in by:
        r = by["resonanz"]["m"]
        fit = r.get("fit")
        lo, hi = STANDARD_TARGETS["resonance_hz"]["ok"]
        if fit:
            f0 = fit["f0_hz"]
            put("resonance_hz", f0)
            put("resonance_peak_db", fit["peak_db"])
            st = judge("resonance_hz", f0)
            parts = [msg("alignment.result.resonance.text", f0=f0, peak=fit["peak_db"], q=fit["q"])]
            if lo <= f0 <= hi:
                parts.append(msg("alignment.result.resonance.in_band"))
            elif f0 < lo:
                parts.append(msg("alignment.result.resonance.too_low"))
            elif f0 <= 15:
                parts.append(msg("alignment.result.resonance.slightly_high"))
            else:
                parts.append(msg("alignment.result.resonance.very_high"))
            # Record warps excite the arm in the silent groove: its low-frequency peak confirms the resonance
            lfp = (by.get("stille_rille", {}).get("m") or {}).get("lf_peaks") or {}
            if lfp.get("vertikal"):
                vert = lfp["vertikal"]["freq_hz"]
                parts.append(msg("alignment.result.resonance.silent_both", vert=vert, lat=lfp["lateral"]["freq_hz"])
                             if lfp.get("lateral") else msg("alignment.result.resonance.silent_vertical", vert=vert))
            if r.get("at_end"):
                parts.append(msg("alignment.result.resonance.at_end"))
            if fit["fit_rms_db"] > 1.5:
                parts.append(msg("alignment.result.resonance.poor_fit", rms=fit["fit_rms_db"]))
            items.append(_item("resonance_hz", msg("alignment.result.resonance.label"), f0, "Hz", st,
                               msg_join(parts), by["resonanz"]["i"]))
        else:
            items.append(_item("resonance_hz", msg("alignment.result.resonance.label"), None, "Hz", "info",
                               msg("alignment.result.resonance.no_fit"), by["resonanz"]["i"]))

    if "gleichlauf" in by:
        w = by["gleichlauf"]["m"]
        sp, wf = w["speed_pct"], w["wf_weighted_2s_pct"]
        put("speed_pct", sp)
        put("wf_weighted_pct", wf)
        put("wf_drive_pct", w["drive_weighted_2s_pct"])
        put("eccentricity_pct", w["eccentricity_pct"])
        st = judge("speed_pct", sp)
        items.append(_item("speed_pct", msg("alignment.result.speed.label"), sp, "%", st,
                           msg("alignment.result.speed.text", freq=w["freq_hz"], nominal=w["nominal_hz"],
                               cents=w["cents"]),
                           by["gleichlauf"]["i"],
                           advice=msg("alignment.result.speed.advice_fast" if sp > 0
                                      else "alignment.result.speed.advice_slow")))
        st = judge("wf_weighted_pct", wf)
        items.append(_item("wf_weighted_pct", msg("alignment.result.wf.label"), wf, "%", st,
                           msg("alignment.result.wf.text", ecc=w["eccentricity_pct"],
                               drive=w["drive_weighted_2s_pct"]),
                           by["gleichlauf"]["i"], advice=msg("alignment.result.wf.advice")))

    if "imd" in by:
        v = max(by["imd"]["m"]["imd_pct"])
        put("imd_pct", v)
        st = judge("imd_pct", v)
        items.append(_item("imd_pct", msg("alignment.result.imd.label"), v, "%", st,
                           msg("alignment.result.imd.text", left=by["imd"]["m"]["imd_pct"][0],
                               right=by["imd"]["m"]["imd_pct"][1]),
                           by["imd"]["i"]))

    # Treble at 10 kHz: preferably from the sweep. The single tones are at −20 dB, where noise and crackle make
    # up 10–15 %, and the value scattered by 2 dB between two runs; the sweep's by 0.5 dB
    tone_d = None
    if "hf_10k" in by and "ref_1k_m20" in by:
        tone_d = [round(a - b, 2) for a, b in zip(by["hf_10k"]["m"]["level_db"], by["ref_1k_m20"]["m"]["level_db"])]
    sweep_d = None
    if "sweep_hoch" in by and by["sweep_hoch"]["m"].get("curve"):
        c = np.array([[np.nan if v is None else v for v in row] for row in by["sweep_hoch"]["m"]["curve"]], float)
        c = c[np.argsort(c[:, 0])]
        if c[:, 0].min() <= 9000 and c[:, 0].max() >= 11000:
            sweep_d = []
            for k in (1, 2):
                ok = np.isfinite(c[:, k])
                sweep_d.append(round(float(np.interp(np.log(10000), np.log(c[ok, 0]), c[ok, k])), 2) if ok.sum() >= 2 else None)
            if None in sweep_d:
                sweep_d = None
    d = sweep_d or tone_d
    if d:
        v = round(float(max(d, key=abs)), 2)                 # worse channel (value and status belong together)
        put("hf_10k_db", v)
        st = judge("hf_10k_db", v)
        src = msg("alignment.result.hf_10k.from_sweep" if sweep_d else "alignment.result.hf_10k.from_tone")
        txt = msg("alignment.result.hf_10k.text", left=d[0], right=d[1], source=src)
        if sweep_d and tone_d:
            txt = msg_join([txt, msg("alignment.result.hf_10k.single_tone", left=tone_d[0], right=tone_d[1])])
        items.append(_item("hf_10k_db", msg("alignment.result.hf_10k.label"), v, "dB", st, txt,
                           by["hf_10k"]["i"] if "hf_10k" in by else by["sweep_hoch"]["i"],
                           advice=msg("alignment.result.hf_10k.advice")))
    if "lf_100" in by and "ref_1k" in by:
        d = [round(a - b, 2) for a, b in zip(by["lf_100"]["m"]["level_db"], by["ref_1k"]["m"]["level_db"])]
        v = round(float(max(d, key=abs)), 2)
        put("lf_100_db", v)
        st = judge("lf_100_db", v)
        items.append(_item("lf_100_db", msg("alignment.result.lf_100.label"), v, "dB", st,
                           msg("alignment.result.lf_100.text", left=d[0], right=d[1]),
                           by["lf_100"]["i"], advice=msg("alignment.result.lf_100.advice")))
    # Treble only up to 16 kHz: the 18 kHz step of the AAPT 1 sweep lies 8–10 dB below 16 and 20 kHz on this
    # pressing, identically on two different turntables, so it is a property of the record
    for role, key, label, rng in (
            ("sweep_hoch", "sweep_hf_dev_db", msg("alignment.result.sweep_hf.label"), (1000, 16000)),
            ("sweep_tief", "sweep_lf_dev_db", msg("alignment.result.sweep_lf.label"), (40, 1000))):
        if role in by and by[role]["m"].get("curve"):
            c = np.array(by[role]["m"]["curve"])
            m = (c[:, 0] >= rng[0] * 0.97) & (c[:, 0] <= rng[1] * 1.03)
            if m.any():
                dev = c[m, 1:]
                k = np.unravel_index(np.argmax(np.abs(dev)), dev.shape)
                v = round(float(dev[k]), 2)
                put(key, v)
                st = judge(key, v)
                f_dev = float(c[m, 0][k[0]])
                channel = msg("alignment.channel.left" if k[1] == 0 else "alignment.channel.right")
                parts = [msg("alignment.result.sweep_dev.khz", db=v, f=f_dev / 1000, channel=channel) if f_dev >= 1000
                         else msg("alignment.result.sweep_dev.hz", db=v, f=f_dev, channel=channel)]
                if role == "sweep_hoch":
                    top = c[(c[:, 0] >= 19000) & (c[:, 0] <= 21000)]
                    if len(top):
                        parts.append(msg("alignment.result.sweep_dev.hf_top", left=float(np.mean(top[:, 1])),
                                         right=float(np.mean(top[:, 2]))))
                if role == "sweep_tief":
                    low = c[c[:, 0] < 40]
                    if len(low):
                        parts.append(msg("alignment.result.sweep_dev.lf_low", db=float(np.max(low[:, 1:]))))
                items.append(_item(key, label, v, "dB", st, msg_join(parts), by[role]["i"]))

    if silent and silent.get("ok"):
        fl = silent.get("clicks") or {}
        if fl.get("ok"):
            put("floor_clicks_per_min", fl["clicks_per_min"])
            put("floor_ticks_per_min", fl["ticks_per_min"])
            items.append(_item("floor_clicks_per_min", msg("alignment.result.click_floor.label"), fl["clicks_per_min"],
                               "/min", "info",
                               msg("alignment.result.click_floor.text", clicks=fl["clicks_per_min"],
                                   ticks=fl["ticks_per_min"], dur=fl["dur_s"]),
                               silent.get("i"), soll=""))
        if silent.get("rumble_db") is not None:
            put("rumble_db", silent["rumble_db"])
            put("noise_a_db", silent["noise_a_db"])
            v = silent["rumble_db"]
            put("hum_db", silent.get("hum_db"))
            st = judge("rumble_db", v)
            parts = [msg("alignment.result.rumble.text", rumble=v, noise=silent["noise_a_db"])]
            if silent.get("hum_db") is not None:
                parts.append(msg("alignment.result.rumble.hum", db=silent["hum_db"]))
        else:
            v, st = silent["rumble_dbfs"], "info"
            parts = [msg("alignment.result.rumble.no_ref", dbfs=v)]
        ln = silent["line"]
        if ln["prominence_db"] >= 10:                # a narrow line ≥ 10 dB above its surroundings
            parts.append(msg("alignment.result.rumble.line_hum" if ln["hum"] else "alignment.result.rumble.line_motor",
                             freq=ln["freq_hz"]))
        pv = silent["lf_peaks"]["vertikal"]
        if pv["prominence_db"] >= 6:
            parts.append(msg("alignment.result.rumble.vertical_peak", freq=pv["freq_hz"]))
        items.append(_item("rumble_db", msg("alignment.result.rumble.label"), v, "dB" if st != "info" else "dBFS", st,
                           msg_join(parts), silent.get("i"), soll="" if st == "info" else None,
                           advice=msg("alignment.result.rumble.advice")))

    return apply_targets(items, STANDARD_TARGETS), summ


def _before_end_groove(audio: np.ndarray, sr: int, t0: float, t1: float) -> float:
    """End of a quiet section before the locked groove, where the groove repeats every revolution.

    Since the band search ignores record warps, the run-out and locked groove are "silent" too (e.g. side 2:
    silent groove up to 680 s, then 20 s of locked groove). The repeat detection breaks up into pieces there
    (clicks, score just above the threshold): the locked groove starts at the first piece from which the
    repetitions cover at least half of the remaining time."""
    T = skips.revolution_s(100 / 3)
    ev = skips.find_repeats(audio[int(t0 * sr):int(t1 * sr)], sr, 100 / 3)
    for i, x in enumerate(ev):
        cover = sum(y["until"] - y["t"] for y in ev[i:])
        if cover >= 3 * T and cover >= 0.5 * (t1 - t0 - x["t"]):
            return t0 + max(0.0, x["t"] - 1.0)
    return t1


def analyze(audio: np.ndarray, sr: int, cfg: AlignmentConfig = AlignmentConfig(), chain_db: float | None = None,
            overview_points: int = 3000, detect_params: detect.DetectParams | None = None) -> dict:
    """Evaluate one side of a test record.

    audio: (n,) or (n, channels) float in ±1; sr: sample rate; chain_db: chain noise from the calibration.
    detect_params: the profile's detector setting (sensitivity), so that the click floor is measured with the
    same setting as the records later on.
    Returns the same structure as analysis.analyze() (metrics/grading/warnings/events/overview), so storage
    and retention work the same way; metrics["kind"] is "justage"."""
    mono = audio.ndim == 1 or audio.shape[1] == 1
    if mono:
        audio = np.column_stack([audio.ravel(), audio.ravel()])
    n, nch = audio.shape
    clip = int(np.sum(np.abs(audio) >= 0.99995))
    peak = [float(20 * np.log10(max(np.max(np.abs(audio[:, c])), 1e-12))) for c in range(nch)]
    fb = find_bands(audio, sr, chain_db)
    F = fb["frames"]
    bands = []
    for s, e in fb["bands"]:
        b = classify(F, s, e, audio, sr)
        bands.append(b)
    sil = None
    silent = []
    for s, e in fb["silent"]:
        e = s + int(np.searchsorted(F["t"][s:e], _before_end_groove(audio, sr, float(F["t"][s]), float(F["t"][e - 1]))))
        if (e - s) * HOP_S >= MIN_SILENT_GROOVE_S:
            silent.append((s, e))
    if silent:
        s, e = max(silent, key=lambda x: x[1] - x[0])
        # 2 s margin: don't measure the set-down/lift-off of the stylus and the settling after it (otherwise the
        # silent groove ran into the lift-off and the rumble came out 5 dB too high)
        edge = int(round(2.0 / HOP_S))
        if (e - s) * HOP_S >= 30.0:                    # only for long grooves (AAPT 1: ≈ 2 min)
            s, e = s + edge, e - edge
        sil = {"t0": round(float(F["t"][s]), 2), "t1": round(float(F["t"][e - 1]), 2), "mode": "–",
               "type": "stille", "level_db": round(float(np.median(F["lv"][s:e])), 2)}
        sil["dur_s"] = round(sil["t1"] - sil["t0"], 2)
        bands.append(sil)
    bands.sort(key=lambda b: b["t0"])
    for i, b in enumerate(bands):
        b["i"] = i + 1
    assign_roles(bands)

    for b in bands:
        role = b.get("role")
        try:
            if role in ("ref_1k", "ref_1k_m20", "hf_10k", "lf_100"):
                r = _tone_band(audio, sr, b)
                b["m"] = {"level_db": [round(v, 2) for v in r["level_db"]], "thd_pct": [round(v, 3) for v in r["thd_pct"]],
                          "thdn_pct": [round(v, 3) for v in r["thdn_pct"]], "freq_hz": round(r["freq_hz"], 2)}
            elif role in ("xt_links", "xt_rechts"):
                # one channel driven: crosstalk = level of the other channel relative to the driven one
                r = _tone_band(audio, sr, b)
                drv, oth = (0, 1) if role == "xt_links" else (1, 0)
                b["m"] = {"level_db": [round(v, 2) for v in r["level_db"]],
                          "crosstalk_db": round(r["level_db"][oth] - r["level_db"][drv], 2)}
            elif role == "vert_1k":
                r = _tone_band(audio, sr, b, ("L", "R", "M", "S"))
                b["m"] = {"level_db": [round(v, 2) for v in r["level_db"][:2]],
                          "mono_cancel_db": round(r["level_db"][2] - r["level_db"][3], 2)}
            elif role == "antiskating":
                b["m"] = ramp_measure(audio, sr, b, cfg)
            elif role == "imd":
                b["m"] = imd_measure(audio, sr, b)
            elif role == "gleichlauf":
                b["m"] = wf_measure(audio, sr, b)
            elif role in ("sweep_hoch", "sweep_tief"):
                resp = sweep_response(audio, sr, b, ("L", "R"))
                ref = (900, 1150)                      # relative to the level around 1 kHz
                b["m"] = {"curve": [[round(v, 2) for v in row] for row in _rel_curve(resp, *ref).tolist()]}
            elif role == "resonanz":
                # the resonance sweep is vertical: measure the side (L − R) component
                resp = sweep_response(audio, sr, b, ("S",), per_octave=24)
                rel = _rel_curve(resp, 100, 400)
                fit = fit_resonance(rel[:, 0], rel[:, 1]) if len(rel) else None
                at_end = bool(len(rel) and np.argmax(rel[:, 1]) == np.argmin(rel[:, 0]))
                b["m"] = {"curve": [[round(v, 2) for v in row] for row in rel.tolist()], "fit": fit,
                          "at_end": at_end}
            elif role in ("rosa_lateral", "rosa_vertikal"):
                x = _seg(audio, sr, b["t0"] + 0.5, b["t1"] - 0.5)
                pw = measure.band_power(x, sr, 20, 20000, 0.5)
                b["m"] = {"balance_db": round(float(measure.db(pw[0]) - measure.db(pw[1])), 2)}
        except Exception as e:  # noqa: BLE001 — one broken band must not prevent the rest
            b["m"] = {"ok": False, "error": str(e)}

    side_ref = None
    by = {b.get("role"): b for b in bands}
    if "ref_1k" in by and "level_db" in by["ref_1k"].get("m", {}):
        side_ref = float(np.mean(by["ref_1k"]["m"]["level_db"]))
    elif "vert_1k" in by and "level_db" in by["vert_1k"].get("m", {}):
        side_ref = float(np.mean(by["vert_1k"]["m"]["level_db"]))
    silent_res = None
    if sil is not None:
        silent_res = silent_measure(audio, sr, sil["t0"], sil["t1"], side_ref)
        silent_res["i"] = sil["i"]
        try:
            silent_res["clicks"] = click_floor(audio, sr, sil["t0"], sil["t1"], side_ref, cfg, detect_params)
        except Exception as e:  # noqa: BLE001 — the click floor must never stop the alignment evaluation
            silent_res["clicks"] = {"ok": False, "error": str(e)}
        sil["m"] = silent_res

    items, summary = evaluate(bands, silent_res, cfg)
    for b in bands:
        b["label"] = ROLE_LABEL.get(b.get("role"), TYPE_LABEL.get(b.get("type"), "–"))
        b.pop("fit", None)

    warnings = []
    if mono:
        warnings.append(msg("alignment.warn.mono"))
    inside = 0
    if clip:
        # Setting down or lifting the stylus often clips briefly (a few samples), which affects no measurement.
        # Only clipping inside a measurement band makes the values unusable.
        at = np.nonzero(np.abs(audio) >= 0.99995)[0] / sr
        inside = int(sum(np.sum((at >= b["t0"]) & (at <= b["t1"])) for b in bands if b.get("role")))
        if inside:
            warnings.append(msg("alignment.warn.clipped_in_bands", n=inside))
        else:
            warnings.append(msg("alignment.warn.clipped_outside", n=clip))
    for b in bands:
        if isinstance(b.get("m"), dict) and b["m"].get("error"):
            warnings.append(msg("alignment.warn.band_failed", i=b["i"], label=b["label"], error=b["m"]["error"]))
    known = [b for b in bands if b.get("role")]
    if not known:
        warnings.append(msg("alignment.warn.no_bands"))
    elif len(known) < len(bands):
        warnings.append(msg("alignment.warn.unassigned", n=len(bands) - len(known)))

    # Overview for the web interface: peak of both channels (in a mono mix the vertical bands would cancel)
    pts = max(1, min(overview_points, n // 64))
    bkt = n // pts
    peak_env = np.max(np.abs(audio[:pts * bkt]), axis=1).reshape(pts, bkt).max(axis=1)
    gs, ge = fb["groove"]
    overview = {"bucket_s": bkt / sr, "min": np.round(-peak_env, 4).tolist(),
                "max": np.round(peak_env, 4).tolist(), "quiet": [],
                "groove": [round(float(gs * HOP_S), 2), round(float(ge * HOP_S), 2)],
                "bands": [[b["t0"], b["t1"], b["i"], b.get("role") or "", b["label"]] for b in bands]}
    metrics = {"kind": "justage", "duration_s": round(n / sr, 2), "samplerate": sr, "channels": nch,
               "peak_dbfs": [round(p, 2) for p in peak], "clip_samples": clip, "clip_in_bands": inside,
               "groove_noise_dbfs": None if fb["groove_noise_dbfs"] is None else round(fb["groove_noise_dbfs"], 2),
               "bands": bands, "results": items, "summary": summary}
    return _py({"metrics": metrics, "warnings": warnings, "events": [], "overview": overview,
                "grading": {"grade": None, "score": None, "per_metric": {}, "calibrated": False, "hinweis": None}})


def analyze_file(path, cfg: AlignmentConfig = AlignmentConfig(), chain_db: float | None = None,
                 detect_params: detect.DetectParams | None = None) -> dict:
    """analyze() on an audio file."""
    import soundfile as sf
    audio, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return analyze(audio, sr, cfg, chain_db, detect_params=detect_params)


# Order of the findings: first what decides whether the stylus stays in the groove, then the rest in the order
# in which a turntable is adjusted
RESULT_ORDER = ["antiskating", "tracking_reserve_db", "pegel", "balance_db", "crosstalk_lr_db", "crosstalk_rl_db",
                "azimuth_asym_db", "mono_cancel_db", "resonance_hz", "imd_pct", "speed_pct", "wf_weighted_pct",
                "hf_10k_db", "lf_100_db", "sweep_hf_dev_db", "sweep_lf_dev_db", "rumble_db"]


def _result_key(it: dict) -> tuple:
    """Sort findings: worst status first, then RESULT_ORDER."""
    order = {"bad": 0, "warn": 1, "ok": 2, "info": 3}
    k = it["key"]
    return order.get(it["status"], 9), RESULT_ORDER.index(k) if k in RESULT_ORDER else 99


def merge_summaries(analyses: list[dict]) -> tuple[dict, list[dict]]:
    """Findings of a run made of several sides: per metric the latest recording that measures it wins.
    analyses: list of metrics dicts, oldest first. Returns (summary, findings)."""
    summary, items = {}, {}
    for m in analyses:
        summary.update(m.get("summary", {}))
        for it in m.get("results", []):
            items[it["key"]] = it
    return summary, sorted(items.values(), key=_result_key)


def compare_summaries(a: dict, b: dict) -> list[dict]:
    """Two runs side by side: per metric A, B, Δ (B − A) and which one is better ("a", "b" or None)."""
    rows = []
    for key, (label, unit, better) in SUMMARY_SPEC.items():
        va, vb = a.get(key), b.get(key)
        if va is None and vb is None:
            continue
        delta = None if va is None or vb is None else round(vb - va, 3)
        win = None
        # differences within the measurement scatter don't count as better or worse
        sig = {"dB": 0.3, "Hz": 0.3, "%": max(0.01, 0.1 * max(abs(va or 0), abs(vb or 0)))}.get(unit, 0.0)
        if delta is not None and better and abs(delta) > sig:
            if better == "lower":
                win = "b" if vb < va else "a"
            elif better == "higher":
                win = "b" if vb > va else "a"
            elif better == "abs":
                win = None if abs(vb) == abs(va) else ("b" if abs(vb) < abs(va) else "a")
            elif isinstance(better, tuple):
                win = "b" if abs(vb - better[1]) < abs(va - better[1]) else "a"
        rows.append({"key": key, "label": label, "unit": unit, "a": va, "b": vb, "delta": delta, "better": win})
    return rows

"""Technical data of a recording (record check), as mastering and restoration tools show them.

Sample peak, true peak (4× oversampled), RMS, crest factor, loudness per ITU-R BS.1770-4 (K weighting,
400 ms blocks, absolute and relative gate), DC offset, correlation and stereo width in the music, tonal
balance (band shares), rumble and mains hum in quiet passages. Everything is measured inside the groove only.
Called by analysis.analyze(); the result is stored as metrics["technical"].

Works block by block, so a 30-minute side gets through on a Raspberry Pi without much memory.
"""

from __future__ import annotations

import numpy as np
from scipy import signal

from .i18n import msg

# Tonal balance bands (name, low Hz, high Hz); the names are stored keys
BANDS = (("bass", 20, 150), ("low_mid", 150, 500), ("mid", 500, 2000), ("presence", 2000, 6000), ("treble", 6000, 20000))


def _db(p) -> float:
    return float(10 * np.log10(max(float(p), 1e-20)))


def k_weighting_sos(sr: int) -> np.ndarray:
    """K weighting per BS.1770-4: high shelf +4 dB from 1.68 kHz and high-pass at 38 Hz, designed for any sample
    rate (at 48 kHz this matches the coefficients of the standard to 1e-4). Returns second-order sections."""
    # High shelf in the form that gives exactly the standard's coefficients at 48 kHz (b 1.535125 −2.691696
    # 1.198393; a 1 −1.690659 0.732481); the RBJ shelf formula was off by 0.2 dB at the 997 Hz reference tone
    g, fc, q = 3.999843853973347, 1681.974450955533, 0.7071752369554196
    k = np.tan(np.pi * fc / sr)
    vh = 10 ** (g / 20)
    vb = vh ** 0.4996667741545416
    a0 = 1 + k / q + k * k
    shelf = np.array([(vh + vb * k / q + k * k) / a0, 2 * (k * k - vh) / a0, (vh - vb * k / q + k * k) / a0,
                      1.0, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0])
    # high-pass
    fc, q = 38.13547087602444, 0.5003270373238773
    k = np.tan(np.pi * fc / sr)
    d = 1 + k / q + k * k
    hp = np.array([1.0, -2.0, 1.0, 1.0, 2 * (k * k - 1) / d, (1 - k / q + k * k) / d])
    return np.vstack([shelf, hp])


def loudness_lufs(x: np.ndarray, sr: int) -> float | None:
    """Integrated loudness (LUFS) of a stereo or mono block per BS.1770-4; None if too short or all gated."""
    if len(x) < int(0.4 * sr):
        return None
    sos = k_weighting_sos(sr)
    step = int(0.1 * sr)
    n = len(x) // step * step
    if n < 4 * step:
        return None
    means = []                                           # per channel: mean power per 100 ms step
    for c in range(x.shape[1]):
        z = signal.sosfilt(sos, x[:n, c].astype(np.float64))
        means.append(np.mean(z.reshape(-1, step) ** 2, axis=1))
    m = np.sum(means, axis=0)                            # channel weights 1 (L, R)
    blocks = (m[:-3] + m[1:-2] + m[2:-1] + m[3:]) / 4    # 400 ms, 75 % overlap
    lk = -0.691 + 10 * np.log10(np.maximum(blocks, 1e-20))
    g1 = lk > -70.0                                      # absolute gate
    if not g1.any():
        return None
    rel = -0.691 + 10 * np.log10(np.mean(blocks[g1])) - 10.0
    g2 = g1 & (lk > rel)                                 # relative gate (−10 LU)
    if not g2.any():
        return None
    return round(-0.691 + 10 * np.log10(np.mean(blocks[g2])), 2)


def true_peak_dbtp(x: np.ndarray, sr: int, chunk_s: float = 10.0) -> list[float]:
    """True peak per channel (dBTP): 4× polyphase oversampling, in chunks to bound memory use."""
    out = []
    n = int(chunk_s * sr)
    for c in range(x.shape[1]):
        tp = 0.0
        for i in range(0, len(x), n):
            y = signal.resample_poly(x[i:i + n, c].astype(np.float32), 4, 1)
            tp = max(tp, float(np.max(np.abs(y))) if y.size else 0.0)
        out.append(round(20 * np.log10(max(tp, 1e-9)), 2))
    return out


def measure(audio: np.ndarray, sr: int, groove: tuple[float, float], frame_music: np.ndarray,
            frame_quiet: np.ndarray, hop_s: float, music_ref_db: float | None) -> dict:
    """Technical data of the groove area.

    audio (n, ch) float ±1; groove (start, end) in s; frame_music/frame_quiet: masks per analysis frame
    (hop_s) for music and quiet passages; music_ref_db: music reference level for the relative rumble/hum
    values. Returns a dict with "ok" and the values (see the module docstring), or "reason" if not measurable."""
    g0, g1 = int(groove[0] * sr), int(groove[1] * sr)
    x = audio[g0:g1]
    nch = x.shape[1]
    if len(x) < 10 * sr:
        return {"ok": False, "reason": msg("technical.groove_too_short", s=10)}
    out: dict = {"ok": True}
    # peak, RMS, crest factor, DC
    peak = np.max(np.abs(x), axis=0)
    out["peak_dbfs"] = [round(float(20 * np.log10(max(p, 1e-9))), 2) for p in peak]
    out["true_peak_dbtp"] = true_peak_dbtp(x, sr)
    ms = np.mean(x.astype(np.float64) ** 2, axis=0)
    out["rms_dbfs"] = [round(_db(v), 2) for v in ms]
    out["crest_db"] = [round(p - r, 2) for p, r in zip(out["peak_dbfs"], out["rms_dbfs"])]
    dc = np.mean(x, axis=0)
    out["dc_pct"] = [round(float(v) * 100, 3) for v in dc]
    out["loudness_lufs"] = loudness_lufs(x, sr)
    # 1 s blocks over the groove: music/quiet per block from the frame masks (majority of the frames)
    per_blk = int(round(1 / hop_s))
    f0 = int(groove[0] / hop_s)
    nblk = len(x) // sr
    def mask_blocks(fm):
        out_m = np.zeros(nblk, bool)
        for b in range(nblk):
            seg = fm[f0 + b * per_blk:f0 + (b + 1) * per_blk]
            out_m[b] = seg.size > 0 and seg.mean() >= 0.5
        return out_m
    music_b, quiet_b = mask_blocks(frame_music), mask_blocks(frame_quiet)
    win = np.hanning(sr).astype(np.float32)
    fr = np.fft.rfftfreq(sr, 1 / sr)
    spec_music = np.zeros(len(fr)); spec_quiet = np.zeros(len(fr))
    nm = nq = 0
    corr, width = [], []
    for b in range(nblk):
        blk = x[b * sr:(b + 1) * sr]
        if music_b[b] or quiet_b[b]:
            mono = blk.mean(axis=1) if nch > 1 else blk[:, 0]
            p = np.abs(np.fft.rfft(mono * win)) ** 2
            if music_b[b]:
                spec_music += p; nm += 1
            else:
                spec_quiet += p; nq += 1
        if music_b[b] and nch >= 2:
            l, r = blk[:, 0].astype(np.float64), blk[:, 1].astype(np.float64)
            l -= l.mean(); r -= r.mean()
            pl, pr = np.sum(l * l), np.sum(r * r)
            if pl > 0 and pr > 0:
                corr.append(np.sum(l * r) / np.sqrt(pl * pr))
                m_, s_ = (l + r) / 2, (l - r) / 2
                width.append(_db(np.mean(s_ ** 2)) - _db(np.mean(m_ ** 2)))
    out["music_blocks_s"], out["quiet_blocks_s"] = int(nm), int(nq)
    out["correlation"] = round(float(np.median(corr)), 3) if corr else None
    out["stereo_width_db"] = round(float(np.median(width)), 2) if width else None
    if nm:
        total = spec_music[(fr >= 20) & (fr <= 20000)].sum()
        out["balance_db"] = {name: round(_db(spec_music[(fr >= lo) & (fr < hi)].sum() / max(total, 1e-20)), 1) for name, lo, hi in BANDS}
        # spectral centroid (Hz) as a single number for "bright/dark"
        m = (fr >= 20) & (fr <= 20000)
        out["centroid_hz"] = round(float(np.sum(fr[m] * spec_music[m]) / max(spec_music[m].sum(), 1e-20)), 0)
    if nq:
        sq = spec_quiet / nq
        lines = (np.abs(fr - 50) <= 1.5) | (np.abs(fr - 100) <= 1.5)
        band = (fr >= 20) & (fr <= 100)
        scale = 2 / (sr * np.sum(win ** 2))                    # power per bin → mean power in the signal
        rumble = float(sq[band & ~lines].sum() * scale)
        hum = float(sq[lines].sum() * scale)
        out["rumble_dbfs"] = round(_db(rumble), 2)
        out["hum_dbfs"] = round(_db(hum), 2)
        if music_ref_db is not None:
            out["rumble_rel_db"] = round(_db(rumble) - music_ref_db, 2)
            out["hum_rel_db"] = round(_db(hum) - music_ref_db, 2)
        # prominence of the 50/100 Hz lines over their surroundings (30–200 Hz, median): > 10 dB = distinct hum
        env = (fr >= 30) & (fr <= 200)
        med = float(np.median(sq[env])) if env.any() else 0.0
        peaks = {}
        for f_ in (50, 100):
            sel = np.abs(fr - f_) <= 1.5
            peaks[str(f_)] = round(_db(sq[sel].max()) - _db(med), 1) if sel.any() and med > 0 else None
        out["hum_prominence_db"] = peaks
    return out

"""Second opinion on click candidates: the click detector network of GrooveClean 1.2.1, queried only at the
candidates found by our own detector (detect.py).

GrooveClean (github.com/Booyaka101/grooveclean, MIT licence, copyright Chris Bosch) contains a small 1-D CNN
(32 channels, 8 dilated residual blocks, receptive field 2,041 samples at 44.1 kHz), trained on synthetic clicks
in clean music, that outputs a click probability per sample. On its own it is not a usable detector for vinyl
transfers: on a densely produced rock record it flagged about 1,200 positions per minute, and 10 to 14 per minute
in pink noise. As a second opinion on our candidates it is useful: it confirmed 97–100 % of them in quiet passages
and 75 % in music, and mostly rejected weak "ticks" (crackle).

The network does not run over the whole side but over the union of windows ±MARGIN_S around the candidates,
block by block (CHUNK_S) with margins for the normalization (NORM_MS) and the receptive field, resampled to
44.1 kHz. For each candidate the network's maximum within ±CORE_MS over both channels is its score; from
THRESHOLD on the candidate counts as confirmed.

Weights: weights/grooveclean-detector.pt (SHA-256 21543f3b…), redistributed under the MIT licence
(weights/LICENSE-grooveclean.txt), with GrooveClean's training metadata in grooveclean-detector.json. Architecture
and normalization are ports of grooveclean/detect.py; the threshold is GrooveClean's own calibration
("lo" = 0.55 at sensitivity 0.5; its "hi" is 0.95).

Two backends give the same numbers: PyTorch when it is installed (faster, the Raspberry Pi station installs
python3-torch), otherwise plain NumPy from weights/grooveclean-detector.npz (the same checkpoint with the
BatchNorm layers folded into the convolutions, made by tools/convert_grooveclean.py). The NumPy backend
normalizes each window like the PyTorch one and then evaluates the network only on a crop of ±(CORE_MS + half
the receptive field) around every candidate, batched; the outputs within ±CORE_MS do not depend on anything
outside that crop, so the values agree with the full evaluation. The second opinion is therefore available
everywhere, and a grade does not depend on whether PyTorch is installed.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
except Exception:  # noqa: BLE001 — PyTorch is optional
    torch = None

WEIGHTS = Path(__file__).resolve().parent / "weights" / "grooveclean-detector.pt"     # PyTorch backend
WEIGHTS_NP = WEIGHTS.with_suffix(".npz")                                                 # NumPy backend
META = WEIGHTS.with_suffix(".json")
MODEL = "grooveclean 1.2.1 detector (MIT)"
ANALYSIS_RATE = 44100
NORM_MS = 100.0
CORE_MS = 2.7              # ±: the network's maximum within this window is its verdict on the candidate
MARGIN_S = 0.15            # window around each candidate; overlapping windows are merged
CHUNK_S = 5.0              # block length for the network (bounds memory)
THRESHOLD = 0.55           # GrooveClean calibration "lo" at sensitivity 0.5: confirmed from here on
_state: dict = {}


def backend() -> str | None:
    """"torch" when PyTorch and the checkpoint are there, else "numpy" with the folded weights, else None."""
    if torch is not None and WEIGHTS.exists():
        return "torch"
    return "numpy" if WEIGHTS_NP.exists() else None


def available() -> bool:
    """The weights are present (PyTorch is optional)."""
    return backend() is not None


def info() -> dict:
    """Model description for the API: availability, name, threshold, held-out F1 from the training metadata."""
    meta = {}
    if META.exists():
        try:
            meta = json.loads(META.read_text())
        except Exception:  # noqa: BLE001
            meta = {}
    return {"available": available(), "backend": backend(), "model": MODEL, "threshold": THRESHOLD,
            "held_out_f1": (meta.get("held_out") or {}).get("f1"), "torch": getattr(torch, "__version__", None)}


if torch is not None:

    class ResBlock(nn.Module):
        def __init__(self, channels: int, kernel: int, dilation: int):
            super().__init__()
            self.conv = nn.Conv1d(channels, channels, kernel, dilation=dilation, padding=dilation * (kernel // 2))
            self.norm = nn.BatchNorm1d(channels)
            self.mix = nn.Conv1d(channels, channels, 1)
            self.norm2 = nn.BatchNorm1d(channels)

        def forward(self, x):
            h = F.relu(self.norm(self.conv(x)))
            return F.relu(x + self.norm2(self.mix(h)))

    class ClickCNN(nn.Module):
        def __init__(self, channels: int = 32, blocks: int = 8, kernel: int = 9):
            super().__init__()
            self.stem = nn.Conv1d(1, channels, kernel, padding=kernel // 2)
            self.blocks = nn.ModuleList([ResBlock(channels, kernel, 2 ** i) for i in range(blocks)])
            self.head = nn.Sequential(nn.Conv1d(channels, channels, 1), nn.ReLU(), nn.Conv1d(channels, 1, 1))
            self.receptive_field = 1 + (kernel - 1) * (2 ** blocks - 1)

        def forward(self, x):
            h = self.stem(x)
            for block in self.blocks:
                h = block(h)
            return self.head(h).squeeze(1)

    def _box_mean(x, width: int):
        width = max(1, width | 1)
        pad = width // 2
        flat = x.reshape(-1, x.shape[-1]).to(torch.float64)
        xp = F.pad(flat[:, None, :], (pad, pad), mode="reflect")[:, 0, :]
        c = F.pad(torch.cumsum(xp, dim=-1), (1, 0))
        return ((c[..., width:] - c[..., :-width]) / width).reshape(x.shape).to(x.dtype)

    def _normalize(x, rate: int):
        """Remove DC and local level, then asinh compression (so the network sees shape, not level)."""
        width = max(3, int(round(NORM_MS * 1e-3 * rate)) | 1)
        centred = x - _box_mean(x, width)
        level = torch.sqrt(_box_mean(centred * centred, width).clamp_min(0.0))
        floor = torch.clamp(centred.abs().amax(dim=-1, keepdim=True) * 1e-4, min=1e-9)
        return torch.asinh(centred / torch.clamp(level, min=floor))

    def _load():
        if "model" not in _state:
            blob = torch.load(str(WEIGHTS), map_location="cpu", weights_only=True)
            model = ClickCNN(**blob["config"])
            model.load_state_dict(blob["state_dict"])
            model.eval()
            torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
            _state["model"] = model
            _state["cal"] = blob.get("calibration", {})
        return _state["model"]

    def _probs(x: np.ndarray) -> np.ndarray:
        """Click probability per sample of a mono signal at ANALYSIS_RATE, computed block by block with margins."""
        model = _load()
        n = len(x)
        pad = model.receptive_field + int(2 * NORM_MS * 1e-3 * ANALYSIS_RATE)
        chunk = int(CHUNK_S * ANALYSIS_RATE)
        out = np.empty(n, dtype=np.float32)
        xt = torch.from_numpy(np.ascontiguousarray(x, dtype=np.float32))
        with torch.no_grad():
            for s in range(0, n, chunk):
                e = min(n, s + chunk)
                lo, hi = max(0, s - pad), min(n, e + pad)
                xn = _normalize(xt[None, lo:hi], ANALYSIS_RATE)
                out[s:e] = torch.sigmoid(model(xn[:, None, :]))[0, s - lo:s - lo + (e - s)].numpy()
        return out


# ─── NumPy backend ───────────────────────────────────────────────────────────────────────────────────────────────

BATCH = 24                 # crops per matrix product (about 60 MB of im2col columns)


def _np_load() -> dict:
    if "np" not in _state:
        with np.load(WEIGHTS_NP) as z:
            w = {k: z[k] for k in z.files}
        w["receptive_field"] = 1 + (int(w["kernel"]) - 1) * (2 ** int(w["blocks"]) - 1)
        _state["np"] = w
    return _state["np"]


def _np_box_mean(x: np.ndarray, width: int) -> np.ndarray:
    """Moving average over `width` samples with reflect padding (float64 cumulative sums), as _box_mean."""
    width = max(1, width | 1)
    pad = width // 2
    xp = np.pad(x.astype(np.float64), pad, mode="reflect")
    c = np.concatenate([[0.0], np.cumsum(xp)])
    return ((c[width:] - c[:-width]) / width).astype(x.dtype)


def _np_normalize(x: np.ndarray, rate: int) -> np.ndarray:
    """_normalize in NumPy: remove DC and local level, asinh compression. x float32 (n,)."""
    width = max(3, int(round(NORM_MS * 1e-3 * rate)) | 1)
    centred = x - _np_box_mean(x, width)
    level = np.sqrt(np.maximum(_np_box_mean(centred * centred, width), 0.0))
    floor = max(float(np.abs(centred).max()) * 1e-4, 1e-9)
    return np.arcsinh(centred / np.maximum(level, np.float32(floor))).astype(np.float32)


def _np_conv(x: np.ndarray, w: np.ndarray, b: np.ndarray, dilation: int) -> np.ndarray:
    """Dilated 'same' convolution (cross-correlation, as Conv1d) of x (B, C, L) with w (O, C, K): (B, O, L)."""
    k, L = w.shape[2], x.shape[2]
    pad = dilation * (k // 2)
    xp = np.pad(x, ((0, 0), (0, 0), (pad, pad)))
    cols = np.stack([xp[:, :, j * dilation:j * dilation + L] for j in range(k)], axis=2).reshape(x.shape[0], -1, L)
    return np.matmul(w.reshape(w.shape[0], -1), cols) + b[None, :, None]


def _np_forward(x: np.ndarray) -> np.ndarray:
    """Logits (B, L) for normalized crops x (B, L): ClickCNN with the BatchNorm layers folded in."""
    w = _np_load()
    h = _np_conv(x[:, None, :], w["stem_w"], w["stem_b"], 1)
    for i in range(int(w["blocks"])):
        a = np.maximum(_np_conv(h, w[f"block{i}_conv_w"], w[f"block{i}_conv_b"], 2 ** i), 0)
        h = np.maximum(h + np.matmul(w[f"block{i}_mix_w"], a) + w[f"block{i}_mix_b"][None, :, None], 0)
    a = np.maximum(np.matmul(w["head0_w"], h) + w["head0_b"][None, :, None], 0)
    return (np.matmul(w["head1_w"], a) + w["head1_b"][None, :, None])[:, 0, :]


def _np_scores(seg: np.ndarray, ks: np.ndarray, core: int) -> np.ndarray:
    """Per candidate (sample index within the segment seg (n, channels) at ANALYSIS_RATE): the network's
    maximum probability within ±core samples over all channels. Each channel is normalized over the whole
    segment, then only a crop of ±(core + half the receptive field) around each candidate goes through the
    network; the outputs within ±core do not depend on anything outside it."""
    w = _np_load()
    half = core + (w["receptive_field"] - 1) // 2
    L = 2 * half + 1
    out = np.zeros(len(ks))
    for c in range(seg.shape[1]):
        xp = np.pad(_np_normalize(np.ascontiguousarray(seg[:, c], dtype=np.float32), ANALYSIS_RATE), half)
        for s in range(0, len(ks), BATCH):
            crops = np.stack([xp[k:k + L] for k in ks[s:s + BATCH]])
            peak = _np_forward(crops)[:, half - core:half + core + 1].max(axis=1)
            out[s:s + BATCH] = np.maximum(out[s:s + BATCH], 1.0 / (1.0 + np.exp(-peak.astype(np.float64))))
    return out


def segments(times: np.ndarray, margin_s: float = MARGIN_S) -> list[tuple[float, float]]:
    """Merged windows ±margin_s around the given times (seconds), in ascending order."""
    segs: list[list[float]] = []
    for t in np.sort(times):
        a, b = float(t) - margin_s, float(t) + margin_s
        if segs and a <= segs[-1][1]:
            segs[-1][1] = max(segs[-1][1], b)
        else:
            segs.append([a, b])
    return [(a, b) for a, b in segs]


def opinion(audio: np.ndarray, sr: int, times: np.ndarray) -> np.ndarray | None:
    """The network's click probability per candidate time (seconds): maximum within ±CORE_MS over all channels.
    None if the weights are missing. PyTorch when installed, otherwise NumPy, with the same result."""
    be = backend()
    if be is None:
        return None
    times = np.asarray(times, dtype=float)
    out = np.zeros(len(times))
    if times.size == 0:
        return out
    if audio.ndim == 1:
        audio = audio[:, None]
    core = int(round(CORE_MS * 1e-3 * ANALYSIS_RATE))
    for a, b in segments(times):
        i0, i1 = max(0, int(a * sr)), min(len(audio), int(np.ceil(b * sr)))
        if i1 - i0 < int(0.05 * sr):
            continue
        seg = audio[i0:i1]
        if sr != ANALYSIS_RATE:
            seg = resample_poly(seg, ANALYSIS_RATE, sr, axis=0)
        sel = np.flatnonzero((times >= a) & (times <= b))
        ks = np.round((times[sel] - i0 / sr) * ANALYSIS_RATE).astype(int)
        ok = (ks >= 0) & (ks < len(seg))
        if be == "torch":
            pm = np.max(np.stack([_probs(seg[:, c]) for c in range(seg.shape[1])]), axis=0)
            for i, k in zip(sel[ok], ks[ok]):
                out[i] = float(pm[max(0, k - core):k + core + 1].max())
        elif ok.any():
            out[sel[ok]] = _np_scores(seg, ks[ok], core)
    return out


def timed_opinion(audio: np.ndarray, sr: int, times: np.ndarray) -> tuple[np.ndarray | None, float]:
    """opinion() plus the time it took (seconds)."""
    t0 = time.time()
    return opinion(audio, sr, times), round(time.time() - t0, 1)

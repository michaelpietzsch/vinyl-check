"""Spectrogram of a recording for visual inspection: time × frequency (logarithmic) as a uint8 image.

Used to compare two recordings of the same side by eye: clicks are vertical stripes across all frequencies,
groove noise is an even haze, rumble a band at the very bottom, hum a horizontal line. The matrix is computed
directly at the requested image size (columns = time, rows = frequency from fmin to fmax, logarithmic), with
the power averaged per cell. The file is read in blocks so that a whole LP side (~250 MB) never has to be held
in memory.

Levels: 255 = 99.5th percentile of the image (or a given `top_db`, so that two recordings get the same color
scale), 0 = `db_range` below that. Because the image is linear in dB, a finished image can be converted to
another scale (`rescale`) without reading the audio again. Images are computed and stored with STORE_RANGE
(100 dB) and displayed with DISPLAY_RANGE (80 dB): this leaves 20 dB of headroom at the bottom, so rescaling to
the scale of another recording does not cut anything off.

The overview image of the whole recording (FULL_COLS × FULL_ROWS) is computed by the worker after the analysis
and stored in the database: it is available immediately and survives the deletion of the audio file.
"""

from __future__ import annotations

import os

import numpy as np
import soundfile as sf
from numpy.lib.stride_tricks import sliding_window_view
from scipy import fft as sfft

NPERSEG = 2048
FRAMES_PER_CHUNK = 2048          # ≈ 44 s per block at 48 kHz
FULL_COLS, FULL_ROWS = 1600, 160  # overview image (precomputed by the worker)
STORE_RANGE = 100.0               # range of computed/stored images (dB below the top)
DISPLAY_RANGE = 80.0              # range of displayed images
FFT_WORKERS = max(1, min(4, os.cpu_count() or 1))   # more than 4 threads gain nothing for 2048-point blocks


def _row_bins(sr: int, rows: int, fmin: float, fmax: float) -> tuple[np.ndarray, np.ndarray]:
    """Map FFT bins to frequency rows: start bin per row plus an end index (for add.reduceat; the result has one
    extra column that is dropped) and the number of bins per row. Rows without a bin of their own (low
    frequencies, where a row is narrower than one bin) get the bin containing their center frequency; reduceat
    returns exactly that bin when the start index equals the next one."""
    edges = np.geomspace(fmin, fmax, rows + 1)
    fbin = np.fft.rfftfreq(NPERSEG, 1 / sr)
    centers = np.sqrt(edges[:-1] * edges[1:])
    starts = np.searchsorted(fbin, edges[:-1], side="left")
    ends = np.searchsorted(fbin, edges[1:], side="left")
    count = ends - starts
    empty = count <= 0
    starts[empty] = np.clip(np.round(centers[empty] / fbin[1]).astype(int), 0, len(fbin) - 1)
    count = np.maximum(count, 1)
    starts = np.append(starts, ends[-1])                  # end index: count nothing above fmax
    starts = np.maximum.accumulate(starts)                # reduceat needs non-decreasing indices
    return np.minimum(starts, len(fbin) - 1), count


def rescale(img: np.ndarray, top_old: float, top_new: float, range_old: float = STORE_RANGE,
            range_new: float = DISPLAY_RANGE) -> np.ndarray:
    """Convert a finished image to another top level and/or another range (dB)."""
    db = (top_old - range_old) + img.astype(np.float32) * (range_old / 255.0)
    return np.clip((db - (top_new - range_new)) / range_new * 255.0, 0, 255).astype(np.uint8)


def compute(path, cols: int = FULL_COLS, rows: int = FULL_ROWS, fmin: float = 20.0, fmax: float = 16000.0,
            t0: float = 0.0, t1: float | None = None, db_range: float = STORE_RANGE,
            top_db: float | None = None) -> dict:
    """Spectrogram of an audio file between t0 and t1 seconds (mono mix).

    Returns img (rows × cols, uint8, row 0 = fmin), the time window, frequency limits and the scale in dB
    (top_db, floor_db) plus the sample rate.
    """
    with sf.SoundFile(str(path)) as f:
        sr = f.samplerate
        dur = f.frames / sr
        t1 = dur if t1 is None else min(float(t1), dur)
        t0 = max(0.0, min(float(t0), t1))
        i0 = int(t0 * sr)
        n = int(t1 * sr) - i0
        hop = NPERSEG // 2
        nfr = max(1, (n - NPERSEG) // hop + 1)
        cols = max(1, min(int(cols), nfr))
        fmax = min(float(fmax), sr / 2)
        starts, count = _row_bins(sr, rows, fmin, fmax)
        win = np.hanning(NPERSEG).astype(np.float32)
        acc = np.zeros((cols, rows))
        cnt = np.zeros(cols)
        for k0 in range(0, nfr, FRAMES_PER_CHUNK):
            kk = min(FRAMES_PER_CHUNK, nfr - k0)
            need = (kk - 1) * hop + NPERSEG
            f.seek(i0 + k0 * hop)
            x = f.read(need, dtype="float32", always_2d=True).mean(axis=1, dtype=np.float32)
            if len(x) < need:
                x = np.pad(x, (0, need - len(x)))
            fr = sliding_window_view(x, NPERSEG)[::hop][:kk] * win
            spec = sfft.rfft(fr, axis=1, workers=FFT_WORKERS)  # float32 stays float32, several cores
            p = spec.real ** 2 + spec.imag ** 2
            pr = np.add.reduceat(p, starts, axis=1)[:, :-1] / count   # bins → rows (mean)
            col = np.minimum(np.arange(k0, k0 + kk) * cols // nfr, cols - 1)
            np.add.at(acc, col, pr)
            np.add.at(cnt, col, 1)
    db = 10 * np.log10(acc / np.maximum(cnt, 1)[:, None] + 1e-20).T      # rows × cols
    top = float(np.percentile(db, 99.5)) if top_db is None else float(top_db)
    floor = top - db_range
    img = np.clip((db - floor) / (top - floor) * 255, 0, 255).astype(np.uint8)
    return {"img": img, "rows": rows, "cols": cols, "t0": t0, "t1": t1, "fmin": fmin, "fmax": fmax,
            "top_db": round(top, 2), "floor_db": round(floor, 2), "sr": sr}

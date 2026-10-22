"""PDF reports: test pressing approval of a record and the report of a single recording.

ReportLab (Debian: python3-reportlab) typesets the reports, Pillow colors the spectrograms, the font is IBM Plex Sans
(Debian: fonts-ibm-plex, TrueType); without it the reports fall back to Helvetica. All graphics are vector drawings
(ReportLab shapes), only the spectrograms are images. Output is the PDF as bytes.

Both reports are rendered in the language of the current request: every static text comes from the ``report``
namespace of the server catalogs (``i18n.t``), numbers and dates use the reader's conventions. Values handed over by
other modules may be i18n messages or plain strings (results stored before 1.0); they always pass through
``_text`` (``i18n.render``) before they are placed on the page.

- ``build(data)``: test pressing approval. Input from web.approval_report_data() (approval, defects shared by several
  copies, per copy the master comparison and the real clicks, spectrograms as uint8 matrices). Layout: cover page
  with recommendation, key facts and an overview per side; one new page per record side with checks, copies, audible
  stamper defects, timeline, spectrogram, close-ups, frequency response, wow and level per track; appendix with
  method and thresholds. Header with the record, footer with "page x of y".
- ``build_recording(data)``: report of one recording (grade, key figures, clicks across the side, level, spectrogram,
  tracks, grades per metric, skips, strongest detections, master comparison, technical data, grading scale). Input
  from web.recording_report_data().
"""
from __future__ import annotations

import io
import math
import os
import time
from pathlib import Path

import numpy as np
from PIL import Image as PILImage
from reportlab.graphics.shapes import Circle, Drawing, Image as DImage, Line, PolyLine, Polygon, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (BaseDocTemplate, CondPageBreak, Frame, KeepTogether, PageBreak, PageTemplate, Paragraph,
                                Spacer, Table, TableStyle)

from . import i18n

# ─── Design ──────────────────────────────────────────────────────────────────
INK, MUTED, FAINT, LINE, PAPER = "#1d2229", "#5f6b7a", "#f4f6f8", "#dde2e8", "#ffffff"
ACCENT = "#c8473a"
STATUS = {"ok": "#2f8f5b", "warn": "#c7871a", "bad": "#c03a48", "info": "#7a8696"}
KIND = {"pop": "#d64545", "click": "#e0a526", "tick": "#8c96a3"}
COPY_COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#8c564b", "#17becf", "#7f7f7f"]
SG_SCALE = [(0, 0, 4), (40, 12, 70), (101, 21, 110), (159, 42, 99), (212, 72, 66), (245, 125, 21), (250, 193, 39), (252, 253, 191)]
# Approval verdicts arrive from web.approval() as German text (interface values until a later pass gives them enum
# values). Rendering a verdict in German maps both those plain strings and messages with the same German text to a
# status; the report then shows its own label for that status in the request language.
VERDICT_STATUS = {"Freigabe empfohlen": "ok", "Freigabe mit Hinweisen": "warn", "nicht freigeben": "bad"}
VERDICT_LABEL = {"ok": "report.verdict_ok", "warn": "report.verdict_warn", "bad": "report.verdict_bad"}
# Event kinds (stored enum values) → catalog keys
KIND_LABEL = {"pop": "report.kind_pop", "click": "report.kind_click", "tick": "report.kind_tick"}

PAGE_W, PAGE_H = A4
MARGIN_L = MARGIN_R = 16 * mm
MARGIN_T, MARGIN_B = 22 * mm, 18 * mm
FRAME_W = PAGE_W - MARGIN_L - MARGIN_R

FONT_DIRS = [os.environ.get("VINYLCHECK_FONT_DIR", ""), "/usr/share/fonts/truetype/ibm-plex"]
_fonts = {"regular": "Helvetica", "medium": "Helvetica", "semibold": "Helvetica-Bold", "bold": "Helvetica-Bold",
          "italic": "Helvetica-Oblique", "mono": "Courier"}


def _register_fonts() -> None:
    """Use IBM Plex from the first font directory that has all cuts; otherwise keep the Helvetica fallback."""
    want = {"regular": "IBMPlexSans-Regular", "medium": "IBMPlexSans-Medium", "semibold": "IBMPlexSans-SemiBold",
            "bold": "IBMPlexSans-Bold", "italic": "IBMPlexSans-Italic", "mono": "IBMPlexMono-Regular"}
    for d in FONT_DIRS:
        if not d or not Path(d).is_dir():
            continue
        if all((Path(d) / f"{n}.ttf").exists() for n in want.values()):
            for key, name in want.items():
                if name not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont(name, str(Path(d) / f"{name}.ttf")))
                _fonts[key] = name
            return


def _styles() -> dict:
    f = _fonts
    base = dict(fontName=f["regular"], fontSize=8.6, leading=11.6, textColor=colors.HexColor(INK))
    return {
        "body": ParagraphStyle("body", **base),
        "small": ParagraphStyle("small", **{**base, "fontSize": 7.4, "leading": 9.8, "textColor": colors.HexColor(MUTED)}),
        "tiny": ParagraphStyle("tiny", **{**base, "fontSize": 6.6, "leading": 8.4, "textColor": colors.HexColor(MUTED)}),
        "cell": ParagraphStyle("cell", **{**base, "fontSize": 7.8, "leading": 10}),
        "cellr": ParagraphStyle("cellr", **{**base, "fontSize": 7.8, "leading": 10, "alignment": TA_RIGHT}),
        "kicker": ParagraphStyle("kicker", **{**base, "fontName": f["semibold"], "fontSize": 8, "leading": 10,
                                              "textColor": colors.HexColor(ACCENT)}),
        "title": ParagraphStyle("title", **{**base, "fontName": f["semibold"], "fontSize": 21, "leading": 25}),
        "subtitle": ParagraphStyle("subtitle", **{**base, "fontSize": 10, "leading": 13.5, "textColor": colors.HexColor(MUTED)}),
        "h1": ParagraphStyle("h1", **{**base, "fontName": f["semibold"], "fontSize": 15, "leading": 19, "spaceAfter": 2}),
        "h2": ParagraphStyle("h2", **{**base, "fontName": f["semibold"], "fontSize": 10.5, "leading": 13, "spaceBefore": 9,
                                      "spaceAfter": 4}),
        "th": ParagraphStyle("th", **{**base, "fontName": f["medium"], "fontSize": 6.9, "leading": 8.6,
                                      "textColor": colors.HexColor(MUTED)}),
        "thr": ParagraphStyle("thr", **{**base, "fontName": f["medium"], "fontSize": 6.9, "leading": 8.6,
                                       "textColor": colors.HexColor(MUTED), "alignment": TA_RIGHT}),
    }


# ─── Text and number formatting ──────────────────────────────────────────────
def de(v, d: int = 1, sign: bool = False, unit: str = "") -> str:
    """A number for display in the request language: `d` decimals, local decimal separator, typographic minus, a sign
    on request and an optional unit; "–" without a value. (web.py formats its table values with it as well.)"""
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "–"
    s = i18n.num(v, d)
    if sign and not s.startswith("-"):
        s = "+" + s
    return s.replace("-", "−") + (f" {unit}" if unit else "")


def _decimal_sep() -> str:
    return i18n.SEPARATORS.get(i18n.current_lang(), (".", ","))[0]


def ft(t: float) -> str:
    """Time as m:ss.ss with the local decimal separator (master time of defects); "–" without a value."""
    if t is None:
        return "–"
    m, s = divmod(float(t), 60)
    return f"{int(m)}:{s:05.2f}".replace(".", _decimal_sep())


def fmin_(t: float) -> str:
    """Time as m:ss, rounded to whole seconds."""
    m, s = divmod(int(round(t)), 60)
    return f"{m}:{s:02d}"


def esc(s) -> str:
    """Escape text for ReportLab paragraph markup."""
    return str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _text(value) -> str:
    """Text of a value from another module: an i18n message rendered in the request language, or a plain string."""
    v = i18n.render(value)
    return "" if v is None else str(v)


def _label(mapping: dict, value, fallback: str | None = None) -> str:
    """Display label of an enum value via a {value: catalog key} map; unknown values show `fallback` or themselves."""
    key = mapping.get(value)
    if key:
        return i18n.t(key)
    return fallback if fallback is not None else ("–" if value is None else str(value))


def _verdict(verdict) -> tuple[str, str]:
    """(label in the request language, status ok/warn/bad/info) of an approval verdict, see VERDICT_STATUS. Unknown
    verdicts are shown as they come, without a verdict "no master on file"."""
    if not verdict:
        return i18n.t("report.no_master"), "info"
    status = VERDICT_STATUS.get(i18n.render(verdict, "de"), "info")
    return (i18n.t(VERDICT_LABEL[status]) if status in VERDICT_LABEL else _text(verdict)), status


# Polarity values of master.check (stored enum values) → catalog keys. web.py passes "wie Master" for "ok" in the
# recording report data.
POLARITY_LABEL = {"ok": "report.polarity_ok", "wie Master": "report.polarity_ok", "gedreht": "report.polarity_inverted",
                  "links verpolt": "report.polarity_left", "rechts verpolt": "report.polarity_right"}


def _polarity(value) -> str:
    v = _text(value)
    return _label(POLARITY_LABEL, v, v or "–")


def _short_copy(name: str) -> str:
    """Copy name for narrow chart legends: a leading "Exemplar " (the usual German copy label) becomes "Ex. "."""
    return str(name).replace("Exemplar ", "Ex. ")


# ─── Building blocks ─────────────────────────────────────────────────────────
def _badge(text: str, status: str, st: dict, big: bool = False) -> Table:
    """Colored label (recommendation)."""
    col = colors.HexColor(STATUS.get(status, STATUS["info"]))
    p = Paragraph(f'<font color="{STATUS.get(status, STATUS["info"])}">{esc(text)}</font>',
                  ParagraphStyle("b", parent=st["body"], fontName=_fonts["semibold"], fontSize=13 if big else 8.2,
                                 leading=16 if big else 10))
    from reportlab.pdfbase.pdfmetrics import stringWidth
    w = stringWidth(text, _fonts["semibold"], 13 if big else 8.2) + (20 if big else 12)
    t = Table([[p]], colWidths=[w], hAlign="LEFT")
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.8, col),
                           ("BACKGROUND", (0, 0), (-1, -1), colors.Color(col.red, col.green, col.blue, alpha=0.08)),
                           ("LEFTPADDING", (0, 0), (-1, -1), 9 if big else 5), ("RIGHTPADDING", (0, 0), (-1, -1), 9 if big else 5),
                           ("TOPPADDING", (0, 0), (-1, -1), 5 if big else 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 6 if big else 2.5),
                           ("ROUNDEDCORNERS", [3, 3, 3, 3])]))
    return t


def _table(rows: list[list], widths: list[float], header_rows: int = 1, zebra: bool = True, align_right: set | None = None) -> Table:
    t = Table(rows, colWidths=widths, repeatRows=header_rows, hAlign="LEFT")
    style = [("FONTNAME", (0, 0), (-1, -1), _fonts["regular"]), ("FONTSIZE", (0, 0), (-1, -1), 7.8),
             ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
             ("TOPPADDING", (0, 0), (-1, -1), 2.6), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.6),
             ("LINEBELOW", (0, header_rows - 1), (-1, header_rows - 1), 0.7, colors.HexColor(INK)),
             ("LINEBELOW", (0, header_rows), (-1, -1), 0.25, colors.HexColor(LINE))]
    if zebra:
        for r in range(header_rows, len(rows)):
            if (r - header_rows) % 2:
                style.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor(FAINT)))
    for c in align_right or ():
        style.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
    t.setStyle(TableStyle(style))
    return t


def _header(cols: list[tuple[str, str]], st: dict) -> list:
    """Table header row from (text, style) pairs."""
    return [Paragraph(esc(text), st[style]) for text, style in cols]


def _grade_chip(g: str | None, st: dict) -> Paragraph:
    col = {"NM": "#2E8B57", "VG+": "#7E9F21", "VG": "#D29A00", "G+": "#E67E22", "G": "#D95524", "F": "#C23B4A"}.get(g or "", MUTED)
    return Paragraph(f'<font name="{_fonts["semibold"]}" color="{col}">{esc(g or "–")}</font>', st["cell"])


def _legend(items: list[tuple[str, str, str]], width: float) -> Drawing:
    """Horizontal legend: (shape "dot"/"bar"/"line", color, text)."""
    d = Drawing(width, 10)
    x = 0.0
    for shape, col, label in items:
        c = colors.HexColor(col)
        if shape == "dot":
            d.add(Circle(x + 3, 4, 2.6, fillColor=c, strokeColor=None))
        elif shape == "line":
            d.add(Line(x, 4, x + 7, 4, strokeColor=c, strokeWidth=1.4))
        else:
            d.add(Rect(x, 1.5, 6, 6, fillColor=c, strokeColor=None))
        d.add(_txt(x + 9, 1.8, label, 6.4, MUTED))
        from reportlab.pdfbase.pdfmetrics import stringWidth
        x += 9 + stringWidth(label, _fonts["regular"], 6.4) + 10
    return d


def _status_dot(status: str) -> Drawing:
    d = Drawing(9, 9)
    d.add(Circle(4.5, 4.5, 3.6, fillColor=colors.HexColor(STATUS.get(status, STATUS["info"])), strokeColor=None))
    return d


def _lut() -> np.ndarray:
    """256-entry color lookup table interpolated from SG_SCALE (dark → bright)."""
    lut = np.zeros((256, 3), np.uint8)
    n = len(SG_SCALE) - 1
    for i in range(256):
        x = i / 255 * n
        k = min(n - 1, int(x))
        f = x - k
        lut[i] = [round(SG_SCALE[k][c] * (1 - f) + SG_SCALE[k + 1][c] * f) for c in range(3)]
    return lut


LUT = _lut()


def _sg_image(img: np.ndarray) -> PILImage.Image:
    """uint8 matrix (row 0 = lowest frequency) → color image with low frequencies at the bottom."""
    return PILImage.fromarray(LUT[np.flipud(img)], "RGB")


# ─── Charts ──────────────────────────────────────────────────────────────────
def _txt(x, y, s, size=6.4, color=MUTED, anchor="start", font=None) -> String:
    return String(x, y, s, fontName=font or _fonts["regular"], fontSize=size, fillColor=colors.HexColor(color), textAnchor=anchor)


def timeline(side: dict, width: float, audible: float) -> Drawing:
    """Timeline of a side on the master time axis: tracks, a stamper row, and the real clicks of each copy."""
    copies = side["copies_detail"]
    n = len(copies)
    dur = max(1.0, float(side["master"].get("duration_s") or 1))
    pad_l, pad_r, row_h, top_h, axis_h = 52, 4, 13.5, 14, 22
    h = top_h + (n + 1) * row_h + axis_h
    d = Drawing(width, h)
    X = lambda t: pad_l + (max(0.0, min(dur, t)) / dur) * (width - pad_l - pad_r)
    y_top = h - top_h
    # Tracks as a band
    for i, it in enumerate((copies[0]["levels"] or {}).get("items") or [] if copies else []):
        a, b = (it.get("t_m") or [0, 0])[:2]
        d.add(Rect(X(a), y_top + 1.5, max(0.5, X(b) - X(a) - 0.6), 10, fillColor=colors.HexColor("#e9edf2" if i % 2 else "#dfe5ec"),
                   strokeColor=None))
        label = f'{it.get("pos", "")} {_text(it.get("title", ""))}'.strip()
        maxch = int((X(b) - X(a)) / 3.6)
        if maxch > 3:
            d.add(_txt(X(a) + 2.5, y_top + 4.3, label[:maxch], 6.2, INK))
    # Rows
    for r in range(n + 1):
        yc = y_top - (r + 0.5) * row_h
        d.add(Line(pad_l, yc, width - pad_r, yc, strokeColor=colors.HexColor(LINE), strokeWidth=0.4))
        d.add(_txt(pad_l - 4, yc - 2.2, i18n.t("report.stamper") if r == 0 else copies[r - 1]["copy"], 6.6, INK, "end",
                   _fonts["semibold"] if r == 0 else None))
    # Stamper / cut: tick height grows with the number of copies; audible ones bold, labeled and dashed across all rows
    yS = y_top - 0.5 * row_h
    for x in side["common"]:
        on_all = x["n"] >= n
        loud = x["max_strength"] >= audible
        col = colors.HexColor(STATUS["bad"] if on_all else STATUS["warn"])
        hh = 2 + 4.5 * x["n"] / max(n, 1)
        d.add(Line(X(x["tm"]), yS - hh, X(x["tm"]), yS + hh, strokeColor=col, strokeWidth=1.4 if loud else 0.45,
                   strokeOpacity=1 if loud else 0.55))
        if loud:
            d.add(Line(X(x["tm"]), y_top - (n + 1) * row_h, X(x["tm"]), y_top, strokeColor=col, strokeWidth=0.6,
                       strokeDashArray=[2, 2]))
            d.add(_txt(X(x["tm"]) + 1.5, y_top - (n + 1) * row_h - 7.5, f'{ft(x["tm"])} ({x["n"]}/{n})', 6.2,
                       STATUS["bad"] if on_all else STATUS["warn"], font=_fonts["medium"]))
    # Real clicks per copy: pops above, crackle below the line, size by strength, outlined when audible
    for i, c in enumerate(copies):
        yc = y_top - (i + 1.5) * row_h
        for e in c["vinyl"]:
            r = max(0.7, min(3.2, math.log10(1 + (e.get("strength") or 1)) * 1.25))
            dy = 2.6 if e["kind"] == "pop" else -2.6 if e["kind"] == "tick" else 0
            d.add(Circle(X(e["tm"]), yc + dy, r, fillColor=colors.HexColor(KIND.get(e["kind"], KIND["click"])), fillOpacity=0.8,
                         strokeColor=colors.HexColor(INK) if (e.get("strength") or 0) >= audible else None, strokeWidth=0.5))
    # Axis
    y0 = y_top - (n + 1) * row_h
    d.add(Line(pad_l, y0, width - pad_r, y0, strokeColor=colors.HexColor(MUTED), strokeWidth=0.5))
    step = 120 if dur > 900 else 60
    t = 0.0
    while t <= dur + 0.1:
        d.add(Line(X(t), y0, X(t), y0 - 2, strokeColor=colors.HexColor(MUTED), strokeWidth=0.5))
        d.add(_txt(X(t), y0 - 17, fmin_(t), 6.2, MUTED, "middle"))
        t += step
    return d


def spectrogram(sg: dict, width: float, height: float, markers: list[dict], freq_axis: bool = True) -> Drawing:
    """Spectrogram (image, log frequency axis) with vector axes and markers ({t, status, label?, line?, width?})."""
    pad_l, pad_b = (22 if freq_axis else 1), 11
    d = Drawing(width, height + pad_b)
    w, h = width - pad_l, height
    d.add(DImage(pad_l, pad_b, w, h, _sg_image(sg["img"])))
    d.add(Rect(pad_l, pad_b, w, h, fillColor=None, strokeColor=colors.HexColor(LINE), strokeWidth=0.4))
    lf = math.log(sg["fmax"] / sg["fmin"])
    for f in (50, 100, 200, 500, 1000, 2000, 5000, 10000) if freq_axis else ():
        if sg["fmin"] <= f <= sg["fmax"]:
            y = pad_b + math.log(f / sg["fmin"]) / lf * h
            d.add(Line(pad_l - 2, y, pad_l, y, strokeColor=colors.HexColor(MUTED), strokeWidth=0.4))
            d.add(_txt(pad_l - 3, y - 2, f"{f // 1000}k" if f >= 1000 else str(f), 5.6, MUTED, "end"))
    span = sg["t1"] - sg["t0"]
    step = 120 if span > 900 else 60 if span > 240 else 10 if span > 40 else 1 if span > 4 else 0.1
    t = math.ceil(sg["t0"] / step) * step
    while t <= sg["t1"] + 1e-9:
        x = pad_l + (t - sg["t0"]) / span * w
        d.add(Line(x, pad_b, x, pad_b - 2, strokeColor=colors.HexColor(MUTED), strokeWidth=0.4))
        d.add(_txt(x, 1.5, fmin_(t) if step >= 1 else de(t % 60, 1), 5.6, MUTED, "middle"))
        t += step
    for m in markers:
        x = pad_l + (m["t"] - sg["t0"]) / span * w
        if not pad_l <= x <= width:
            continue
        col = colors.HexColor(STATUS.get(m.get("status", "bad")))
        if m.get("line", True):
            d.add(Line(x, pad_b, x, pad_b + h, strokeColor=col, strokeWidth=m.get("width", 0.9)))
        else:                                                   # only triangles at top and bottom, the middle stays clear
            d.add(Polygon([x - 3.5, pad_b + h, x + 3.5, pad_b + h, x, pad_b + h - 5], fillColor=col, strokeColor=None))
            d.add(Polygon([x - 3.5, pad_b, x + 3.5, pad_b, x, pad_b + 5], fillColor=col, strokeColor=None))
        if m.get("label"):
            label = _text(m["label"])
            d.add(Rect(x + 1.5, pad_b + h - 9, 3.2 * len(label) + 4, 8, fillColor=colors.Color(0, 0, 0, alpha=0.55),
                       strokeColor=None))
            d.add(_txt(x + 3.5, pad_b + h - 7, label, 5.8, "#ffffff", font=_fonts["medium"]))
    return d


def freq_chart(copies: list[dict], width: float, height: float) -> Drawing | None:
    """Frequency response vs. master: median of the copies (mid signal) for each third of the side."""
    f = [c["freq"] for c in copies if c.get("freq") and c["freq"].get("bands") and c["freq"].get("segments")]
    if not f:
        return None
    bands = f[0]["bands"]
    segs = [_text(s["label"]) for s in f[0]["segments"]]
    lines = []
    for si in range(len(segs)):
        ln = []
        for bi in range(len(bands)):
            vals = [x["segments"][si]["mid_db"][bi] for x in f if si < len(x["segments"]) and bi < len(x["segments"][si].get("mid_db") or [])
                    and x["segments"][si]["mid_db"][bi] is not None]
            ln.append(float(np.median(vals)) if vals else None)
        lines.append(ln)
    vals = [v for ln in lines for v in ln if v is not None]
    lo, hi = min(-12, math.floor(min(vals) / 3) * 3), max(6, math.ceil(max(vals) / 3) * 3)
    pl, pr, pt, pb = 22, 4, 26, 13
    d = Drawing(width, height)
    X = lambda hz: pl + (math.log(hz) - math.log(bands[0])) / (math.log(bands[-1]) - math.log(bands[0])) * (width - pl - pr)
    Y = lambda v: pb + (v - lo) / (hi - lo) * (height - pt - pb)
    d.add(_txt(0, height - 8, i18n.t("report.chart_freq"), 7.4, INK, font=_fonts["semibold"]))
    for db in range(int(lo), int(hi) + 1, 3):
        d.add(Line(pl, Y(db), width - pr, Y(db), strokeColor=colors.HexColor(MUTED if db == 0 else LINE), strokeWidth=0.6 if db == 0 else 0.3))
        d.add(_txt(pl - 3, Y(db) - 2, str(db).replace("-", "−"), 5.6, MUTED, "end"))
    for hz in (50, 100, 200, 500, 1000, 2000, 5000, 10000):
        if bands[0] <= hz <= bands[-1]:
            d.add(_txt(X(hz), 2, f"{hz // 1000}k" if hz >= 1000 else str(hz), 5.6, MUTED, "middle"))
    col = [STATUS["ok"], STATUS["warn"], STATUS["bad"]]
    for si, ln in enumerate(lines):
        pts = [v for bi, val in enumerate(ln) if val is not None for v in (X(bands[bi]), Y(val))]
        if len(pts) >= 4:
            d.add(PolyLine(pts, strokeColor=colors.HexColor(col[si % 3]), strokeWidth=1.1))
        d.add(Rect(si * 40, height - 19, 6, 6, fillColor=colors.HexColor(col[si % 3]), strokeColor=None))
        d.add(_txt(9 + si * 40, height - 18.2, segs[si], 6.2, INK))
    return d


def _copy_legend(d: Drawing, names: list[str], height: float) -> None:
    """Color legend of the copies at the top of a chart, spaced by the width of each (abbreviated) name."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    x = 0.0
    for ci, name in enumerate(names):
        label = _short_copy(name)
        d.add(Rect(x, height - 19, 6, 6, fillColor=colors.HexColor(COPY_COLORS[ci % len(COPY_COLORS)]), strokeColor=None))
        d.add(_txt(x + 8, height - 18.2, label, 6, INK))
        x += 8 + stringWidth(label, _fonts["regular"], 6) + 6


def wow_chart(copies: list[dict], width: float, height: float) -> Drawing | None:
    """Wow per revolution at start, middle and end of the side, one bar per copy."""
    rows = [(c["copy"], (c.get("wow") or {}).get("segments") or []) for c in copies]
    rows = [r for r in rows if r[1]]
    if not rows:
        return None
    labels = [_text(s["label"]) for s in rows[0][1]]
    mx = max(0.2, max((s.get("wow_pct") or 0) for _, segs in rows for s in segs))
    mx = math.ceil(mx * 20) / 20
    pl, pr, pt, pb = 22, 4, 26, 13
    d = Drawing(width, height)
    gw = (width - pl - pr) / len(labels)
    bw = min(9, (gw - 10) / len(rows))
    Y = lambda v: pb + v / mx * (height - pt - pb)
    d.add(_txt(0, height - 8, i18n.t("report.chart_wow"), 7.4, INK, font=_fonts["semibold"]))
    v = 0.0
    while v <= mx + 1e-9:
        d.add(Line(pl, Y(v), width - pr, Y(v), strokeColor=colors.HexColor(MUTED if v == 0 else LINE), strokeWidth=0.3))
        d.add(_txt(pl - 3, Y(v) - 2, de(v, 2), 5.6, MUTED, "end"))
        v += 0.05 if mx <= 0.3 else 0.1
    for gi, lb in enumerate(labels):
        x0 = pl + gi * gw + (gw - bw * len(rows)) / 2
        for ci, (_, segs) in enumerate(rows):
            val = (segs[gi].get("wow_pct") or 0) if gi < len(segs) else 0
            d.add(Rect(x0 + ci * bw, Y(0), bw - 1, Y(val) - Y(0), fillColor=colors.HexColor(COPY_COLORS[ci % len(COPY_COLORS)]),
                       strokeColor=None))
        d.add(_txt(pl + gi * gw + gw / 2, 2, lb, 6.2, INK, "middle"))
    _copy_legend(d, [name for name, _ in rows], height)
    return d


def levels_chart(copies: list[dict], width: float, height: float) -> Drawing | None:
    """Level per track vs. master (relative to the median of the tracks), one dot per copy."""
    items = ((copies[0].get("levels") or {}).get("items") or []) if copies else []
    if not items:
        return None
    vals = [x.get("rel_db") for c in copies for x in ((c.get("levels") or {}).get("items") or []) if x.get("rel_db") is not None]
    lim = max(1.5, math.ceil(max(abs(v) for v in vals) * 2) / 2) if vals else 1.5
    pl, pr, pt, pb = 22, 4, 26, 20
    d = Drawing(width, height)
    gw = (width - pl - pr) / len(items)
    Y = lambda v: pb + (v + lim) / (2 * lim) * (height - pt - pb)
    d.add(_txt(0, height - 8, i18n.t("report.chart_levels"), 7.4, INK, font=_fonts["semibold"]))
    for v in (-lim, -lim / 2, 0, lim / 2, lim):
        d.add(Line(pl, Y(v), width - pr, Y(v), strokeColor=colors.HexColor(MUTED if v == 0 else LINE), strokeWidth=0.6 if v == 0 else 0.3))
        d.add(_txt(pl - 3, Y(v) - 2, de(v, 1, sign=v != 0), 5.6, MUTED, "end"))
    for i, it in enumerate(items):
        d.add(_txt(pl + i * gw + gw / 2, 9, str(it.get("pos", "")), 6.4, INK, "middle", _fonts["medium"]))
        d.add(_txt(pl + i * gw + gw / 2, 2, _text(it.get("title", ""))[:max(4, int(gw / 3.2))], 5.2, MUTED, "middle"))
    for ci, c in enumerate(copies):
        for i, x in enumerate((c.get("levels") or {}).get("items") or []):
            if x.get("rel_db") is None or i >= len(items):
                continue
            d.add(Circle(pl + i * gw + gw / 2 + (ci - (len(copies) - 1) / 2) * 5, Y(x["rel_db"]), 2.1,
                         fillColor=colors.HexColor(COPY_COLORS[ci % len(COPY_COLORS)]), strokeColor=None))
    _copy_legend(d, [c["copy"] for c in copies], height)
    return d


# ─── Pages ───────────────────────────────────────────────────────────────────
class _NumberedCanvas(rl_canvas.Canvas):
    """Header and footer with "page x of y" (a second pass over the stored pages once the total is known).
    `meta` (kicker, record, version, date) is set per build on a subclass, see _canvas_maker."""

    meta: dict = {}

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._saved = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for st in self._saved:
            self.__dict__.update(st)
            self._decorate(total)
            super().showPage()
        super().save()

    def _decorate(self, total: int):
        m = self.meta
        page = self._pageNumber
        self.saveState()
        self.setStrokeColor(colors.HexColor(LINE))
        self.setLineWidth(0.5)
        if page > 1:
            self.setFont(_fonts["semibold"], 7.4)
            self.setFillColor(colors.HexColor(ACCENT))
            self.drawString(MARGIN_L, PAGE_H - 12 * mm, m.get("kicker", ""))
            self.setFont(_fonts["regular"], 7.4)
            self.setFillColor(colors.HexColor(MUTED))
            self.drawRightString(PAGE_W - MARGIN_R, PAGE_H - 12 * mm, m.get("record", ""))
            self.line(MARGIN_L, PAGE_H - 14 * mm, PAGE_W - MARGIN_R, PAGE_H - 14 * mm)
        self.line(MARGIN_L, 12 * mm, PAGE_W - MARGIN_R, 12 * mm)
        self.setFont(_fonts["regular"], 6.8)
        self.setFillColor(colors.HexColor(MUTED))
        self.drawString(MARGIN_L, 8 * mm, i18n.t("report.footer", version=m.get("version", ""), date=m.get("date", "")))
        self.drawRightString(PAGE_W - MARGIN_R, 8 * mm, i18n.t("report.page_of", page=page, total=total))
        self.restoreState()


def _canvas_maker(meta: dict):
    """Canvas class carrying this report's header/footer data (a subclass per build, so concurrent reports in different
    languages do not share it)."""
    return type("_ReportCanvas", (_NumberedCanvas,), {"meta": meta})


def _cover(data: dict, st: dict) -> list:
    rec = data["record"]
    out = [Spacer(1, 8 * mm), Paragraph(esc(i18n.t("report.approval_kicker")), st["kicker"]), Spacer(1, 2.5 * mm),
           Paragraph(f'{esc(rec["artist"])} – {esc(rec["title"])}', st["title"]), Spacer(1, 2 * mm),
           Paragraph(esc(i18n.t("report.approval_subtitle", date=i18n.date(data["created_at"]))), st["subtitle"]),
           Spacer(1, 7 * mm)]
    out += [Paragraph(esc(i18n.t("report.recommendation")), st["small"]), Spacer(1, 1.2 * mm),
            _badge(*_verdict(data.get("verdict")), st, big=True), Spacer(1, 2.5 * mm)]
    notes = [(sd["side"], c) for sd in data["sides"] for c in sd["checks"] if c["status"] in ("warn", "bad")]
    for s_, c in notes:
        head = esc(i18n.t("report.side_check", side=s_, name=_text(c["name"])))
        out.append(Table([[_status_dot(c["status"]), Paragraph(f'<font name="{_fonts["semibold"]}">{head}</font> '
                                                               f'{esc(_text(c["text"]))}', st["body"])]], colWidths=[6 * mm, FRAME_W - 6 * mm],
                         hAlign="LEFT", style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                                          ("TOPPADDING", (0, 0), (0, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)])))
    out.append(Spacer(1, 7 * mm))
    # Key facts
    n_copies = max((len(sd["copies"]) for sd in data["sides"]), default=0)
    facts = [[i18n.t("report.record"), f'{rec["artist"]} – {rec["title"]}'],
             [i18n.t("report.copies"), i18n.t("report.copies_per_side", n=n_copies) if n_copies else "–"]]
    for sd in data["sides"]:
        m = sd["master"] or {}
        facts.append([i18n.t("report.master_side", side=sd["side"]), f'{m.get("name", "–")} ({fmin_(m.get("duration_s") or 0)}, '
                                                                       f'{de((m.get("samplerate") or 0) / 1000, 1)} kHz)'])
    facts.append([i18n.t("report.measurement"), i18n.t("report.measurement_text")])
    rows = [[Paragraph(f"<font color='{MUTED}'>{esc(k)}</font>", st["cell"]), Paragraph(esc(v), st["cell"])] for k, v in facts]
    t = Table(rows, colWidths=[32 * mm, FRAME_W - 32 * mm], hAlign="LEFT")
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 2),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 2), ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor(LINE))]))
    out += [Paragraph(esc(i18n.t("report.key_facts")), st["h2"]), t, Spacer(1, 6 * mm)]

    # Overview per side: short figures from the copies
    def med(xs):
        xs = sorted(x for x in xs if x is not None)
        return None if not xs else (xs[len(xs) // 2] if len(xs) % 2 else (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2)
    grades = ["NM", "VG+", "VG", "G+", "G", "F"]
    hdr = [(i18n.t("report.col_side"), "th"), (i18n.t("report.recommendation"), "th"), (i18n.t("report.col_stamper_audible"), "th"),
           (i18n.t("report.col_speed"), "thr"), (i18n.t("report.center_hole"), "thr"), (i18n.t("report.copies"), "th"),
           (i18n.t("report.col_pre_echo"), "thr"), (i18n.t("report.col_clicks_min"), "thr")]
    rows = [_header(hdr, st)]
    for sd in data["sides"]:
        cp = sd["copies"]
        loud = sd.get("loud_common") or []
        n_all = sum(1 for x in loud if x["n"] >= len(cp))
        gm = [grades.index(c["grade_master"]) for c in cp if c.get("grade_master") in grades]
        cut = med([c.get("cut_pct") if c.get("cut_pct") is not None else c.get("speed_pct") for c in cp])
        pe = [c.get("pre_echo_db") for c in cp if c.get("pre_echo_db") is not None]
        rows.append([Paragraph(f'<font name="{_fonts["semibold"]}">{esc(sd["side"])}</font>', st["cell"]),
                     _badge(*_verdict(sd["verdict"]), st),
                     Paragraph(esc(i18n.t("report.stamper_counts", all=n_all, several=len(loud))), st["cell"]),
                     Paragraph(de(cut, 3, sign=True, unit="%"), st["cellr"]), Paragraph(de(med([c.get("ecc_mm") for c in cp]), 2, unit="mm"), st["cellr"]),
                     Paragraph(esc(i18n.t("report.median_grade", grade=grades[int(med(gm) + 0.5)])) if gm else "–", st["cell"]),
                     Paragraph(de(max(pe), 0, unit="dB") if pe else "–", st["cellr"]),
                     Paragraph(de(med([c.get("clicks_vinyl") for c in cp]), 1), st["cellr"])])
    out += [Paragraph(esc(i18n.t("report.overview_title")), st["h2"]),
            _table(rows, [12 * mm, 44 * mm, 34 * mm, 18 * mm, 18 * mm, 20 * mm, 16 * mm, FRAME_W - 162 * mm])]
    out.append(Paragraph(esc(i18n.t("report.overview_note")), st["tiny"]))
    out += [Spacer(1, 6 * mm), Paragraph(esc(i18n.t("report.how_to_read")), st["small"])]
    return out


def _side(data: dict, sd: dict, st: dict) -> list:
    audible = data["audible"]
    copies = sd["copies"]
    out = [PageBreak()]
    head = Table([[Paragraph(esc(i18n.t("report.side_title", side=sd["side"])), st["h1"]),
                   _badge(*_verdict(sd["verdict"]), st)]],
                 colWidths=[28 * mm, None], hAlign="LEFT")
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    m = sd["master"] or {}
    out += [head, Paragraph(esc(i18n.t("report.side_master_line", name=m.get("name", "–"), duration=fmin_(m.get("duration_s") or 0),
                                       khz=de((m.get("samplerate") or 0) / 1000, 1), n=len(copies))), st["small"])]
    # Checks
    rows = []
    for c in sd["checks"]:
        txt = esc(_text(c["text"])) + (f'<br/><font size="6.8" color="{MUTED}">{esc(_text(c["detail"]))}</font>' if c.get("detail") else "")
        rows.append([_status_dot(c["status"]), Paragraph(f'<font name="{_fonts["semibold"]}">{esc(_text(c["name"]))}</font>', st["cell"]),
                     Paragraph(txt, st["cell"])])
    t = Table(rows, colWidths=[6 * mm, 36 * mm, FRAME_W - 42 * mm], hAlign="LEFT")
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                           ("LEFTPADDING", (0, 0), (-1, -1), 2), ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor(LINE)),
                           ("TOPPADDING", (0, 0), (0, -1), 4.5)]))
    out += [Paragraph(esc(i18n.t("report.checks")), st["h2"]), t]
    # Copies
    hdr = [(i18n.t("report.col_copy"), "th"), (i18n.t("report.col_grade"), "th"), (i18n.t("report.col_vs_master"), "th"),
           (i18n.t("report.col_clicks_min"), "thr"), (i18n.t("report.col_pops_min"), "thr"), (i18n.t("report.col_loud"), "thr"),
           (i18n.t("report.col_speed_pct"), "thr"), (i18n.t("report.col_center_hole_mm"), "thr"),
           (i18n.t("report.col_treble_db"), "thr"), (i18n.t("report.col_pre_echo_db"), "thr"), (i18n.t("report.polarity"), "th")]
    rows = [_header(hdr, st)]
    for c in copies:
        R = lambda v: Paragraph(v, st["cellr"])
        rows.append([Paragraph(esc(c["copy"]), st["cell"]), _grade_chip(c.get("grade"), st), _grade_chip(c.get("grade_master"), st),
                     R(de(c.get("clicks_vinyl"), 1)), R(de(c.get("pops_vinyl"), 2)), R(str(c.get("audible", "–"))),
                     R(de(c.get("cut_pct") if c.get("cut_pct") is not None else c.get("speed_pct"), 3, sign=True)),
                     R(de(c.get("ecc_mm"), 2)), R(de(c.get("hf_loss_end_db"), 1, sign=True)), R(de(c.get("pre_echo_db"), 0)),
                     Paragraph(esc(_polarity(c.get("polarity"))), st["cell"])])
    widths = [18 * mm, 10 * mm, 17 * mm, 17 * mm, 16 * mm, 10 * mm, 17 * mm, 19 * mm, 15 * mm, 16 * mm]
    widths.append(FRAME_W - sum(widths))
    out += [Paragraph(esc(i18n.t("report.copies")), st["h2"]), _table(rows, widths, align_right={3, 4, 5, 6, 7, 8, 9}),
            Spacer(1, 1.5 * mm),
            Paragraph(esc(i18n.t("report.copies_note", audible=de(audible, 0))), st["tiny"])]
    # Audible stamper defects
    loud = sd.get("loud_common") or []
    if loud:
        rows = [_header([(i18n.t("report.col_master_time"), "th"), (i18n.t("report.col_kind"), "th"),
                         (i18n.t("report.copies"), "th"), (i18n.t("report.col_strength"), "thr")], st)]
        for x in loud:
            names = ", ".join(sorted((x.get("copies") or {}).keys(), key=lambda s: (len(s), s)))
            rows.append([Paragraph(f'<font name="{_fonts["medium"]}">{ft(x["tm"])}</font>', st["cell"]),
                         Paragraph(esc(_label(KIND_LABEL, x["kind"])), st["cell"]),
                         Paragraph(esc(i18n.t("report.n_of_copies", n=x["n"], total=len(copies), names=names)), st["cell"]),
                         Paragraph(de(x["max_strength"], 0), st["cellr"])])
        out += [KeepTogether([Paragraph(esc(i18n.t("report.audible_on_several")), st["h2"]),
                              _table(rows, [22 * mm, 18 * mm, FRAME_W - 58 * mm, 18 * mm], align_right={3})])]
    # Timeline
    out += [CondPageBreak(75 * mm), Paragraph(esc(i18n.t("report.timeline_title")), st["h2"]), timeline(sd, FRAME_W, audible),
            Spacer(1, 1.5 * mm),
            _legend([("line", STATUS["bad"], i18n.t("report.legend_stamper_all")), ("line", STATUS["warn"], i18n.t("report.legend_on_several")),
                     ("dot", KIND["pop"], i18n.t("report.kind_pop")), ("dot", KIND["click"], i18n.t("report.kind_click")),
                     ("dot", KIND["tick"], i18n.t("report.kind_tick"))], FRAME_W),
            Spacer(1, 1 * mm),
            Paragraph(esc(i18n.t("report.timeline_note")), st["tiny"])]
    # Spectrogram
    if sd.get("spectrogram"):
        sg = sd["spectrogram"]
        out += [CondPageBreak(62 * mm),
                Paragraph(esc(i18n.t("report.spectrogram_copy", copy=sg["copy"])), st["h2"]),
                spectrogram(sg, FRAME_W, 46 * mm, sg["markers"]),
                Paragraph(esc(i18n.t("report.spectrogram_note")), st["tiny"])]
    # Close-ups
    zooms = sd.get("zooms") or []
    if zooms:
        out += [CondPageBreak(40 * mm), Paragraph(esc(i18n.t("report.zooms_title", s=de(0.25, 2))), st["h2"])]
        for z in zooms:
            cells = z["cells"]
            gap, axis = 1.5 * mm, 21
            w = (FRAME_W - axis - (len(cells) - 1) * gap) / max(1, len(cells))
            row_imgs, row_caps, widths = [], [], []
            for i, c in enumerate(cells):
                cw = w + (axis if i == 0 else 0)
                row_imgs.append(spectrogram(c, cw, 25 * mm, [{"t": c["t"], "status": "bad", "line": False}], freq_axis=i == 0))
                row_caps.append(Paragraph(esc(c["copy"]), ParagraphStyle("cap", parent=st["tiny"], alignment=1,
                                                                         leftIndent=axis if i == 0 else 0)))
                widths.append(cw + (gap if i < len(cells) - 1 else 0))
            t = Table([row_imgs, row_caps], colWidths=widths, hAlign="LEFT")
            t.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), gap),
                                   ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
            caption = esc(i18n.t("report.zoom_caption", kind=_label(KIND_LABEL, z["kind"]), n=z["n"], total=len(copies),
                                 strength=de(z["max_strength"], 0)))
            out.append(KeepTogether([Paragraph(f'<font name="{_fonts["semibold"]}">{ft(z["tm"])}</font> · {caption}', st["cell"]),
                                     Spacer(1, 1 * mm), t, Spacer(1, 2 * mm)]))
        out.append(Paragraph(esc(i18n.t("report.zooms_note")), st["tiny"]))
    # Frequency response, wow, level per track
    det = sd["copies_detail"]
    gap3 = 5 * mm
    third = (FRAME_W - 2 * gap3) / 3
    charts = [c for c in (freq_chart(det, third, 50 * mm), wow_chart(det, third, 50 * mm), levels_chart(det, third, 50 * mm)) if c is not None]
    if charts:
        t = Table([charts + [Spacer(1, 1)] * (3 - len(charts))], colWidths=[third + gap3, third + gap3, third], hAlign="LEFT")
        t.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm)]))
        out += [CondPageBreak(66 * mm), Paragraph(esc(i18n.t("report.charts_title")), st["h2"]), t,
                Paragraph(esc(i18n.t("report.charts_note")), st["tiny"])]
    return out


def _appendix(data: dict, st: dict) -> list:
    c = data["criteria"]
    n_hint = c.get("stamper_hint_max", 3)
    above = i18n.t("report.crit_above")
    rows = [_header([(i18n.t("report.col_check"), "th"), (i18n.t("report.col_ok"), "th"), (i18n.t("report.col_note"), "th"),
                     (i18n.t("report.col_not_ok"), "th")], st),
            [i18n.t("report.crit_stamper"), i18n.t("report.crit_none"), i18n.t("report.crit_up_to_per_side", n=n_hint),
             i18n.t("report.crit_more_than", n=n_hint)],
            [i18n.t("report.crit_cut_speed"), f"±{de(c['cut_ok'], 1)} %", i18n.t("report.crit_up_to", v=f"±{de(c['cut_warn'], 1)} %"), above],
            [i18n.t("report.crit_center_hole"), i18n.t("report.crit_up_to", v=de(c["ecc_ok"], 2, unit="mm")),
             i18n.t("report.crit_up_to", v=de(c["ecc_warn"], 2, unit="mm")), above],
            [i18n.t("report.crit_copies"), "NM, VG+", "VG", i18n.t("report.crit_worse")],
            [i18n.t("report.crit_pre_echo"), i18n.t("report.crit_below", v=de(c["pre_echo_warn"], 0, unit="dB")),
             i18n.t("report.crit_from", v=de(c["pre_echo_warn"], 0, unit="dB")), "–"],
            [i18n.t("report.crit_treble"), i18n.t("report.crit_info"), i18n.t("report.crit_from", v=de(c["hf_warn"], 0, unit="dB")), "–"]]
    rows = [rows[0]] + [[Paragraph(esc(r[0]), st["cell"])] + [Paragraph(esc(x), st["cell"]) for x in r[1:]] for r in rows[1:]]
    return [PageBreak(), Paragraph(esc(i18n.t("report.method_title")), st["h1"]), Spacer(1, 2 * mm),
            Paragraph(esc(i18n.t("report.method_text")), st["body"]),
            Spacer(1, 4 * mm), Paragraph(esc(i18n.t("report.thresholds_title")), st["h2"]),
            _table(rows, [70 * mm, 32 * mm, 36 * mm, FRAME_W - 138 * mm]), Spacer(1, 3 * mm),
            Paragraph(esc(i18n.t("report.thresholds_note", audible=de(data["audible"], 0))), st["small"])]


def _doc(buf: io.BytesIO, **meta) -> BaseDocTemplate:
    """A4 document with one full-width frame inside the margins."""
    doc = BaseDocTemplate(buf, pagesize=A4, leftMargin=MARGIN_L, rightMargin=MARGIN_R, topMargin=MARGIN_T, bottomMargin=MARGIN_B,
                          author="vinyl-check", **meta)
    doc.addPageTemplates([PageTemplate(id="p", frames=[Frame(MARGIN_L, MARGIN_B, FRAME_W, PAGE_H - MARGIN_T - MARGIN_B, id="f",
                                                             leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)])])
    return doc


def build(data: dict) -> bytes:
    """Test pressing approval as PDF bytes, in the current request language."""
    _register_fonts()
    st = _styles()
    rec = data["record"]
    record = f'{rec["artist"]} – {rec["title"]}'
    buf = io.BytesIO()
    doc = _doc(buf, title=i18n.t("report.approval_doc_title", record=record), subject=i18n.t("report.approval_doc_subject"),
               creator=f'vinyl-check {data.get("version", "")}')
    story = _cover(data, st)
    for sd in data["sides"]:
        story += _side(data, sd, st)
    story += _appendix(data, st)
    meta = {"kicker": i18n.t("report.approval_kicker"), "record": record, "version": data.get("version", ""),
            "date": i18n.date(data["created_at"])}
    doc.build(story, canvasmaker=_canvas_maker(meta))
    return buf.getvalue()


# ─── Report of one recording ─────────────────────────────────────────────────
GRADE_COL = {"NM": "#2E8B57", "VG+": "#7E9F21", "VG": "#D29A00", "G+": "#E67E22", "G": "#D95524", "F": "#C23B4A"}
# Color stickers per grade: (sticker text as printed, catalog key of the sticker color, color)
STICKER = {"NM": ("Excellent", "report.color_green", "#2a9d3a"), "VG+": ("Great", "report.color_blue", "#1f9fe5"),
           "VG": ("Good", "report.color_yellow", "#f2dc00"), "G+": ("Okay", "report.color_orange", "#f08a1d"),
           "G": ("Bad", "report.color_pink", "#ee5fa4"), "F": ("Very Bad", "report.color_red", "#e3262c")}
# Event regions, event judgments and skip judgments (stored enum values) → catalog keys
REGION_LABEL = {"quiet": "report.region_quiet", "music": "report.region_music", "excluded": "report.region_excluded"}
EVENT_LABEL = {"knackser": "report.event_label_click", "musik": "report.event_label_music", "unsicher": "report.event_label_unsure"}
SKIP_LABEL = {"endrille": "report.skip_label_runout", "kein_sprung": "report.skip_label_no_skip"}
GRADE_MEANING = {"NM": "report.meaning_nm", "VG+": "report.meaning_vg_plus", "VG": "report.meaning_vg",
                 "G+": "report.meaning_g_plus", "G": "report.meaning_g", "F": "report.meaning_f"}


def _grade_block(g: dict, st: dict) -> Table:
    """Grade tile (grade and score) with the matching color sticker and the grading hint."""
    grade = g.get("grade")
    col = GRADE_COL.get(grade or "", MUTED)
    stick = STICKER.get(grade or "")
    d = Drawing(24 * mm, 24 * mm)
    c = colors.HexColor(col)
    d.add(Rect(0, 0, 24 * mm, 24 * mm, rx=4, ry=4, fillColor=colors.Color(c.red, c.green, c.blue, alpha=0.12), strokeColor=c, strokeWidth=0.8))
    d.add(String(12 * mm, 9 * mm, grade or "–", fontName=_fonts["semibold"], fontSize=24, fillColor=c, textAnchor="middle"))
    d.add(String(12 * mm, 3.5 * mm, f'{de(g.get("score"), 0)} / 100', fontName=_fonts["regular"], fontSize=7, fillColor=colors.HexColor(MUTED),
                 textAnchor="middle"))
    txt = [Paragraph(esc(i18n.t("report.grade_suggested")), st["small"])]
    if stick:
        txt.append(Paragraph(f'{esc(i18n.t("report.sticker"))}: <font name="{_fonts["semibold"]}" color="{stick[2]}">{stick[0]}</font> '
                             f'({esc(i18n.t(stick[1]))})', st["cell"]))
    if g.get("hinweis"):
        txt.append(Spacer(1, 1 * mm))
        txt.append(Paragraph(esc(_text(g["hinweis"])), st["tiny"]))
    t = Table([[d, txt]], colWidths=[28 * mm, None], hAlign="LEFT")
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    return t


def _kpis(items: list[tuple[str, str, str]], st: dict) -> Table:
    """Key figure tiles: (title, value, note)."""
    cells = [[Paragraph(esc(a), st["tiny"]), Paragraph(f'<font name="{_fonts["semibold"]}" size="13">{esc(b)}</font>', st["body"]),
              Paragraph(esc(c), st["tiny"])] for a, b, c in items]
    w = (FRAME_W - (len(items) - 1) * 2 * mm) / len(items)
    t = Table([cells], colWidths=[w + 2 * mm] * (len(items) - 1) + [w], hAlign="LEFT")
    style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 2 * mm)]
    t.setStyle(TableStyle(style))
    inner = []
    for c in cells:
        it = Table([[x] for x in c], colWidths=[w])
        it.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor(LINE)), ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(FAINT)),
                                ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (0, 0), 4),
                                ("BOTTOMPADDING", (0, -1), (-1, -1), 5), ("TOPPADDING", (0, 1), (-1, -1), 0), ("ROUNDEDCORNERS", [3, 3, 3, 3])]))
        inner.append(it)
    t = Table([inner], colWidths=[w + 2 * mm] * (len(items) - 1) + [w], hAlign="LEFT")
    t.setStyle(TableStyle(style))
    return t


def clicks_chart(ov: dict, m: dict, width: float, height: float, labels: dict | None = None) -> Drawing:
    """Detections per time slice (stacked: crackle, click, pop; judged false alarms gray); lead-in and run-out
    (excluded, not counted) as a narrow strip below the axis; backward skips as dashed lines."""
    n = len(ov.get("level_db") or ov.get("min") or [])
    dur = max(1.0, n * float(ov.get("bucket_s") or 1)) if n else max([e[0] for e in ov.get("events") or [[1]]] + [1])
    pl, pr, pt, pb = 26, 4, 6, 14
    d = Drawing(width, height)
    B = 2.0 if dur > 600 else 1.0
    nb = int(math.ceil(dur / B))
    up = np.zeros((4, nb))
    down = np.zeros(nb)
    order = {"tick": 0, "click": 1, "pop": 2}
    for ev in ov.get("events") or []:
        t, k, r = ev[0], ev[1], ev[2]
        lab = ev[4] if len(ev) > 4 else None
        bi = min(nb - 1, int(t / B))
        if r == "excluded":
            down[bi] += 1
        else:
            up[3 if lab == "musik" else order.get(k, 0), bi] += 1
    tot = up.sum(axis=0)
    mx = max(1.0, float(np.percentile(tot[tot > 0], 99)) if (tot > 0).any() else 1.0)   # 99th pct.: single bursts do not flatten the rest
    axis_y = pb + (height - pt - pb) * 0.16            # a narrow strip at the bottom for lead-in/run-out, the clicks above
    down_h = axis_y - pb - 1
    mx_down = max(1.0, float(down.max()))
    X = lambda t: pl + t / dur * (width - pl - pr)
    bw = max(0.35, (width - pl - pr) / nb)
    for q in ov.get("quiet") or []:
        d.add(Rect(X(q[0]), axis_y, max(0.3, X(q[1]) - X(q[0])), height - pt - axis_y, fillColor=colors.HexColor("#e8f3ec"), strokeColor=None))
    colmap = [KIND["tick"], KIND["click"], KIND["pop"], "#c9ced6"]
    for bi in range(nb):
        y = axis_y
        for k in range(4):
            v = up[k, bi]
            if v <= 0:
                continue
            hh = min(height - pt - axis_y, v / mx * (height - pt - axis_y))
            hh = min(hh, height - pt - y)
            if hh <= 0:
                break
            d.add(Rect(X(bi * B), y, bw, hh, fillColor=colors.HexColor(colmap[k]), strokeColor=None))
            y += hh
        if down[bi]:
            hh = max(0.8, down[bi] / mx_down * down_h)
            d.add(Rect(X(bi * B), axis_y - hh, bw, hh, fillColor=colors.HexColor("#b9c0ca"), strokeColor=None))
    d.add(Line(pl, axis_y, width - pr, axis_y, strokeColor=colors.HexColor(MUTED), strokeWidth=0.5))
    for v in (mx / 2, mx):
        y = axis_y + v / mx * (height - pt - axis_y)
        d.add(Line(pl, y, width - pr, y, strokeColor=colors.HexColor(LINE), strokeWidth=0.3))
        d.add(_txt(pl - 3, y - 2, str(int(round(v))), 5.6, MUTED, "end"))
    step = 120 if dur > 900 else 60
    t = 0.0
    while t <= dur:
        d.add(_txt(X(t), 2, fmin_(t), 5.8, MUTED, "middle"))
        t += step
    for sk in m.get("skips") or []:
        if sk.get("kind") == "rueckwaerts" and not sk.get("label"):
            d.add(Line(X(sk["t"]), pb, X(sk["t"]), height - pt, strokeColor=colors.HexColor(STATUS["bad"]), strokeWidth=0.9, strokeDashArray=[2, 2]))
    return d


def level_chart(ov: dict, m: dict, width: float, height: float) -> Drawing | None:
    """Level over the side (dBFS) with groove noise, chain noise and music reference lines; stylus-up parts shaded."""
    lv = ov.get("level_db") or []
    if not lv:
        return None
    B = float(ov.get("bucket_s") or 1)
    dur = len(lv) * B
    pl, pr, pt, pb = 26, 4, 6, 14
    y0, y1 = -90.0, 0.0
    d = Drawing(width, height)
    X = lambda t: pl + t / dur * (width - pl - pr)
    Y = lambda v: pb + (min(max(v, y0), y1) - y0) / (y1 - y0) * (height - pt - pb)
    g = ov.get("groove") or [0, dur]
    for a, b, col in ((0, g[0], "#e3e7ec"), (g[1], dur, "#e3e7ec")):
        if b > a:
            d.add(Rect(X(a), pb, X(b) - X(a), height - pt - pb, fillColor=colors.HexColor(col), strokeColor=None))
    for q in ov.get("quiet") or []:
        d.add(Rect(X(q[0]), pb, max(0.3, X(q[1]) - X(q[0])), height - pt - pb, fillColor=colors.HexColor("#e8f3ec"), strokeColor=None))
    for v in range(0, -81, -20):
        d.add(Line(pl, Y(v), width - pr, Y(v), strokeColor=colors.HexColor(LINE), strokeWidth=0.3))
        d.add(_txt(pl - 3, Y(v) - 2, str(v).replace("-", "−"), 5.6, MUTED, "end"))
    for val, col, dash in ((m.get("chain_db"), MUTED, [1, 2]), (m.get("noise_dbfs"), STATUS["ok"], [3, 2]), (m.get("music_ref_dbfs"), MUTED, [0.6, 1.4])):
        if val is not None:
            d.add(Line(pl, Y(val), width - pr, Y(val), strokeColor=colors.HexColor(col), strokeWidth=0.7, strokeDashArray=dash))
    step = max(1, len(lv) // 1500)
    pts = []
    for i in range(0, len(lv), step):
        pts += [X(i * B), Y(lv[i])]
    d.add(PolyLine(pts, strokeColor=colors.HexColor("#3f6fb5"), strokeWidth=0.5))
    st_ = 120 if dur > 900 else 60
    t = 0.0
    while t <= dur:
        d.add(_txt(X(t), 2, fmin_(t), 5.8, MUTED, "middle"))
        t += st_
    return d


def build_recording(data: dict) -> bytes:
    """Report of one recording as PDF bytes, in the current request language."""
    _register_fonts()
    st = _styles()
    rec, r, m, g = data["record"], data["recording"], data["metrics"], data["grading"]
    title = f'{rec.get("artist", "")} – {rec.get("title", "")}' if rec else i18n.t("report.recording_number", id=r["id"])
    side = i18n.t("report.side_title", side=r.get("side", "–"))
    sub = [side, _text(data.get("kind_label")) or r.get("kind", "")]
    if r.get("copy_label"):
        sub.append(r["copy_label"])
    if r.get("cleaning_method"):
        sub.append(r["cleaning_method"])
    sub.append(i18n.t("report.recorded_at", date=i18n.date(r["created_at"])))
    if data.get("profile"):
        sub.append(data["profile"])
    sub.append(i18n.t("report.recording_number", id=r["id"]))
    story = [Spacer(1, 2 * mm), Paragraph(esc(i18n.t("report.recording_kicker")), st["kicker"]), Spacer(1, 1.5 * mm),
             Paragraph(esc(title), st["title"]), Spacer(1, 1 * mm), Paragraph(esc(" · ".join(sub)), st["subtitle"]), Spacer(1, 5 * mm),
             _grade_block(g, st), Spacer(1, 4 * mm)]
    so = m.get("second_opinion") or {}
    lm = m.get("learned") or {}
    music = so.get("clicks_music_confirmed_per_min")
    floor = m.get("floor") or {}
    raw = lambda v, d=1: i18n.t("report.kpi_raw", value=de(v, d))
    music_note = [raw(m.get("clicks_music_per_min"))]
    if lm.get("clicks_music_per_min") is not None:
        music_note.append(i18n.t("report.kpi_estimated_real", value=de(lm.get("clicks_music_per_min"), 1)))
    clicks_note, ticks_note = [raw(m.get("clicks_quiet_per_min"))], [raw(m.get("ticks_quiet_per_min"))]
    if floor:
        clicks_note.append(i18n.t("report.kpi_floor", value=de(floor.get("clicks_per_min"), 1)))
        ticks_note.append(i18n.t("report.kpi_floor", value=de(floor.get("ticks_per_min"), 1)))
    kpis = [(i18n.t("report.kpi_clicks_music_confirmed") if music is not None else i18n.t("report.kpi_clicks_music"),
             de(music if music is not None else m.get("clicks_music_per_min"), 1), " · ".join(music_note)),
            (i18n.t("report.kpi_clicks_quiet_net") if floor else i18n.t("report.kpi_clicks_quiet"),
             de(m.get("clicks_quiet_net_per_min") if floor else m.get("clicks_quiet_per_min"), 1), " · ".join(clicks_note)),
            (i18n.t("report.kpi_ticks_quiet_net") if floor else i18n.t("report.kpi_ticks_quiet"),
             de(m.get("ticks_quiet_net_per_min") if floor else m.get("ticks_quiet_per_min"), 1), " · ".join(ticks_note)),
            (i18n.t("report.kpi_pops_music"), de(m.get("pops_per_min"), 2),
             i18n.t("report.kpi_pops_total", n=(m.get("counts") or {}).get("pop", 0))),
            (i18n.t("report.kpi_noise"), de(m.get("noise_rel_db"), 1, unit="dB"),
             i18n.t("report.kpi_noise_note", dbfs=de(m.get("noise_dbfs"), 1)))]
    story += [_kpis(kpis, st), Spacer(1, 3 * mm)]
    notes = [_text(w) for w in data.get("warnings") or []]
    if data.get("findings"):
        notes.append(i18n.t("report.findings", text=_text(data["findings"])))
    if r.get("notes"):
        notes.append(i18n.t("report.user_note", text=r["notes"]))
    for w in notes:
        story.append(Table([[_status_dot("warn"), Paragraph(esc(w), st["cell"])]], colWidths=[6 * mm, FRAME_W - 6 * mm], hAlign="LEFT",
                           style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                             ("TOPPADDING", (0, 0), (0, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5)])))
    ov = data.get("overview") or {}
    if ov:
        story += [Paragraph(esc(i18n.t("report.clicks_title")), st["h2"]), clicks_chart(ov, m, FRAME_W, 34 * mm), Spacer(1, 1 * mm),
                  _legend([("bar", KIND["pop"], i18n.t("report.kind_pop")), ("bar", KIND["click"], i18n.t("report.kind_click")),
                           ("bar", KIND["tick"], i18n.t("report.kind_tick")), ("bar", "#c9ced6", i18n.t("report.legend_false_alarm")),
                           ("bar", "#e8f3ec", i18n.t("report.legend_quiet")), ("line", STATUS["bad"], i18n.t("report.legend_skip_back"))],
                          FRAME_W),
                  Paragraph(esc(i18n.t("report.clicks_note")), st["tiny"])]
        lc = level_chart(ov, m, FRAME_W, 34 * mm)
        if lc:
            story += [Paragraph(esc(i18n.t("report.level_title")), st["h2"]), lc, Spacer(1, 1 * mm),
                      _legend([("line", "#3f6fb5", i18n.t("report.level_title")),
                               ("line", STATUS["ok"], i18n.t("report.legend_groove_noise", value=de(m.get("noise_dbfs"), 1))),
                               ("line", MUTED, i18n.t("report.legend_music_ref", value=de(m.get("music_ref_dbfs"), 1))),
                               ("bar", "#e8f3ec", i18n.t("report.legend_quiet")), ("bar", "#e3e7ec", i18n.t("report.legend_stylus_up"))],
                              FRAME_W)]
    sg = data.get("spectrogram")
    if sg:
        story += [CondPageBreak(60 * mm), Paragraph(esc(i18n.t("report.spectrogram")), st["h2"]),
                  spectrogram(sg, FRAME_W, 44 * mm, sg.get("markers") or [])]
    # Tracks
    tr = m.get("tracks") or []
    if tr:
        worst = max((x.get("clicks_per_min") or 0) for x in tr)
        rows = [_header([(i18n.t("report.col_no"), "th"), (i18n.t("report.col_title"), "th"), (i18n.t("report.col_duration"), "thr"),
                         (i18n.t("report.col_clicks_min"), "thr"), (i18n.t("report.col_crackle_min"), "thr"),
                         (i18n.t("report.col_pops"), "thr"), (i18n.t("report.col_scratches"), "thr")], st)]
        for x in tr:
            c = x.get("clicks_per_min")
            rows.append([Paragraph(esc(x.get("pos", "")), st["cell"]), Paragraph(esc(x.get("title", "")), st["cell"]),
                         Paragraph(fmin_((x.get("t1") or 0) - (x.get("t0") or 0)), st["cellr"]),
                         Paragraph((f'<font color="{STATUS["bad"]}" name="{_fonts["semibold"]}">{de(c, 1)}</font>' if c == worst and worst > 0 else de(c, 1)), st["cellr"]),
                         Paragraph(de(x.get("ticks_per_min"), 1), st["cellr"]), Paragraph(str(x.get("pops", "–")), st["cellr"]),
                         Paragraph(str(x.get("scratches", 0) or "–"), st["cellr"])])
        story += [CondPageBreak(40 * mm), Paragraph(esc(i18n.t("report.tracks_title")), st["h2"]),
                  _table(rows, [12 * mm, FRAME_W - 102 * mm, 16 * mm, 20 * mm, 20 * mm, 14 * mm, 20 * mm], align_right=set())]
    # Grades per metric
    per = g.get("per_metric") or {}
    if per:
        role = lambda p: (i18n.t("report.role_graded") if p.get("in_grade") else i18n.t("report.role_secondary") if p.get("secondary")
                          else i18n.t("report.role_info"))
        rows = [_header([(i18n.t("report.col_metric"), "th"), (i18n.t("report.col_value"), "thr"), (i18n.t("report.col_grade"), "th"),
                         (i18n.t("report.col_role"), "th")], st)]
        for p in per.values():
            # the label may carry the role in parentheses ("… (info only)"); the role has its own column here
            rows.append([Paragraph(esc(_text(p.get("label", "")).split(" (")[0]), st["cell"]), Paragraph(de(p.get("value"), 2), st["cellr"]),
                         _grade_chip(p.get("grade"), st), Paragraph(esc(role(p)), st["cell"])])
        story += [CondPageBreak(35 * mm), Paragraph(esc(i18n.t("report.grades_title")), st["h2"]),
                  _table(rows, [80 * mm, 22 * mm, 16 * mm, FRAME_W - 118 * mm])]
    # Backward skips
    back = [x for x in m.get("skips") or [] if x.get("kind") == "rueckwaerts"]
    if back:
        rows = [_header([(i18n.t("report.col_time"), "th"), (i18n.t("report.col_grooves"), "thr"), (i18n.t("report.col_repeats"), "thr"),
                         (i18n.t("report.col_position"), "thr"), (i18n.t("report.col_confidence"), "thr"),
                         (i18n.t("report.col_judgment"), "th")], st)]
        for x in back:
            rows.append([Paragraph(ft(x["t"]), st["cell"]), Paragraph(str(x.get("grooves", "–")), st["cellr"]), Paragraph(str(x.get("repeats", "–")), st["cellr"]),
                         Paragraph(de(x.get("position_pct"), 0, unit="%"), st["cellr"]), Paragraph(de(x.get("score"), 2), st["cellr"]),
                         Paragraph(esc(_label(SKIP_LABEL, x.get("label"), "–")), st["cell"])])
        story += [CondPageBreak(30 * mm), Paragraph(esc(i18n.t("report.skips_title")), st["h2"]),
                  _table(rows, [22 * mm, 18 * mm, 26 * mm, 20 * mm, 20 * mm, FRAME_W - 106 * mm])]
    # Strongest detections
    top = data.get("top_events") or []
    if top:
        rows = [_header([(i18n.t("report.col_time"), "th"), (i18n.t("report.col_kind"), "th"), (i18n.t("report.col_region"), "th"),
                         (i18n.t("report.col_strength"), "thr"), (i18n.t("report.col_level_dbfs"), "thr"), (i18n.t("report.col_channel"), "th"),
                         (i18n.t("report.col_nn"), "thr"), (i18n.t("report.col_real"), "thr"), (i18n.t("report.col_judgment"), "th")], st)]
        pct = lambda v: "–" if v is None else f"{round(v * 100)} %"
        for e in top:
            rows.append([Paragraph(ft(e["t"]), st["cell"]), Paragraph(esc(_label(KIND_LABEL, e["kind"])), st["cell"]),
                         Paragraph(esc(_label(REGION_LABEL, e["region"])), st["cell"]), Paragraph(de(e.get("strength"), 0), st["cellr"]),
                         Paragraph(de(e.get("amp_dbfs"), 1), st["cellr"]), Paragraph(["", "L", "R", "L+R"][e.get("ch") or 0] if (e.get("ch") or 0) < 4 else "–", st["cell"]),
                         Paragraph(pct(e.get("gc")), st["cellr"]), Paragraph(pct(e.get("lp")), st["cellr"]),
                         Paragraph(esc(_label(EVENT_LABEL, e.get("label"), "–")), st["cell"])])
        story += [CondPageBreak(50 * mm), Paragraph(esc(i18n.t("report.top_events_title", n=len(top))), st["h2"]),
                  _table(rows, [20 * mm, 18 * mm, 18 * mm, 15 * mm, 19 * mm, 13 * mm, 14 * mm, 15 * mm, FRAME_W - 132 * mm]),
                  Paragraph(esc(i18n.t("report.top_events_note")), st["tiny"])]
    # Master comparison
    mc = data.get("master")
    if mc:
        rows = [[i18n.t("report.mc_grade"), mc.get("grade") or "–"], [i18n.t("report.mc_clicks"), de(mc.get("clicks"), 1)],
                [i18n.t("report.mc_pops"), de(mc.get("pops"), 2)], [i18n.t("report.mc_cut_speed"), de(mc.get("cut_pct"), 3, sign=True, unit="%")],
                [i18n.t("report.center_hole"), de(mc.get("ecc_mm"), 2, unit="mm")], [i18n.t("report.polarity"), _polarity(mc.get("polarity"))]]
        rows = [[Paragraph(f"<font color='{MUTED}'>{esc(a)}</font>", st["cell"]), Paragraph(esc(b), st["cell"])] for a, b in rows]
        t = Table(rows, colWidths=[55 * mm, FRAME_W - 55 * mm], hAlign="LEFT")
        t.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                               ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor(LINE))]))
        story += [CondPageBreak(40 * mm), Paragraph(esc(i18n.t("report.master_title", name=mc.get("name", ""))), st["h2"]), t]
    # Technical data
    tech = data.get("tech") or []
    if tech:
        rows = [[Paragraph(f"<font color='{MUTED}'>{esc(_text(a))}</font>", st["cell"]), Paragraph(esc(_text(b)), st["cell"])] for a, b in tech]
        t = Table(rows, colWidths=[45 * mm, FRAME_W - 45 * mm], hAlign="LEFT")
        t.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                               ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor(LINE)), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story += [CondPageBreak(50 * mm), Paragraph(esc(i18n.t("report.tech_title")), st["h2"]), t]
    # Grading scale
    rows = [_header([(i18n.t("report.col_grade"), "th"), (i18n.t("report.col_meaning"), "th"), (i18n.t("report.sticker"), "th")], st)]
    for gr in ("NM", "VG+", "VG", "G+", "G", "F"):
        rows.append([_grade_chip(gr, st), Paragraph(esc(i18n.t(GRADE_MEANING[gr])), st["cell"]),
                     Paragraph(esc(f'{STICKER[gr][0]} ({i18n.t(STICKER[gr][1])})'), st["cell"])])
    story += [CondPageBreak(45 * mm), Paragraph(esc(i18n.t("report.scale_title")), st["h2"]), _table(rows, [16 * mm, 100 * mm, FRAME_W - 116 * mm]),
              Paragraph(esc(i18n.t("report.scale_note")), st["tiny"])]
    buf = io.BytesIO()
    doc = _doc(buf, title=i18n.t("report.recording_doc_title", title=title, side=r.get("side", "")),
               subject=i18n.t("report.recording_doc_subject"), creator=f'vinyl-check {data.get("version", "")}')
    meta = {"kicker": i18n.t("report.recording_kicker"), "record": f"{title} · {side}", "version": data.get("version", ""),
            "date": i18n.date(time.time())}
    doc.build(story, canvasmaker=_canvas_maker(meta))
    return buf.getvalue()

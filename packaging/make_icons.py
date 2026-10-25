"""App icons for the desktop apps, drawn from the brand signet.

The signet (vinylcheck/static/brand/vinyl-check-signet-inverse.svg) consists only of circles and one straight line
with square ends (see the brand README), so it is rendered here with Pillow, without an SVG library: the shapes are
read from the SVG file, drawn four times larger than needed and scaled down (anti-aliasing), light on a rounded
anthracite tile. Output:

- ``vinyl-check.png``: 1024 px, for Linux and documentation;
- ``vinyl-check.ico``: 16 to 256 px, for the Windows executable, installer and shortcuts;
- ``vinyl-check.icns``: 16 to 1024 px, for the macOS app bundle (tile with the margin of the macOS icon grid).

The PyInstaller spec calls ``build()``, so the repository keeps no generated binary icons.
Usage on its own: ``python packaging/make_icons.py [output directory]`` (default ``build/icons``).
"""

from __future__ import annotations

import math
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
SIGNET = ROOT / "vinylcheck" / "static" / "brand" / "vinyl-check-signet-inverse.svg"
TILE = "#1a1b1c"            # anthracite of the brand
SS = 4                      # supersampling factor


def _shapes(svg: Path) -> tuple[float, list[dict]]:
    """(viewBox size, shapes) of the signet: circles and lines with fill, stroke and stroke width."""
    root = ET.parse(svg).getroot()
    size = float(root.get("viewBox").split()[2])
    shapes = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        style = {"fill": el.get("fill"), "stroke": el.get("stroke"), "width": float(el.get("stroke-width") or 0)}
        if tag == "circle":
            shapes.append({"kind": "circle", "c": (float(el.get("cx")), float(el.get("cy"))), "r": float(el.get("r")),
                           **style})
        elif tag == "path":
            nums = [float(n) for n in re.findall(r"-?\d*\.?\d+", el.get("d"))]
            if len(nums) != 4:
                raise ValueError("the signet's path must be one straight line (M x y x y)")
            shapes.append({"kind": "line", "a": tuple(nums[:2]), "b": tuple(nums[2:]), **style})
    return size, shapes


def render(px: int, margin: float, radius: float) -> Image.Image:
    """The icon at `px` pixels: a tile inset by `margin` (share of the size) with corner `radius` (share of the tile),
    the signet centred on it at 64 % of the tile."""
    big = px * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = margin * big
    tile = big - 2 * m
    d.rounded_rectangle((m, m, big - m, big - m), radius=radius * tile, fill=TILE)
    vb, shapes = _shapes(SIGNET)
    scale = 0.64 * tile / vb
    off = m + (tile - vb * scale) / 2
    pt = lambda x, y: (off + x * scale, off + y * scale)
    for s in shapes:
        w = s["width"] * scale
        if s["kind"] == "circle":
            (cx, cy), r = pt(*s["c"]), s["r"] * scale
            if s["fill"] and s["fill"] != "none":
                d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=s["fill"])
            if s["stroke"] and s["stroke"] != "none" and w:
                ro = r + w / 2                          # SVG strokes are centred on the outline
                d.ellipse((cx - ro, cy - ro, cx + ro, cy + ro), outline=s["stroke"], width=round(w))
        else:
            (ax, ay), (bx, by) = pt(*s["a"]), pt(*s["b"])
            length = math.hypot(bx - ax, by - ay)
            ux, uy = (bx - ax) / length, (by - ay) / length
            h = w / 2                                   # square caps: the line extends by half its width
            ax, ay, bx, by = ax - ux * h, ay - uy * h, bx + ux * h, by + uy * h
            nx, ny = -uy * h, ux * h
            d.polygon([(ax + nx, ay + ny), (bx + nx, by + ny), (bx - nx, by - ny), (ax - nx, ay - ny)],
                      fill=s["stroke"])
    return img.resize((px, px), Image.LANCZOS)


def build(out: Path) -> dict[str, Path]:
    """Write the three icon files to `out` and return their paths by kind (png, ico, icns)."""
    out.mkdir(parents=True, exist_ok=True)
    # macOS: the icon grid leaves about 10 % around the tile; Windows and Linux icons use the full square
    mac = render(1024, margin=0.098, radius=0.225)
    full = render(1024, margin=0.03, radius=0.2)
    paths = {"png": out / "vinyl-check.png", "ico": out / "vinyl-check.ico", "icns": out / "vinyl-check.icns"}
    full.save(paths["png"])
    full.save(paths["ico"], format="ICO", sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
    mac.save(paths["icns"], format="ICNS")
    return paths


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build" / "icons"
    for kind, path in build(target).items():
        print(f"{kind}: {path} ({path.stat().st_size // 1024} KB)")

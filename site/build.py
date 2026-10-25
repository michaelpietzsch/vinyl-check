"""Build the project website: a landing page, a download page and download.json, from the GitHub releases.

Every release asset is classified by its file name (operating system, architecture) and listed with its size and
SHA256 checksum, which GitHub computes for each uploaded asset. Only the standard library is used, so the GitHub
Actions workflow (.github/workflows/pages.yml) needs nothing but Python.

    python site/build.py --repo michaelpietzsch/vinyl-check --out _site            # GITHUB_TOKEN from the environment
    python site/build.py --releases releases.json --out _site                         # offline, e.g. for a preview
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_URL = "https://github.com/{repo}"

OS_LABEL = {"macos": "macOS", "windows": "Windows", "linux": "Linux", "any": "Python (all systems)",
            "source": "Source code", "other": "Other"}
ARCH_LABEL = {"arm64": "arm64", "x64": "x64", "any": "any"}
POPULAR_OS = ("macos", "windows", "any")


# ─── Releases ────────────────────────────────────────────────────────────────

def fetch_releases(repo: str, token: str | None) -> list[dict]:
    """All published releases of `repo`, newest first (GitHub REST API, paginated)."""
    out, page = [], 1
    while True:
        req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases?per_page=100&page={page}",
                                     headers={"Accept": "application/vnd.github+json",
                                              **({"Authorization": f"Bearer {token}"} if token else {})})
        with urllib.request.urlopen(req, timeout=30) as r:
            batch = json.load(r)
        out += batch
        if len(batch) < 100:
            return out
        page += 1


def classify(name: str) -> tuple[str, str]:
    """(os, arch) of a release asset, from the naming convention vinyl-check-<version>-<os>-<arch>[-kind].<ext>."""
    m = re.search(r"-(macos|windows|linux)-(arm64|x64|universal)", name)
    if m:
        return m.group(1), m.group(2)
    if name.endswith("-none-any.whl"):
        return "any", "any"                     # pure Python wheel: Linux, Raspberry Pi, macOS, Windows
    if name.endswith((".tar.gz", ".zip")) and "-" in name:
        return "source", "any"
    if name.endswith(".AppImage"):
        return "linux", "x64"
    return "other", "any"


def kind_of(name: str) -> str:
    """Short description of what the file is."""
    if name.endswith(".dmg"):
        return "Disk image with the app"
    if name.endswith("-setup.exe") or name.endswith(".msi"):
        return "Installer"
    if name.endswith("-portable.zip"):
        return "Portable app (ZIP)"
    if name.endswith(".whl"):
        return "Python package (pip install)"
    if name.endswith(".tar.gz"):
        return "Source archive"
    return ""


def releases_model(raw: list[dict]) -> list[dict]:
    """The data of the download page and download.json: published releases, newest first, with their files."""
    rels = [r for r in raw if not r.get("draft")]
    rels.sort(key=lambda r: r.get("published_at") or "", reverse=True)
    stable = [r for r in rels if not r.get("prerelease")]
    latest = stable[0]["tag_name"] if stable else None
    out = []
    for r in rels:
        files = []
        for a in r.get("assets", []):
            os_, arch = classify(a["name"])
            digest = a.get("digest") or ""
            files.append({"name": a["name"], "os": os_, "arch": arch, "kind": kind_of(a["name"]), "size": a["size"],
                          "sha256": digest.split(":", 1)[1] if digest.startswith("sha256:") else None,
                          "url": a["browser_download_url"]})
        order = {"macos": 0, "windows": 1, "linux": 2, "any": 3, "source": 4, "other": 5}
        first = (".dmg", "-setup.exe", ".msi")                 # the installer before the portable variant
        files.sort(key=lambda f: (order.get(f["os"], 9), f["arch"], not f["name"].endswith(first), f["name"]))
        out.append({"version": r["tag_name"].lstrip("v"), "tag": r["tag_name"], "date": (r.get("published_at") or "")[:10],
                    "latest": r["tag_name"] == latest, "prerelease": bool(r.get("prerelease")),
                    "notes": r.get("html_url"), "files": files})
    return out


# ─── Pages ───────────────────────────────────────────────────────────────────

def size_str(n: int) -> str:
    return f"{n / 1048576:.2f} MiB" if n >= 1048576 else f"{n / 1024:.0f} KiB"


def page(title: str, body: str, repo: str, depth: int = 0) -> str:
    up = "../" * depth
    gh = REPO_URL.format(repo=repo)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="icon" href="{up}assets/vinyl-check-signet.svg" type="image/svg+xml">
<link rel="stylesheet" href="{up}assets/site.css">
</head>
<body>
<header class="top">
  <a class="brand" href="{up}index.html" aria-label="vinyl-check home"><picture>
    <source media="(prefers-color-scheme: light)" srcset="{up}assets/vinyl-check-logo.svg">
    <img src="{up}assets/vinyl-check-logo-inverse.svg" alt="vinyl-check" height="28"></picture></a>
  <nav><a href="{up}download/">Download</a><a href="{gh}/tree/main/docs">Docs</a><a href="{gh}">GitHub</a></nav>
</header>
<main>
{body}
</main>
<footer>
  <span>vinyl-check · free for non-commercial use</span>
  <a href="{gh}/blob/main/LICENSE">License</a>
  <a href="{gh}/blob/main/docs/code-signing.md">Code signing policy</a>
  <a href="{gh}/blob/main/CHANGELOG.md">Changelog</a>
</footer>
</body>
</html>
"""


def landing(repo: str, latest: dict | None) -> str:
    gh = REPO_URL.format(repo=repo)
    version = f" {html.escape(latest['version'])}" if latest else ""
    return f"""<section class="hero">
  <h1>Measure the condition of your records, and the alignment of the turntable that plays them.</h1>
  <p class="lead">vinyl-check records a side of a record as a WAV file, exactly as the stylus picks it up, and analyses
  it: clicks, pops and crackle, groove noise, a condition grade, the effect of a cleaning, the alignment of your
  turntable with a test record and the comparison with the digital master. It runs on your own hardware, from a cheap
  USB turntable to a high-end chain, and works without a cloud service.</p>
  <p class="actions"><a class="button primary" href="download/">Download{version}</a>
  <a class="button" href="{gh}/blob/main/docs/setup-guide.md">Setup guide</a>
  <a class="button" href="{gh}">Source code</a></p>
</section>
<section class="shots">
  <figure><img src="assets/overview.png" alt="Record overview with grades and recordings"><figcaption>Records and grades</figcaption></figure>
  <figure><img src="assets/recording.png" alt="Recording view with click, level and spectrogram charts"><figcaption>Every click, with level and spectrogram</figcaption></figure>
  <figure><img src="assets/alignment.png" alt="Turntable alignment run with findings and targets"><figcaption>Turntable alignment with a test record</figcaption></figure>
</section>
<section class="features">
  <div><h2>Condition grade</h2><p>Clicks and pops per minute of music, weighted into a grade on the Goldmine scale,
  with the criteria behind it.</p></div>
  <div><h2>Cleaning, before and after</h2><p>Two recordings of the same side, aligned to the sample: what the
  cleaning removed, what is damage.</p></div>
  <div><h2>Turntable alignment</h2><p>Anti-skating, azimuth, crosstalk, resonance, speed and wow &amp; flutter from a
  test record, each with a target and advice.</p></div>
  <div><h2>Master comparison</h2><p>Test pressings measured against the digital master: which clicks are on the vinyl,
  which belong to the music.</p></div>
</section>
"""


def download_page(model: list[dict], repo: str) -> str:
    gh = REPO_URL.format(repo=repo)
    blocks = []
    for rel in model:
        badge = ' <span class="badge latest">LATEST</span>' if rel["latest"] else (
            ' <span class="badge pre">PRE-RELEASE</span>' if rel["prerelease"] else "")
        rows = []
        for f in rel["files"]:
            popular = "1" if f["os"] in POPULAR_OS else "0"
            rows.append(
                f'<tr data-os="{f["os"]}" data-arch="{f["arch"]}" data-popular="{popular}">'
                f'<td><a href="{html.escape(f["url"])}">{html.escape(f["name"])}</a>'
                f'{"<small>" + html.escape(f["kind"]) + "</small>" if f["kind"] else ""}</td>'
                f'<td>{OS_LABEL.get(f["os"], f["os"])}</td><td>{ARCH_LABEL.get(f["arch"], f["arch"])}</td>'
                f'<td class="num">{size_str(f["size"])}</td>'
                f'<td class="sha">{html.escape(f["sha256"] or "–")}</td></tr>')
        blocks.append(f"""<div class="release" data-version="{html.escape(rel["version"])}">
  <div class="release-head"><span><b>{html.escape(rel["version"])}</b> / {html.escape(rel["date"])}{badge}</span>
  <a href="{html.escape(rel["notes"] or gh + "/releases")}">Release notes</a></div>
  <table><colgroup><col class="c-name"><col class="c-os"><col class="c-arch"><col class="c-size"><col class="c-sha"></colgroup>
  <thead><tr><th>File name</th><th>OS</th><th>Arch</th><th class="num">Size</th><th>SHA256 checksum</th></tr></thead>
  <tbody>{"".join(rows) or '<tr><td colspan="5">No files</td></tr>'}</tbody></table>
</div>""")
    return f"""<h1>Download</h1>
<p>vinyl-check is available as an app for macOS and Windows, as a Python package for Linux and the Raspberry Pi
(<code>pip install vinyl_check-…-py3-none-any.whl</code>, see the <a href="{gh}#quick-start">quick start</a>), and as
source code. The apps are not signed yet; the <a href="{gh}#download">first start</a> needs one extra confirmation.</p>
<p>A machine-readable list of all releases is available at <a href="../download.json">download.json</a>.</p>
<div class="filters">
  <label>Operating system<select id="f-os">
    <option value="popular">popular</option><option value="all">all</option><option value="macos">macOS</option>
    <option value="windows">Windows</option><option value="linux">Linux</option>
    <option value="any">Python (all systems)</option><option value="source">Source code</option></select></label>
  <label>Architecture<select id="f-arch">
    <option value="all">all</option><option value="arm64">arm64</option><option value="x64">x64</option>
    <option value="any">any</option></select></label>
</div>
<section class="box">
  <h2>vinyl-check</h2>
  <p class="muted">Vinyl record condition, cleaning comparison, turntable alignment and master comparison.
  <a class="right" href="{gh}">{html.escape(repo)}</a></p>
  {"".join(blocks) or "<p>No releases yet.</p>"}
</section>
<h2 class="verify">Verify a download</h2>
<p>Compare the SHA256 checksum of the downloaded file with the table:</p>
<pre>shasum -a 256 vinyl-check-*.dmg                  # macOS
certutil -hashfile vinyl-check-*-setup.exe SHA256  # Windows
sha256sum vinyl_check-*.whl                        # Linux</pre>
<script>
// Filter the tables by operating system and architecture; a release without matching files is hidden.
const fos = document.getElementById("f-os"), farch = document.getElementById("f-arch");
function filter() {{
  document.querySelectorAll(".release").forEach((rel) => {{
    let shown = 0;
    rel.querySelectorAll("tbody tr[data-os]").forEach((tr) => {{
      const os = fos.value === "all" || (fos.value === "popular" ? tr.dataset.popular === "1" : tr.dataset.os === fos.value);
      const ok = os && (farch.value === "all" || tr.dataset.arch === farch.value);
      tr.hidden = !ok; shown += ok;
    }});
    rel.hidden = !shown;
  }});
}}
fos.onchange = farch.onchange = filter; filter();
</script>
"""


CSS = """:root { --bg:#141516; --card:#202224; --ink:#e9eaeb; --muted:#9da1a5; --line:#34373a; --accent:#e05a48;
  --link:#7fb3ff; --ok:#58c08a; --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace; color-scheme: dark; }
@media (prefers-color-scheme: light) { :root { --bg:#eef0f1; --card:#fff; --ink:#1a1b1c; --muted:#5f6468;
  --line:#d5d8db; --accent:#b7382c; --link:#1a5fb4; --ok:#2a7a4c; color-scheme: light; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); line-height: 1.55;
  font: 16px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
a { color: var(--link); text-decoration: none; } a:hover { text-decoration: underline; }
.top { display: flex; align-items: center; justify-content: space-between; gap: 1rem; padding: .9rem 1.5rem;
  border-bottom: 1px solid var(--line); }
.top nav { display: flex; gap: 1.4rem; } .top nav a { color: var(--ink); }
.brand img { display: block; }
main { max-width: 1180px; margin: 0 auto; padding: 1.5rem 1.5rem 3rem; }
h1 { font-size: 2.2rem; line-height: 1.2; margin: 1.2rem 0 1rem; }
.hero h1 { max-width: 46rem; } .lead { font-size: 1.12rem; color: var(--muted); max-width: 52rem; }
.actions { display: flex; gap: .7rem; flex-wrap: wrap; margin: 1.4rem 0 2rem; }
.button { border: 1px solid var(--line); border-radius: 8px; padding: .6rem 1.1rem; color: var(--ink);
  background: var(--card); font-weight: 600; }
.button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
.shots { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 1rem; }
.shots img { width: 100%; aspect-ratio: 16 / 10; object-fit: cover; object-position: top left;
  border: 1px solid var(--line); border-radius: 8px; display: block; }
figure { margin: 0; } figcaption { color: var(--muted); font-size: .9rem; margin-top: .4rem; }
.features { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 1.2rem; margin-top: 2rem; }
.features h2 { font-size: 1.1rem; margin: 0 0 .3rem; } .features p { margin: 0; color: var(--muted); }
.filters { display: flex; gap: 1rem; margin: 1.5rem 0 1rem; flex-wrap: wrap; }
.filters label { display: grid; gap: .3rem; font-size: .9rem; }
select { min-width: 12rem; padding: .5rem .6rem; border-radius: 6px; border: 1px solid var(--line);
  background: var(--card); color: var(--ink); font: inherit; }
.box { border: 1px solid var(--line); border-radius: 6px; padding: 1rem 1.1rem; background: var(--card); }
.box h2 { margin: 0 0 .2rem; font-size: 1.6rem; } .muted { color: var(--muted); } .right { float: right; }
.release { border: 1px solid var(--line); border-radius: 4px; margin-top: 1rem; overflow-x: auto; }
.release-head { display: flex; justify-content: space-between; padding: .55rem .7rem; border-bottom: 1px solid var(--line); }
.badge { font-size: .7rem; font-weight: 700; border-radius: 99px; padding: .1rem .5rem; margin-left: .4rem;
  vertical-align: 1px; color: #fff; }
.badge.latest { background: var(--ok); } .badge.pre { background: var(--muted); }
table { width: 100%; border-collapse: collapse; font-size: .92rem; table-layout: fixed; min-width: 760px; }
.c-name { width: 33%; } .c-os { width: 14%; } .c-arch { width: 8%; } .c-size { width: 10%; } .c-sha { width: 35%; }
td a { word-break: break-word; }
th, td { text-align: left; padding: .5rem .7rem; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-weight: 600; } tr:last-child td { border-bottom: 0; }
td small { display: block; color: var(--muted); font-size: .8rem; }
.num { text-align: right; white-space: nowrap; } .sha { font-family: var(--mono); font-size: .78rem; color: var(--muted);
  word-break: break-all; }
pre { background: var(--card); border: 1px solid var(--line); border-radius: 6px; padding: .8rem 1rem; overflow-x: auto;
  font: .85rem/1.5 var(--mono); }
code { font-family: var(--mono); font-size: .88em; }
.verify { margin-top: 2rem; }
footer { display: flex; gap: 1.4rem; flex-wrap: wrap; justify-content: center; padding: 1.5rem; color: var(--muted);
  border-top: 1px solid var(--line); font-size: .9rem; }
@media (max-width: 640px) { .top { flex-direction: column; align-items: flex-start; } h1 { font-size: 1.7rem; } }
"""


def build(raw: list[dict], repo: str, out: Path) -> list[dict]:
    """Write the site to `out` and return the release model."""
    model = releases_model(raw)
    if out.exists():
        shutil.rmtree(out)
    (out / "assets").mkdir(parents=True)
    (out / "download").mkdir()
    brand = ROOT / "vinylcheck" / "static" / "brand"
    for name in ("vinyl-check-logo.svg", "vinyl-check-logo-inverse.svg", "vinyl-check-signet.svg"):
        shutil.copy(brand / name, out / "assets" / name)
    for name in ("overview.png", "recording.png", "alignment.png"):
        shutil.copy(ROOT / "docs" / "images" / name, out / "assets" / name)
    (out / "assets" / "site.css").write_text(CSS, encoding="utf-8")
    latest = next((r for r in model if r["latest"]), None)
    (out / "index.html").write_text(page("vinyl-check", landing(repo, latest), repo), encoding="utf-8")
    (out / "download" / "index.html").write_text(page("Download – vinyl-check", download_page(model, repo), repo, 1),
                                                 encoding="utf-8")
    (out / "download.json").write_text(json.dumps(model, indent=2), encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    return model


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "michaelpietzsch/vinyl-check"))
    ap.add_argument("--releases", type=Path, help="releases as JSON (GitHub API format) instead of fetching them")
    ap.add_argument("--out", type=Path, default=Path("_site"))
    args = ap.parse_args()
    raw = (json.loads(args.releases.read_text(encoding="utf-8")) if args.releases
           else fetch_releases(args.repo, os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")))
    model = build(raw, args.repo, args.out)
    print(f"{len(model)} releases, {sum(len(r['files']) for r in model)} files → {args.out}")


if __name__ == "__main__":
    main()

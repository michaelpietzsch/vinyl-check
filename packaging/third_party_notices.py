"""THIRD-PARTY-NOTICES.txt for the desktop apps: the licenses of everything the bundle redistributes.

The packaged apps contain Python, the PyInstaller bootloader and every Python distribution whose modules PyInstaller
collected, some of them with native libraries inside (libsndfile, PortAudio, OpenBLAS, the GCC runtime, WebView2).
This script writes, for each of them, name, version, license and the full license texts shipped in its dist-info
(``License-File`` entries and LICENSE*, LICENCE*, COPYING*, NOTICE* files), plus sections for the pieces that
dist-info does not cover or that need an explicit mention. Standard library only.

The PyInstaller spec calls ``write()`` with the modules of the analysis, so exactly the bundled distributions are
listed. On its own, ``python packaging/third_party_notices.py OUT`` lists every installed distribution except the
build tools (for a look at the result).
"""

from __future__ import annotations

import importlib.metadata as md
import re
import sys
import sysconfig
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINE = "-" * 100
LICENSE_NAMES = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE)", re.I)
# Distributions that build the app but are not part of it (the PyInstaller bootloader has its own section)
BUILD_ONLY = {"pip", "setuptools", "wheel", "pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "macholib",
              "pefile", "pywin32-ctypes", "vinyl-check"}

PORTAUDIO_LICENSE = """PortAudio Portable Real-Time Audio Library
Copyright (c) 1999-2011 Ross Bencina and Phil Burk

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
documentation files (the "Software"), to deal in the Software without restriction, including without limitation the
rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the
Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE."""


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def top_levels(toc) -> set[str]:
    """Top-level module and directory names of PyInstaller TOC entries (name, path, type): "numpy/_core/x.so" and
    "numpy._core" both give "numpy", "_cffi_backend.cpython-313-darwin.so" gives "_cffi_backend"."""
    out = set()
    for entry in toc:
        name = str(entry[0]).replace("\\", "/")
        first = name.split("/")[0] if "/" in name else name
        out.add(first.split(".")[0])
    return out


def distributions(modules: set[str] | None) -> list[md.Distribution]:
    """The installed distributions providing `modules` (None = all installed), without the build tools."""
    by_name: dict[str, md.Distribution] = {}
    if modules is None:
        for d in md.distributions():
            by_name.setdefault(_norm(d.metadata["Name"]), d)
    else:
        owners = md.packages_distributions()
        for mod in modules:
            for dist_name in owners.get(mod, []):
                try:
                    d = md.distribution(dist_name)
                except md.PackageNotFoundError:
                    continue
                by_name.setdefault(_norm(d.metadata["Name"]), d)
    return [by_name[k] for k in sorted(by_name) if k not in BUILD_ONLY]


def license_of(d: md.Distribution) -> str:
    """License expression, else the short License field, else the license classifiers."""
    m = d.metadata
    if m.get("License-Expression"):
        return m["License-Expression"]
    text = (m.get("License") or "").strip()
    if text and "\n" not in text and len(text) <= 120:
        return text
    classifiers = [c.split("::")[-1].strip() for c in m.get_all("Classifier") or [] if c.startswith("License ::")]
    return ", ".join(classifiers) or "see the license text below"


def license_texts(d: md.Distribution) -> list[tuple[str, str]]:
    """(file name, text) of the license files in a distribution's metadata."""
    wanted = {f.replace("\\", "/") for f in d.metadata.get_all("License-File") or []}
    found: dict[str, str] = {}
    for f in d.files or []:
        p = str(f).replace("\\", "/")
        parts = p.split("/")
        if not parts[0].endswith(".dist-info"):
            continue
        rel = "/".join(parts[2:] if len(parts) > 2 and parts[1] == "licenses" else parts[1:])
        if rel in wanted or LICENSE_NAMES.match(parts[-1]):
            try:
                found.setdefault(rel, Path(f.locate()).read_text(encoding="utf-8", errors="replace").strip())
            except (OSError, UnicodeError):
                pass
    if not found and d.metadata.get("License") and "\n" in d.metadata["License"]:
        found["License (metadata)"] = d.metadata["License"].strip()          # the whole text in the License field
    return sorted(found.items())


def _python_license() -> str:
    for p in (Path(sysconfig.get_paths()["stdlib"]) / "LICENSE.txt", Path(sys.base_prefix) / "LICENSE.txt"):
        if p.is_file():
            return p.read_text(encoding="utf-8", errors="replace").strip()
    return "See https://docs.python.org/3/license.html"


def _pyinstaller_license() -> tuple[str, str]:
    try:
        d = md.distribution("pyinstaller")
    except md.PackageNotFoundError:
        return "?", "See https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt"
    texts = license_texts(d)
    return d.version, "\n\n".join(t for _, t in texts) or "See COPYING.txt of PyInstaller"


def _package_dir(module: str) -> Path | None:
    try:
        import importlib.util
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        return None
    if spec and spec.submodule_search_locations:
        return Path(list(spec.submodule_search_locations)[0])
    return None


def special_sections(modules: set[str] | None, binaries: set[str]) -> list[tuple[str, str, str]]:
    """(title, license, text) of bundled pieces that the dist-info of their Python package does not cover."""
    has = (lambda m: True) if modules is None else (lambda m: m in modules)
    sections = [(f"Python {sys.version.split()[0]}", "Python Software Foundation License Version 2 (PSF-2.0)",
                 "The application contains the Python interpreter and parts of its standard library. Python is built "
                 "with further libraries (among them OpenSSL, SQLite, libffi, zlib, XZ and expat); their licenses are "
                 "listed under \"Licenses and Acknowledgements for Incorporated Software\" at "
                 "https://docs.python.org/3/license.html\n\n" + _python_license())]
    version, text = _pyinstaller_license()
    sections.append((f"PyInstaller bootloader {version}",
                     "GPL-2.0-or-later with the PyInstaller bootloader exception; runtime hooks Apache-2.0",
                     "The program that starts the application is the PyInstaller bootloader. Its license allows "
                     "distributing it with applications under any license (bootloader exception).\n\n" + text))
    if has("_soundfile_data") or has("soundfile"):
        d = _package_dir("_soundfile_data")
        copying = d / "COPYING" if d else None
        sections.append(("libsndfile (in soundfile)", "LGPL-2.1-or-later",
                         "libsndfile is included as a separate shared library (the libsndfile file in the "
                         "_soundfile_data folder of the application). It is dynamically linked and can be replaced "
                         "by another build of libsndfile with the same interface. Source: "
                         "https://github.com/libsndfile/libsndfile\n\n"
                         + (copying.read_text(encoding="utf-8", errors="replace").strip()
                            if copying and copying.is_file()
                            else "GNU Lesser General Public License 2.1: https://www.gnu.org/licenses/lgpl-2.1.txt")))
    if has("_sounddevice_data") or has("sounddevice"):
        sections.append(("PortAudio (in sounddevice)", "MIT",
                         "PortAudio is included as a shared library (the libportaudio files in the "
                         "_sounddevice_data folder of the application).\n\n" + PORTAUDIO_LICENSE))
    if any("-asio" in b.lower() for b in binaries):
        sections.append(("Steinberg ASIO SDK (in the ASIO build of PortAudio, Windows)", "Steinberg ASIO SDK license",
                         "libportaudio*-asio.dll contains the Steinberg Audio Stream I/O API. ASIO is a trademark and "
                         "software of Steinberg Media Technologies GmbH. vinyl-check uses ASIO support under the "
                         "Steinberg ASIO SDK Licensing Agreement (proprietary option). License terms: "
                         "https://www.steinberg.net/developers/"))
    if any("webview2" in b.lower() for b in binaries):
        sections.append(("Microsoft WebView2 SDK (in pywebview, Windows)", "Microsoft WebView2 SDK license",
                         "Microsoft.Web.WebView2.Core.dll, Microsoft.Web.WebView2.WinForms.dll and WebView2Loader.dll "
                         "are redistributable files of the Microsoft.Web.WebView2 SDK, (c) Microsoft Corporation, "
                         "distributed under its license: https://www.nuget.org/packages/Microsoft.Web.WebView2/ "
                         "(License). The WebView2 Runtime itself is part of Windows and is not included."))
    for title, lic, path in (("IBM Plex fonts (PDF reports)", "OFL-1.1", "vinylcheck/fonts/LICENSE-IBM-Plex.txt"),
                             ("GrooveClean click detector weights", "MIT", "vinylcheck/weights/LICENSE-grooveclean.txt")):
        p = ROOT / path
        sections.append((title, lic, p.read_text(encoding="utf-8").strip() if p.is_file() else f"See {path}"))
    return sections


def notices(modules: set[str] | None = None, binaries: set[str] = frozenset()) -> str:
    """The complete text. `modules`: top-level names in the bundle (None = everything installed); `binaries`: file
    names of bundled native libraries (to mention the ASIO and WebView2 parts)."""
    m = re.search(r'__version__ = "(.+?)"', (ROOT / "vinylcheck" / "__init__.py").read_text(encoding="utf-8"))
    version = m.group(1) if m else "?"
    sections = special_sections(modules, set(binaries))
    for d in distributions(modules):
        texts = license_texts(d)
        body = "\n\n".join(f"[{name}]\n{text}" for name, text in texts) or "No license file in the package metadata."
        sections.append((f"{d.metadata['Name']} {d.version}", license_of(d), body))
    head = [f"vinyl-check {version}: third-party notices", "=" * 100, "",
            "The vinyl-check application bundles the third-party software listed below. Each part keeps its own",
            "license, quoted here in full as shipped with it. vinyl-check's own license is in LICENSE.txt next to",
            "this file.", "", "Contents", ""]
    head += [f"  {i:3d}. {title}: {lic}" for i, (title, lic, _) in enumerate(sections, 1)]
    out = ["\n".join(head)]
    for i, (title, lic, body) in enumerate(sections, 1):
        out.append(f"{LINE}\n{i}. {title}\nLicense: {lic}\n{LINE}\n\n{body}")
    return "\n\n\n".join(out) + "\n"


def write(path: Path, modules: set[str] | None = None, binaries: set[str] = frozenset()) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(notices(modules, binaries), encoding="utf-8")
    return path


if __name__ == "__main__":
    target = write(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build" / "THIRD-PARTY-NOTICES.txt")
    print(f"{target}: {target.stat().st_size // 1024} KB")

"""License texts at /licenses: vinyl-check's own license and the notices of the third-party software it ships.

The desktop apps carry LICENSE.txt and THIRD-PARTY-NOTICES.txt (written at build time by
packaging/third_party_notices.py from the bundled packages) next to the program; a frozen app finds them in
``sys._MEIPASS``. An installation from source shows the project's LICENSE and the license files that come with the
package (fonts, detector weights); everything else it uses is installed separately, with its own license files.

Server catalog: ``about`` (the headings around the license texts, which stay in their original wording).
"""

from __future__ import annotations

import importlib.metadata
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from . import __version__, i18n

PACKAGE = Path(__file__).resolve().parent
RULE = "=" * 100
# License files inside the package: the bundled IBM Plex fonts and the GrooveClean detector weights
PACKAGE_LICENSES = (("IBM Plex", PACKAGE / "fonts" / "LICENSE-IBM-Plex.txt"),
                    ("GrooveClean", PACKAGE / "weights" / "LICENSE-grooveclean.txt"))


def _bundle() -> Path | None:
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else None


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def own_license() -> str | None:
    """vinyl-check's license: in the app bundle, in a source checkout, or in the metadata of an installed wheel."""
    candidates = [PACKAGE.parent / "LICENSE"]
    if _bundle():
        candidates.insert(0, _bundle() / "LICENSE.txt")
    for p in candidates:
        text = _read(p)
        if text:
            return text
    try:
        dist = importlib.metadata.distribution("vinyl-check")
    except importlib.metadata.PackageNotFoundError:
        return None
    for f in dist.files or []:
        if ".dist-info" in str(f) and f.name.upper().startswith("LICENSE") and "grooveclean" not in str(f).lower():
            try:
                return f.read_text(encoding="utf-8").strip()
            except OSError:
                continue
    return None


def third_party() -> str:
    """The notices of the app bundle, or the license files that ship inside the package."""
    if _bundle():
        text = _read(_bundle() / "THIRD-PARTY-NOTICES.txt")
        if text:
            return text
    parts = [i18n.t("about.third_party_source")]
    for name, path in PACKAGE_LICENSES:
        text = _read(path)
        if text:
            parts += ["", f"--- {name} ({path.relative_to(PACKAGE.parent).as_posix()}) ---", "", text]
    return "\n".join(parts)


def text() -> str:
    """The complete page, headings in the language of the request."""
    # The Windows app records through Steinberg's ASIO (in PortAudio's ASIO build): its trademark notice belongs up here
    return "\n".join([f"vinyl-check {__version__}", i18n.t("about.summary"), i18n.t("audioset.asio_notice"), "",
                      RULE, i18n.t("about.own_license"), RULE, "", own_license() or i18n.t("about.own_license_missing"),
                      "", RULE, i18n.t("about.third_party"), RULE, "", third_party(), ""])


def install(app: FastAPI) -> None:
    @app.get("/licenses", include_in_schema=False)
    def licenses():
        return PlainTextResponse(text(), headers={"Cache-Control": "no-cache"})

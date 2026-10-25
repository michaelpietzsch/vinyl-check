# -*- mode: python -*-
# PyInstaller spec of the vinyl-check desktop apps (one folder, no single-file archive: starts faster and the worker
# process does not unpack anything). Run it from the repository root:
#
#     pip install ".[desktop]" pyinstaller
#     pyinstaller packaging/vinyl-check.spec --noconfirm
#
# Output: macOS dist/vinyl-check.app, Windows dist/vinyl-check/vinyl-check.exe (packed by windows/vinyl-check.iss),
# Linux dist/vinyl-check/vinyl-check. The icons are drawn from the brand signet during the build (make_icons.py), the
# third-party license notices are collected from the bundled packages (third_party_notices.py).
# PyTorch is left out (size; the second opinion is optional and everything else works without it).

import os
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / "packaging")]     # the package from this checkout, and make_icons
import make_icons  # noqa: E402
import third_party_notices  # noqa: E402

VERSION = re.search(r'__version__ = "(.+?)"', (ROOT / "vinylcheck" / "__init__.py").read_text()).group(1)
ICONS = make_icons.build(Path(workpath) / "icons")      # build/vinyl-check/icons unless --workpath says otherwise
NAME = "vinyl-check"

# Interface, catalogs, fonts and detector weights travel as data files next to the modules (the code finds them
# relative to __file__)
datas = collect_data_files("vinylcheck")
# uvicorn picks its protocol and loop implementations by name at run time; the PortAudio capture backend and the QR
# code are imported only when used. sounddevice's PortAudio libraries (on Windows also the ASIO variant) are collected
# by the sounddevice hook of pyinstaller-hooks-contrib.
hiddenimports = collect_submodules("uvicorn") + ["vinylcheck.desktop", "vinylcheck.synth"]
for optional in ("vinylcheck.capture_portaudio", "sounddevice", "_sounddevice", "segno"):
    try:
        __import__(optional)
        hiddenimports.append(optional)
    except ImportError:                               # not part of this version or not installed
        pass
excludes = ["torch", "torchvision", "torchaudio", "tkinter", "_tkinter", "matplotlib", "IPython", "pytest",
            "PyQt5", "PyQt6", "PySide2", "PySide6", "gi", "cefpython3"]


def windows_version_info():
    """Version resource of vinyl-check.exe: product name and version are what Windows (and code signing) read."""
    from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                     VarFileInfo, VarStruct, VSVersionInfo)
    nums = tuple(int(n) for n in re.findall(r"\d+", VERSION)[:3])
    nums = nums + (0,) * (4 - len(nums))
    strings = {"CompanyName": "Michael Pietzsch", "FileDescription": NAME, "FileVersion": VERSION,
               "InternalName": NAME, "LegalCopyright": "Copyright (c) 2026 Michael Pietzsch",
               "OriginalFilename": f"{NAME}.exe", "ProductName": NAME, "ProductVersion": VERSION}
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=nums, prodvers=nums, mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1,
                          subtype=0x0, date=(0, 0)),
        kids=[StringFileInfo([StringTable("040904B0", [StringStruct(k, v) for k, v in strings.items()])]),
              VarFileInfo([VarStruct("Translation", [1033, 1200])])])


a = Analysis(
    [str(ROOT / "packaging" / "vinyl-check-desktop.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
# The sounddevice hook collects the PortAudio builds of all platforms; outside Windows the DLLs are dead weight
if sys.platform != "win32":
    a.binaries = [e for e in a.binaries if not str(e[0]).lower().endswith(".dll")]
    a.datas = [e for e in a.datas if not str(e[0]).lower().endswith(".dll")]

# License texts of everything the bundle redistributes, and vinyl-check's own license, next to the program (the engine
# serves both at /licenses; the disk image and the Windows installer put them next to the app as well)
NOTICES = third_party_notices.write(
    Path(workpath) / "THIRD-PARTY-NOTICES.txt",
    modules=third_party_notices.top_levels(a.pure) | third_party_notices.top_levels(a.binaries)
    | third_party_notices.top_levels(a.datas),
    binaries={Path(str(b[0])).name for b in a.binaries})
a.datas += [("THIRD-PARTY-NOTICES.txt", str(NOTICES), "DATA"), ("LICENSE.txt", str(ROOT / "LICENSE"), "DATA")]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    console=False,                      # a windowed app; --selftest --output FILE reports without a console
    icon=str(ICONS["icns"] if sys.platform == "darwin" else ICONS["ico"]),
    version=windows_version_info() if sys.platform == "win32" else None,
    upx=False,
    argv_emulation=False,
    # Unsigned builds get an ad-hoc signature. With a Developer ID (docs/code-signing.md) the hardened runtime needs
    # the entitlements, above all audio input.
    codesign_identity=os.environ.get("VC_CODESIGN_IDENTITY") or None,
    entitlements_file=str(ROOT / "packaging" / "macos" / "entitlements.plist")
    if os.environ.get("VC_CODESIGN_IDENTITY") else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{NAME}.app",
        icon=str(ICONS["icns"]),
        bundle_identifier="io.github.michaelpietzsch.vinyl-check",
        version=VERSION,
        info_plist={
            "CFBundleName": NAME,
            "CFBundleDisplayName": NAME,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "CFBundleDevelopmentRegion": "en",
            "CFBundleLocalizations": ["en", "de"],
            "LSApplicationCategoryType": "public.app-category.music",
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": "Copyright (c) 2026 Michael Pietzsch",
            # macOS asks for permission before the first recording and shows this reason; without the key a
            # recording only gets silence
            "NSMicrophoneUsageDescription": "vinyl-check records your turntable through the audio interface.",
        },
    )

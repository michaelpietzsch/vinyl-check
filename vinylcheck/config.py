"""Configuration: /etc/vinyl-check/config.toml (path via VINYLCHECK_CONFIG), otherwise the defaults below.

Where the file and the data live depends on how vinyl-check runs (`config_file`):
- as a service (VINYLCHECK_CONFIG set, as in the systemd unit, or /etc/vinyl-check/config.toml present): that file
  and the service paths below (/mnt/data/vinyl-check, /var/lib/vinyl-check), exactly as on a Raspberry Pi station;
- otherwise (desktop app, or `vinyl-check` started by a user without a service configuration): the platform's usual
  places in the user's profile (`user_dirs`); the configuration file there is optional.

The TOML file has top-level keys for the service (the fields of `Config`) and the sections [detect], [analysis],
[grading] and [alignment] (alignment with a test record). Unknown keys are an error, so typos do not go
unnoticed. Configuration files of versions before 1.0 call the alignment section [justage]; it is still read.
Some settings can also be changed at run time in the web interface; those are stored in the database and win.

Single values can be overridden by environment variables (handy for development), see `load()`:
VINYLCHECK_DATA_DIR, VINYLCHECK_PORT, VINYLCHECK_FAKE_SOURCE, VINYLCHECK_REQUIRE_MOUNT, …
"""

from __future__ import annotations

import logging
import os
import sys
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

from .analysis import AnalysisConfig
from .detect import DetectParams
from .grading import GradingConfig
from .alignment import AlignmentConfig
from .learning import FEATURE_VERSIONS
from .vinylmind import DEFAULT_VINYL_MIND_URL

log = logging.getLogger("vinylcheck.config")


@dataclass(frozen=True)
class Config:
    data_dir: Path = Path("/mnt/data/vinyl-check")      # recordings (large) → fast data disk
    state_dir: Path = Path("/var/lib/vinyl-check")      # database (small, valuable) → system disk
    require_mount: str = ""        # e.g. "/mnt/data": no recording without this mount (keeps the SD card free)
    host: str = "0.0.0.0"
    port: int = 8080
    language: str = "en"           # interface language until a user chooses one; "auto" = follow the browser
    access_code: str = ""          # other devices must enter this code once (then a cookie); empty = open network
    device: str = ""               # empty = first USB capture device
    rate: int = 48000
    channels: int = 2
    capture_channels: int = 0      # channels at the device, 0 = same as `channels` (some interfaces only do 4)
    input_channels: tuple[int, ...] = ()  # which of them are left, right (1-based); empty = the first `channels`
    sample_format: str = "S16_LE"
    max_record_minutes: int = 45
    autostop_end_groove: bool = True   # recording stops by itself when the stylus runs in the run-out groove
    min_free_gb: float = 2.0
    retention_days: int = 30       # delete original audio after this many days (measurements stay), 0 = never
    discogs_sync_minutes: int = 15 # sync the collection with Discogs this often (1 request per 100 records)
    clip_grace_s: float = 5.0      # clipping: grace period after cueing and before lifting the stylus (seconds)
    clip_mention_samples: int = 200    # … from this many clipped samples in the music it is mentioned
    clip_max_samples: int = 500        # … from this many there is no grade
    engineer_mode: bool = False        # engineer mode: measure recordings against the master of their side
    learn_enabled: bool = True         # learn from master comparisons and verdicts, show estimated real clicks
    learn_grade: bool = False          # … and compute the grade from the estimate (once verified on more records)
    # Feature version of the learning model (learning.FEATURE_VERSIONS): "1" relative to each recording's music
    # reference; "2" (experimental) also relative to the recording's turntable profile. Vinyl-Mind contributions and
    # global models always carry the version in use, and versions are never mixed.
    feature_version: str = "1"
    # Vinyl-Mind server (docs/vinyl-mind.md): the preset that "Reset to default" in the settings restores, so a group
    # of stations can be pointed at a self-hosted hub; contributing and using the model are switched there
    vinyl_mind_url: str = DEFAULT_VINYL_MIND_URL
    # Where the operator of a hub without accounts hands out invitation codes: when set, "Where do I get a code?" in
    # the settings links there
    vinyl_mind_invite_url: str = ""
    discogs_token: str = ""        # personal access token (Discogs → Settings → Developers); empty = Discogs off
    discogs_username: str = ""     # Discogs user name (collection of this account); with OAuth it comes from Discogs
    discogs_consumer_key: str = ""     # OAuth app (Discogs → Settings → Developers → Applications) …
    discogs_consumer_secret: str = ""  # … the account is then authorized in the web interface
    discogs_url: str = ""          # deprecated and ignored; kept so that old configuration files still load
    fake_source: str = ""          # WAV file instead of a turntable (development/tests)
    fake_speed: float = 1.0        # playback speed of fake_source (1 = real time)
    # Audio backend (capture.resolve_backend): "auto" = arecord on Linux when installed (Raspberry Pi), else PortAudio
    audio_backend: str = "auto"    # "auto" | "alsa" | "portaudio"
    wasapi_exclusive: bool = True  # Windows: WASAPI exclusive mode, past the mixer; shared mode if not available
    # Windows: "auto" loads PortAudio with ASIO support (SD_ENABLE_ASIO; it carries WASAPI too) and prefers ASIO
    # drivers; false = WASAPI only. true is accepted as well (see capture_portaudio.asio_mode)
    asio: bool | str = "auto"
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    grading: GradingConfig = field(default_factory=GradingConfig)
    alignment: AlignmentConfig = field(default_factory=AlignmentConfig)   # section [alignment]
    # Top-level keys set in the configuration file or by an environment variable, filled in by load(). The audio
    # settings of the web interface use it to tell a value from the file from a default (audio_settings.py).
    file_keys: frozenset = field(default=frozenset(), compare=False, repr=False, metadata={"internal": True})

    @property
    def db_path(self) -> Path:
        return self.state_dir / "vinyl-check.sqlite"

    @property
    def rec_dir(self) -> Path:
        return self.data_dir / "recordings"


def _build(cls, data: dict):
    """Instance of the dataclass `cls` from a TOML table; lists become tuples, unknown keys raise ValueError."""
    known = {f.name: f for f in fields(cls) if not f.metadata.get("internal")}
    kw = {}
    for k, v in data.items():
        if k not in known:
            raise ValueError(f"Unknown configuration key: {k}")
        kw[k] = tuple(v) if isinstance(v, list) else v
    return cls(**kw)


# ─── Where configuration and data live ───────────────────────────────────────
APP_DIR_NAME = "vinyl-check"
SERVICE_CONFIG = Path("/etc/vinyl-check/config.toml")      # written by deploy.sh on a service installation


def user_dirs() -> tuple[Path, Path]:
    """(configuration directory, data directory) in the user's profile, for use without a service configuration.

    macOS: both ~/Library/Application Support/vinyl-check. Windows: both %LOCALAPPDATA%\\vinyl-check (local, not
    roaming: recordings are large and must not be synced with a domain profile). Linux and other systems follow the
    XDG base directories: $XDG_CONFIG_HOME/vinyl-check (~/.config) and $XDG_DATA_HOME/vinyl-check (~/.local/share)."""
    home = Path.home()
    if sys.platform == "darwin":
        d = home / "Library" / "Application Support" / APP_DIR_NAME
        return d, d
    if sys.platform == "win32":
        d = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / APP_DIR_NAME
        return d, d
    conf = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    return conf / APP_DIR_NAME, data / APP_DIR_NAME


def config_file() -> tuple[Path, bool]:
    """The configuration file in effect and whether it belongs to a service installation.

    $VINYLCHECK_CONFIG wins (the systemd unit sets it), then /etc/vinyl-check/config.toml if it exists (a service set
    up by deploy.sh; not looked for on Windows), else the file in the user's configuration directory, which need
    not exist."""
    env = os.environ.get("VINYLCHECK_CONFIG", "")
    if env:
        return Path(env), True
    if sys.platform != "win32" and SERVICE_CONFIG.exists():
        return SERVICE_CONFIG, True
    return user_dirs()[0] / "config.toml", False


def load(path: str | None = None) -> Config:
    """Read the configuration file and apply environment overrides.

    `path` (tests, tools) is read like a service configuration. Without it, `config_file()` decides: a service
    keeps the service paths as defaults; on a desktop, recordings and database default to the user's data directory
    (`user_dirs`). A missing file means defaults."""
    user_mode = False
    if not path:
        file, service = config_file()
        path, user_mode = str(file), not service
    return _load(path, user_mode)


def load_user() -> Config:
    """Configuration of the desktop app: the user's file and directories, even on a computer that also runs a
    service (two engines must not share one database)."""
    return _load(str(user_dirs()[0] / "config.toml"), True)


def _load(path: str, user_mode: bool) -> Config:
    data: dict = {}
    if path and Path(path).exists():
        data = tomllib.loads(Path(path).read_text())
    if user_mode:
        # keys in the user's file and the environment overrides below still win
        data.setdefault("data_dir", str(user_dirs()[1]))
        data.setdefault("state_dir", str(user_dirs()[1]))
    detect = _build(DetectParams, data.pop("detect", {}))
    ana = data.pop("analysis", {})
    analysis = replace(_build(AnalysisConfig, {k: v for k, v in ana.items()}), detect=detect)
    grading = _build(GradingConfig, data.pop("grading", {}))
    align = data.pop("alignment", None)
    if "justage" in data:                     # section name of versions before 1.0
        old = data.pop("justage")
        if align is None:
            log.warning("%s: the section [justage] is now called [alignment]; please rename it", path)
            align = old
        else:
            log.warning("%s: both [alignment] and the old [justage] are present; [justage] is ignored", path)
    alignment = _build(AlignmentConfig, align or {})
    for k in ("data_dir", "state_dir"):
        if k in data:
            data[k] = Path(data[k])
    cfg = replace(_build(Config, data), analysis=analysis, grading=grading, alignment=alignment,
                  file_keys=frozenset(data))
    # TOML may give the version as a number (feature_version = 2); an unknown one is an error like an unknown key
    fv = str(cfg.feature_version)
    if fv not in FEATURE_VERSIONS:
        raise ValueError(f"feature_version must be one of {', '.join(FEATURE_VERSIONS)}, not {cfg.feature_version!r}")
    cfg = replace(cfg, feature_version=fv)

    env = {
        "VINYLCHECK_DATA_DIR": ("data_dir", Path),
        "VINYLCHECK_STATE_DIR": ("state_dir", Path),
        "VINYLCHECK_PORT": ("port", int),
        "VINYLCHECK_LANGUAGE": ("language", str),
        "VINYLCHECK_FAKE_SOURCE": ("fake_source", str),
        "VINYLCHECK_FAKE_SPEED": ("fake_speed", float),
        "VINYLCHECK_REQUIRE_MOUNT": ("require_mount", str),
        "VINYLCHECK_DISCOGS_TOKEN": ("discogs_token", str),
        "VINYLCHECK_DISCOGS_USERNAME": ("discogs_username", str),
        "VINYLCHECK_DISCOGS_CONSUMER_KEY": ("discogs_consumer_key", str),
        "VINYLCHECK_DISCOGS_CONSUMER_SECRET": ("discogs_consumer_secret", str),
        "VINYLCHECK_RETENTION_DAYS": ("retention_days", int),
        "VINYLCHECK_DISCOGS_SYNC_MINUTES": ("discogs_sync_minutes", int),
        "VINYLCHECK_AUDIO_BACKEND": ("audio_backend", str),
    }
    over = {k: conv(os.environ[e]) for e, (k, conv) in env.items() if e in os.environ}
    return replace(cfg, **over, file_keys=cfg.file_keys | set(over)) if over else cfg

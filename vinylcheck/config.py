"""Configuration: /etc/vinyl-check/config.toml (path via VINYLCHECK_CONFIG), otherwise the defaults below.

The TOML file has top-level keys for the service (the fields of `Config`) and the sections [detect], [analysis],
[grading] and [justage] (alignment with a test record). Unknown keys are an error, so typos do not go unnoticed.
Some settings can also be changed at run time in the web interface; those are stored in the database and win.

Single values can be overridden by environment variables (handy for development), see `load()`:
VINYLCHECK_DATA_DIR, VINYLCHECK_PORT, VINYLCHECK_FAKE_SOURCE, VINYLCHECK_REQUIRE_MOUNT, …
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

from .analysis import AnalysisConfig
from .detect import DetectParams
from .grading import GradingConfig
from .alignment import AlignmentConfig


@dataclass(frozen=True)
class Config:
    data_dir: Path = Path("/mnt/data/vinyl-check")      # recordings (large) → fast data disk
    state_dir: Path = Path("/var/lib/vinyl-check")      # database (small, valuable) → system disk
    require_mount: str = ""        # e.g. "/mnt/data": no recording without this mount (keeps the SD card free)
    host: str = "0.0.0.0"
    port: int = 8080
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
    discogs_token: str = ""        # personal access token (Discogs → Settings → Developers); empty = Discogs off
    discogs_username: str = ""     # Discogs user name (collection of this account); with OAuth it comes from Discogs
    discogs_consumer_key: str = ""     # OAuth app (Discogs → Settings → Developers → Applications) …
    discogs_consumer_secret: str = ""  # … the account is then authorized in the web interface
    discogs_url: str = ""          # deprecated and ignored; kept so that old configuration files still load
    fake_source: str = ""          # WAV file instead of a turntable (development/tests)
    fake_speed: float = 1.0        # playback speed of fake_source (1 = real time)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    grading: GradingConfig = field(default_factory=GradingConfig)
    justage: AlignmentConfig = field(default_factory=AlignmentConfig)   # section [justage]: alignment

    @property
    def db_path(self) -> Path:
        return self.state_dir / "vinyl-check.sqlite"

    @property
    def rec_dir(self) -> Path:
        return self.data_dir / "recordings"


def _build(cls, data: dict):
    """Instance of the dataclass `cls` from a TOML table; lists become tuples, unknown keys raise ValueError."""
    known = {f.name: f for f in fields(cls)}
    kw = {}
    for k, v in data.items():
        if k not in known:
            raise ValueError(f"Unknown configuration key: {k}")
        kw[k] = tuple(v) if isinstance(v, list) else v
    return cls(**kw)


def load(path: str | None = None) -> Config:
    """Read the configuration file (`path`, else $VINYLCHECK_CONFIG; missing file = defaults) and apply
    environment overrides."""
    path = path or os.environ.get("VINYLCHECK_CONFIG", "")
    data: dict = {}
    if path and Path(path).exists():
        data = tomllib.loads(Path(path).read_text())
    detect = _build(DetectParams, data.pop("detect", {}))
    ana = data.pop("analysis", {})
    analysis = replace(_build(AnalysisConfig, {k: v for k, v in ana.items()}), detect=detect)
    grading = _build(GradingConfig, data.pop("grading", {}))
    justage = _build(AlignmentConfig, data.pop("justage", {}))
    for k in ("data_dir", "state_dir"):
        if k in data:
            data[k] = Path(data[k])
    cfg = replace(_build(Config, data), analysis=analysis, grading=grading, justage=justage)

    env = {
        "VINYLCHECK_DATA_DIR": ("data_dir", Path),
        "VINYLCHECK_STATE_DIR": ("state_dir", Path),
        "VINYLCHECK_PORT": ("port", int),
        "VINYLCHECK_FAKE_SOURCE": ("fake_source", str),
        "VINYLCHECK_FAKE_SPEED": ("fake_speed", float),
        "VINYLCHECK_REQUIRE_MOUNT": ("require_mount", str),
        "VINYLCHECK_DISCOGS_TOKEN": ("discogs_token", str),
        "VINYLCHECK_DISCOGS_USERNAME": ("discogs_username", str),
        "VINYLCHECK_DISCOGS_CONSUMER_KEY": ("discogs_consumer_key", str),
        "VINYLCHECK_DISCOGS_CONSUMER_SECRET": ("discogs_consumer_secret", str),
        "VINYLCHECK_RETENTION_DAYS": ("retention_days", int),
        "VINYLCHECK_DISCOGS_SYNC_MINUTES": ("discogs_sync_minutes", int),
    }
    over = {k: conv(os.environ[e]) for e, (k, conv) in env.items() if e in os.environ}
    return replace(cfg, **over) if over else cfg

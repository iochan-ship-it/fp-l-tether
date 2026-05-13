"""Configuration loader & validator for fp-l-tether.

Reads ``config.toml`` (or falls back to built-in defaults) and validates it
with Pydantic. Designed so the app starts even if no config file exists.

Usage::

    from fp_l_tether.config import load_config

    cfg = load_config()                       # default search path
    cfg = load_config("/path/to/config.toml") # explicit
    print(cfg.lightroom.watch_folder)         # resolved Path
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Literal

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

from pydantic import BaseModel, Field, field_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class CameraConfig(BaseModel):
    """Camera-side knobs."""

    serial_number: str = ""
    poll_idle_ms: int = 200
    poll_active_ms: int = 100
    chunk_size: int = 1_048_576
    clear_image_db_after_download: bool = True

    # SnapCommand CaptureMode (per Sigma SDK header)
    #   1 = GENERAL_CAPTURE       — AF + shutter (default, normal shooting)
    #   2 = NON_AF_CAPTURE        — skip AF, use current focus (MF scenarios)
    #   3 = AF_DRIVE_ONLY         — focus only, no shutter
    #   6 = START_CAPTURE         — begin sequence shooting
    snap_mode: int = 1

    # USB keep-alive heartbeat. The fp L slips into an internal
    # power-saving state after ~5 min of bus idle (even in PC capture
    # mode) and starts returning 0-byte data phases for vendor get-*
    # opcodes. A periodic benign ping (sigma_get_camera_info) keeps
    # the camera awake; failures are logged but not retried since
    # the watcher's recovery path will catch any drift on the next
    # status poll.
    keep_alive_enabled: bool = True
    keep_alive_interval_s: float = 60.0

    # Burst-aware post-capture quiet window (see TetherDaemon._arm_quiet_window).
    # After a burst settles (snap queue drains), the daemon enforces
    # a no-PTP-traffic window of
    #   ``commit_window_base_s + commit_window_per_shot_s * (burst-1)``
    # to let the camera finish its internal ImageDB consolidation
    # without being pushed into a wedged endpoint by retries.
    # ``burst`` is the number of shots in the just-finished
    # contiguous run (counter resets when the window arms). A
    # contiguous run = shots downloaded back-to-back without a
    # quiet window between them, regardless of wall-clock gap.
    # Tuned against the 2026-05-13 10-shot live test (~13 s
    # commit tail observed → per-shot=1.0 picks 14 s of safety).
    commit_window_base_s: float = 5.0
    commit_window_per_shot_s: float = 1.0


class OutputConfig(BaseModel):
    """File naming and conflict resolution."""

    root: Path = Field(default=Path("~/Pictures/Tether"))
    filename_template: str = "{session}_{shot:04d}.{ext}"
    session_template: str = "{date}_{name}"
    default_item: str = "untitled"
    auto_increment_shot: bool = True
    on_conflict: Literal["skip", "overwrite", "rename"] = "rename"

    @field_validator("root", mode="before")
    @classmethod
    def expand_root(cls, v: object) -> Path:
        if isinstance(v, str):
            return Path(os.path.expanduser(v))
        if isinstance(v, Path):
            return Path(os.path.expanduser(str(v)))
        raise TypeError(f"output.root must be str or Path, got {type(v)}")


class LightroomConfig(BaseModel):
    """Lightroom Classic integration."""

    mode: Literal["watch", "session"] = "watch"
    watch_folder: Path = Field(default=Path("~/Pictures/Tether/_watch"))

    @field_validator("watch_folder", mode="before")
    @classmethod
    def expand_watch(cls, v: object) -> Path:
        if isinstance(v, str):
            return Path(os.path.expanduser(v))
        if isinstance(v, Path):
            return Path(os.path.expanduser(str(v)))
        raise TypeError(
            f"lightroom.watch_folder must be str or Path, got {type(v)}"
        )


class UIConfig(BaseModel):
    """Floating panel preferences."""

    panel_position: Literal["top-right", "top-left", "bottom-right", "bottom-left"] = "top-right"
    panel_opacity: float = 0.95
    show_thumbnails: bool = False  # Phase 1.2
    shutter_key: str = "space"
    quit_key: str = "cmd+q"


class LiveViewConfig(BaseModel):
    """Live view streaming preferences.

    ``pause_during_snap``: when True, the daemon explicitly suspends
    the LiveViewStream thread for the duration of each snap+download
    transaction. This eliminates the first-snap busy storm (~5 s of
    PTP_RC_DeviceBusy observed when LV runs through a full capture).

    ``busy_backoff_ms`` / ``max_consecutive_busy``: defense in depth.
    If a busy still slips through, sleep for that many ms; if N busies
    happen in a row, halve the effective target_fps (recovers slowly
    after sustained success).
    """

    enabled: bool = True
    # 10 fps is the sustained ceiling for fp L (rate-tested 2026-05-13).
    # 15 fps reliably triggers PTP_RC_DeviceBusy (0x2019) and stalls
    # the bulk endpoint; 12 fps untested. Stay conservative.
    target_fps: int = 10
    # When True, suspend LV across the full snap → poll → download →
    # clear window. This eliminates the 0x2019 busy storm the camera
    # otherwise produces while committing the capture, at the cost of
    # a ~5 s LV freeze per shot (matches DNG download time at the
    # fp L's ~5 MB/s bulk-out rate). Acceptable for studio tether
    # where the user is focused on Lightroom between shots.
    pause_during_snap: bool = True
    # TODO(Phase 3.x): if a finer-grained trade-off is wanted, add
    # `pause_mode: Literal["full", "snap_only", "off"]` here and wire
    # it through to LiveViewStream + watcher. "full" covers the whole
    # commit+drain (current default; zero busies, 5s LV gap). The
    # plumbing — pause()/resume() on the stream and helper methods
    # on the daemon — is already in place; only the resume-point
    # selection in watcher.py would need to switch on the mode.
    busy_backoff_ms: int = 500
    max_consecutive_busy: int = 5
    # Number of consecutive successful frames before we promote
    # effective_fps by +1 back toward target. Lower = faster recovery
    # from a transient demotion. At 5 fps, 10 frames ≈ 2 s.
    promote_after_consecutive_ok: int = 10
    # When the daemon resumes LV after a snap+download, the camera
    # often returns one or two 0x2019 busies as its commit state
    # settles. These are NOT a rate problem — counting them toward
    # demotion drags effective_fps down inappropriately. Within this
    # window after each resume(), busies are still logged + back-off
    # is applied, but they don't tick max_consecutive_busy.
    post_snap_busy_grace_s: float = 1.5
    # The very first snap of a session frequently triggers a longer
    # busy burst even with pause/resume around the full commit
    # window. Don't let that one-off drag the session-long
    # effective_fps down — within this window from stream start,
    # busies are tolerated without demotion.
    first_storm_grace_s: float = 30.0
    # Note: the old ``resume_delay_after_capture_s`` knob was superseded
    # by the burst-aware quiet window in CameraConfig
    # (commit_window_base_s / commit_window_per_shot_s). LV resume is
    # now centrally lifted by the daemon's main loop once the quiet
    # window expires, so this LiveViewConfig field is no longer needed.


class TelemetryConfig(BaseModel):
    """Logging behavior."""

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    json_log_file: Path | None = None
    text_log_file: Path | None = None

    @field_validator("json_log_file", "text_log_file", mode="before")
    @classmethod
    def expand_log_path(cls, v: object) -> Path | None:
        if v is None or v == "":
            return None
        if isinstance(v, str):
            return Path(os.path.expanduser(v))
        if isinstance(v, Path):
            return Path(os.path.expanduser(str(v)))
        raise TypeError(f"log path must be str/Path/None, got {type(v)}")


# ---------------------------------------------------------------------------
# Root model
# ---------------------------------------------------------------------------


class AppConfig(BaseModel):
    """Top-level config."""

    camera: CameraConfig = Field(default_factory=CameraConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    lightroom: LightroomConfig = Field(default_factory=LightroomConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    liveview: LiveViewConfig = Field(default_factory=LiveViewConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


DEFAULT_CONFIG_LOCATIONS = [
    PROJECT_ROOT / "config.toml",
    Path("~/.config/fp-l-tether/config.toml").expanduser(),
]


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load and validate config from TOML, or use defaults if no file exists.

    Search order if ``path`` is None:
      1. ``./config.toml`` in the project root
      2. ``~/.config/fp-l-tether/config.toml``
      3. Built-in defaults
    """
    if path is not None:
        toml_path = Path(os.path.expanduser(str(path)))
        if not toml_path.exists():
            raise FileNotFoundError(f"config file not found: {toml_path}")
    else:
        toml_path = next(
            (p for p in DEFAULT_CONFIG_LOCATIONS if p.exists()),
            None,
        )

    if toml_path is None:
        return AppConfig()  # all defaults

    with toml_path.open("rb") as f:
        raw = tomllib.load(f)
    return AppConfig(**raw)


def print_config(cfg: AppConfig) -> None:
    """Pretty-print the resolved config (useful for debugging)."""
    import json
    print(json.dumps(cfg.model_dump(mode="json"), indent=2, ensure_ascii=False))


if __name__ == "__main__":  # pragma: no cover
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else None)
    print_config(cfg)

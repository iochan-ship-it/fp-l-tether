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

    ``pause_during_snap`` is implemented via a shared PTP lock — the
    live view loop blocks while a snap/download is in flight so the
    USB bulk endpoint isn't contended.
    """

    enabled: bool = True
    target_fps: int = 15
    pause_during_snap: bool = True


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

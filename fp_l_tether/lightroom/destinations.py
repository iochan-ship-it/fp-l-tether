"""Destination resolution for downloaded shots.

Routes between the two Lightroom integration modes:

  - **watch**: ``~/Pictures/Tether/_watch/`` — Lightroom Auto Import
    monitors this folder; every shot lands there, atomic rename triggers
    LR's import. Single flat folder.
  - **session**: ``~/Pictures/Tether/Session_YYYYMMDD_<name>/`` — shots
    go into a per-session sub-folder; LR side does a manual "Synchronize
    Folder" at the end.

The filename template uses keys: ``{session}``, ``{shot:04d}``, ``{date}``,
``{time}``, ``{ext}``, ``{image_id}``, ``{camera_name}``.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from pathlib import Path

from fp_l_tether.config import AppConfig


@dataclass
class Destination:
    """One resolved output destination for a single shot.

    Attributes:
        path:        Final file path (where the bytes go).
        is_watch:    True if this is the Auto Import folder (mode='watch').
        session_dir: For mode='session', the per-session directory.
    """

    path: Path
    is_watch: bool
    session_dir: Path | None = None


def build_destination(
    cfg: AppConfig,
    *,
    shot_index: int,
    session_name: str = "default",
    item_name: str | None = None,
    image_id: int = 0,
    camera_name: str = "fpL",
    file_ext: str = "jpg",
    now: _dt.datetime | None = None,
) -> Destination:
    """Compute where to write the next shot.

    Args:
        cfg:           Loaded AppConfig.
        shot_index:    1-based shot counter within the current session.
        session_name:  Used in filename and session folder template.
        item_name:     Optional sub-identifier (e.g. product code).
        image_id:      Camera-assigned ImageID (low-byte, 0-255).
        camera_name:   "fp", "fpL", etc.
        file_ext:      "jpg", "dng", … (no leading dot).
        now:           Inject for testing; defaults to current local time.

    Returns:
        Destination dataclass with resolved Path.
    """
    if now is None:
        now = _dt.datetime.now()
    date_str = now.strftime("%Y%m%d")
    time_str = now.strftime("%H%M%S")

    fname = cfg.output.filename_template.format(
        session=session_name,
        item=item_name or cfg.output.default_item,
        shot=shot_index,
        date=date_str,
        time=time_str,
        ext=file_ext.lstrip("."),
        image_id=image_id,
        camera_name=camera_name,
    )

    if cfg.lightroom.mode == "watch":
        # Single flat folder; LR Auto Import picks up via watchdog
        out_path = cfg.lightroom.watch_folder / fname
        return Destination(path=out_path, is_watch=True, session_dir=None)

    # mode == "session"
    session_dir_name = cfg.output.session_template.format(
        date=date_str,
        time=time_str,
        name=session_name,
    )
    session_dir = cfg.output.root / session_dir_name
    out_path = session_dir / fname
    return Destination(path=out_path, is_watch=False, session_dir=session_dir)

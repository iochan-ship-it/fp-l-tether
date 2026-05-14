"""Persistent cache of user-dialed camera settings.

The fp / fp L resets its internal DG1/DG2 state to a hard-coded
"PC tether" template the moment USB enumeration completes — before
any PTP traffic can read the user's pre-plug values. This is a
firmware-level reset that Capture One also can't avoid; they work
around it by maintaining their own settings state in the host app.

We do the same. This module owns the on-disk JSON cache. The daemon:

  1. Loads the cache on startup / each (re)connect.
  2. Writes the cached values to the camera right after ``sigma_init``
     (so the user sees their settings restored rather than reset).
  3. Updates the cache whenever the user changes a setting via the
     floating panel — that becomes the new "remembered state".

Cache layout (``~/.fp-l-tether/user_settings.json``)::

    {
        "version": 1,
        "saved_at": "2026-05-13T22:30:00",
        "dg1": {"ISOSpeed": 64, "ShutterSpeed": 112, "Aperture": 32, ...},
        "dg2": {"ImageQuality": 18, "WhiteBalance": 1, "DriveMode": 1, ...}
    }

Writes are atomic (write-temp-then-rename) so a crash mid-save can't
corrupt the cache. Reads gracefully degrade to "no cache" on any
parse / IO error — a missing or broken cache is exactly the same as
a fresh first run.

Permissions: we deliberately chown the cache to the SUDO_USER when
the daemon runs as root (same trick as the atomic image writer in
``fp_l_tether.transfer.atomic``), so the file stays readable / editable
by the human user even though the daemon process is root-owned.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

CACHE_SCHEMA_VERSION = 1
_DEFAULT_CACHE_DIR = Path("~/.fp-l-tether").expanduser()
_CACHE_FILENAME = "user_settings.json"


@dataclass
class SettingsCache:
    """In-memory representation of the on-disk settings cache."""

    dg1: dict[str, int] = field(default_factory=dict)
    dg2: dict[str, int] = field(default_factory=dict)
    saved_at: str | None = None
    version: int = CACHE_SCHEMA_VERSION

    def is_empty(self) -> bool:
        return not self.dg1 and not self.dg2

    def to_dict(self) -> dict[str, Any]:
        # Always stamp the current time on save — ``self.saved_at`` is the
        # value loaded from disk and shouldn't be reused (otherwise the
        # "saved_at" log line lies about when the cache was last persisted).
        return {
            "version": self.version,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "dg1": dict(self.dg1),
            "dg2": dict(self.dg2),
        }

    def update_from(self, *, dg1: dict[str, int] | None = None,
                    dg2: dict[str, int] | None = None) -> bool:
        """Merge new values in, return True if anything actually changed."""
        changed = False
        if dg1:
            for k, v in dg1.items():
                if self.dg1.get(k) != v:
                    self.dg1[k] = v
                    changed = True
        if dg2:
            for k, v in dg2.items():
                if self.dg2.get(k) != v:
                    self.dg2[k] = v
                    changed = True
        return changed


def cache_path(directory: Path | None = None) -> Path:
    """Return the on-disk path of the settings cache."""
    return (directory or _DEFAULT_CACHE_DIR) / _CACHE_FILENAME


def _maybe_chown_to_sudo_user(path: Path) -> None:
    """If running under sudo, hand ownership back to the real user.

    Mirrors the pattern in ``fp_l_tether.transfer.atomic`` so cache
    files written by ``sudo fp-l-tether start`` aren't unreadable
    afterwards from a normal user shell.
    """
    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")
    if sudo_uid is None or sudo_gid is None:
        return
    try:
        os.chown(path, int(sudo_uid), int(sudo_gid))
        # And the parent dir too if we created it — only chown if we own it now.
        parent = path.parent
        if parent.stat().st_uid == 0:
            os.chown(parent, int(sudo_uid), int(sudo_gid))
    except OSError:  # pragma: no cover — best-effort
        pass


def load_settings_cache(path: Path | None = None) -> SettingsCache | None:
    """Read the cache off disk. Returns None on missing / corrupt files.

    Never raises — caller can treat a None return identically to a
    fresh first-run state.
    """
    cp = path or cache_path()
    if not cp.exists():
        return None
    try:
        raw = cp.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("version", 0)
    if version != CACHE_SCHEMA_VERSION:
        # Future-proofing: a version mismatch means the schema changed.
        # Discard rather than risk feeding bad data to the camera.
        return None
    dg1_raw = data.get("dg1", {})
    dg2_raw = data.get("dg2", {})
    if not isinstance(dg1_raw, dict) or not isinstance(dg2_raw, dict):
        return None
    # Coerce values to int; drop anything non-numeric so a corrupt entry
    # can't crash the restore call.
    dg1: dict[str, int] = {}
    for k, v in dg1_raw.items():
        if isinstance(k, str) and isinstance(v, int):
            dg1[k] = v
    dg2: dict[str, int] = {}
    for k, v in dg2_raw.items():
        if isinstance(k, str) and isinstance(v, int):
            dg2[k] = v
    return SettingsCache(
        dg1=dg1,
        dg2=dg2,
        saved_at=data.get("saved_at"),
        version=version,
    )


def save_settings_cache(cache: SettingsCache, path: Path | None = None) -> bool:
    """Atomically write the cache to disk. Returns True on success.

    Failures (disk full, no write permission, etc.) are caught and
    logged as a False return — caller decides whether to surface
    that to the user.
    """
    cp = path or cache_path()
    cp.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(cache.to_dict(), indent=2, ensure_ascii=False)

    # Atomic write: temp file in the same dir, then rename.
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=cp.parent,
            prefix=".user_settings.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
    except OSError:
        return False

    try:
        tmp_path.replace(cp)
    except OSError:
        try:
            tmp_path.unlink()
        except OSError:  # pragma: no cover
            pass
        return False

    _maybe_chown_to_sudo_user(cp)
    return True

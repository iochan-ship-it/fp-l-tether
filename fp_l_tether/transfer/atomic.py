"""Atomic file write — critical for Lightroom Auto Import.

If Lightroom's watcher sees a half-written file, it tries to import it and
fails (or worse, imports a truncated/corrupt asset). The fix is the classic
"write to a sibling tempfile, then rename(2)" pattern: ``rename`` is atomic
on the same filesystem on POSIX, so the destination either doesn't exist
yet or is the fully-written file.

When running under ``sudo`` (required on macOS for libusb kernel-driver
detach), files created by the daemon are owned by ``root``. Lightroom and
the Finder run as the regular user and CANNOT read root-owned files. To
work around this, after each rename we chown the file back to the
``SUDO_USER`` so the desktop apps can pick it up.

Usage::

    from fp_l_tether.transfer.atomic import write_atomic
    write_atomic(Path("~/Pictures/Tether/_watch/shot_0001.jpg").expanduser(), data)
"""

from __future__ import annotations

import logging
import os
import pwd
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def _drop_to_user_ownership(path: Path) -> None:
    """If running under sudo, chown the file back to the invoking user.

    Reads ``SUDO_USER`` env var. No-op if not set or if not root.
    """
    sudo_user = os.environ.get("SUDO_USER")
    if not sudo_user or os.geteuid() != 0:
        return
    try:
        pw = pwd.getpwnam(sudo_user)
        os.chown(path, pw.pw_uid, pw.pw_gid)
        logger.debug(
            "chown %s → %s (uid=%d gid=%d)",
            path.name, sudo_user, pw.pw_uid, pw.pw_gid,
        )
    except (KeyError, OSError) as e:
        logger.warning("could not chown %s to %s: %s", path, sudo_user, e)


def write_atomic(
    path: Path,
    data: bytes,
    *,
    on_conflict: str = "rename",
    fsync: bool = True,
) -> Path:
    """Write ``data`` to ``path`` atomically.

    Steps:
      1. Ensure parent directory exists.
      2. Open a ``NamedTemporaryFile`` in the *same directory* (so rename
         stays on one filesystem and is therefore atomic).
      3. Write all bytes, ``fsync`` the file descriptor.
      4. ``os.rename`` to the final path.

    ``on_conflict`` decides what happens if the destination already exists:
      - ``"rename"``: append ``_001``, ``_002``, … until a free name is found
      - ``"overwrite"``: replace the existing file
      - ``"skip"``: leave the existing file alone, return its path

    Returns the actual path that was written (may differ from ``path`` when
    on_conflict="rename" and a collision occurred).
    """
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Conflict resolution
    if path.exists():
        if on_conflict == "skip":
            logger.info("write_atomic: skipping existing file %s", path)
            return path
        if on_conflict == "rename":
            path = _find_free_name(path)
        elif on_conflict == "overwrite":
            pass  # rename will replace
        else:
            raise ValueError(f"on_conflict must be skip|overwrite|rename, got {on_conflict!r}")

    # NamedTemporaryFile in same dir → rename stays atomic (same filesystem)
    fd, tmp_str = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        # Atomic rename (POSIX guarantees same-filesystem rename is atomic)
        os.rename(tmp, path)
        # If we're running under sudo, give ownership back to the real user
        # so Lightroom / Finder / Quick Look can read the file.
        _drop_to_user_ownership(path)
        logger.debug("write_atomic: wrote %d bytes → %s", len(data), path)
        return path
    except Exception:
        # Clean up tempfile if anything went wrong
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def _find_free_name(path: Path) -> Path:
    """Append _001, _002, … until a non-existing path is found."""
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    for n in range(1, 10_000):
        candidate = parent / f"{stem}_{n:03d}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not find a free name for {path} after 9999 attempts")

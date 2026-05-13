"""File transfer & atomic write helpers.

The daemon (``watcher``) pulls in heavy USB deps (libusb/pyusb), so import
that lazily — atomic write is dependency-free and safe to import anywhere.
"""
from fp_l_tether.transfer.atomic import write_atomic

__all__ = ["write_atomic", "TetherDaemon", "ShotEvent"]


def __getattr__(name: str):  # noqa: ANN202
    """Lazy-import TetherDaemon / ShotEvent to avoid pulling pyusb in tests."""
    if name in ("TetherDaemon", "ShotEvent"):
        from fp_l_tether.transfer.watcher import ShotEvent, TetherDaemon
        return {"TetherDaemon": TetherDaemon, "ShotEvent": ShotEvent}[name]
    raise AttributeError(f"module 'fp_l_tether.transfer' has no attribute {name!r}")

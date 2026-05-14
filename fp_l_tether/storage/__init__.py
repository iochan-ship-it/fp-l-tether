"""Persistent on-disk storage for fp-l-tether daemon state.

The daemon caches a handful of values across sessions so the user's
camera settings survive (a) the camera's internal reset on USB plug,
(b) the USB recovery cycle that re-enumerates the device, and (c)
restarts of the daemon itself.
"""

from fp_l_tether.storage.settings_cache import (
    SettingsCache,
    cache_path,
    load_settings_cache,
    save_settings_cache,
)

__all__ = [
    "SettingsCache",
    "cache_path",
    "load_settings_cache",
    "save_settings_cache",
]

"""Lightroom Classic integration: watch-folder vs session-folder routing."""
from fp_l_tether.lightroom.destinations import (
    Destination,
    build_destination,
    pick_shot_index,
)

__all__ = ["Destination", "build_destination", "pick_shot_index"]

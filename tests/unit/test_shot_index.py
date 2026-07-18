"""Unit tests for the B6 shot-counter selection (Phase 3.18)."""

from __future__ import annotations

from fp_l_tether.lightroom.destinations import pick_shot_index


def test_item_template_uses_per_item_counter() -> None:
    assert pick_shot_index(
        "{session}_{item}_{shot:04d}.{ext}",
        per_item_next=1, session_count=7,
    ) == 1


def test_itemless_template_uses_session_counter() -> None:
    """Without {item}, vase_0001 and bowl_0001 would collide — the
    session-global counter keeps numbering monotonic instead."""
    assert pick_shot_index(
        "{session}_{shot:04d}.{ext}",
        per_item_next=1, session_count=7,
    ) == 7

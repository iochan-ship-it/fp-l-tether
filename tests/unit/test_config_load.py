"""Unit tests for the config loader hardening (Phase 3.15).

Focus areas:
  - Unknown TOML keys are detected (A12) — the pydantic models ignore
    them at load, but the user gets an explicit list instead of a
    silently dead setting.
  - The app_prefs overlay can never crash startup, even on a
    wrong-typed hand-edited cache (A17).
  - validate_assignment rejects invalid overlay values so they can't
    flow into the runtime config (A17).
"""

from __future__ import annotations

import pytest

import fp_l_tether.storage as storage_mod
from fp_l_tether.config import (
    AppConfig,
    _collect_unknown_keys,
    load_config,
)
from fp_l_tether.storage import AppPrefs, SettingsCache


def test_collect_unknown_keys_flags_stale_sections() -> None:
    """The pre-3.x example-config sections must all be flagged."""
    raw = {
        "logging": {"level": "DEBUG"},          # real section is [telemetry]
        "debug": {"save_ptp_trace": False},     # no such model
        "ui": {"mode": "both", "panel_opacity": 0.9},
        "camera": {"snap_mode": 2, "bogus_knob": 1},
        "lightroom": {"mode": "watch", "use_atomic_write": True},
        "liveview": {
            "target_fps": 10,
            "histogram": {"position": "top_right", "nope": 1},
        },
    }
    unknown = set(_collect_unknown_keys(raw))
    assert "[logging]" in unknown
    assert "[debug]" in unknown
    assert "ui.mode" in unknown
    assert "camera.bogus_knob" in unknown
    assert "lightroom.use_atomic_write" in unknown
    assert "liveview.histogram.nope" in unknown
    # Valid keys must NOT be flagged.
    assert "ui.panel_opacity" not in unknown
    assert "camera.snap_mode" not in unknown
    assert "lightroom.mode" not in unknown
    assert "liveview.target_fps" not in unknown
    assert "liveview.histogram.position" not in unknown


def test_collect_unknown_keys_empty_config() -> None:
    assert _collect_unknown_keys({}) == []


def test_bad_typed_app_prefs_never_crashes_load(monkeypatch) -> None:
    """A wrong-typed cache value must be skipped, not kill startup (A17).

    ``watch_folder=123`` makes ``to_config_overrides`` raise TypeError
    (``Path(123)``); before Phase 3.15 that escaped ``load_config`` and
    even read-only CLI commands died with a traceback.
    """
    bad = SettingsCache(app_prefs=AppPrefs(watch_folder=123))  # type: ignore[arg-type]
    monkeypatch.setattr(
        storage_mod, "load_settings_cache", lambda path=None: bad
    )
    cfg = load_config()  # must not raise
    assert isinstance(cfg, AppConfig)


def test_assignment_validation_rejects_bad_values() -> None:
    """validate_assignment guards the overlay's setattr path (A17)."""
    cfg = AppConfig()
    with pytest.raises(ValueError):
        cfg.output.on_conflict = "keep"  # not a valid Literal member
    # Valid assignment still works (and coerces/validates).
    cfg.output.on_conflict = "skip"
    assert cfg.output.on_conflict == "skip"
    with pytest.raises(ValueError):
        cfg.lightroom.mode = 5  # type: ignore[assignment]


def test_overlay_applies_valid_prefs(monkeypatch) -> None:
    """Happy path: valid app_prefs still override the TOML layer."""
    prefs = SettingsCache(
        app_prefs=AppPrefs(
            lightroom_mode="session",
            default_subject="vase",
            on_conflict="skip",
        )
    )
    monkeypatch.setattr(
        storage_mod, "load_settings_cache", lambda path=None: prefs
    )
    cfg = load_config()
    assert cfg.lightroom.mode == "session"
    assert cfg.output.default_item == "vase"
    assert cfg.output.on_conflict == "skip"

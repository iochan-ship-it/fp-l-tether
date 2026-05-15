"""Unit tests for the on-disk settings cache (Phase 3.12 additions).

Focus areas:
  - lv_window section round-trips through to_dict / load
  - Older caches lacking lv_window degrade gracefully to None
  - Corrupt or non-finite frame coords drop frame but preserve detached
  - dg1 / dg2 fields survive a panel-side lv_window-only save (the
    re-load-then-merge contract the panel relies on)
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from fp_l_tether.storage.app_prefs import AppPrefs
from fp_l_tether.storage.settings_cache import (
    CACHE_SCHEMA_VERSION,
    LVWindowState,
    SettingsCache,
    load_settings_cache,
    save_settings_cache,
)


def _cache_file(tmp_path: Path) -> Path:
    return tmp_path / "user_settings.json"


def test_lv_window_round_trip(tmp_path: Path) -> None:
    """lv_window survives save → load with byte-identical frame floats."""
    cp = _cache_file(tmp_path)
    src = SettingsCache(
        dg1={"ISOSpeed": 64},
        dg2={"ImageQuality": 18},
        lv_window=LVWindowState(detached=True, frame=(120.5, 800.0, 720.0, 480.0)),
    )
    assert save_settings_cache(src, path=cp)

    loaded = load_settings_cache(path=cp)
    assert loaded is not None
    assert loaded.lv_window is not None
    assert loaded.lv_window.detached is True
    assert loaded.lv_window.frame == (120.5, 800.0, 720.0, 480.0)
    # dg1 / dg2 came along for the ride
    assert loaded.dg1 == {"ISOSpeed": 64}
    assert loaded.dg2 == {"ImageQuality": 18}


def test_missing_lv_window_section_loads_as_none(tmp_path: Path) -> None:
    """A v1 cache file written before Phase 3.12 must still load fine."""
    cp = _cache_file(tmp_path)
    cp.write_text(
        json.dumps(
            {
                "version": 1,
                "saved_at": "2026-05-13T22:30:00",
                "dg1": {"ISOSpeed": 64},
                "dg2": {},
                # no lv_window section
            }
        ),
        encoding="utf-8",
    )
    loaded = load_settings_cache(path=cp)
    assert loaded is not None
    assert loaded.lv_window is None
    assert loaded.dg1 == {"ISOSpeed": 64}


def test_corrupt_frame_preserves_detached(tmp_path: Path) -> None:
    """Garbage in frame[] → frame=None but detached flag survives."""
    cp = _cache_file(tmp_path)
    cp.write_text(
        json.dumps(
            {
                "version": 1,
                "dg1": {},
                "dg2": {},
                "lv_window": {"detached": True, "frame": ["bogus", 0, 0, 0]},
            }
        ),
        encoding="utf-8",
    )
    loaded = load_settings_cache(path=cp)
    assert loaded is not None
    assert loaded.lv_window is not None
    assert loaded.lv_window.detached is True
    assert loaded.lv_window.frame is None


def test_non_finite_frame_rejected(tmp_path: Path) -> None:
    """NaN / inf in frame is rejected so it can't poison the resolver."""
    cp = _cache_file(tmp_path)
    cp.write_text(
        json.dumps(
            {
                "version": 1,
                "dg1": {},
                "dg2": {},
                # JSON has no NaN literal; use a value the loader will
                # coerce to float and then validate against math.isfinite.
                "lv_window": {"detached": True, "frame": [1e9, 0.0, 100.0, 100.0]},
            }
        ),
        encoding="utf-8",
    )
    loaded = load_settings_cache(path=cp)
    assert loaded is not None
    assert loaded.lv_window is not None
    # 1e9 is finite but out of the sanity range [-50000, 50000] → drop.
    assert loaded.lv_window.frame is None
    assert loaded.lv_window.detached is True


def test_zero_size_frame_rejected(tmp_path: Path) -> None:
    """w/h must be positive — zero-size frame drops to None."""
    cp = _cache_file(tmp_path)
    cp.write_text(
        json.dumps(
            {
                "version": 1,
                "dg1": {},
                "dg2": {},
                "lv_window": {"detached": False, "frame": [100.0, 100.0, 0.0, 480.0]},
            }
        ),
        encoding="utf-8",
    )
    loaded = load_settings_cache(path=cp)
    assert loaded is not None
    assert loaded.lv_window is not None
    assert loaded.lv_window.detached is False
    assert loaded.lv_window.frame is None


def test_lv_window_only_save_preserves_dg1_dg2(tmp_path: Path) -> None:
    """Panel-side flow: load disk → mutate lv_window → save → dg1/dg2 survive.

    This mirrors ``_save_lv_window_state`` in floating_panel.py — the
    panel never touches dg1/dg2 but its writes must not erase them.
    """
    cp = _cache_file(tmp_path)
    # Seed: watcher writes dg1/dg2 (no lv_window section).
    watcher_cache = SettingsCache(
        dg1={"ISOSpeed": 64, "ShutterSpeed": 112},
        dg2={"ImageQuality": 18, "WhiteBalance": 1},
    )
    assert save_settings_cache(watcher_cache, path=cp)

    # Panel-side: re-load, mutate lv_window only, save.
    on_disk = load_settings_cache(path=cp)
    assert on_disk is not None
    assert on_disk.lv_window is None
    on_disk.lv_window = LVWindowState(
        detached=True, frame=(50.0, 50.0, 720.0, 480.0)
    )
    assert save_settings_cache(on_disk, path=cp)

    # Verify both sides of the cache are intact.
    final = load_settings_cache(path=cp)
    assert final is not None
    assert final.dg1 == {"ISOSpeed": 64, "ShutterSpeed": 112}
    assert final.dg2 == {"ImageQuality": 18, "WhiteBalance": 1}
    assert final.lv_window is not None
    assert final.lv_window.detached is True
    assert final.lv_window.frame == (50.0, 50.0, 720.0, 480.0)


def test_is_empty_treats_lv_window_as_content(tmp_path: Path) -> None:
    """A cache with only lv_window set is NOT empty.

    Important for the watcher: ``is_empty()`` gates the seed-from-camera
    fast path, which creates a fresh SettingsCache and would otherwise
    erase a user's saved lv_window state.
    """
    c = SettingsCache(lv_window=LVWindowState(detached=True))
    assert not c.is_empty()
    assert SettingsCache().is_empty()


def test_version_mismatch_drops_cache(tmp_path: Path) -> None:
    """A future schema version makes the loader return None (defensive)."""
    cp = _cache_file(tmp_path)
    cp.write_text(
        json.dumps(
            {
                "version": CACHE_SCHEMA_VERSION + 99,
                "dg1": {},
                "dg2": {},
                "lv_window": {"detached": True, "frame": [0, 0, 100, 100]},
            }
        ),
        encoding="utf-8",
    )
    assert load_settings_cache(path=cp) is None


def test_lv_window_section_omitted_when_state_is_none(tmp_path: Path) -> None:
    """Sessions that never detach must not pollute the JSON with empty keys."""
    cp = _cache_file(tmp_path)
    src = SettingsCache(dg1={"ISOSpeed": 64})
    assert save_settings_cache(src, path=cp)
    raw = json.loads(cp.read_text(encoding="utf-8"))
    assert "lv_window" not in raw


# ---------------------------------------------------------------------
# Phase 3.14 — app_prefs section
# ---------------------------------------------------------------------


def test_app_prefs_round_trip(tmp_path: Path) -> None:
    """app_prefs survives save → load with every field intact."""
    cp = _cache_file(tmp_path)
    src = SettingsCache(
        dg1={"ISOSpeed": 64},
        app_prefs=AppPrefs(
            lightroom_mode="session",
            watch_folder="~/Pictures/Tether/_watch",
            session_root="~/Pictures/Tether",
            default_subject="still_life",
            filename_template="{session}_{shot:04d}.{ext}",
            on_conflict="rename",
            log_dir="~/Library/Logs/fp-l-tether",
            json_log_enabled=True,
        ),
    )
    assert save_settings_cache(src, path=cp)

    loaded = load_settings_cache(path=cp)
    assert loaded is not None
    assert loaded.app_prefs is not None
    p = loaded.app_prefs
    assert p.lightroom_mode == "session"
    assert p.watch_folder == "~/Pictures/Tether/_watch"
    assert p.session_root == "~/Pictures/Tether"
    assert p.default_subject == "still_life"
    assert p.filename_template == "{session}_{shot:04d}.{ext}"
    assert p.on_conflict == "rename"
    assert p.log_dir == "~/Library/Logs/fp-l-tether"
    assert p.json_log_enabled is True


def test_v1_cache_loads_without_app_prefs(tmp_path: Path) -> None:
    """A v1 cache file (Phase 3.12 / 3.13 era) must still load cleanly.

    Critical compat: users with a pre-3.14 cache must not see a crash
    or have their dg1/dg2/lv_window state wiped on next launch.
    """
    cp = _cache_file(tmp_path)
    cp.write_text(
        json.dumps(
            {
                "version": 1,
                "saved_at": "2026-05-13T22:30:00",
                "dg1": {"ISOSpeed": 64, "ShutterSpeed": 112},
                "dg2": {"ImageQuality": 18},
                "lv_window": {
                    "detached": True,
                    "frame": [120.0, 800.0, 720.0, 480.0],
                },
            }
        ),
        encoding="utf-8",
    )
    loaded = load_settings_cache(path=cp)
    assert loaded is not None
    assert loaded.app_prefs is None
    assert loaded.dg1 == {"ISOSpeed": 64, "ShutterSpeed": 112}
    assert loaded.dg2 == {"ImageQuality": 18}
    assert loaded.lv_window is not None
    assert loaded.lv_window.detached is True


def test_app_prefs_omitted_when_empty(tmp_path: Path) -> None:
    """No user overrides → no app_prefs key in JSON, keeping the file tidy."""
    cp = _cache_file(tmp_path)
    src = SettingsCache(dg1={"ISOSpeed": 64}, app_prefs=AppPrefs())
    assert save_settings_cache(src, path=cp)
    raw = json.loads(cp.read_text(encoding="utf-8"))
    assert "app_prefs" not in raw


def test_v2_schema_marker_on_save(tmp_path: Path) -> None:
    """Saving stamps the current schema version (2 after Phase 3.14)."""
    cp = _cache_file(tmp_path)
    src = SettingsCache(dg1={"ISOSpeed": 64})
    assert save_settings_cache(src, path=cp)
    raw = json.loads(cp.read_text(encoding="utf-8"))
    assert raw["version"] == CACHE_SCHEMA_VERSION == 2


def test_reset_app_prefs_preserves_dg1_dg2_and_lv_window(tmp_path: Path) -> None:
    """Reset (app_prefs=None) must not touch camera state or window state.

    Mirrors ``resetToDefaults_`` in preferences_window.py — that flow
    sets ``cache.app_prefs = None`` and saves. dg1/dg2 must survive.
    """
    cp = _cache_file(tmp_path)
    src = SettingsCache(
        dg1={"ISOSpeed": 64, "ShutterSpeed": 112},
        dg2={"ImageQuality": 18},
        lv_window=LVWindowState(detached=True, frame=(50.0, 50.0, 720.0, 480.0)),
        app_prefs=AppPrefs(default_subject="x", watch_folder="~/x"),
    )
    assert save_settings_cache(src, path=cp)

    # Reset: load, blank app_prefs, save.
    cache = load_settings_cache(path=cp)
    assert cache is not None
    cache.app_prefs = None
    assert save_settings_cache(cache, path=cp)

    final = load_settings_cache(path=cp)
    assert final is not None
    assert final.dg1 == {"ISOSpeed": 64, "ShutterSpeed": 112}
    assert final.dg2 == {"ImageQuality": 18}
    assert final.lv_window is not None
    assert final.lv_window.detached is True
    assert final.app_prefs is None


def test_app_prefs_to_config_overrides_paths_expanded() -> None:
    """to_config_overrides expands ``~`` and emits the expected tuples."""
    p = AppPrefs(
        lightroom_mode="watch",
        watch_folder="~/wf",
        session_root="~/sr",
        default_subject="hero",
        filename_template="{ext}",
        on_conflict="overwrite",
        log_dir="~/logs",
        json_log_enabled=False,
    )
    out = p.to_config_overrides()
    assert out[("lightroom", "mode")] == "watch"
    # Paths come back as Path objects with $HOME expanded.
    assert str(out[("lightroom", "watch_folder")]).startswith("/")
    assert str(out[("lightroom", "watch_folder")]).endswith("/wf")
    assert str(out[("output", "root")]).endswith("/sr")
    assert out[("output", "default_item")] == "hero"
    assert out[("output", "filename_template")] == "{ext}"
    assert out[("output", "on_conflict")] == "overwrite"
    assert str(out[("logging", "log_dir")]).endswith("/logs")
    assert out[("logging", "json_log_enabled")] is False


def test_app_prefs_only_non_none_serialised() -> None:
    """Unset fields stay out of the JSON — keeps overrides surgical."""
    p = AppPrefs(default_subject="only-this")
    out = p.to_dict()
    assert out == {"default_subject": "only-this"}


def test_app_prefs_from_dict_ignores_unknown_keys() -> None:
    """Hand-edited cache with stale keys must not crash the loader."""
    p = AppPrefs.from_dict({
        "default_subject": "x",
        "this_field_does_not_exist": 42,
    })
    assert p.default_subject == "x"

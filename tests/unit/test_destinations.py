"""Tests for fp_l_tether.lightroom.destinations.build_destination."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

from fp_l_tether.config import AppConfig, LightroomConfig, OutputConfig
from fp_l_tether.lightroom import build_destination


FIXED_NOW = _dt.datetime(2026, 5, 12, 17, 41, 35)


def _watch_cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.lightroom = LightroomConfig(mode="watch", watch_folder=tmp_path / "_watch")
    cfg.output = OutputConfig(root=tmp_path)
    return cfg


def _session_cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.lightroom = LightroomConfig(mode="session", watch_folder=tmp_path / "_watch")
    cfg.output = OutputConfig(root=tmp_path)
    return cfg


class TestWatchMode:
    def test_path_lands_in_watch_folder(self, tmp_path: Path) -> None:
        cfg = _watch_cfg(tmp_path)
        dest = build_destination(
            cfg,
            shot_index=1,
            session_name="still_life",
            file_ext="jpg",
            now=FIXED_NOW,
        )
        assert dest.is_watch is True
        assert dest.path.parent == cfg.lightroom.watch_folder
        assert dest.session_dir is None

    def test_filename_uses_template(self, tmp_path: Path) -> None:
        cfg = _watch_cfg(tmp_path)
        cfg.output.filename_template = "{session}_{shot:04d}.{ext}"
        dest = build_destination(
            cfg,
            shot_index=42,
            session_name="ceramic",
            file_ext="jpg",
            now=FIXED_NOW,
        )
        assert dest.path.name == "ceramic_0042.jpg"

    def test_dng_extension(self, tmp_path: Path) -> None:
        cfg = _watch_cfg(tmp_path)
        dest = build_destination(
            cfg, shot_index=1, session_name="x", file_ext="dng", now=FIXED_NOW,
        )
        assert dest.path.name.endswith(".dng")


class TestSessionMode:
    def test_session_dir_built_from_template(self, tmp_path: Path) -> None:
        cfg = _session_cfg(tmp_path)
        cfg.output.session_template = "{date}_{name}"
        dest = build_destination(
            cfg,
            shot_index=1,
            session_name="still_life",
            file_ext="jpg",
            now=FIXED_NOW,
        )
        assert dest.is_watch is False
        assert dest.session_dir is not None
        assert dest.session_dir.name == "20260512_still_life"
        assert dest.path.parent == dest.session_dir

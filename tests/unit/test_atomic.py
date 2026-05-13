"""Tests for fp_l_tether.transfer.atomic.write_atomic."""

from __future__ import annotations

from pathlib import Path

import pytest

from fp_l_tether.transfer.atomic import write_atomic


class TestWriteAtomic:
    def test_basic_write(self, tmp_path: Path) -> None:
        out = tmp_path / "shot.jpg"
        ret = write_atomic(out, b"\xff\xd8\xff\xe0FAKE_JPEG")
        assert ret == out
        assert out.read_bytes() == b"\xff\xd8\xff\xe0FAKE_JPEG"

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        out = tmp_path / "nested" / "deeper" / "shot.jpg"
        ret = write_atomic(out, b"data")
        assert ret == out
        assert out.exists()

    def test_no_tempfile_left_behind(self, tmp_path: Path) -> None:
        write_atomic(tmp_path / "shot.jpg", b"data")
        # No .tmp / .shot.jpg.* files should remain
        leftovers = [p for p in tmp_path.iterdir() if p.name != "shot.jpg"]
        assert leftovers == [], f"unexpected leftovers: {leftovers}"

    def test_conflict_rename(self, tmp_path: Path) -> None:
        target = tmp_path / "shot.jpg"
        target.write_bytes(b"first")
        ret = write_atomic(target, b"second", on_conflict="rename")
        assert ret != target
        assert ret.name == "shot_001.jpg"
        assert target.read_bytes() == b"first"  # original untouched
        assert ret.read_bytes() == b"second"

    def test_conflict_rename_multiple(self, tmp_path: Path) -> None:
        target = tmp_path / "shot.jpg"
        target.write_bytes(b"0")
        (tmp_path / "shot_001.jpg").write_bytes(b"1")
        ret = write_atomic(target, b"2", on_conflict="rename")
        assert ret.name == "shot_002.jpg"

    def test_conflict_skip(self, tmp_path: Path) -> None:
        target = tmp_path / "shot.jpg"
        target.write_bytes(b"original")
        ret = write_atomic(target, b"new", on_conflict="skip")
        assert ret == target
        assert target.read_bytes() == b"original"  # untouched

    def test_conflict_overwrite(self, tmp_path: Path) -> None:
        target = tmp_path / "shot.jpg"
        target.write_bytes(b"original")
        ret = write_atomic(target, b"new", on_conflict="overwrite")
        assert ret == target
        assert target.read_bytes() == b"new"

    def test_invalid_on_conflict(self, tmp_path: Path) -> None:
        target = tmp_path / "shot.jpg"
        target.write_bytes(b"x")
        with pytest.raises(ValueError, match="on_conflict"):
            write_atomic(target, b"y", on_conflict="invalid")

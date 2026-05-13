#!/usr/bin/env python3
"""Phase 3.1 — Live view smoke test.

Pulls ONE live-view frame off the camera via GetCamViewFrame (0x902B),
slices the JPEG, and saves it to ~/Desktop/test_frame.jpg.

Usage::

    sudo killall ptpcamerad 2>/dev/null
    sudo venv/bin/python scripts/phase3_liveview_one_frame.py

What it verifies:
  1. Opcode 0x902B is accepted in the post-init PC-capture state
  2. The response actually contains a JPEG SOI..EOI pair
  3. The frame opens cleanly in Preview.app (visual sanity check)

Common failure modes:
  - Empty payload  → camera not in live-view mode. May need to write a
    ViewFrameSetting datagroup first (TBD — sigma-ptpy schema lookup).
  - PTP error 0x2002 (Parameter Not Supported) → fp L firmware
    variation; check that init / PC capture mode setup ran.
  - 0-byte JPEG slice → SOI/EOI markers missing; dump first 64 bytes
    to see the wrapper format.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

# Allow running from the repo root without installing.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fp_l_tether.camera.usb_bridge import USBBridge  # noqa: E402


def _hex_head(buf: bytes, n: int = 32) -> str:
    return " ".join(f"{b:02X}" for b in buf[:n])


def _jpeg_dimensions(jpeg: bytes) -> tuple[int, int] | None:
    """Walk JPEG segments to find the SOF (start-of-frame) marker and
    extract (width, height). Returns None if not found."""
    i = 2  # skip SOI
    while i < len(jpeg) - 1:
        if jpeg[i] != 0xFF:
            return None
        marker = jpeg[i + 1]
        # SOF0..SOF15 except DHT(0xC4)/JPG(0xC8)/DAC(0xCC)
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            # length(2) + precision(1) + height(2) + width(2)
            h, w = struct.unpack(">HH", jpeg[i + 5 : i + 9])
            return w, h
        if marker == 0xD9:  # EOI
            return None
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", jpeg[i + 2 : i + 4])[0]
        i += 2 + seg_len
    return None


def main() -> int:
    out_path = Path.home() / "Desktop" / "test_frame.jpg"

    bridge = USBBridge.find_sigma_fp_l()
    bridge.open()
    bridge.open_session()
    print("  session opened")
    try:
        bridge.sigma_init()
        print("  init complete (PC capture mode)")

        jpeg = bridge.sigma_get_view_frame()
        print(f"  got {len(jpeg)} bytes of JPEG")
        if not jpeg:
            print("  ✗ empty JPEG — view frame not produced. See script docstring.")
            return 2

        print(f"  first 32 bytes: {_hex_head(jpeg)}")
        if not jpeg.startswith(b"\xFF\xD8\xFF"):
            print("  ✗ does not start with SOI — extractor bug?")
            return 3
        if not jpeg.endswith(b"\xFF\xD9"):
            print("  ✗ does not end with EOI — extractor bug?")
            return 3

        dims = _jpeg_dimensions(jpeg)
        if dims is not None:
            print(f"  JPEG dimensions: {dims[0]} x {dims[1]}")
        else:
            print("  (could not parse JPEG dimensions; saving anyway)")

        out_path.write_bytes(jpeg)
        print(f"  ✓ saved to {out_path}")
        print("    open in Preview.app to visually verify")
        return 0
    finally:
        try:
            bridge.close_session()
        except Exception:
            pass
        try:
            bridge.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())

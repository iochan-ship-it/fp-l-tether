"""Probe different wire formats for SetCamDataGroupFocus (0x9032).

The camera accepts the bytes (PTP returns OK) but the AF point on the LCD
does not move. This script tries several format variants to find one that
actually works.

Usage:
    sudo killall ptpcamerad
    sudo venv/bin/python scripts/probe_set_focus_format.py [variant]

variant: minimal | wrapped | full_ifd | with_focus_area
         (default: minimal)

Run each variant in sequence and visually check the LCD between runs.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from fp_l_tether.camera.ptp_codes import SigmaOperationCode, sigma_checksum
from fp_l_tether.camera.usb_bridge import USBBridge

# Test coordinate: top-left corner of valid range
X_TEST, Y_TEST = 85, 96


def _build_minimal_ifd(x: int, y: int) -> bytes:
    """20 bytes content + 1 byte checksum = 21 bytes total."""
    payload = (
        (20).to_bytes(4, "little")     # declared_length
        + (1).to_bytes(4, "little")    # entries count
        + (0x000D).to_bytes(2, "little")  # tag
        + (0x0007).to_bytes(2, "little")  # type UNDEFINED
        + (4).to_bytes(4, "little")    # count
        + x.to_bytes(2, "little") + y.to_bytes(2, "little")
    )
    chk = sigma_checksum(payload)
    return payload + bytes([chk])


def _build_full_ifd_from_snapshot(snapshot_path: Path, x: int, y: int) -> bytes:
    """Replay the entire Get response with tag 0x000D's value overwritten."""
    raw = bytes.fromhex(json.loads(snapshot_path.read_text())["focus"]["raw_hex"])
    # tag 0x000D is at entry index 7, offset = 8 + 7*12 = 92
    # value bytes are at offset 100..103
    new = bytearray(raw)
    new[100] = x & 0xFF
    new[101] = (x >> 8) & 0xFF
    new[102] = y & 0xFF
    new[103] = (y >> 8) & 0xFF
    # Recompute checksum: sum(bytes[0..len-2]) & 0xFF, last byte is checksum
    # The trailing 4 zero bytes between entries and tag 0x000E trailing data
    # are part of the structure but not declared_length.
    # We assume checksum is over all bytes except itself (last byte).
    checksum_offset = len(new) - 1
    new[checksum_offset] = sum(new[:checksum_offset]) & 0xFF
    return bytes(new)


def run_variant(variant: str, x: int, y: int) -> None:
    bridge = USBBridge.find_sigma_fp_l()
    bridge.open()
    bridge.open_session()
    print(f"  session opened. variant={variant}, target=(x={x}, y={y})")
    try:
        bridge.sigma_init()
        print("  init complete.")

        if variant == "minimal":
            # 21-byte IFD (declared_length=20 + checksum). No outer wrap.
            out = _build_minimal_ifd(x, y)
            print(f"  sending {len(out)} bytes (no outer wrap): {out.hex(' ')}")
            resp = bridge.send_command_raw(
                SigmaOperationCode.SET_CAM_DATA_GROUP_FOCUS,
                out_data=out, data_phase_out=True, data_phase_in=False,
            )
            resp.raise_for_status()
            print("  ✓ PTP OK (minimal)")

        elif variant == "wrapped":
            # Outer 4-byte length prefix wrap (a la SnapCommand)
            body = _build_minimal_ifd(x, y)
            out = len(body).to_bytes(4, "little") + body
            print(f"  sending {len(out)} bytes (outer-wrap): {out.hex(' ')}")
            resp = bridge.send_command_raw(
                SigmaOperationCode.SET_CAM_DATA_GROUP_FOCUS,
                out_data=out, data_phase_out=True, data_phase_in=False,
            )
            resp.raise_for_status()
            print("  ✓ PTP OK (wrapped)")

        elif variant == "full_ifd":
            snap = Path.home() / "Desktop/fp_l_inspect/af_point/af_center.json"
            if not snap.exists():
                print(f"  ✗ snapshot missing: {snap}")
                return
            out = _build_full_ifd_from_snapshot(snap, x, y)
            print(f"  sending {len(out)} bytes (full IFD replay, no outer wrap):")
            print(f"    {out.hex(' ')}")
            resp = bridge.send_command_raw(
                SigmaOperationCode.SET_CAM_DATA_GROUP_FOCUS,
                out_data=out, data_phase_out=True, data_phase_in=False,
            )
            resp.raise_for_status()
            print("  ✓ PTP OK (full_ifd)")

        elif variant == "full_ifd_wrapped":
            snap = Path.home() / "Desktop/fp_l_inspect/af_point/af_center.json"
            if not snap.exists():
                print(f"  ✗ snapshot missing: {snap}")
                return
            body = _build_full_ifd_from_snapshot(snap, x, y)
            out = len(body).to_bytes(4, "little") + body
            print(f"  sending {len(out)} bytes (full IFD + outer wrap)")
            resp = bridge.send_command_raw(
                SigmaOperationCode.SET_CAM_DATA_GROUP_FOCUS,
                out_data=out, data_phase_out=True, data_phase_in=False,
            )
            resp.raise_for_status()
            print("  ✓ PTP OK (full_ifd_wrapped)")

        else:
            print(f"  ✗ unknown variant: {variant}")
            return

        # Read back to see if anything changed (expected: still 54 01 00 02)
        time.sleep(0.3)
        focus = bridge.sigma_get_cam_datagroup_focus()
        # tag 0x000D is at offset 100..103 in the response payload
        print(f"  readback bytes[100..103] = {focus[100:104].hex(' ')}")
    finally:
        try:
            bridge.close_session()
        except Exception:  # noqa: BLE001
            pass
        bridge.close()


if __name__ == "__main__":
    variant = sys.argv[1] if len(sys.argv) > 1 else "minimal"
    run_variant(variant, X_TEST, Y_TEST)

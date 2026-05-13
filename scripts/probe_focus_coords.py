"""Triangulate the coordinate system of SetCamDataGroupFocus tag 0x000D.

We confirmed SetCamDataGroupFocus produces a visible change on the LCD,
but (85, 96) did NOT produce a top-left AF box. We need to map the
coordinate space empirically by sending a series of well-spaced values
and observing the LCD between each.

Usage::

    sudo killall ptpcamerad
    sudo venv/bin/python scripts/probe_focus_coords.py X Y

Examples to run in sequence (USB stays connected — no need to unplug)::

    1) 340 512   — default ("center" per the read-back). Visual: ?
    2) 200 200   — well inside the range. Visual: ?
    3) 500 800   — well inside, opposite quadrant. Visual: ?
    4) 100 500   — left side, middle height. Visual: ?
    5) 500 100   — right side, top. Visual: ?

After each, describe what the LCD shows (position, size, shape).
"""
from __future__ import annotations

import sys

from fp_l_tether.camera.ptp_codes import SigmaOperationCode, sigma_checksum
from fp_l_tether.camera.usb_bridge import USBBridge


def _build(x: int, y: int) -> bytes:
    payload = (
        (20).to_bytes(4, "little")
        + (1).to_bytes(4, "little")
        + (0x000D).to_bytes(2, "little")
        + (0x0007).to_bytes(2, "little")
        + (4).to_bytes(4, "little")
        + x.to_bytes(2, "little") + y.to_bytes(2, "little")
    )
    return payload + bytes([sigma_checksum(payload)])


def main(x: int, y: int) -> None:
    bridge = USBBridge.find_sigma_fp_l()
    bridge.open()
    bridge.open_session()
    try:
        bridge.sigma_init()
        # Precondition: pull one LiveView frame to (apparently) arm focus edits
        lv = bridge.send_command_raw(
            0x902B, out_data=None,
            data_phase_out=False, data_phase_in=True,
        )
        print(f"  LV frame: {len(lv.in_data)} bytes (response 0x{lv.response_code:04X})")
        out = _build(x, y)
        print(f"sending (x={x}, y={y}): {out.hex(' ')}")
        resp = bridge.send_command_raw(
            SigmaOperationCode.SET_CAM_DATA_GROUP_FOCUS,
            out_data=out, data_phase_out=True, data_phase_in=False,
        )
        resp.raise_for_status()
        print("✓ PTP OK — observe LCD now.")
    finally:
        try:
            bridge.close_session()
        except Exception:  # noqa: BLE001
            pass
        bridge.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    main(int(sys.argv[1]), int(sys.argv[2]))

"""Set AF point by sending the FULL focus IFD (all 13 tags preserved)
with only tag 0x000D's 4 value bytes overwritten.

Hypothesis: sending the minimal IFD (tag 0x000D only) causes the camera
to default all other focus group tags to 0, which switches AF Area Mode
to a "wide/horizontal bars" rendering and ignores X/Y. To test the
coordinate interpretation correctly, we must preserve all 13 tags.

Workflow per call:
  1. open / sigma_init
  2. pull one LiveView frame (precondition)
  3. Get current focus IFD live from the camera
  4. overwrite tag 0x000D's 4 value bytes (offset 100..103) with (x, y)
  5. recompute the trailing checksum
  6. Send to SetCamDataGroupFocus

Usage::

    sudo killall ptpcamerad
    sudo venv/bin/python scripts/probe_focus_full_ifd.py X Y
"""
from __future__ import annotations

import sys

from fp_l_tether.camera.ptp_codes import SigmaOperationCode
from fp_l_tether.camera.usb_bridge import USBBridge


def main(x: int, y: int) -> None:
    bridge = USBBridge.find_sigma_fp_l()
    bridge.open()
    bridge.open_session()
    try:
        bridge.sigma_init()
        lv = bridge.send_command_raw(
            0x902B, out_data=None,
            data_phase_out=False, data_phase_in=True,
        )
        print(f"  LV frame: {len(lv.in_data)} bytes")

        # 1. Get the live focus IFD
        current = bytearray(bridge.sigma_get_cam_datagroup_focus())
        print(f"  current focus IFD: {len(current)} bytes")
        print(f"  tag 0x000D before = {bytes(current[100:104]).hex(' ')}")

        # 2. Overwrite tag 0x000D's value bytes (offset 100..103)
        current[100] = x & 0xFF
        current[101] = (x >> 8) & 0xFF
        current[102] = y & 0xFF
        current[103] = (y >> 8) & 0xFF

        # 3. Recompute the trailing checksum (sum of all bytes except last)
        checksum_offset = len(current) - 1
        current[checksum_offset] = sum(current[:checksum_offset]) & 0xFF

        out = bytes(current)
        print(f"  sending {len(out)} bytes, target=(x={x}, y={y})")
        resp = bridge.send_command_raw(
            SigmaOperationCode.SET_CAM_DATA_GROUP_FOCUS,
            out_data=out, data_phase_out=True, data_phase_in=False,
        )
        resp.raise_for_status()
        print(f"  ✓ PTP {resp.response_code:#06x}")
        print("  → observe LCD now.")
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

"""After Set causes the [ ] bars, dump the focus IFD in the same session
to discover which tag actually changed.

Run after physically resetting AF to center via USB-unplug.

Usage::

    sudo killall ptpcamerad
    sudo venv/bin/python scripts/probe_focus_after_set.py
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from fp_l_tether.camera.ptp_codes import SigmaOperationCode
from fp_l_tether.camera.usb_bridge import USBBridge

OUT = Path.home() / "Desktop/fp_l_inspect/af_point/focus_after_set.json"


def _build_minimal(x: int, y: int) -> bytes:
    from fp_l_tether.camera.ptp_codes import sigma_checksum
    payload = (
        (20).to_bytes(4, "little")
        + (1).to_bytes(4, "little")
        + (0x000D).to_bytes(2, "little")
        + (0x0007).to_bytes(2, "little")
        + (4).to_bytes(4, "little")
        + x.to_bytes(2, "little") + y.to_bytes(2, "little")
    )
    return payload + bytes([sigma_checksum(payload)])


def main() -> None:
    bridge = USBBridge.find_sigma_fp_l()
    bridge.open()
    bridge.open_session()
    try:
        bridge.sigma_init()

        # Pull LV (precondition)
        bridge.send_command_raw(0x902B, out_data=None,
                                data_phase_out=False, data_phase_in=True)

        # Step 1: focus IFD BEFORE Set
        before = bridge.sigma_get_cam_datagroup_focus()

        # Step 2: send minimal Set with (340, 512) — should trigger [ ] bars
        out = _build_minimal(340, 512)
        resp = bridge.send_command_raw(
            SigmaOperationCode.SET_CAM_DATA_GROUP_FOCUS,
            out_data=out, data_phase_out=True, data_phase_in=False,
        )
        resp.raise_for_status()
        print("  ✓ Set sent. LCD should now show [ ] bars.")
        time.sleep(0.5)

        # Step 3: focus IFD AFTER Set (same session)
        after = bridge.sigma_get_cam_datagroup_focus()

        print(f"\n  before ({len(before)} bytes): {before.hex(' ')}")
        print(f"\n  after  ({len(after)} bytes): {after.hex(' ')}")

        # Byte-by-byte diff
        diffs = []
        L = min(len(before), len(after))
        for i in range(L):
            if before[i] != after[i]:
                diffs.append((i, before[i], after[i]))
        if not diffs:
            print("\n  >>> NO BYTE CHANGED between before/after Set <<<")
            print("      (Get always returns static state; the camera's")
            print("       actual AF state isn't reflected in the Get response.)")
        else:
            print(f"\n  byte diffs ({len(diffs)}):")
            for off, b, a in diffs:
                print(f"    off 0x{off:03x} ({off:3d}): {b:#04x} → {a:#04x}")

        OUT.write_text(json.dumps({
            "before_hex": before.hex(),
            "after_hex": after.hex(),
            "diffs": diffs,
        }, indent=2))
        print(f"\n  saved → {OUT}")
    finally:
        try:
            bridge.close_session()
        except Exception:  # noqa: BLE001
            pass
        bridge.close()


if __name__ == "__main__":
    main()

"""Probe preconditions for SetCamDataGroupFocus (0x9032).

The Set command returns PTP OK but the AF frame does not visibly move on
the LCD. We suspect a missing precondition. This script probes two
candidates:

  1. GetCamOpPermission (0x9039)  — never called in sigma_init; might
     unlock "PC control mode" required for focus edits.
  2. GetCamViewFrame   (0x902B)   — LiveView may need to be streaming
     for focus point changes to apply.

Usage::

    sudo killall ptpcamerad
    sudo venv/bin/python scripts/probe_focus_preconditions.py [STAGE]

STAGE:
    perm        — call OpPermission then SetFocus(85,96). Visually check LCD.
    lv          — call OpPermission + GetCamViewFrame then SetFocus(85,96).
    both        — perm + lv combined, then SetFocus.
    (default)   — perm
"""
from __future__ import annotations

import sys
import time

from fp_l_tether.camera.ptp_codes import SigmaOperationCode, sigma_checksum
from fp_l_tether.camera.usb_bridge import USBBridge

X_TEST, Y_TEST = 85, 96  # top-left corner


def _build_minimal_set_focus(x: int, y: int) -> bytes:
    payload = (
        (20).to_bytes(4, "little")
        + (1).to_bytes(4, "little")
        + (0x000D).to_bytes(2, "little")
        + (0x0007).to_bytes(2, "little")
        + (4).to_bytes(4, "little")
        + x.to_bytes(2, "little") + y.to_bytes(2, "little")
    )
    return payload + bytes([sigma_checksum(payload)])


def _dump_op_permission(bridge: USBBridge) -> None:
    print("\n[probe] GetCamOpPermission (0x9039)")
    try:
        resp = bridge.send_sigma_command(
            SigmaOperationCode.GET_CAM_OP_PERMISSION
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ exception: {exc}")
        return
    print(f"  response_code = 0x{resp.response_code:04X}")
    print(f"  in_data ({len(resp.in_data)} bytes) = {resp.in_data.hex(' ')}")
    if len(resp.in_data) >= 8:
        length = int.from_bytes(resp.in_data[0:4], "little")
        mode = int.from_bytes(resp.in_data[4:8], "little")
        print(f"  parsed: length={length}, mode_word=0x{mode:08X}")
        if mode == 0x00010001:
            print("  → matches 'PC control mode enabled' expectation")


def _try_view_frame(bridge: USBBridge) -> None:
    print("\n[probe] GetCamViewFrame (0x902B) — LV one-shot")
    try:
        resp = bridge.send_command_raw(
            0x902B,
            out_data=None,
            data_phase_out=False,
            data_phase_in=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ exception: {exc}")
        return
    print(f"  response_code = 0x{resp.response_code:04X}")
    print(f"  in_data length = {len(resp.in_data)} bytes")
    if resp.in_data:
        head = resp.in_data[:16].hex(' ')
        print(f"  first 16 bytes: {head}")
        if resp.in_data[:2] == b"\xff\xd8":
            print("  → looks like a JPEG (SOI marker)")


def _send_set_focus(bridge: USBBridge, x: int, y: int) -> None:
    print(f"\n[probe] SetCamDataGroupFocus(x={x}, y={y})")
    out = _build_minimal_set_focus(x, y)
    print(f"  sending {len(out)} bytes: {out.hex(' ')}")
    resp = bridge.send_command_raw(
        SigmaOperationCode.SET_CAM_DATA_GROUP_FOCUS,
        out_data=out,
        data_phase_out=True,
        data_phase_in=False,
    )
    resp.raise_for_status()
    print(f"  ✓ PTP {resp.response_code:#06x}")


def run(stage: str) -> None:
    bridge = USBBridge.find_sigma_fp_l()
    bridge.open()
    bridge.open_session()
    print(f"  session opened. stage={stage}")
    try:
        bridge.sigma_init()
        print("  init complete.")

        if stage in ("perm", "both"):
            _dump_op_permission(bridge)

        if stage in ("lv", "both"):
            _dump_op_permission(bridge)  # also dump for reference
            _try_view_frame(bridge)
            time.sleep(0.3)

        _send_set_focus(bridge, X_TEST, Y_TEST)
        print("\n  → check LCD: AF frame should be at (85,96) = top-left.")
        time.sleep(0.5)

        # Re-dump permission to see if anything flipped
        print("\n[probe] GetCamOpPermission after SetFocus:")
        _dump_op_permission(bridge)
    finally:
        try:
            bridge.close_session()
        except Exception:  # noqa: BLE001
            pass
        bridge.close()


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "perm"
    run(stage)

#!/usr/bin/env python3
"""Phase 0-C — scan FieldPresent bits to map which bit changes which byte.

The fp L's SgmDataGroup3 SET command accepted our writes (response 0x2001)
but values 0x02/0x03/0x01 appeared at bytes 14/15/16 instead of our target
DestinationToSave=0x04. This means the FieldPresent bit-to-byte mapping
differs from the SDK header (because fp L has fewer fields than the SDK
documents).

This script:
  1. Reads original CamDataGroup3
  2. For EACH bit of FieldPresent1 (0x01..0x80) AND FieldPresent2:
     a. SETs with only that bit, value 0xAB at every struct byte
     b. Reads back
     c. Diffs against the previous read
  3. Reports which bit affected which byte → builds the FieldPresent map

Run with sudo::

    sudo "/Users/PI/Documents/Claude/Projects/FP L Tether APP/venv/bin/python" \\
         scripts/phase0_fieldpresent_scan.py
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fp_l_tether.camera.ptp_codes import (  # noqa: E402
    SigmaOperationCode,
)
from fp_l_tether.camera.usb_bridge import (  # noqa: E402
    USBBridge,
    USBBridgeError,
)

STRUCT_SIZE = 17  # bytes of CamDataGroup3 struct on fp L (no checksum)


def hex_dump(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def diff_bytes(a: bytes, b: bytes) -> list[tuple[int, int, int]]:
    """Return list of (offset, old, new) for differing bytes."""
    return [(i, a[i], b[i]) for i in range(min(len(a), len(b))) if a[i] != b[i]]


def read_dg3(bridge: USBBridge) -> bytes:
    return bridge.send_sigma_command(SigmaOperationCode.GET_CAM_DATA_GROUP_3).in_data


def set_dg3(bridge: USBBridge, fp1: int, fp2: int, marker: int = 0xAB) -> int:
    """Send a SET with given FieldPresent bytes, and ``marker`` value at
    every payload byte slot (so whichever byte the camera writes to,
    we'll see it change to 0xAB)."""
    payload = bytearray(STRUCT_SIZE)
    payload[0] = fp1
    payload[1] = fp2
    # Fill positions 2..16 with the marker
    for i in range(2, STRUCT_SIZE):
        payload[i] = marker
    r = bridge.send_sigma_command(
        SigmaOperationCode.SET_CAM_DATA_GROUP_3,
        sigma_payload=bytes(payload),
        data_phase_in=False,
        timeout_ms=10000,
    )
    return r.response_code


def restore_original(bridge: USBBridge, original_18b: bytes) -> int:
    """Try to restore the original struct values by writing all bytes
    with FieldPresent=0xFF 0xFF (set all known fields)."""
    payload = bytearray(STRUCT_SIZE)
    payload[0] = 0xFF
    payload[1] = 0xFF
    payload[2:STRUCT_SIZE] = original_18b[2:STRUCT_SIZE]
    r = bridge.send_sigma_command(
        SigmaOperationCode.SET_CAM_DATA_GROUP_3,
        sigma_payload=bytes(payload),
        data_phase_in=False,
        timeout_ms=10000,
    )
    return r.response_code


def main() -> int:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    print("=" * 70)
    print("Phase 0-C — FieldPresent bit-to-byte mapping scanner")
    print("=" * 70)
    print()
    print("This will issue ~16 SetCamDataGroup3 commands. The camera might")
    print("change real settings (Contrast, Sharpness, etc) which we'll try")
    print("to restore at the end. Recommended: take note of your current")
    print("camera settings before running.")
    print()
    input("Press Enter to begin... ")

    try:
        with USBBridge.find_sigma_fp_l() as bridge:
            bridge.open_session(session_id=1)
            print("✓ Session opened.\n")

            # MANDATORY warmup — without ConfigApi the camera refuses SETs
            print("--- Warmup (mandatory before SET) ---")
            for op in [SigmaOperationCode.CONFIG_API,
                       SigmaOperationCode.GET_CAM_OP_PERMISSION]:
                r = bridge.send_sigma_command(op)
                print(f"  0x{int(op):04X} → 0x{r.response_code:04X}, "
                      f"{len(r.in_data)} bytes")
                if r.response_code != 0x2001:
                    print(f"  ✗ Warmup op failed. Camera may be in a bad state.")
                    print(f"    Please disconnect USB, power-cycle camera, "
                          f"reconnect, and retry.")
                    return 1

            # Baseline
            print("\n--- Reading baseline CamDataGroup3 ---")
            baseline = read_dg3(bridge)
            print(f"  {hex_dump(baseline)}")
            if len(baseline) < STRUCT_SIZE + 1:
                print(f"  ✗ Baseline read returned only {len(baseline)} bytes "
                      f"(expected {STRUCT_SIZE + 1}).")
                print(f"  Camera is in a bad state. Disconnect USB, power-cycle,"
                      f" reconnect.")
                return 1
            current = baseline
            print()

            # Scan FieldPresent1 bits 0..7
            results: list[tuple[str, list[tuple[int, int, int]]]] = []

            for fp_byte_idx, fp_name in [(0, "FieldPresent1"), (1, "FieldPresent2")]:
                for bit in range(8):
                    bit_val = 1 << bit
                    label = f"{fp_name} bit {bit} (= 0x{bit_val:02X})"
                    print(f"--- {label} ---")
                    fp1 = bit_val if fp_byte_idx == 0 else 0
                    fp2 = bit_val if fp_byte_idx == 1 else 0
                    try:
                        rc = set_dg3(bridge, fp1, fp2)
                        new_state = read_dg3(bridge)
                        delta = diff_bytes(current, new_state)
                        if rc != 0x2001:
                            print(f"  SET rejected: 0x{rc:04X}")
                        elif not delta:
                            print(f"  No bytes changed (camera silently ignored)")
                        else:
                            print(f"  Changed bytes:")
                            for offset, old, new in delta:
                                marker_match = " ← matches marker 0xAB" if new == 0xAB else ""
                                print(f"    byte {offset:2}: 0x{old:02X} → 0x{new:02X}{marker_match}")
                        results.append((label, delta))
                        current = new_state
                    except Exception as e:  # noqa: BLE001
                        print(f"  EXCEPTION: {e!r}")
                    time.sleep(0.2)

            print()
            print("=" * 70)
            print("Summary")
            print("=" * 70)
            for label, delta in results:
                if delta:
                    bytes_changed = ", ".join(
                        f"byte{o}" + (" ★marker" if new == 0xAB else "")
                        for o, _, new in delta
                    )
                    print(f"  {label}: changed {bytes_changed}")
                else:
                    print(f"  {label}: no effect")

            # Try to restore
            print()
            print("--- Attempting to restore baseline ---")
            rc = restore_original(bridge, baseline)
            print(f"  restore SET response: 0x{rc:04X}")
            restored = read_dg3(bridge)
            print(f"  restored state: {hex_dump(restored)}")
            print(f"  original state: {hex_dump(baseline)}")
            if restored == baseline:
                print("  ✓ Successfully restored")
            else:
                print("  ⚠ Could not fully restore — please re-check camera settings")
                print("    (manually if needed)")

            return 0

    except USBBridgeError as e:
        print(f"\n✗ {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\n✗ Unexpected: {e!r}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

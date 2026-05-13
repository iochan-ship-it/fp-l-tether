#!/usr/bin/env python3
"""Phase 0-C v5 — set DestinationToSave with the CORRECT FieldPresent bit.

The FieldPresent bit-to-byte scan (phase0_fieldpresent_scan.py) showed:
    FieldPresent1 bit 0 (= 0x01) → writes byte 16 (DestinationToSave)
    (it also sets bytes 14 and 15 to camera-chosen defaults)

NOT FieldPresent2 bit 7 as I'd assumed from the SDK header. The fp L
firmware uses a different FieldPresent mapping than the SDK documents.

This script does the corrected SET workflow:
    1. Warmup
    2. Read current CamDataGroup3
    3. SET with FieldPresent1=0x01 + byte 16 = 0x04 (try several values)
    4. Read back, verify DestinationToSave value
    5. Confirm SgmCaptStatus.dest reflects the change
    6. Send SnapCommand and watch for ImageID change

Run with sudo::

    sudo "/Users/PI/Documents/Claude/Projects/FP L Tether APP/venv/bin/python" \\
         scripts/phase0_set_dest_v3.py
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fp_l_tether.camera.ptp_codes import (  # noqa: E402
    SgmCaptStatus,
    SigmaOperationCode,
)
from fp_l_tether.camera.usb_bridge import (  # noqa: E402
    USBBridge,
    USBBridgeError,
)

STRUCT_SIZE = 17
DEST_OFFSET = 16
FIELD_PRESENT_BIT_DEST = (1, 0x01)  # (FP1_byte_idx=0, bit=0x01)

POLL_MS = 250
POLL_TIMEOUT_S = 8.0


def parse_status(data: bytes) -> SgmCaptStatus | None:
    if len(data) < 8:
        return None
    try:
        return SgmCaptStatus.from_wire(data)
    except Exception:
        return None


def hex_dump(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def read_dg3(bridge: USBBridge) -> bytes:
    return bridge.send_sigma_command(SigmaOperationCode.GET_CAM_DATA_GROUP_3).in_data


def set_destination(bridge: USBBridge, current: bytes, dest_value: int) -> int:
    """Build a SET payload that flips FieldPresent1 bit 0 and writes
    dest_value to byte 16."""
    payload = bytearray(STRUCT_SIZE)
    payload[0] = 0x01  # FieldPresent1 bit 0 — enables DestinationToSave write
    payload[1] = 0x00  # FieldPresent2 — nothing else
    # Preserve middle fields so we don't accidentally change anything
    if len(current) >= STRUCT_SIZE:
        payload[2:16] = current[2:16]
    payload[DEST_OFFSET] = dest_value & 0xFF

    print(f"  SET payload (17B): {hex_dump(payload)}")
    r = bridge.send_sigma_command(
        SigmaOperationCode.SET_CAM_DATA_GROUP_3,
        sigma_payload=bytes(payload),
        data_phase_in=False,
        timeout_ms=10000,
    )
    return r.response_code


def fire_and_poll(bridge: USBBridge) -> tuple[int, int | None, int | None,
                                              SgmCaptStatus | None]:
    r = bridge.send_sigma_command(SigmaOperationCode.GET_CAM_CAPT_STATUS)
    base = parse_status(r.in_data)
    base_id = base.image_id if base else None
    base_dest = base.destination_to_save if base else None
    print(f"  baseline: ImageID={base_id}, "
          f"dest={f'0x{base_dest:02X}' if base_dest is not None else 'n/a'}")

    snap_payload = bytes([0x02, 0x01, 0x01])
    r = bridge.send_sigma_command(
        SigmaOperationCode.SNAP_COMMAND,
        sigma_payload=snap_payload,
        data_phase_in=False,
        timeout_ms=10000,
    )
    print(f"  SnapCommand response: 0x{r.response_code:04X}")
    if r.response_code != 0x2001:
        return (r.response_code, base_id, None, None)

    start = time.monotonic()
    iteration = 0
    last_status = None
    while time.monotonic() - start < POLL_TIMEOUT_S:
        iteration += 1
        rs = bridge.send_sigma_command(SigmaOperationCode.GET_CAM_CAPT_STATUS)
        s = parse_status(rs.in_data)
        if s is None:
            time.sleep(POLL_MS / 1000)
            continue
        last_status = s
        if iteration <= 3 or iteration % 4 == 0:
            print(f"    [poll {iteration}] {s}")
        if (base_id is not None and s.image_id != base_id) or s.has_new_image:
            return (r.response_code, base_id, s.image_id, s)
        time.sleep(POLL_MS / 1000)
    return (r.response_code, base_id, None, last_status)


def main() -> int:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    print("=" * 70)
    print("Phase 0-C v5 — DestinationToSave via FieldPresent1 bit 0 (CORRECT)")
    print("=" * 70)
    print()
    input("Aim the camera, press Enter to begin... ")

    try:
        with USBBridge.find_sigma_fp_l() as bridge:
            bridge.open_session(session_id=1)
            print("✓ Session opened.\n")

            # Mandatory warmup
            for op in [SigmaOperationCode.CONFIG_API,
                       SigmaOperationCode.GET_CAM_OP_PERMISSION]:
                bridge.send_sigma_command(op)
            print("✓ Warmup done.\n")

            current = read_dg3(bridge)
            print(f"--- Baseline CamDataGroup3 ({len(current)} bytes) ---")
            print(f"  {hex_dump(current)}")
            print(f"  byte 16 (DestinationToSave) = 0x{current[16]:02X}")

            for dest in [0x04, 0x02, 0x03, 0x01]:
                print(f"\n=== Setting DestinationToSave = 0x{dest:02X} ===")
                rc = set_destination(bridge, current, dest)
                print(f"  SET response: 0x{rc:04X}")
                if rc != 0x2001:
                    continue

                # Read back
                readback = read_dg3(bridge)
                print(f"  Read-back: {hex_dump(readback)}")
                actual = readback[16] if len(readback) >= 17 else -1
                ok = (actual == dest)
                print(f"  DestinationToSave readback: 0x{actual:02X} "
                      f"(wanted 0x{dest:02X}) {'✓' if ok else '✗'}")

                # Also check SgmCaptStatus.dest
                r = bridge.send_sigma_command(SigmaOperationCode.GET_CAM_CAPT_STATUS)
                s = parse_status(r.in_data)
                if s:
                    print(f"  SgmCaptStatus.dest = 0x{s.destination_to_save:02X}")

                if not ok:
                    print(f"  → DestinationToSave didn't stick at this value, "
                          f"trying next")
                    current = readback
                    continue

                # Snap and poll
                print(f"\n  --- Firing SnapCommand with dest=0x{dest:02X} ---")
                rc2, base_id, new_id, final_status = fire_and_poll(bridge)
                if new_id is not None:
                    print()
                    print("=" * 70)
                    print(f"🎯🎯🎯 SUCCESS! DestinationToSave=0x{dest:02X}")
                    print(f"   ImageID {base_id} → {new_id}")
                    if final_status:
                        print(f"   Final state: {final_status}")
                        print(f"   has_new_image={final_status.has_new_image}, "
                              f"is_capturing={final_status.is_capturing}")
                    print("=" * 70)
                    return 0

                print(f"  → SnapCommand fired but ImageID didn't change")
                current = readback
                time.sleep(0.5)

            print()
            print("=" * 70)
            print("✗ No DestinationToSave value caused ImageID to change.")
            print("=" * 70)
            return 1

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

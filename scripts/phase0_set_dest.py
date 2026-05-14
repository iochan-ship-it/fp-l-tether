#!/usr/bin/env python3
"""Phase 0-C v3 — set DestinationToSave then SnapCommand.

Discovery: SnapCommand now fires the physical shutter (camera beeps),
but the captured image goes to SD card only — never to PC's ImageDB.
The DestinationToSave field in SgmCaptStatus was 0x00 (SD only).

This script:
  1. Reads CamDataGroup3 (which contains DestinationToSave)
  2. Sends SetCamDataGroup3 with FieldPresent=DestinationToSave bit only,
     trying values 0x01, 0x02, 0x03, 0x04 in turn
  3. After each SET, reads back and tries SnapCommand to see if PC ImageDB
     gets the captured image

Run with sudo::

    sudo "/path/to/fp-l-tether/venv/bin/python" \\
         scripts/phase0_set_dest.py
"""

from __future__ import annotations

import logging
import struct
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
    unwrap_sigma_fixed_struct,
)


# SgmDataGroup3 layout (18 bytes total — empirically observed on fp L):
#  off  size  field
#  0    1     FieldPresent1   (bits 0-7 select fields 1-8)
#  1    1     FieldPresent2   (bits 0-7 select fields 9-16)
#  2    1     Contrast        (field 1)
#  3    1     Sharpness       (field 2)
#  4    1     Saturation      (field 3)
#  5    1     ColorSpace      (field 4)
#  6    1     ColorMode       (field 5)
#  7    1     BatteryKind     (field 6)
#  8-9  2     LensWideFocalLength    (field 7)
# 10-11 2     LensTeleFocalLength    (field 8)
# 12    1     AFAuxiliaryLight (field 9)
# 13    1     AFBeep           (field 10)
# 14    1     UPSetting        (field 11)
# 15    1     ExtendedMode     (field 12)
# 16    1     AutoRotate       (field 13)
# 17    1     TimerSound       (field 14)
# 18    1     RCChannel        (field 15)
# 19    1     DestinationToSave (field 16)
#
# But our actual read was 18 bytes total, so some fields might be missing
# on the fp L. We'll dump the actual bytes to confirm.

POLL_MS = 250
POLL_TIMEOUT_S = 5.0


def parse_status(in_data: bytes) -> SgmCaptStatus | None:
    if len(in_data) < 8:
        return None
    try:
        return SgmCaptStatus.from_wire(in_data)
    except Exception:
        return None


def hex_dump(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def read_data_group_3(bridge: USBBridge) -> bytes:
    r = bridge.send_sigma_command(SigmaOperationCode.GET_CAM_DATA_GROUP_3)
    return r.in_data


def set_destination_to_save(bridge: USBBridge, dest_value: int) -> int:
    """Send SetCamDataGroup3 to update only DestinationToSave.

    Returns the response code.
    """
    # We'll attempt with FieldPresent2 bit 7 (DestinationToSave = field 16,
    # bit 7 of FieldPresent2). If the camera responds 0x201D Invalid Parameter,
    # the bit position might be different.

    # Try a struct with 18 bytes (matching what we read), all zeros except
    # FieldPresent2 = 0x80 (bit 7) and the LAST byte = dest_value
    struct_bytes = bytearray(18)
    struct_bytes[0] = 0x00  # FieldPresent1
    struct_bytes[1] = 0x80  # FieldPresent2 bit 7 = DestinationToSave
    struct_bytes[17] = dest_value  # DestinationToSave (last byte)

    print(f"  SET payload (18B): {hex_dump(struct_bytes)}")
    r = bridge.send_sigma_command(
        SigmaOperationCode.SET_CAM_DATA_GROUP_3,
        sigma_payload=bytes(struct_bytes),
        data_phase_in=False,
        timeout_ms=10000,
    )
    return r.response_code


def fire_and_poll(bridge: USBBridge) -> tuple[int, int | None, int | None]:
    """Fire SnapCommand and poll for ImageID change."""
    # Get baseline
    r = bridge.send_sigma_command(SigmaOperationCode.GET_CAM_CAPT_STATUS)
    base = parse_status(r.in_data)
    base_id = base.image_id if base else None
    base_dest = base.destination_to_save if base else None
    print(f"  baseline: ImageID={base_id}, DestinationToSave={base_dest}")

    # Snap (no length prefix, struct = 02 01 01)
    snap_payload = bytes([0x02, 0x01, 0x01])
    r = bridge.send_sigma_command(
        SigmaOperationCode.SNAP_COMMAND,
        sigma_payload=snap_payload,
        data_phase_in=False,
        timeout_ms=10000,
    )
    print(f"  SnapCommand response: 0x{r.response_code:04X}")
    if r.response_code != 0x2001:
        return (r.response_code, base_id, None)

    # Poll
    print(f"  Polling for state change up to {POLL_TIMEOUT_S}s...")
    start = time.monotonic()
    iteration = 0
    while time.monotonic() - start < POLL_TIMEOUT_S:
        iteration += 1
        rs = bridge.send_sigma_command(SigmaOperationCode.GET_CAM_CAPT_STATUS)
        s = parse_status(rs.in_data)
        if s is None:
            time.sleep(POLL_MS / 1000)
            continue
        if iteration <= 2 or iteration % 4 == 0:
            print(f"    [poll {iteration}] {s}")
        if base_id is not None and s.image_id != base_id:
            print(f"    ★ ImageID changed: {base_id} → {s.image_id}")
            return (r.response_code, base_id, s.image_id)
        if s.is_capturing or s.has_new_image:
            print(f"    capturing detected: {s}")
        time.sleep(POLL_MS / 1000)

    return (r.response_code, base_id, None)


def main() -> int:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    print("=" * 70)
    print("Phase 0-C v3 — set DestinationToSave then SnapCommand")
    print("=" * 70)
    print()
    input("Aim the camera, press Enter to begin... ")

    try:
        with USBBridge.find_sigma_fp_l() as bridge:
            bridge.open_session(session_id=1)
            print("✓ Session opened.\n")

            # Warmup
            print("--- Warmup ---")
            for op in [
                SigmaOperationCode.CONFIG_API,
                SigmaOperationCode.GET_CAM_OP_PERMISSION,
            ]:
                r = bridge.send_sigma_command(op)
                print(f"  0x{int(op):04X} → 0x{r.response_code:04X}")

            # Read current DataGroup3
            print("\n--- Current DataGroup3 ---")
            dg3 = read_data_group_3(bridge)
            print(f"  raw ({len(dg3)} bytes): {hex_dump(dg3)}")
            if len(dg3) >= 19:
                # Try fixed-form unwrap (struct + 1 byte checksum)
                # We don't know the exact struct size for fp L, but 18 was
                # what we got, so the struct is 17 bytes + 1 byte checksum?
                # Or 18 bytes + 0 bytes checksum? Let's just print raw.
                pass

            # Now try various DestinationToSave values
            print("\n=== Trying DestinationToSave values ===")
            for dest in [0x04, 0x01, 0x02, 0x03]:
                print(f"\n--- Setting DestinationToSave = 0x{dest:02X} ---")
                try:
                    rc = set_destination_to_save(bridge, dest)
                    print(f"  SetCamDataGroup3 response: 0x{rc:04X}")
                except Exception as e:  # noqa: BLE001
                    print(f"  SET EXCEPTION: {e!r}")
                    continue

                if rc != 0x2001:
                    print(f"  → SET was rejected, skipping snap attempt")
                    continue

                # Read back to see if it took effect
                dg3_after = read_data_group_3(bridge)
                print(f"  DataGroup3 after SET: {hex_dump(dg3_after)}")

                # Verify DestinationToSave in SgmCaptStatus
                r = bridge.send_sigma_command(SigmaOperationCode.GET_CAM_CAPT_STATUS)
                s = parse_status(r.in_data)
                if s:
                    print(f"  CaptStatus: dest={s.destination_to_save:#x}")

                # Try snap
                print(f"  --- Firing SnapCommand with dest=0x{dest:02X} ---")
                rc, base_id, new_id = fire_and_poll(bridge)
                if new_id is not None:
                    print()
                    print("=" * 70)
                    print(f"🎯 SUCCESS! DestinationToSave=0x{dest:02X}")
                    print(f"   ImageID {base_id} → {new_id}")
                    print(f"   PC ImageDB is now receiving captures.")
                    print("=" * 70)
                    return 0

                time.sleep(0.5)

            print()
            print("=" * 70)
            print("✗ No DestinationToSave value caused ImageID to change.")
            print("  Camera fired the shutter (beep heard) but PC ImageDB never updated.")
            print("  Next steps to consider:")
            print("  - Different FieldPresent bit position (maybe DestinationToSave")
            print("    is at a different field number than I assumed)")
            print("  - Write back the FULL CamDataGroup3 with just DestinationToSave changed")
            print("  - Look at SgmDataGroup5 / DataGroup1 for similar fields")
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

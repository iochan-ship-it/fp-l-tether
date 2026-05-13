#!/usr/bin/env python3
"""Phase 0-C v4 — set DestinationToSave (corrected struct size).

The v3 attempt used an 18-byte payload, but the fp L's SgmDataGroup3 struct
is actually 17 bytes (with the 18th wire byte being the external checksum).
This corrected version sends 17 bytes of struct content and lets
``send_sigma_command`` append the checksum.

Layout (17 bytes, deduced from the 18-byte GET response with verified
sum-mod-256 checksum at byte 17):

  off  field
  0    FieldPresent1
  1    FieldPresent2
  2    Contrast
  3    Sharpness
  4    Saturation
  5    ColorSpace
  6    ColorMode
  7    BatteryKind
  8-9  LensWideFocalLength  (uint16 LE)
 10-11 LensTeleFocalLength  (uint16 LE)
 12    AFAuxiliaryLight
 13    AFBeep
 14    UPSetting
 15    ExtendedMode
 16    DestinationToSave   ← TARGET
 (17th byte = external checksum, appended automatically by send_sigma_command)

Note: the SDK header includes AutoRotate, TimerSound, RCChannel between
ExtendedMode and DestinationToSave (would push DestinationToSave to byte 19),
but the fp L apparently omits those — our read was only 18 bytes total.

Run with sudo::

    sudo "/Users/PI/Documents/Claude/Projects/FP L Tether APP/venv/bin/python" \\
         scripts/phase0_set_dest_v2.py
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

POLL_MS = 250
POLL_TIMEOUT_S = 5.0
STRUCT_SIZE = 17  # bytes of SgmDataGroup3 on fp L (excluding ext checksum)
DEST_TO_SAVE_OFFSET = 16  # last byte of the 17-byte struct
FIELD_PRESENT_2_BIT = 0x80  # bit 7 = DestinationToSave per SDK header


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


def make_set_payload(
    current_18b: bytes,
    new_dest: int,
    preserve_other_fields: bool = True,
) -> bytes:
    """Build a 17-byte SetCamDataGroup3 struct that updates DestinationToSave.

    If preserve_other_fields is True, copy bytes 2..16 from the GET response
    and replace byte 16 with new_dest. Otherwise zero-fill.
    """
    if len(current_18b) < 18:
        raise ValueError(f"Need 18-byte GET response, got {len(current_18b)}")

    payload = bytearray(STRUCT_SIZE)  # 17 bytes
    payload[0] = 0x00  # FieldPresent1 — nothing in this byte's range
    payload[1] = FIELD_PRESENT_2_BIT  # 0x80 = DestinationToSave per SDK
    if preserve_other_fields:
        # Copy bytes 2..15 from current read (preserves Contrast..ExtendedMode)
        payload[2:16] = current_18b[2:16]
    payload[DEST_TO_SAVE_OFFSET] = new_dest & 0xFF
    return bytes(payload)


def set_destination(bridge: USBBridge, current: bytes, new_dest: int,
                    preserve: bool) -> int:
    payload = make_set_payload(current, new_dest, preserve_other_fields=preserve)
    print(f"  SET payload ({len(payload)}B preserve={preserve}): {hex_dump(payload)}")
    r = bridge.send_sigma_command(
        SigmaOperationCode.SET_CAM_DATA_GROUP_3,
        sigma_payload=payload,
        data_phase_in=False,
        timeout_ms=10000,
    )
    return r.response_code


def fire_and_poll(bridge: USBBridge) -> tuple[int, int | None, int | None]:
    """Fire SnapCommand and poll for ImageID change."""
    r = bridge.send_sigma_command(SigmaOperationCode.GET_CAM_CAPT_STATUS)
    base = parse_status(r.in_data)
    base_id = base.image_id if base else None
    base_dest = base.destination_to_save if base else None
    print(f"  baseline: ImageID={base_id}, DestinationToSave=0x{base_dest:02X}"
          if base_dest is not None else f"  baseline: ImageID={base_id}")

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
        time.sleep(POLL_MS / 1000)
    return (r.response_code, base_id, None)


def main() -> int:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    print("=" * 70)
    print("Phase 0-C v4 — corrected SetCamDataGroup3 (17-byte struct)")
    print("=" * 70)
    print()
    input("Aim the camera, press Enter to begin... ")

    try:
        with USBBridge.find_sigma_fp_l() as bridge:
            bridge.open_session(session_id=1)
            print("✓ Session opened.\n")

            # Warmup
            for op in [SigmaOperationCode.CONFIG_API,
                       SigmaOperationCode.GET_CAM_OP_PERMISSION]:
                bridge.send_sigma_command(op)

            # Read current CamDataGroup3
            current = read_data_group_3(bridge)
            print(f"--- Current DataGroup3 ({len(current)} bytes):")
            print(f"    {hex_dump(current)}")
            print(f"    DestinationToSave (byte 16) = 0x{current[16]:02X}")
            print(f"    External checksum (byte 17) = 0x{current[17]:02X}")

            # Try various DestinationToSave values, with preserve_other_fields=True
            for dest in [0x04, 0x01, 0x02, 0x03]:
                print(f"\n=== Setting DestinationToSave = 0x{dest:02X} "
                      f"(preserve other fields) ===")
                rc = set_destination(bridge, current, dest, preserve=True)
                print(f"  SET response: 0x{rc:04X}")
                if rc != 0x2001:
                    continue

                # Verify by re-reading
                readback = read_data_group_3(bridge)
                print(f"  Read-back ({len(readback)} bytes): {hex_dump(readback)}")
                actual = readback[16]
                print(f"  DestinationToSave readback: 0x{actual:02X} "
                      f"(wanted 0x{dest:02X}) "
                      f"{'✓' if actual == dest else '✗ did not stick'}")

                # Check SgmCaptStatus
                r = bridge.send_sigma_command(SigmaOperationCode.GET_CAM_CAPT_STATUS)
                s = parse_status(r.in_data)
                if s:
                    print(f"  SgmCaptStatus.dest = 0x{s.destination_to_save:02X}")

                if actual != dest:
                    print(f"  → SET didn't change value; skipping snap")
                    continue

                print(f"  --- Snap with dest=0x{dest:02X} ---")
                rc, base_id, new_id = fire_and_poll(bridge)
                if new_id is not None:
                    print()
                    print("=" * 70)
                    print(f"🎯 SUCCESS! DestinationToSave=0x{dest:02X}")
                    print(f"   ImageID {base_id} → {new_id}")
                    print("=" * 70)
                    return 0
                time.sleep(0.5)

            print()
            print("✗ No success. Try variant runs with preserve=False or different bits.")
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

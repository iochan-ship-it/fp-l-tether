#!/usr/bin/env python3
"""Phase 0-C redux — SnapCommand via libusb.

Tries to:
  1. Open PTP session
  2. Send standard initialization handshake (ConfigApi → GetCamOpPermission)
  3. Read baseline GetCamCaptStatus
  4. Send SnapCommand (Mac triggers shutter)
  5. Poll GetCamCaptStatus for state change
  6. Report any change with diagnostics

Does NOT yet download the DNG — that comes after we confirm SnapCommand
actually fires the shutter and produces an ImageDB update.

Run with sudo::

    sudo "/path/to/fp-l-tether/venv/bin/python" \\
         scripts/phase0_usb_snap.py
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fp_l_tether.camera.ptp_codes import (  # noqa: E402
    SgmCaptStatus,
    SgmSnapState,
    SigmaOperationCode,
    SnapCaptureMode,
)
from fp_l_tether.camera.usb_bridge import (  # noqa: E402
    USBBridge,
    USBBridgeError,
)

POLL_MS = 200
POLL_TIMEOUT_S = 30.0


def parse_capt_status_safe(in_data: bytes) -> SgmCaptStatus | None:
    if len(in_data) < 8:
        return None
    try:
        return SgmCaptStatus.from_wire(in_data)
    except Exception:
        return None


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    print("=" * 70)
    print("Phase 0-C (USB) — SnapCommand via libusb")
    print("=" * 70)
    print()
    print("Workflow:")
    print("  1. Open PTP session")
    print("  2. ConfigApi → GetCamOpPermission (handshake)")
    print("  3. Read baseline GetCamCaptStatus")
    print("  4. Send SnapCommand (Mac triggers shutter)")
    print("  5. Poll GetCamCaptStatus for change")
    print()
    print("Expected: camera physically clicks the shutter, ImageID increments.")
    print()
    input("Aim the camera at something visible, then press Enter to begin... ")

    try:
        with USBBridge.find_sigma_fp_l() as bridge:
            bridge.open_session(session_id=1)
            print("✓ Session opened.")

            # 1. ConfigApi handshake
            print("\n[1] ConfigApi (0x9035)")
            r = bridge.send_sigma_command(SigmaOperationCode.CONFIG_API)
            print(f"    response=0x{r.response_code:04X}, in_data={len(r.in_data)} bytes")

            # 2. GetCamOpPermission
            print("\n[2] GetCamOpPermission (0x9039)")
            r = bridge.send_sigma_command(SigmaOperationCode.GET_CAM_OP_PERMISSION)
            print(f"    response=0x{r.response_code:04X}, in_data={len(r.in_data)} bytes")
            if len(r.in_data) >= 9:
                # The op permission payload starts with a length prefix.
                # After the 4-byte length, the next uint32 is the "mode" value.
                # 0x00010001 in the trace seems to mean "PC control mode enabled".
                import struct
                length = struct.unpack("<I", r.in_data[:4])[0]
                mode_word = struct.unpack("<I", r.in_data[4:8])[0]
                print(f"    parsed: length={length}, mode_word=0x{mode_word:08X}")

            # 3. Baseline GetCamCaptStatus
            print("\n[3] Baseline GetCamCaptStatus (0x9015)")
            r = bridge.send_sigma_command(SigmaOperationCode.GET_CAM_CAPT_STATUS)
            baseline = parse_capt_status_safe(r.in_data)
            print(f"    response=0x{r.response_code:04X}, raw={r.in_data.hex()}")
            if baseline is not None:
                print(f"    parsed: {baseline}")
            else:
                print("    (could not parse — see raw bytes)")

            baseline_image_id = baseline.image_id if baseline else None

            # 4. SnapCommand
            print("\n[4] SnapCommand (0x901B) — sending shutter trigger")
            snap_payload = SgmSnapState(
                capture_mode=SnapCaptureMode.NON_AF_CAPTURE,
                capture_amount=1,
            ).to_bytes()
            print(f"    payload (struct only, 3 bytes): {snap_payload.hex()}")
            print(f"    payload will be wrapped with 4-byte length + 1-byte checksum")
            try:
                r = bridge.send_sigma_command(
                    SigmaOperationCode.SNAP_COMMAND,
                    sigma_payload=snap_payload,
                    data_phase_in=False,  # SnapCommand has no IN data phase
                    timeout_ms=10000,
                )
                print(f"    ✓ response=0x{r.response_code:04X}, "
                      f"params={r.response_params}")
                if r.response_code != 0x2001:
                    print(f"    ✗ Camera rejected SnapCommand (code 0x{r.response_code:04X}).")
            except Exception as e:  # noqa: BLE001
                print(f"    ✗ EXCEPTION: {e!r}")
                import traceback
                traceback.print_exc()
                return 1

            # 5. Poll for change
            print(f"\n[5] Polling GetCamCaptStatus every {POLL_MS}ms (timeout {POLL_TIMEOUT_S}s)")
            start = time.monotonic()
            iteration = 0
            changed_status: SgmCaptStatus | None = None
            new_image_id: int | None = None
            while time.monotonic() - start < POLL_TIMEOUT_S:
                iteration += 1
                try:
                    r = bridge.send_sigma_command(SigmaOperationCode.GET_CAM_CAPT_STATUS)
                except Exception as e:  # noqa: BLE001
                    print(f"  [poll {iteration}] EXCEPTION: {e!r}")
                    break
                status = parse_capt_status_safe(r.in_data)
                if status is None:
                    if iteration <= 3 or iteration % 10 == 0:
                        print(f"  [poll {iteration}] raw={r.in_data.hex()} (unparsed)")
                    time.sleep(POLL_MS / 1000)
                    continue

                changed = (
                    baseline_image_id is not None
                    and status.image_id != baseline_image_id
                ) or status.is_capturing or status.has_new_image

                if iteration <= 3 or changed or iteration % 10 == 0:
                    marker = " ← CHANGED!" if changed else ""
                    print(f"  [poll {iteration}] {status}{marker}")

                if changed and changed_status is None:
                    changed_status = status
                    new_image_id = status.image_id
                    # Keep polling a bit longer to catch the "ready" state
                    if status.has_new_image:
                        break
                time.sleep(POLL_MS / 1000)

            # 6. Result summary
            print()
            print("=" * 70)
            if changed_status is None:
                print("✗ GetCamCaptStatus never changed.")
                print(f"  Baseline: ImageID={baseline_image_id}")
                print("  Possible causes:")
                print("  - SnapCommand was accepted but shutter didn't fire (settings issue)")
                print("  - We need additional pre-snap handshake (SetCamDataGroupX)")
                print("  - Camera physically clicked but ImageID didn't increment in DB")
                return 1
            else:
                print(f"✓ Camera state changed after SnapCommand.")
                print(f"  Baseline ImageID: {baseline_image_id}")
                print(f"  New ImageID:      {new_image_id}")
                print(f"  Final status:     {changed_status}")
                if changed_status.has_new_image:
                    print(f"  → has_new_image=True, ready for GetPictFileInfo2 + download")
                else:
                    print(f"  → still capturing, would need to keep polling")
                return 0

    except USBBridgeError as e:
        print(f"\n✗ USBBridge error: {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\n✗ Unexpected error: {e!r}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

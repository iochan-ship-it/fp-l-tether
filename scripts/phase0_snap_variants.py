#!/usr/bin/env python3
"""Phase 0-C diagnostic — try multiple SnapCommand payload variants.

SnapCommand 0x901B returned 0x201D (Sigma vendor-specific rejection)
with our previous payload `02 01 03`. The third byte of SgmSnapState
(sometimes called "CheckSum" in reference notes) seems to have a different value than
a simple sum. This script tries several variants to find the working one.

Also tries adding a fuller "warmup" sequence (GetCamDataGroup 1-5) before
SnapCommand, matching the warmup sequence seen in the libgphoto2 #882 trace.

Run::

    sudo "/path/to/fp-l-tether/venv/bin/python" \\
         scripts/phase0_snap_variants.py
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
POLL_TIMEOUT_S = 6.0


def get_capt_status(bridge: USBBridge) -> SgmCaptStatus | None:
    r = bridge.send_sigma_command(SigmaOperationCode.GET_CAM_CAPT_STATUS)
    if r.response_code != 0x2001 or len(r.in_data) < 8:
        return None
    try:
        return SgmCaptStatus.from_wire(r.in_data)
    except Exception:
        return None


def warmup(bridge: USBBridge) -> None:
    """Read the camera's full setting state, matching the reference warmup."""
    print("\n=== Warmup: ConfigApi + GetCamDataGroup 1-5 + Focus + Movie + Permission ===")
    for op in [
        SigmaOperationCode.CONFIG_API,
        SigmaOperationCode.GET_CAM_DATA_GROUP_1,
        SigmaOperationCode.GET_CAM_DATA_GROUP_2,
        SigmaOperationCode.GET_CAM_DATA_GROUP_3,
        SigmaOperationCode.GET_CAM_DATA_GROUP_4,
        SigmaOperationCode.GET_CAM_DATA_GROUP_5,
        SigmaOperationCode.GET_CAM_DATA_GROUP_FOCUS,
        SigmaOperationCode.GET_CAM_DATA_GROUP_MOVIE,
        SigmaOperationCode.GET_CAM_CAN_SET_INFO_5,
        SigmaOperationCode.GET_CAM_OP_PERMISSION,
    ]:
        try:
            r = bridge.send_sigma_command(op)
            mark = "✓" if r.response_code == 0x2001 else "✗"
            print(f"  {mark} 0x{int(op):04X} → response=0x{r.response_code:04X}, "
                  f"data={len(r.in_data)}B")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ 0x{int(op):04X} raised: {e!r}")


def try_snap(
    bridge: USBBridge,
    variant_name: str,
    struct_bytes: bytes,
) -> tuple[bool, str]:
    """Try one variant of SnapCommand. Returns (success, summary)."""
    print(f"\n--- Variant: {variant_name} — struct={struct_bytes.hex()} ---")

    baseline = get_capt_status(bridge)
    baseline_id = baseline.image_id if baseline else None
    print(f"  baseline: ImageID={baseline_id}")

    try:
        r = bridge.send_sigma_command(
            SigmaOperationCode.SNAP_COMMAND,
            sigma_payload=struct_bytes,
            data_phase_in=False,
            timeout_ms=10000,
        )
        rc = r.response_code
    except Exception as e:  # noqa: BLE001
        return False, f"EXCEPTION: {e!r}"

    print(f"  SnapCommand response: 0x{rc:04X}")
    if rc != 0x2001:
        return False, f"rejected with 0x{rc:04X}"

    # Poll briefly for ImageID change
    print(f"  Polling for state change (max {POLL_TIMEOUT_S}s)...")
    start = time.monotonic()
    iteration = 0
    while time.monotonic() - start < POLL_TIMEOUT_S:
        iteration += 1
        status = get_capt_status(bridge)
        if status is None:
            time.sleep(POLL_MS / 1000)
            continue
        changed = (baseline_id is not None and status.image_id != baseline_id)
        if iteration <= 3 or changed or iteration % 5 == 0:
            print(f"    [poll {iteration}] {status}")
        if changed or status.is_capturing or status.has_new_image:
            return True, (f"ImageID {baseline_id}→{status.image_id}, "
                          f"capt_status=0x{status.capt_status:04X}")
        time.sleep(POLL_MS / 1000)

    return False, f"response=0x2001 but no state change after {POLL_TIMEOUT_S}s"


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    print("=" * 70)
    print("Phase 0-C — SnapCommand payload variant probe")
    print("=" * 70)
    print()
    print("Will try multiple SgmSnapState byte-3 values to find the one")
    print("the fp L accepts. After each Snap attempt, polls GetCamCaptStatus")
    print(f"for {POLL_TIMEOUT_S}s to detect ImageID increment.")
    print()
    input("Aim the camera at something visible, press Enter to begin... ")

    try:
        with USBBridge.find_sigma_fp_l() as bridge:
            bridge.open_session(session_id=1)
            print("✓ Session opened.")

            warmup(bridge)

            # Common: CaptureMode=0x02 (NON_AF_CAPTURE), CaptureAmount=0x01 (1 shot)
            mode = 0x02
            amount = 0x01

            # Variants of the 3rd "CheckSum" byte to try
            variants: list[tuple[str, bytes]] = [
                ("byte3=0x00 (treat as unused)",
                 bytes([mode, amount, 0x00])),
                ("byte3=0x01 (matching Windows trace constant)",
                 bytes([mode, amount, 0x01])),
                ("byte3 = CaptureAmount-1 = 0x00",
                 bytes([mode, amount, max(0, amount - 1)])),
                ("byte3 = mode XOR amount = 0x03",
                 bytes([mode, amount, mode ^ amount])),
                ("byte3 = mode+amount = 0x03 (our original)",
                 bytes([mode, amount, (mode + amount) & 0xFF])),
                ("byte3=0xFF (probe)",
                 bytes([mode, amount, 0xFF])),
                # Different CaptureMode values
                ("mode=0x01 (GENERAL_CAPTURE guess), byte3=0x01",
                 bytes([0x01, amount, 0x01])),
                ("mode=0x06 (START_CAPTURE), byte3=0x01",
                 bytes([0x06, amount, 0x01])),
            ]

            results: list[tuple[str, bool, str]] = []
            for name, payload in variants:
                ok, msg = try_snap(bridge, name, payload)
                results.append((name, ok, msg))
                # If a variant fires the shutter, save what we know
                if ok:
                    print(f"\n  ★★★ {name}: {msg}")
                    print(f"  ★★★ Payload that worked: {payload.hex()}")
                    # Stop here — found a working variant!
                    break
                # Brief settle between attempts
                time.sleep(0.5)

            print()
            print("=" * 70)
            print("Summary")
            print("=" * 70)
            for name, ok, msg in results:
                tick = "✓" if ok else "✗"
                print(f"  {tick} {name}")
                print(f"     → {msg}")

            return 0 if any(r[1] for r in results) else 1

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

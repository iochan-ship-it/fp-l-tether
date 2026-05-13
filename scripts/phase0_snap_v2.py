#!/usr/bin/env python3
"""Phase 0-C v2 — try SnapCommand with various PTP command-parameter combos.

The first variant pass (phase0_snap_variants.py) tried different 3rd-byte
values in the SgmSnapState data, all returning 0x201D (InvalidParameter).
This script tries DIFFERENT PTP COMMAND PARAMETERS instead, based on
Windows SDK trace patterns:

  Windows trace: ``0x00 0x00 0x00 0x00 0x04 0x00 0x00 0x00 0x02 0x02 0x01``
  Re-interpretation:
    command params: (0, 4)   ← param1=0, param2=4 (= data length?)
    data phase: ``02 02 01 05`` (struct + external checksum, no length prefix)

We also try variants without an inner length prefix wrapping the struct.

Run with sudo::

    sudo "/Users/PI/Documents/Claude/Projects/FP L Tether APP/venv/bin/python" \\
         scripts/phase0_snap_v2.py
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
    sigma_checksum,
)
from fp_l_tether.camera.usb_bridge import (  # noqa: E402
    USBBridge,
    USBBridgeError,
)

POLL_MS = 250
POLL_TIMEOUT_S = 5.0


def parse_status(in_data: bytes) -> SgmCaptStatus | None:
    if len(in_data) < 8:
        return None
    try:
        return SgmCaptStatus.from_wire(in_data)
    except Exception:
        return None


def get_current_image_id(bridge: USBBridge) -> int | None:
    r = bridge.send_sigma_command(SigmaOperationCode.GET_CAM_CAPT_STATUS)
    s = parse_status(r.in_data) if r.response_code == 0x2001 else None
    return s.image_id if s else None


def try_variant(
    bridge: USBBridge,
    label: str,
    params: tuple[int, ...],
    out_data: bytes | None,
    data_phase_out: bool,
) -> tuple[int, int | None, int | None]:
    """Try one SnapCommand variant.

    Returns (response_code, baseline_image_id, new_image_id_or_None).
    """
    print(f"\n--- {label} ---")
    print(f"  command params: {params}")
    if out_data is not None:
        print(f"  out_data ({len(out_data)} bytes): {out_data.hex()}")
    else:
        print(f"  out_data: None (no data phase out)")

    baseline_id = get_current_image_id(bridge)
    print(f"  baseline ImageID: {baseline_id}")

    try:
        resp = bridge.send_command_raw(
            SigmaOperationCode.SNAP_COMMAND,
            params=params,
            out_data=out_data,
            data_phase_out=data_phase_out,
            data_phase_in=False,
            timeout_ms=10000,
        )
        rc = resp.response_code
    except Exception as e:  # noqa: BLE001
        print(f"  EXCEPTION: {e!r}")
        return (0xFFFF, baseline_id, None)

    print(f"  response: 0x{rc:04X}")
    if rc != 0x2001:
        return (rc, baseline_id, None)

    # Brief poll for ImageID change
    print(f"  Polling for state change up to {POLL_TIMEOUT_S}s...")
    start = time.monotonic()
    iteration = 0
    new_id = None
    while time.monotonic() - start < POLL_TIMEOUT_S:
        iteration += 1
        cur = get_current_image_id(bridge)
        if iteration <= 2 or iteration % 5 == 0:
            print(f"    [poll {iteration}] ImageID={cur}")
        if cur is not None and baseline_id is not None and cur != baseline_id:
            new_id = cur
            print(f"  ★ ImageID changed: {baseline_id} → {new_id}")
            break
        time.sleep(POLL_MS / 1000)

    return (rc, baseline_id, new_id)


def main() -> int:
    logging.basicConfig(
        level=logging.WARNING,  # quieter to keep variant output readable
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    print("=" * 70)
    print("Phase 0-C v2 — SnapCommand with PTP command params + various data forms")
    print("=" * 70)
    print()
    input("Aim the camera, press Enter to begin... ")

    # Build the basic SgmSnapState struct (3 bytes per SDK).
    # Mode=0x02 NON_AF_CAPTURE, Amount=0x01 (1 shot), CheckSum byte = 0x01 (Windows trace)
    struct_3b = bytes([0x02, 0x01, 0x01])
    ext_chk = sigma_checksum(struct_3b)  # for variants that append it

    # Wrapped forms
    no_wrap = struct_3b + bytes([ext_chk])               # 4 bytes
    len_wrap = struct.pack("<I", 4) + struct_3b + bytes([ext_chk])  # 8 bytes

    results: list[tuple[str, int, int | None, int | None]] = []

    try:
        with USBBridge.find_sigma_fp_l() as bridge:
            bridge.open_session(session_id=1)
            print("✓ Session opened.\n")

            # Mandatory handshake
            print("--- Warmup ---")
            for op in [
                SigmaOperationCode.CONFIG_API,
                SigmaOperationCode.GET_CAM_DATA_GROUP_1,
                SigmaOperationCode.GET_CAM_DATA_GROUP_2,
                SigmaOperationCode.GET_CAM_DATA_GROUP_3,
                SigmaOperationCode.GET_CAM_DATA_GROUP_4,
                SigmaOperationCode.GET_CAM_DATA_GROUP_5,
                SigmaOperationCode.GET_CAM_OP_PERMISSION,
            ]:
                try:
                    r = bridge.send_sigma_command(op)
                    print(f"  0x{int(op):04X} → 0x{r.response_code:04X}, "
                          f"{len(r.in_data)}B")
                except Exception as e:  # noqa: BLE001
                    print(f"  0x{int(op):04X} EXCEPTION: {e!r}")

            # ---------------- Variants ----------------
            variants = [
                # (label, params, out_data, data_phase_out)
                ("v1: params=(), data=len_wrapped (current code, fails)",
                 (), len_wrap, True),
                ("v2: params=(), data=struct+ext_chk only (no length prefix)",
                 (), no_wrap, True),
                ("v3: params=(0,), data=struct+ext_chk only",
                 (0,), no_wrap, True),
                ("v4: params=(0, 4), data=struct+ext_chk only ← Windows trace shape",
                 (0, 4), no_wrap, True),
                ("v5: params=(0, 4), data=len_wrapped",
                 (0, 4), len_wrap, True),
                ("v6: params=(4,), data=struct+ext_chk only",
                 (4,), no_wrap, True),
                ("v7: params=(0, 4), data=struct only (no ext checksum)",
                 (0, 4), struct_3b, True),
                ("v8: params=(0, 4), NO data phase",
                 (0, 4), None, False),
            ]

            for label, params, out_data, data_phase_out in variants:
                rc, base, new = try_variant(
                    bridge, label, params, out_data, data_phase_out
                )
                results.append((label, rc, base, new))
                if new is not None:
                    print(f"\n  🎯🎯🎯 SUCCESS! {label}")
                    print(f"  🎯 Camera fired the shutter (ImageID {base}→{new})")
                    break
                time.sleep(0.3)

            # Summary
            print()
            print("=" * 70)
            print("Summary")
            print("=" * 70)
            for label, rc, base, new in results:
                if new is not None:
                    print(f"  ★ {label}")
                    print(f"     → SUCCESS: ImageID {base}→{new}")
                else:
                    print(f"  ✗ {label}")
                    print(f"     → rc=0x{rc:04X}")

            return 0 if any(r[3] is not None for r in results) else 1

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

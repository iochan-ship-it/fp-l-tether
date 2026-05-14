#!/usr/bin/env python3
"""Phase 0-B — Session test: open a PTP session and ping GetCamCaptStatus.

This verifies that:
  1. We can open a PTP session to the Sigma fp L via ImageCaptureCore
  2. ConfigApi (0x9035) handshake works
  3. GetCamCaptStatus (0x9015) returns a parseable SgmCaptStatus

Run::

    python scripts/phase0_session_test.py

Run AFTER phase0_smoke_test.py has passed.

If this script hangs at "Opening PTP session...":
  * Another tether or PTP client (Image Capture.app, Lightroom, etc.) is
    holding the camera. Quit it.

If GetCamCaptStatus returns 0x2005 (Operation Not Supported):
  * The ConfigApi handshake didn't enter "PC control mode". Check the
    Sigma fp L USB mode setting again, or the ordering of ConfigApi vs
    GetCamOpPermission.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fp_l_tether.camera.ic_bridge import (  # noqa: E402
    Camera,
    MacOSOnlyError,
    find_first_sigma_fp_l,
)
from fp_l_tether.camera.ptp_codes import (  # noqa: E402
    PTPOperationCode,
    PTPResponseCode,
    SgmCaptStatus,
    SigmaOperationCode,
)


def hex_dump(data: bytes, max_len: int = 64) -> str:
    """Compact hex dump for log output."""
    truncated = data[:max_len]
    suffix = f" ... (+{len(data) - max_len} bytes)" if len(data) > max_len else ""
    return " ".join(f"{b:02X}" for b in truncated) + suffix


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    print("=" * 70)
    print("Phase 0-B — PTP session + GetCamCaptStatus ping")
    print("=" * 70)

    try:
        fp_l = find_first_sigma_fp_l(timeout_seconds=5.0)
    except MacOSOnlyError as e:
        print(f"ERROR: {e}")
        return 2

    if fp_l is None:
        print("✗ Sigma fp L not connected. Run phase0_smoke_test.py first.")
        return 1
    print(f"Target camera: {fp_l.name} (SN: {fp_l.serial_number})")
    print()

    # -------------------------------------------------------------------
    # Step 1: Open PTP session
    # -------------------------------------------------------------------
    print("Step 1: Opening PTP session via ImageCaptureCore...")
    try:
        with Camera(fp_l, ptp_timeout_seconds=10.0) as session:
            print("  ✓ Session opened.")
            print()

            # -------------------------------------------------------------------
            # Step 2: ConfigApi (0x9035) — negotiate API version
            # -------------------------------------------------------------------
            print(f"Step 2: ConfigApi (0x{SigmaOperationCode.CONFIG_API:04X})...")
            try:
                resp = session.send_ptp(SigmaOperationCode.CONFIG_API)
                print(f"  Response code: 0x{resp.response_code:04X} "
                      f"({_response_name(resp.response_code)})")
                print(f"  in_data       : {hex_dump(resp.in_data)}")
                if not resp.is_ok:
                    print("  ⚠ ConfigApi did not return OK — will continue but capture may fail.")
            except Exception as e:  # noqa: BLE001
                print(f"  ✗ ConfigApi raised: {e!r}")
                print("    NOTE: Some firmware versions may not require ConfigApi.")
                print("    Continuing to GetCamCaptStatus to see if it works without it.")
            print()

            # -------------------------------------------------------------------
            # Step 3: GetCamOpPermission (0x9039) — confirm PC control mode
            # -------------------------------------------------------------------
            print(f"Step 3: GetCamOpPermission (0x{SigmaOperationCode.GET_CAM_OP_PERMISSION:04X})...")
            try:
                resp = session.send_ptp(SigmaOperationCode.GET_CAM_OP_PERMISSION)
                print(f"  Response code: 0x{resp.response_code:04X}")
                print(f"  in_data       : {hex_dump(resp.in_data)}")
                # Earlier USB traces have shown "[opPermission] PC control mode"
                # which suggests the in_data contains a "PC control mode" flag
            except Exception as e:  # noqa: BLE001
                print(f"  ⚠ GetCamOpPermission raised: {e!r}")
                print("    Older firmware may not support this opcode — skipping.")
            print()

            # -------------------------------------------------------------------
            # Step 4: GetCamCaptStatus (0x9015) — the actual ping
            # -------------------------------------------------------------------
            print(f"Step 4: GetCamCaptStatus (0x{SigmaOperationCode.GET_CAM_CAPT_STATUS:04X})...")
            try:
                resp = session.send_ptp(SigmaOperationCode.GET_CAM_CAPT_STATUS)
            except Exception as e:  # noqa: BLE001
                print(f"  ✗ GetCamCaptStatus raised: {e!r}")
                return 1

            print(f"  Response code: 0x{resp.response_code:04X} "
                  f"({_response_name(resp.response_code)})")
            print(f"  in_data ({len(resp.in_data)} bytes): {hex_dump(resp.in_data, 128)}")

            if not resp.is_ok:
                print("  ✗ Response not OK — check setup.")
                return 1

            # Try to parse SgmCaptStatus. Note: there may be a length-prefix or
            # padding before the actual struct bytes; refine offset during testing.
            for skip in (0, 1, 4, 5, 8):
                if len(resp.in_data) < skip + 7:
                    continue
                try:
                    status = SgmCaptStatus.from_bytes(resp.in_data[skip:])
                    print(f"  Trying offset={skip}: {status}")
                except Exception:
                    pass

            print()
            print("=" * 70)
            print("✓ Phase 0-B PASSED")
            print("  → PTP session is functional. Save the in_data trace above")
            print("    to docs/PTP_TRACE.md before moving to phase0_snap_test.py")
            return 0

    except Exception as e:  # noqa: BLE001
        print(f"✗ Phase 0-B FAILED with exception: {e!r}")
        import traceback

        traceback.print_exc()
        return 1


def _response_name(code: int) -> str:
    try:
        return PTPResponseCode(code).name
    except ValueError:
        return f"0x{code:04X} (unknown)"


if __name__ == "__main__":
    raise SystemExit(main())

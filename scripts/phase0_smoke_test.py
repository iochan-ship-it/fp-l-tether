#!/usr/bin/env python3
"""Phase 0-A — Smoke test: can macOS see the Sigma fp L at all?

This script does NOT open a PTP session. It only asks Apple's ICDeviceBrowser
which cameras are currently connected, and prints capabilities.

Success criteria:
  * A camera with name containing "fp L" (or product ID 0xC442) shows up
  * Its capabilities list includes "ICCameraDeviceCanAcceptPTPCommands"

Run::

    python scripts/phase0_smoke_test.py

If this fails, do NOT proceed to phase0_session_test.py — the issue is at
the USB / Image Capture level, not at the PTP level.
"""

from __future__ import annotations

import logging
import platform
import sys
from pathlib import Path

# Make the package importable when running this script directly
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fp_l_tether.camera.ic_bridge import (  # noqa: E402
    list_cameras,
    MacOSOnlyError,
)
from fp_l_tether.camera.ptp_codes import (  # noqa: E402
    SIGMA_FP_L_PRODUCT_ID,
    SIGMA_FP_PRODUCT_ID,
    SIGMA_VENDOR_ID,
)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    print("=" * 70)
    print("Phase 0-A — Smoke test: Camera enumeration via ICDeviceBrowser")
    print("=" * 70)
    print(f"Platform           : {platform.platform()}")
    print(f"Python             : {sys.version.split()[0]}")
    print(f"Looking for VID/PID: 0x{SIGMA_VENDOR_ID:04X} / "
          f"0x{SIGMA_FP_PRODUCT_ID:04X} (fp) or 0x{SIGMA_FP_L_PRODUCT_ID:04X} (fp L)")
    print()

    try:
        cameras = list_cameras(timeout_seconds=3.0)
    except MacOSOnlyError as e:
        print(f"ERROR: {e}")
        print("\nThis script must be run on macOS with PyObjC installed.")
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: unexpected exception: {e!r}")
        return 3

    if not cameras:
        print("✗ No cameras detected.")
        print()
        print("Troubleshooting:")
        print("  1. Is the Sigma fp L powered on?")
        print("  2. Is the USB-C cable connected to the Mac?")
        print("  3. Is the camera's USB mode set to 'Camera Control'?")
        print("     (Mass Storage and UVC modes are NOT supported by this app.)")
        print("  4. Is another app (Capture One, Image Capture.app) "
              "holding the camera? Quit it first.")
        print("  5. macOS Privacy & Security: any prompts pending?")
        return 1

    print(f"Found {len(cameras)} camera(s):")
    print("-" * 70)
    sigma_fp_l_found = False
    for i, cam in enumerate(cameras, start=1):
        print(f"\n[{i}] {cam.name}")
        vid_str = f"0x{cam.vendor_id:04X}" if cam.vendor_id else "(unknown)"
        pid_str = f"0x{cam.product_id:04X}" if cam.product_id else "(unknown)"
        print(f"    Vendor ID         : {vid_str}")
        print(f"    Product ID        : {pid_str}")
        print(f"    Serial Number     : {cam.serial_number or '(none)'}")
        print(f"    Transport         : {cam.transport_type}")
        print(f"    PTP capable       : {'YES' if cam.can_accept_ptp_commands else 'NO'}")
        print(f"    Take picture API  : {'YES' if cam.can_take_picture else 'NO'}")
        print(f"    Is Sigma fp / fp L: {'YES' if cam.is_sigma_fp_family else 'no'}")
        if cam.is_fp_l:
            print(f"    *** Sigma fp L detected ***")
            sigma_fp_l_found = True

    print("\n" + "=" * 70)
    if sigma_fp_l_found:
        # Find the fp L specifically
        fp_l = next(c for c in cameras if c.is_fp_l)
        if not fp_l.can_accept_ptp_commands:
            print("✗ Sigma fp L visible but does NOT advertise PTP capability.")
            print("  This is unexpected. Check FW version (need v3.0+) and")
            print("  the USB mode (must be 'Camera Control').")
            return 1
        print("✓ Phase 0-A PASSED: Sigma fp L is connected and PTP capable.")
        print("  → Proceed to phase0_session_test.py")
        return 0
    else:
        print("✗ Sigma fp L not found among connected cameras.")
        print("  Check USB mode = 'Camera Control', power, cable.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

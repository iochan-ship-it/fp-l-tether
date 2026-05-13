#!/usr/bin/env python3
"""Phase 0-C diagnostic — what state is the camera in AFTER a successful capture?

This script takes ONE successful shot, then probes the camera's state
extensively to find out why subsequent snaps don't fire.

What it does:

  1. Open PTP session
  2. Run init (10 calls)
  3. Capture shot 1 (snap + poll + download + clear)
  4. Poll status every 1s for 30s, logging every change
  5. Try various "recovery" commands:
       a. Re-call ConfigApi (0x9035)
       b. Re-call GetCamCanSetInfo5 (0x9030)
       c. Try snap again — see if status changes now
  6. Compare what state worked for shot 1 vs what state shot 2 sees

Outputs verbose timeline so we can spot the moment when the camera
"refuses" the next snap.

Run::

    sudo venv/bin/python scripts/phase0_diag_post_capture.py
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from fp_l_tether.camera.usb_bridge import (  # noqa: E402
    PTPError,
    USBBridge,
    USBBridgeError,
)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(message)s",
        datefmt="%H:%M:%S",
    )


def log_status(bridge: USBBridge, label: str) -> int:
    """Log current capture status. Returns the status code."""
    try:
        s = bridge.sigma_get_capture_status(0)
        print(
            f"  [{label}] status=0x{s.capt_status:04X} image_id=0x{s.image_id:02X} "
            f"dest=0x{s.destination_to_save:02X} db_head=0x{s.image_db_head:02X} "
            f"db_tail=0x{s.image_db_tail:02X}"
        )
        return s.capt_status
    except (PTPError, USBBridgeError) as e:
        print(f"  [{label}] ERROR: {e}")
        return -1


def main() -> int:
    setup_logging()
    print("=" * 70)
    print(" Sigma fp L — post-capture state diagnostic")
    print("=" * 70)

    bridge = USBBridge.find_sigma_fp_l()
    with bridge:
        bridge.open_session()

        print("\n[1] Running init sequence ...")
        bridge.sigma_init()
        print("    ✓ init done")

        print("\n[2] Probing camera state BEFORE first snap:")
        log_status(bridge, "pre-shot-1")

        print("\n[3] First capture (the one we know works) ...")
        try:
            info, data = bridge.sigma_capture_one(
                mode=1, amount=1,
                max_poll_iterations=150,
                poll_interval_s=0.2,
            )
            print(f"    ✓ captured {info.name}{info.fileext} ({len(data):,} bytes)")
        except Exception as e:  # noqa: BLE001
            print(f"    ✗ FAILED: {e}")
            return 1

        # Don't save the file — we don't care about the bytes here.

        print("\n[4] Probing camera state AFTER first capture:")
        log_status(bridge, "post-clear")

        print("\n[5] Polling status every 1s for 30s to see recovery pattern...")
        last_status = None
        for i in range(30):
            time.sleep(1.0)
            try:
                s = bridge.sigma_get_capture_status(0)
                stat = s.capt_status
                if stat != last_status:
                    print(
                        f"  t+{i+1:02d}s status=0x{stat:04X} "
                        f"image_id=0x{s.image_id:02X} dest=0x{s.destination_to_save:02X} "
                        f"db_head=0x{s.image_db_head:02X} db_tail=0x{s.image_db_tail:02X}"
                        f"   ⟵ CHANGED"
                    )
                    last_status = stat
                elif i % 5 == 0:
                    print(f"  t+{i+1:02d}s status=0x{stat:04X} (unchanged)")
            except Exception as e:  # noqa: BLE001
                print(f"  t+{i+1:02d}s status query failed: {e}")

        print("\n[6] Trying snap WITHOUT recovery command:")
        log_status(bridge, "pre-shot-2-direct")
        print("    sending snap(1, 1)...")
        try:
            bridge.sigma_snap(mode=1, amount=1)
        except (PTPError, USBBridgeError) as e:
            print(f"    snap raised: {e}")
        for i in range(15):
            time.sleep(0.5)
            stat = log_status(bridge, f"shot-2 poll {i+1}")
            if stat in (0x0002, 0x0005):
                print("    ✓ snap fired!")
                break
            if (stat & 0xF000) == 0x6000:
                print(f"    ✗ snap failed with status 0x{stat:04X}")
                break
        else:
            print("    ✗ status never changed after snap (likely ignored)")

        print("\n[7] Recovery experiment: re-call ConfigApi then snap")
        try:
            print("    re-calling ConfigApi (0x9035)...")
            data = bridge.sigma_get_camera_info()
            print(f"    ✓ ConfigApi returned {len(data)} bytes")
        except (PTPError, USBBridgeError) as e:
            print(f"    ✗ ConfigApi failed: {e}")

        log_status(bridge, "post-configapi")
        print("    sending snap(1, 1) after ConfigApi...")
        try:
            bridge.sigma_snap(mode=1, amount=1)
        except (PTPError, USBBridgeError) as e:
            print(f"    snap raised: {e}")
        for i in range(15):
            time.sleep(0.5)
            stat = log_status(bridge, f"shot-2b poll {i+1}")
            if stat in (0x0002, 0x0005):
                print("    ✓ snap fired after ConfigApi re-call!")
                return 0
            if (stat & 0xF000) == 0x6000:
                print(f"    ✗ snap failed with status 0x{stat:04X}")
                break
        else:
            print("    ✗ status never changed after ConfigApi+snap")

        print("\n[8] Recovery experiment: try DIFFERENT snap modes")
        for mode in (2, 3, 4, 6):
            print(f"\n    Trying snap(mode={mode}, amount=1)...")
            try:
                bridge.sigma_snap(mode=mode, amount=1)
            except (PTPError, USBBridgeError) as e:
                print(f"    snap mode={mode} raised: {e}")
                continue
            for i in range(10):
                time.sleep(0.5)
                stat = log_status(bridge, f"mode{mode} poll {i+1}")
                if stat in (0x0002, 0x0005):
                    print(f"    🎯 mode={mode} fired! status reached 0x{stat:04X}")
                    return 0
                if (stat & 0xF000) == 0x6000:
                    print(f"    ✗ mode={mode} failed with status 0x{stat:04X}")
                    break
            else:
                print(f"    ✗ mode={mode}: status unchanged")

    print("\n" + "=" * 70)
    print(" Diagnostic complete — see output above for analysis")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

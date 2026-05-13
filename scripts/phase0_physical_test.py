#!/usr/bin/env python3
"""Phase 0-C diagnostic — Test download path only (NO SnapCommand).

If phase0_snap_test.py fails because of SnapCommand, this script isolates
the question: "Can we even download an image that the camera already took?"

The user presses the camera's physical shutter button. The script polls
GetCamCaptStatus until a new image is reported, then tries to download it
via GetPictFileInfo2 + GetBigPartialPictFile.

If this works → SnapCommand is the only problem, fix that.
If this also fails → both paths need fixing; we should also dump raw
                     PTP traces and reconsider the in_data extraction.

Run::

    python scripts/phase0_physical_test.py
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fp_l_tether.camera.ic_bridge import (  # noqa: E402
    Camera,
    MacOSOnlyError,
    find_first_sigma_fp_l,
)
from fp_l_tether.camera.ptp_codes import (  # noqa: E402
    SgmCaptStatus,
    SigmaOperationCode,
)

OUT_DIR = Path("/tmp/fp_l_phase0_physical")
POLL_MS = 200
POLL_TIMEOUT_S = 30.0


def hex_dump(data: bytes, max_len: int = 128) -> str:
    truncated = data[:max_len]
    suffix = f" ... (+{len(data) - max_len} bytes)" if len(data) > max_len else ""
    return " ".join(f"{b:02X}" for b in truncated) + suffix


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    print("=" * 70)
    print("Phase 0-C diagnostic — Physical shutter → download (no SnapCommand)")
    print("=" * 70)
    print()
    print("Workflow:")
    print("  1. App opens PTP session")
    print("  2. Reads initial GetCamCaptStatus (baseline)")
    print("  3. YOU press the physical shutter on the camera")
    print("  4. App polls GetCamCaptStatus until ImageID changes")
    print("  5. App tries GetPictFileInfo2 + GetBigPartialPictFile")
    print()
    input("Press Enter when ready (camera connected, Image Capture.app closed)... ")

    try:
        fp_l = find_first_sigma_fp_l(timeout_seconds=5.0)
    except MacOSOnlyError as e:
        print(f"ERROR: {e}")
        return 2

    if fp_l is None:
        print("Sigma fp L not found.")
        return 1
    print(f"Target: {fp_l.name} (SN: {fp_l.serial_number})")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        with Camera(fp_l, ptp_timeout_seconds=15.0) as session:
            print("Session opened.")

            # Warm up
            print("\nWarmup: ConfigApi + GetCamCaptStatus")
            for op in (SigmaOperationCode.CONFIG_API, SigmaOperationCode.GET_CAM_CAPT_STATUS):
                r = session.send_ptp(op)
                print(f"  0x{int(op):04X} → response=0x{r.response_code:04X} "
                      f"in_data={hex_dump(r.in_data, 32)}")

            # Baseline reading
            print("\nBaseline GetCamCaptStatus reading:")
            baseline = session.send_ptp(SigmaOperationCode.GET_CAM_CAPT_STATUS)
            print(f"  in_data ({len(baseline.in_data)} bytes): {hex_dump(baseline.in_data)}")
            for skip in (0, 1, 2, 4, 5, 8):
                if len(baseline.in_data) >= skip + 7:
                    try:
                        st = SgmCaptStatus.from_bytes(baseline.in_data[skip:])
                        print(f"  parsed @offset={skip}: {st}")
                    except Exception:
                        pass

            # ------------------------------------------------------------------
            # Now wait for the user to physically press the shutter
            # ------------------------------------------------------------------
            print()
            print("=" * 70)
            print(">>> NOW PRESS THE PHYSICAL SHUTTER ON THE CAMERA <<<")
            print("=" * 70)
            print(f"Polling GetCamCaptStatus every {POLL_MS}ms for up to {POLL_TIMEOUT_S}s...")
            print()

            start = time.monotonic()
            iteration = 0
            last_data: bytes | None = None
            changed_iter: int | None = None
            new_status: SgmCaptStatus | None = None
            new_in_data: bytes | None = None

            while time.monotonic() - start < POLL_TIMEOUT_S:
                iteration += 1
                try:
                    r = session.send_ptp(SigmaOperationCode.GET_CAM_CAPT_STATUS)
                except Exception as e:  # noqa: BLE001
                    print(f"  [poll {iteration}] EXCEPTION: {e!r}")
                    time.sleep(POLL_MS / 1000)
                    continue

                changed = (last_data is not None and r.in_data != last_data)
                if iteration <= 3 or changed or iteration % 10 == 0:
                    marker = "CHANGED!" if changed else ""
                    print(f"  [poll {iteration}] resp=0x{r.response_code:04X} "
                          f"in_data={hex_dump(r.in_data, 32)} {marker}")
                if changed and changed_iter is None:
                    changed_iter = iteration
                    new_in_data = r.in_data
                    # Try to parse SgmCaptStatus at various offsets
                    for skip in (0, 1, 2, 4, 5, 8):
                        if len(r.in_data) >= skip + 7:
                            try:
                                new_status = SgmCaptStatus.from_bytes(r.in_data[skip:])
                                print(f"    parsed @offset={skip}: {new_status}")
                            except Exception:
                                pass
                    break

                last_data = r.in_data
                time.sleep(POLL_MS / 1000)

            if changed_iter is None:
                print()
                print("✗ GetCamCaptStatus never reported a change.")
                print("  Either the shutter didn't fire, or in_data is not the")
                print("  data phase but a different field. Save the trace:")
                print(f"  baseline in_data: {baseline.in_data.hex()}")
                # Save a raw dump for inspection
                (OUT_DIR / "baseline_in_data.bin").write_bytes(baseline.in_data)
                print(f"  Saved baseline to {OUT_DIR}/baseline_in_data.bin")
                return 1

            print()
            print(f"✓ Shutter detected at poll #{changed_iter}!")
            print(f"  Baseline:  {baseline.in_data.hex()}")
            print(f"  After:     {new_in_data.hex() if new_in_data else 'n/a'}")
            (OUT_DIR / "after_shutter_in_data.bin").write_bytes(new_in_data or b"")

            # ------------------------------------------------------------------
            # Try GetPictFileInfo2 and GetBigPartialPictFile
            # ------------------------------------------------------------------
            if new_status is None or new_status.image_id == 0:
                print()
                print("Could not parse a valid ImageID. Trying image_id=1 as fallback...")
                image_id = 1
            else:
                image_id = new_status.image_id

            print(f"\nGetPictFileInfo2 (image_id=0x{image_id:02X})...")
            try:
                info_resp = session.send_ptp(
                    SigmaOperationCode.GET_PICT_FILE_INFO_2,
                    parameters=(image_id,),
                )
                print(f"  response=0x{info_resp.response_code:04X}")
                print(f"  in_data ({len(info_resp.in_data)} bytes): "
                      f"{hex_dump(info_resp.in_data, 256)}")
                (OUT_DIR / "pictfileinfo2.bin").write_bytes(info_resp.in_data)
                print(f"  Saved to {OUT_DIR}/pictfileinfo2.bin")
            except Exception as e:  # noqa: BLE001
                print(f"  EXCEPTION: {e!r}")

            print()
            print("=" * 70)
            print("Diagnostic complete.")
            print("=" * 70)
            print(f"Files saved to {OUT_DIR}/")
            print(f"  - baseline_in_data.bin")
            print(f"  - after_shutter_in_data.bin")
            print(f"  - pictfileinfo2.bin")
            print()
            print("Next step: paste the contents of {OUT_DIR}/ into the conversation")
            print("so we can refine the SgmCaptStatus / SgmPictureFileInfo parsers.")
            return 0

    except Exception as e:  # noqa: BLE001
        print(f"\nSession error: {e!r}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

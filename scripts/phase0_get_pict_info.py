#!/usr/bin/env python3
"""Phase 0-C — probe GetPictFileInfo2 with various ImageIDs.

Standard PTP enumeration shows 0 objects, but the Sigma camera reports
ImageID=6 in GetCamCaptStatus, suggesting it has 6 images in its internal
PC-side ImageDB. We just need to query them via the SIGMA-specific
GetPictFileInfo2 (0x902d) and GetBigPartialPictFile (0x9022).

This script:
  1. Reads current GetCamCaptStatus
  2. Tries Sigma GetNumDownloadableObjects (0x9001)
  3. Tries Sigma GetAllObjectInfo (0x9002)
  4. Tries GetPictFileInfo2 with ImageIDs 1, 2, 3, 4, 5, 6, 7
  5. For any that return data, dumps it

Run with sudo::

    sudo "/path/to/fp-l-tether/venv/bin/python" \\
         scripts/phase0_get_pict_info.py
"""

from __future__ import annotations

import logging
import sys
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


def hex_dump(data: bytes, max_len: int = 256) -> str:
    out = []
    show = data[:max_len]
    for i in range(0, len(show), 16):
        chunk = show[i : i + 16]
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"    {i:04X}: {hex_part:<48}  {ascii_part}")
    if len(data) > max_len:
        out.append(f"    ... +{len(data) - max_len} bytes")
    return "\n".join(out) if out else "    (empty)"


def main() -> int:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    print("=" * 70)
    print("Phase 0-C — Sigma-specific GetPictFileInfo2 probe")
    print("=" * 70)

    try:
        with USBBridge.find_sigma_fp_l() as bridge:
            bridge.open_session(session_id=1)
            print("✓ Session opened.\n")

            # Standard warmup
            bridge.send_sigma_command(SigmaOperationCode.CONFIG_API)
            bridge.send_sigma_command(SigmaOperationCode.GET_CAM_OP_PERMISSION)

            # Current status
            r = bridge.send_sigma_command(SigmaOperationCode.GET_CAM_CAPT_STATUS)
            print(f"GetCamCaptStatus: in_data={r.in_data.hex()}")
            if len(r.in_data) >= 8:
                try:
                    s = SgmCaptStatus.from_wire(r.in_data)
                    print(f"  parsed: {s}")
                    print(f"  ImageID={s.image_id}, DBHead={s.image_db_head}, "
                          f"DBTail={s.image_db_tail}")
                except Exception as e:
                    print(f"  parse error: {e}")

            # Sigma GetNumDownloadableObjects (0x9001)
            print("\n--- GetNumDownloadableObjects (0x9001) ---")
            try:
                r = bridge.send_command_raw(
                    SigmaOperationCode.GET_NUM_DOWNLOADABLE_OBJECTS,
                    data_phase_in=False,
                )
                print(f"  response: 0x{r.response_code:04X}, params={r.response_params}")
            except Exception as e:  # noqa: BLE001
                print(f"  EXCEPTION: {e!r}")

            # Sigma GetAllObjectInfo (0x9002)
            print("\n--- GetAllObjectInfo (0x9002) ---")
            try:
                r = bridge.send_command_raw(SigmaOperationCode.GET_ALL_OBJECT_INFO)
                print(f"  response: 0x{r.response_code:04X}, in_data={len(r.in_data)} bytes")
                print(hex_dump(r.in_data, 256))
            except Exception as e:  # noqa: BLE001
                print(f"  EXCEPTION: {e!r}")

            # GetPictFileInfo2 with various ImageIDs
            print("\n--- GetPictFileInfo2 (0x902d) — try ImageID 0..7 ---")
            for image_id in range(8):
                print(f"\n  ImageID = {image_id} (0x{image_id:02X})")
                # Try with no extra params (relying on Sigma OUT data phase
                # to specify ImageID), and also with param1 = image_id.

                # Method 1: pass image_id as a PTP command param
                try:
                    r = bridge.send_command_raw(
                        SigmaOperationCode.GET_PICT_FILE_INFO_2,
                        params=(image_id,),
                        data_phase_in=True,
                    )
                    print(f"    method 1 (params=({image_id},)):")
                    print(f"      response: 0x{r.response_code:04X}, "
                          f"in_data={len(r.in_data)} bytes")
                    if r.response_code == 0x2001 and r.in_data:
                        print(hex_dump(r.in_data, 256))
                    elif r.in_data:
                        print(f"      raw: {r.in_data.hex()}")
                except Exception as e:  # noqa: BLE001
                    print(f"    method 1 EXCEPTION: {e!r}")

            return 0

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

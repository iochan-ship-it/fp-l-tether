#!/usr/bin/env python3
"""Phase 0 deep diagnostic — figure out what Apple's inData actually contains.

The previous tests showed inconsistent inData sizes (4 vs 8 bytes) that don't
match the expected SgmCaptStatus 7-byte struct. This script probes a known
standard PTP operation (GetDeviceInfo) whose response is hundreds of bytes
long with a well-defined format. That will tell us definitively whether:

  (A) Apple's ``inData`` IS the PTP data phase
      → For Sigma ops we're seeing empty/short data because the camera
        returns short data; need to revisit Sigma protocol
  (B) Apple's ``inData`` is the response container parameters (and the
      data phase goes somewhere else)
      → We need a completely different reading strategy

Run::

    python scripts/phase0_diag_v2.py
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
    SigmaOperationCode,
)


def hex_full(data: bytes) -> str:
    """Hex dump with no truncation, 16 bytes per line."""
    out = []
    for i in range(0, len(data), 16):
        chunk = data[i : i + 16]
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"    {i:04X}: {hex_part:<48}  {ascii_part}")
    return "\n".join(out) if out else "    (empty)"


def probe(session, opcode_int: int, label: str, parameters: tuple = ()) -> None:
    print(f"\n--- {label} (0x{opcode_int:04X}) ---")
    print(f"  parameters: {parameters}")
    try:
        resp = session.send_ptp(opcode_int, parameters=parameters)
    except Exception as e:  # noqa: BLE001
        print(f"  EXCEPTION: {e!r}")
        return

    print(f"  response_code parsed: 0x{resp.response_code:04X}")
    print(f"  in_data  (len={len(resp.in_data):>5} bytes):")
    print(hex_full(resp.in_data[:256]))
    if len(resp.in_data) > 256:
        print(f"    ... +{len(resp.in_data) - 256} more bytes truncated for display")
    print(f"  response (len={len(resp.response):>5} bytes):")
    print(hex_full(resp.response[:256]))
    if len(resp.response) > 256:
        print(f"    ... +{len(resp.response) - 256} more bytes truncated for display")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    print("=" * 70)
    print("Phase 0 deep diagnostic — probe what inData actually contains")
    print("=" * 70)

    try:
        fp_l = find_first_sigma_fp_l(timeout_seconds=5.0)
    except MacOSOnlyError as e:
        print(f"ERROR: {e}")
        return 2
    if fp_l is None:
        print("No camera.")
        return 1
    print(f"Target: {fp_l.name}")

    try:
        with Camera(fp_l, ptp_timeout_seconds=15.0) as session:
            print("Session opened.\n")

            # 1) Standard PTP GetDeviceInfo — KNOWN to return a large struct
            #    (manufacturer name, model name, supported operations list, etc.)
            #    If inData here is short, then inData is NOT the data phase.
            probe(session, PTPOperationCode.GET_DEVICE_INFO, "GetDeviceInfo (standard PTP)")

            # 2) Standard PTP GetStorageIDs — returns array of storage IDs
            probe(session, PTPOperationCode.GET_STORAGE_IDS, "GetStorageIDs (standard PTP)")

            # 3) Sigma ConfigApi
            probe(session, SigmaOperationCode.CONFIG_API, "ConfigApi (Sigma)")

            # 4) Sigma GetCamCaptStatus (the one we care most about)
            probe(session, SigmaOperationCode.GET_CAM_CAPT_STATUS, "GetCamCaptStatus (Sigma)")

            # 5) Sigma GetCamDataGroup1 — returns ISO/SS/aperture etc.
            #    This should definitely have meaningful payload bytes
            probe(session, SigmaOperationCode.GET_CAM_DATA_GROUP_1, "GetCamDataGroup1 (Sigma)")

            print("\n" + "=" * 70)
            print("Done. Save this entire output and share for analysis.")
            print("=" * 70)
            return 0

    except Exception as e:  # noqa: BLE001
        print(f"Error: {e!r}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

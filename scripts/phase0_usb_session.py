#!/usr/bin/env python3
"""Phase 0-B redux — open a PTP session via libusb and ping GetCamCaptStatus.

This replaces ``phase0_session_test.py`` which used ImageCaptureCore. With
the USB bridge we expect to see the ACTUAL data phase content (a 7-byte
SgmCaptStatus struct), not a stripped 4-byte ICA artifact.

Run with sudo (required on macOS to detach kernel driver)::

    sudo "/path/to/fp-l-tether/venv/bin/python" \\
         scripts/phase0_usb_session.py
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
    unwrap_sigma_fixed_struct,
    unwrap_sigma_payload,
)


def hex_dump(data: bytes, max_len: int = 128) -> str:
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


def probe(
    bridge: USBBridge,
    opcode: int,
    label: str,
    *,
    wire_format: str = "variable",  # "variable" | "fixed" | "raw"
    fixed_size: int = 0,
) -> None:
    print(f"\n--- {label} (0x{int(opcode):04X}) ---")
    try:
        resp = bridge.send_sigma_command(opcode)
    except Exception as e:  # noqa: BLE001
        print(f"  EXCEPTION: {e!r}")
        return

    print(f"  response_code  : 0x{resp.response_code:04X}")
    print(f"  response_params: {resp.response_params}")
    print(f"  in_data (raw, {len(resp.in_data)} bytes):")
    print(hex_dump(resp.in_data))

    if not resp.in_data:
        return

    if wire_format == "variable":
        try:
            unwrapped = unwrap_sigma_payload(resp.in_data)
            print(f"  variable-form unwrap → {len(unwrapped)} bytes payload:")
            print(hex_dump(unwrapped))
        except ValueError as e:
            print(f"  variable-form unwrap failed: {e}")

    elif wire_format == "fixed":
        try:
            unwrapped = unwrap_sigma_fixed_struct(resp.in_data, fixed_size)
            print(f"  fixed-form unwrap → {len(unwrapped)} bytes struct:")
            print(hex_dump(unwrapped))

            if opcode == SigmaOperationCode.GET_CAM_CAPT_STATUS:
                try:
                    status = SgmCaptStatus.from_wire(resp.in_data)
                    print(f"  parsed SgmCaptStatus: {status}")
                    print(f"  has_new_image: {status.has_new_image}, "
                          f"is_capturing: {status.is_capturing}")
                except Exception as e:  # noqa: BLE001
                    print(f"  SgmCaptStatus parse failed: {e}")
        except ValueError as e:
            print(f"  fixed-form unwrap failed: {e}")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    print("=" * 70)
    print("Phase 0-B (USB) — PTP session + GetCamCaptStatus via libusb")
    print("=" * 70)
    print()

    try:
        with USBBridge.find_sigma_fp_l() as bridge:
            print(f"Opened bridge to {bridge._dev}")
            print()
            print("Opening PTP session...")
            bridge.open_session(session_id=1)
            print("✓ Session opened.")

            # Standard PTP probe (should NOT need sigma unwrap)
            print()
            print("--- GetDeviceInfo (standard PTP, 0x1001) ---")
            try:
                resp = bridge.send_command_raw(0x1001)
                print(f"  response_code: 0x{resp.response_code:04X}")
                print(f"  in_data ({len(resp.in_data)} bytes, expect 100s of bytes "
                      f"for a real DeviceInfo struct):")
                print(hex_dump(resp.in_data, max_len=256))
            except Exception as e:  # noqa: BLE001
                print(f"  EXCEPTION: {e!r}")

            # Sigma-specific probes
            # ConfigApi & GetCamOpPermission use variable-length format
            #   (4-byte length prefix + payload + 1-byte checksum)
            probe(bridge, SigmaOperationCode.CONFIG_API,
                  "ConfigApi (Sigma)", wire_format="variable")
            probe(bridge, SigmaOperationCode.GET_CAM_OP_PERMISSION,
                  "GetCamOpPermission (Sigma)", wire_format="variable")

            # GetCamCaptStatus uses FIXED format: 7-byte struct + 1-byte checksum
            probe(bridge, SigmaOperationCode.GET_CAM_CAPT_STATUS,
                  "GetCamCaptStatus (Sigma) — *** THE BIG ONE ***",
                  wire_format="fixed", fixed_size=7)

            # GetCamDataGroup1 has its own internal format (FieldPresent mask
            # + conditional fields). We just dump the raw bytes for now.
            probe(bridge, SigmaOperationCode.GET_CAM_DATA_GROUP_1,
                  "GetCamDataGroup1 (Sigma)", wire_format="raw")

            print()
            print("Closing session...")
            bridge.close_session()
            print("✓ Session closed.")

            print()
            print("=" * 70)
            print("✓ Phase 0-B (USB) PASSED — see above for actual data phase content")
            print("=" * 70)
            return 0

    except USBBridgeError as e:
        print(f"\n✗ USBBridge error: {e}")
        if "kernel" in str(e).lower():
            print("\n  → Run with sudo:")
            print("    sudo \"/path/to/fp-l-tether/"
                  "venv/bin/python\" scripts/phase0_usb_session.py")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\n✗ Unexpected error: {e!r}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

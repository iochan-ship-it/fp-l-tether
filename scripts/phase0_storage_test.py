#!/usr/bin/env python3
"""Phase 0-C alternative — use standard PTP storage ops to access SD card.

Since the Sigma-specific PC ImageDB workflow (SnapCommand → GetCamCaptStatus
ImageID increment → GetPictFileInfo2 → GetBigPartialPictFile) is stuck on
DestinationToSave clamping, try the STANDARD PTP file enumeration:

  GetStorageIDs (0x1004)    → list of storage IDs on camera
  GetStorageInfo (0x1005)   → details about a storage (capacity, free, etc.)
  GetNumObjects (0x1006)    → count of files
  GetObjectHandles (0x1007) → list of file handles
  GetObjectInfo (0x1008)    → metadata for one file
  GetObject (0x1009)        → download a file by handle

If this works, the workflow becomes:
  1. SnapCommand triggers shutter (camera saves to SD)
  2. Wait briefly
  3. GetObjectHandles to find the new file
  4. GetObject to download
  5. ClearImageDBSingle (skip — not needed for SD card files)

Run with sudo::

    sudo "/path/to/fp-l-tether/venv/bin/python" \\
         scripts/phase0_storage_test.py
"""

from __future__ import annotations

import logging
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fp_l_tether.camera.ptp_codes import (  # noqa: E402
    PTPOperationCode,
    PTPResponseCode,
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


def parse_uint32_array(data: bytes) -> list[int]:
    """Parse a PTP uint32 array: <uint32 count><uint32 item>... LE."""
    if len(data) < 4:
        return []
    count = struct.unpack_from("<I", data, 0)[0]
    out = []
    for i in range(count):
        offset = 4 + i * 4
        if offset + 4 > len(data):
            break
        out.append(struct.unpack_from("<I", data, offset)[0])
    return out


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    print("=" * 70)
    print("Phase 0-C storage test — standard PTP file enumeration")
    print("=" * 70)
    print()

    try:
        with USBBridge.find_sigma_fp_l() as bridge:
            bridge.open_session(session_id=1)
            print("✓ Session opened.\n")

            # 1. GetStorageIDs
            print("--- GetStorageIDs (0x1004) ---")
            r = bridge.send_command_raw(PTPOperationCode.GET_STORAGE_IDS)
            print(f"  response: 0x{r.response_code:04X}, in_data={len(r.in_data)} bytes")
            print(hex_dump(r.in_data, 64))
            if r.response_code != PTPResponseCode.OK:
                print(f"  ✗ Could not get storage IDs")
                return 1
            storage_ids = parse_uint32_array(r.in_data)
            print(f"  Storage IDs: {[hex(s) for s in storage_ids]}")
            if not storage_ids:
                print("  ✗ No storage IDs returned")
                return 1

            # 2. GetStorageInfo for each
            for storage_id in storage_ids:
                print(f"\n--- GetStorageInfo (0x1005) for storage 0x{storage_id:08X} ---")
                r = bridge.send_command_raw(
                    PTPOperationCode.GET_STORAGE_INFO,
                    params=(storage_id,),
                )
                print(f"  response: 0x{r.response_code:04X}, in_data={len(r.in_data)} bytes")
                print(hex_dump(r.in_data, 128))
                if r.response_code == PTPResponseCode.OK and len(r.in_data) >= 26:
                    # Parse StorageInfo: storageType(2) filesystemType(2) accessCapability(2)
                    # maxCapacity(8) freeSpaceInBytes(8) freeSpaceInImages(4)
                    fs_type = struct.unpack_from("<H", r.in_data, 2)[0]
                    max_cap = struct.unpack_from("<Q", r.in_data, 6)[0]
                    free_b = struct.unpack_from("<Q", r.in_data, 14)[0]
                    free_imgs = struct.unpack_from("<I", r.in_data, 22)[0]
                    print(f"    FilesystemType: 0x{fs_type:04X}")
                    print(f"    MaxCapacity:    {max_cap:,} bytes")
                    print(f"    FreeSpace:      {free_b:,} bytes ({free_imgs} images)")

            # 3. GetNumObjects (on first storage)
            sid = storage_ids[0]
            print(f"\n--- GetNumObjects (0x1006) for storage 0x{sid:08X} ---")
            r = bridge.send_command_raw(
                PTPOperationCode.GET_NUM_OBJECTS,
                params=(sid,),
                data_phase_in=False,
            )
            print(f"  response: 0x{r.response_code:04X}, params={r.response_params}")
            if r.response_code == PTPResponseCode.OK and r.response_params:
                print(f"  Number of objects on storage: {r.response_params[0]}")

            # 4. GetObjectHandles (on first storage)
            print(f"\n--- GetObjectHandles (0x1007) for storage 0x{sid:08X} ---")
            # Per PTP spec: param1=storageID, param2=objectFormatCode(0=all),
            # param3=associationHandle(0xFFFFFFFF=all top-level)
            r = bridge.send_command_raw(
                PTPOperationCode.GET_OBJECT_HANDLES,
                params=(sid, 0, 0xFFFFFFFF),
            )
            print(f"  response: 0x{r.response_code:04X}, in_data={len(r.in_data)} bytes")
            print(hex_dump(r.in_data, 256))
            if r.response_code != PTPResponseCode.OK:
                print(f"  ✗ Could not list object handles")
                return 1
            handles = parse_uint32_array(r.in_data)
            print(f"  → {len(handles)} object handles found")
            if handles:
                print(f"  First few: {[hex(h) for h in handles[:10]]}")
                if len(handles) > 10:
                    print(f"  Last few:  {[hex(h) for h in handles[-5:]]}")

            # 5. GetObjectInfo for the LATEST object (most recent capture)
            if not handles:
                print("\n  No objects on camera. Cannot test download.")
                return 1
            latest = handles[-1]
            print(f"\n--- GetObjectInfo (0x1008) for handle 0x{latest:08X} ---")
            r = bridge.send_command_raw(
                PTPOperationCode.GET_OBJECT_INFO,
                params=(latest,),
            )
            print(f"  response: 0x{r.response_code:04X}, in_data={len(r.in_data)} bytes")
            print(hex_dump(r.in_data, 256))
            if r.response_code != PTPResponseCode.OK:
                print(f"  ✗ Could not get object info")
                return 1

            # Parse ObjectInfo struct (PTP standard format):
            # StorageID(4) ObjectFormat(2) ProtectionStatus(2) ObjectCompressedSize(4)
            # ThumbFormat(2) ThumbCompressedSize(4) ThumbPixWidth(4) ThumbPixHeight(4)
            # ImagePixWidth(4) ImagePixHeight(4) ImageBitDepth(4) ParentObject(4)
            # AssociationType(2) AssociationDesc(4) SequenceNumber(4)
            # Filename(string) CaptureDate(string) ModificationDate(string) Keywords(string)
            try:
                d = r.in_data
                obj_format = struct.unpack_from("<H", d, 4)[0]
                obj_size = struct.unpack_from("<I", d, 8)[0]
                pix_w = struct.unpack_from("<I", d, 24)[0]
                pix_h = struct.unpack_from("<I", d, 28)[0]
                # Filename starts at offset 52 with a length-prefixed UCS-2 string
                fn_len = d[52]  # number of UCS-2 chars including null terminator
                fn_bytes = d[53 : 53 + fn_len * 2]
                filename = fn_bytes.decode("utf-16-le", errors="replace").rstrip("\x00")
                print(f"  Filename:      {filename}")
                print(f"  ObjectFormat:  0x{obj_format:04X}")
                print(f"  Size:          {obj_size:,} bytes ({obj_size/1024/1024:.1f} MB)")
                print(f"  Dimensions:    {pix_w}x{pix_h}")
            except Exception as e:  # noqa: BLE001
                print(f"  Parse error: {e}")

            print()
            print("=" * 70)
            print("✓ Standard PTP file enumeration works!")
            print("  This means we can download SD card contents via:")
            print("  1. SnapCommand (camera saves to SD)")
            print("  2. GetObjectHandles to find new file")
            print("  3. GetObject to download DNG")
            print("=" * 70)
            print("\nNext step: try GetObject to actually download the file.")
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

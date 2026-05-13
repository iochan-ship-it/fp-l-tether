#!/usr/bin/env python3
"""Phase 0-C — Full snap → download → clear cycle.

The most important test. This sends SnapCommand from the Mac, polls
GetCamCaptStatus until the image is ready, fetches the file info, downloads
the DNG via GetBigPartialPictFile in chunks, saves it to /tmp, and finally
clears the camera's ImageDB.

Then it repeats 5 times to verify no clone-return bug (libgphoto2 #882).

Run::

    python scripts/phase0_snap_test.py

Run AFTER phase0_session_test.py has passed.

Success criteria:
  * 5 distinct DNG files saved to /tmp/sigma_phase0_*.dng
  * Each file is roughly the expected size (~100 MB for fp L)
  * `file` command identifies each as TIFF/DNG
  * File hashes differ (no clone bug)
"""

from __future__ import annotations

import hashlib
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
    SNAP_SINGLE_STILL_PAYLOAD,
    SgmCaptStatus,
    SigmaOperationCode,
)

OUT_DIR = Path("/tmp/fp_l_phase0_dng")
CHUNK_SIZE = 1024 * 1024  # 1 MiB per GetBigPartialPictFile call
POLL_INTERVAL_MS = 200  # poll GetCamCaptStatus every 200ms
POLL_TIMEOUT_S = 20.0  # give up after 20s
# Start with 1 shot for debugging. Bump to 5 once a single shot reliably works.
NUM_SHOTS = 1


def hex_dump(data: bytes, max_len: int = 64) -> str:
    truncated = data[:max_len]
    suffix = f" ... (+{len(data) - max_len} bytes)" if len(data) > max_len else ""
    return " ".join(f"{b:02X}" for b in truncated) + suffix


def wait_for_image(session: "Camera", deadline_s: float) -> SgmCaptStatus:
    """Poll GetCamCaptStatus until has_new_image is True or timeout."""
    start = time.monotonic()
    last_status: SgmCaptStatus | None = None
    iteration = 0
    while time.monotonic() - start < deadline_s:
        iteration += 1
        resp = session.send_ptp(SigmaOperationCode.GET_CAM_CAPT_STATUS)
        if not resp.is_ok:
            raise RuntimeError(f"GetCamCaptStatus failed: 0x{resp.response_code:04X}")

        # Try to parse — adjust offset based on phase0_session_test findings
        for skip in (0, 1, 4, 5, 8):
            if len(resp.in_data) < skip + 7:
                continue
            try:
                status = SgmCaptStatus.from_bytes(resp.in_data[skip:])
                last_status = status
                if status.has_new_image:
                    print(f"  [poll {iteration}] image ready: {status}")
                    return status
                if iteration <= 3 or iteration % 5 == 0:
                    print(f"  [poll {iteration}] {status}")
                break
            except Exception:
                continue
        time.sleep(POLL_INTERVAL_MS / 1000)

    raise TimeoutError(
        f"No image ready after {deadline_s}s. Last status: {last_status}"
    )


def download_dng(session: "Camera", image_id: int, expected_size: int) -> bytes:
    """Download a single image via GetBigPartialPictFile in chunks.

    Args:
        session: Open Camera session
        image_id: ImageID from SgmCaptStatus
        expected_size: total file size in bytes from GetPictFileInfo2

    Returns:
        The complete DNG file bytes.
    """
    buffer = bytearray()
    offset = 0
    chunk_idx = 0
    while offset < expected_size:
        remaining = expected_size - offset
        this_chunk = min(CHUNK_SIZE, remaining)

        # GetBigPartialPictFile parameters (from SDK signature):
        #   param1 = data pointer (from GetPictFileInfo2)
        #   param2 = start offset (uint64, low 32 bits as param2, high 32 as param3?)
        #   param3 = max length to receive
        # NOTE: exact parameter layout needs to be confirmed during testing.
        # The SDK method signature is:
        #   start:(UInt64)inStartAddress length:(UInt32)inMaxLength imageID:(UInt8)
        resp = session.send_ptp(
            SigmaOperationCode.GET_BIG_PARTIAL_PICT_FILE,
            parameters=(image_id, offset & 0xFFFFFFFF, this_chunk),
        )
        if not resp.is_ok:
            raise RuntimeError(
                f"GetBigPartialPictFile chunk {chunk_idx} failed: "
                f"0x{resp.response_code:04X}"
            )
        # The in_data may include a small header + the actual file bytes.
        # During testing, inspect the first chunk's first bytes to determine
        # the header offset. For now we assume the entire in_data is file data.
        buffer += resp.in_data
        offset += len(resp.in_data)
        chunk_idx += 1
        if chunk_idx <= 2 or chunk_idx % 10 == 0:
            print(f"    chunk {chunk_idx}: {len(resp.in_data)} bytes, "
                  f"total {offset}/{expected_size} ({100 * offset / expected_size:.1f}%)")
        if len(resp.in_data) == 0:
            print(f"  ⚠ Chunk {chunk_idx} returned 0 bytes — stopping.")
            break

    return bytes(buffer)


def shoot_once(session: "Camera", shot_num: int) -> Path:
    """Fire SnapCommand, wait for image, download, save, clear ImageDB."""
    print(f"\n--- Shot {shot_num} ---")

    # 1. SnapCommand
    print(f"  → SnapCommand (payload: {hex_dump(SNAP_SINGLE_STILL_PAYLOAD)})")
    resp = session.send_ptp(
        SigmaOperationCode.SNAP_COMMAND,
        out_data=SNAP_SINGLE_STILL_PAYLOAD,
    )
    if not resp.is_ok:
        raise RuntimeError(f"SnapCommand failed: 0x{resp.response_code:04X}")
    print(f"  ✓ SnapCommand acknowledged")

    # 2. Poll for image ready
    print(f"  → Polling GetCamCaptStatus (every {POLL_INTERVAL_MS}ms)...")
    status = wait_for_image(session, deadline_s=POLL_TIMEOUT_S)

    # 3. GetPictFileInfo2 to get filename + size
    print(f"  → GetPictFileInfo2 (image_id=0x{status.image_id:02X})...")
    resp = session.send_ptp(
        SigmaOperationCode.GET_PICT_FILE_INFO_2,
        parameters=(status.image_id,),
    )
    if not resp.is_ok:
        raise RuntimeError(f"GetPictFileInfo2 failed: 0x{resp.response_code:04X}")
    print(f"    in_data ({len(resp.in_data)} bytes): {hex_dump(resp.in_data, 128)}")

    # TODO: Parse SgmPictureFileInfoData. For now, save raw and stop here
    # if we can't determine file_size.
    # Heuristic: scan for a recognizable filename and a uint32 size after it.
    expected_size = _heuristic_extract_filesize(resp.in_data)
    if expected_size is None or expected_size == 0:
        # Save raw info to a file for offline analysis
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        raw_path = OUT_DIR / f"shot_{shot_num:02d}_pictfileinfo.bin"
        raw_path.write_bytes(resp.in_data)
        print(f"    ⚠ Could not auto-extract file size. Raw saved to {raw_path}.")
        print(f"    Inspect this dump and update SgmPictureFileInfoData.from_bytes.")
        raise RuntimeError("File size unknown — cannot download yet")

    print(f"    expected file size: {expected_size:,} bytes (~{expected_size / 1024 / 1024:.1f} MB)")

    # 4. Download via GetBigPartialPictFile
    print(f"  → Downloading DNG via GetBigPartialPictFile (chunk={CHUNK_SIZE})...")
    t0 = time.monotonic()
    dng_bytes = download_dng(session, status.image_id, expected_size)
    elapsed = time.monotonic() - t0
    speed_mb_s = (len(dng_bytes) / 1024 / 1024) / max(elapsed, 0.001)
    print(f"  ✓ Downloaded {len(dng_bytes):,} bytes in {elapsed:.1f}s "
          f"({speed_mb_s:.1f} MB/s)")

    # 5. Save
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"sigma_phase0_shot_{shot_num:02d}.dng"
    out_path.write_bytes(dng_bytes)
    md5 = hashlib.md5(dng_bytes).hexdigest()[:12]
    print(f"  ✓ Saved to {out_path} (md5: {md5})")

    # 6. ClearImageDBSingle (critical for avoiding the libgphoto2 clone bug)
    print(f"  → ClearImageDBSingle (image_id=0x{status.image_id:02X})...")
    resp = session.send_ptp(
        SigmaOperationCode.CLEAR_IMAGE_DB_SINGLE,
        parameters=(status.image_id,),
    )
    if resp.is_ok:
        print(f"  ✓ ImageDB cleared")
    else:
        print(f"  ⚠ ClearImageDBSingle returned 0x{resp.response_code:04X}")

    return out_path


def _heuristic_extract_filesize(in_data: bytes) -> int | None:
    """Try to find a sensible 32-bit file size in the GetPictFileInfo2 reply.

    Returns the largest plausible uint32 LE in the data that is between
    1 MB and 500 MB (sane bounds for fp L DNG).
    """
    candidates: list[int] = []
    for offset in range(0, max(0, len(in_data) - 4)):
        v = int.from_bytes(in_data[offset : offset + 4], "little")
        if 1_000_000 < v < 500_000_000:
            candidates.append(v)
    if not candidates:
        return None
    # Pick the most common candidate (DNG file size typically appears twice
    # in the struct as FileSize1 — and possibly again as DataPtr1)
    from collections import Counter

    common = Counter(candidates).most_common(1)
    return common[0][0]


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    print("=" * 70)
    print("Phase 0-C — Full shutter → download → clear cycle")
    print("=" * 70)
    print(f"Will fire {NUM_SHOTS} shots and save to {OUT_DIR}/")
    print()
    print("⚠ POINT THE CAMERA AT SOMETHING DIFFERENT EACH SHOT")
    print("  so we can verify no clone-return bug (libgphoto2 #882).")
    print()
    input("Press Enter to begin... ")

    try:
        fp_l = find_first_sigma_fp_l(timeout_seconds=5.0)
    except MacOSOnlyError as e:
        print(f"ERROR: {e}")
        return 2

    if fp_l is None:
        print("✗ Sigma fp L not connected. Run phase0_smoke_test.py first.")
        return 1
    print(f"Target camera: {fp_l.name} (SN: {fp_l.serial_number})")

    saved_paths: list[Path] = []
    failed: list[int] = []
    md5s: dict[int, str] = {}

    try:
        with Camera(fp_l, ptp_timeout_seconds=15.0) as session:
            print("✓ Session opened.")

            # Warmup: ConfigApi + GetCamCaptStatus
            session.send_ptp(SigmaOperationCode.CONFIG_API)
            session.send_ptp(SigmaOperationCode.GET_CAM_CAPT_STATUS)

            for i in range(1, NUM_SHOTS + 1):
                input(f"\n[Shot {i}/{NUM_SHOTS}] Aim the camera, press Enter to shoot...")
                try:
                    path = shoot_once(session, i)
                    saved_paths.append(path)
                    md5s[i] = hashlib.md5(path.read_bytes()).hexdigest()[:12]
                except Exception as e:  # noqa: BLE001
                    print(f"  ✗ Shot {i} FAILED: {e!r}")
                    import traceback

                    traceback.print_exc()
                    failed.append(i)

    except Exception as e:  # noqa: BLE001
        print(f"\n✗ Session error: {e!r}")
        import traceback

        traceback.print_exc()
        return 1

    # Summary
    print()
    print("=" * 70)
    print("Summary")
    print("=" * 70)
    print(f"Saved : {len(saved_paths)} / {NUM_SHOTS}")
    print(f"Failed: {len(failed)} (shots: {failed})")
    print()
    if md5s:
        print("MD5 hashes (first 12 hex chars) — must all be distinct:")
        for shot, h in md5s.items():
            dup = sum(1 for v in md5s.values() if v == h)
            mark = "⚠ DUP!" if dup > 1 else ""
            print(f"  Shot {shot}: {h} {mark}")

    if len(saved_paths) == NUM_SHOTS and len(set(md5s.values())) == NUM_SHOTS:
        print()
        print("✓ Phase 0-C PASSED — all shots succeeded, no clone bug.")
        print("  → Ready to start Phase 1 MVP implementation.")
        return 0
    elif saved_paths and len(set(md5s.values())) < len(saved_paths):
        print()
        print("✗ Phase 0-C FAILED — clone-return bug detected (matches libgphoto2 #882).")
        print("  → Investigate ClearImageDBSingle timing or use different SnapCommand mode.")
        return 1
    else:
        print()
        print("✗ Phase 0-C FAILED — see errors above.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

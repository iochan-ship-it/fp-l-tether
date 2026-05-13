#!/usr/bin/env python3
"""Phase 0-C v2 — full Sigma fp/fp L capture + download cycle.

This is the canonical test that proves the full PTP sequence works end-to-end:

    init (10 calls) → snap → poll → file_info → download → clear

Implementation mirrors libgphoto2's ``camera_sigma_fp_capture`` exactly
(see research/libgphoto2/camlibs/ptp2/library.c lines 5675-5747).

What's different from earlier attempts:

  - Snap payload is the wire-correct 4 bytes ``[0x02, mode, amount, chk]``,
    not the 3-byte form we tried before.
  - Mode 1 (libgphoto2 default) instead of mode 2 from SDK header.
  - Init sequence (10 PTP calls) is run BEFORE snap, mirroring
    ``camera_init`` so the camera enters tether-ready state.
  - Status parser uses the libgphoto2 wire format (data[0]=0x06 length byte).
  - GetBigPartialPictFile result has its 4-byte length prefix stripped.

Run with the camera connected and in Camera Control mode, no sudo unless
the libusb backend complains about the kernel driver::

    venv/bin/python scripts/phase0_capture_v2.py

To take N shots in a row (verify Issue #882 "3rd shot clone" behavior)::

    venv/bin/python scripts/phase0_capture_v2.py --shots 5
"""

from __future__ import annotations

import argparse
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


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def run_one_shot(
    bridge: USBBridge,
    output_dir: Path,
    index: int,
    mode: int = 2,
    poll_iter: int = 150,
    poll_interval: float = 0.2,
    clear_strategy: str = "image_db_head",
    pre_snap_pc_setup: bool = True,
) -> Path:
    """Take one shot and save to disk. Returns the output path."""
    print(f"\n  ━━━ Shot {index} ━━━")
    t0 = time.monotonic()
    info, data = bridge.sigma_capture_one(
        mode=mode,
        amount=1,
        max_poll_iterations=poll_iter,
        poll_interval_s=poll_interval,
        clear_strategy=clear_strategy,
        pre_snap_pc_setup=pre_snap_pc_setup,
    )
    t1 = time.monotonic()

    # Build output filename. libgphoto2's strncpy(9) leaves info.name with a
    # trailing dot ("SDIM0001."), so we strip that and re-attach the ext.
    # Add an index suffix for multi-shot tests so filenames stay unique
    # even if the camera returns the same name (Issue #882 clone case).
    base = (info.name or f"shot_{index:04d}").rstrip(".")
    ext = info.fileext or "bin"
    fname = f"{base}_{index:02d}.{ext}"
    out_path = output_dir / fname

    out_path.write_bytes(data)
    elapsed = t1 - t0
    mbps = (len(data) / 1024 / 1024) / elapsed if elapsed > 0 else 0
    print(
        f"    ✓ {len(data):,} bytes in {elapsed:.2f}s ({mbps:.1f} MB/s) "
        f"→ {out_path.name}"
    )
    print(f"      meta: {info.width}×{info.height} ext={info.fileext} "
          f"path={info.path} addr=0x{info.fileaddress:X}")
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--shots", type=int, default=1,
        help="number of consecutive shots (default: 1)",
    )
    parser.add_argument(
        "--gap", type=float, default=2.0,
        help="seconds between shots (default: 2.0)",
    )
    parser.add_argument(
        "--mode", type=int, default=2,
        help=(
            "Snap CaptureMode. Default 2 (NON_AF_CAPTURE per Sigma SDK + fp trace). "
            "libgphoto2 uses 1 which only works for the FIRST shot."
        ),
    )
    parser.add_argument(
        "--no-pc-setup", action="store_true",
        help=(
            "skip the pre-snap SetDataGroup3(PC mode) call. "
            "Useful to compare with libgphoto2-style flow (which omits this and "
            "consequently breaks after shot 1)."
        ),
    )
    parser.add_argument(
        "--reinit-between", action="store_true",
        help=(
            "run the full sigma_init() sequence before EACH shot. Expensive but "
            "may be the only way to repeatedly clear camera-side state on fp L."
        ),
    )
    parser.add_argument(
        "--reopen-session", action="store_true",
        help=(
            "close + reopen the PTP session before each shot (heavier reset "
            "than --reinit-between). Use if --reinit-between alone doesn't help."
        ),
    )
    parser.add_argument(
        "--poll-iter", type=int, default=150,
        help="max status-poll iterations (default 150 = 30s at 0.2s)",
    )
    parser.add_argument(
        "--poll-interval", type=float, default=0.2,
        help="seconds between status polls (default 0.2)",
    )
    parser.add_argument(
        "--clear", default="image_db_head",
        choices=["image_id", "image_db_head", "image_db_tail", "all", "none"],
        help=(
            "which value to pass to ClearImageDBSingle after each shot. "
            "Default 'image_db_head' (likely correct for fp L). "
            "'image_id' = libgphoto2's choice (no-op on fp L). "
            "'all' = ClearImageDBAll. 'none' = skip cleanup."
        ),
    )
    parser.add_argument(
        "--output", type=Path,
        default=PROJECT_ROOT / "captures",
        help="output directory for downloaded files (default: ./captures)",
    )
    parser.add_argument(
        "--skip-init", action="store_true",
        help="skip the 10-call init sequence (assume already initialized)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="DEBUG-level logging (full PTP trace)",
    )
    args = parser.parse_args()
    setup_logging(args.verbose)

    args.output.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print(f" Sigma fp/fp L — Phase 0-C v2 (capture × {args.shots})")
    print("=" * 70)
    print(f" Output: {args.output}")
    print(f" Init:   {'SKIP' if args.skip_init else 'RUN (10 PTP calls)'}")
    print(f" Mode:   {args.mode}, Amount: 1")
    print(f" Poll:   {args.poll_iter} × {args.poll_interval}s = "
          f"{args.poll_iter * args.poll_interval:.0f}s max")
    print(f" Clear:  {args.clear}")

    bridge = USBBridge.find_sigma_fp_l()
    with bridge:
        bridge.open_session()
        print("\n[1] PTP session opened")

        if not args.skip_init:
            print("\n[2] Running Sigma init sequence (10 PTP calls)…")
            try:
                results = bridge.sigma_init()
                print(f"    ✓ init complete — {len(results)} response blobs")
                for name, blob in results.items():
                    print(f"      {name:18s} {len(blob):>6d} bytes")
            except PTPError as e:
                print(f"    ✗ init failed at PTP layer: {e}")
                return 2
            except USBBridgeError as e:
                print(f"    ✗ init failed at USB layer: {e}")
                return 2

        print(f"\n[3] Capturing {args.shots} shot(s)…")
        saved: list[Path] = []
        for i in range(1, args.shots + 1):
            # Optional heavy resets between shots (after shot 1)
            if i > 1:
                if args.reopen_session:
                    print(f"    reopen-session before shot {i}…")
                    bridge.close_session()
                    bridge.open_session()
                if args.reinit_between or args.reopen_session:
                    print(f"    full re-init before shot {i}…")
                    bridge.sigma_init()
            try:
                path = run_one_shot(
                    bridge, args.output, i,
                    mode=args.mode,
                    poll_iter=args.poll_iter,
                    poll_interval=args.poll_interval,
                    clear_strategy=args.clear,
                    pre_snap_pc_setup=not args.no_pc_setup,
                )
                saved.append(path)
            except TimeoutError as e:
                print(f"    ✗ shot {i} timed out: {e}")
                break
            except (PTPError, USBBridgeError) as e:
                print(f"    ✗ shot {i} failed: {e}")
                break
            if i < args.shots:
                print(f"    sleeping {args.gap:.1f}s before next shot…")
                time.sleep(args.gap)

        print(f"\n[4] Saved {len(saved)}/{args.shots} files:")
        for p in saved:
            print(f"      {p.stat().st_size:>10,} B  {p.name}")

        # Quick clone-bug check (Issue #882): compare file sizes pairwise
        if len(saved) >= 2:
            sizes = [p.stat().st_size for p in saved]
            unique_sizes = set(sizes)
            if len(unique_sizes) < len(sizes):
                print(
                    "\n    ⚠ WARNING: some shots share the same exact byte size — "
                    "possible Issue #882 clone-return bug. Compare bytes manually:\n"
                    f"      sha256sum {' '.join(str(p) for p in saved)}"
                )
            else:
                print("    ✓ all shot sizes are unique — no obvious clone-return bug")

        print("\n[5] Closing session…")

    print("\n" + "=" * 70)
    if len(saved) == args.shots:
        print(f" 🎯 SUCCESS — all {args.shots} shot(s) captured & saved")
        return 0
    else:
        print(f" ⚠ PARTIAL — {len(saved)} of {args.shots} succeeded")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

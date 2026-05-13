#!/usr/bin/env python3
"""Phase 3.2b — Live-view rate ceiling test.

Streams GetCamViewFrame (0x902B) at several target fps values and
measures how many frames the camera can produce before returning
0x2019 (PTP_RC_DeviceBusy) — i.e., the sustained-rate ceiling.

Each rate runs in its own USB session because once the camera goes
busy, the bulk endpoint stays unresponsive for several seconds and
would contaminate subsequent rates.

What we want to learn:
  - At which target fps does the camera sustain indefinitely?
  - At which target fps does it busy out, and after how many frames?
  - Does the camera recover (returns to producing frames) without
    a USB reset, or does the session die for good?

Run::

    sudo killall ptpcamerad 2>/dev/null
    sudo venv/bin/python scripts/phase3_liveview_rate_test.py

Reads no config; uses the bridge directly so we can isolate behavior
from the daemon's state machine.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fp_l_tether.camera.usb_bridge import (  # noqa: E402
    PTPError,
    USBBridge,
    USBBridgeError,
)


TEST_RATES_FPS = [2, 5, 8, 10, 15]
DURATION_S = 20.0          # per rate
INTER_RATE_PAUSE_S = 3.0   # let the camera settle between rates
PTP_RC_DEVICE_BUSY = 0x2019


@dataclass
class RateResult:
    target_fps: int
    frames_ok: int
    busy_count: int
    empty_count: int
    other_error_count: int
    first_busy_at_frame: int | None
    first_busy_at_s: float | None
    last_frame_at_s: float | None
    sustained: bool   # True if no busy in window
    actual_fps: float


def run_one_rate(target_fps: int, duration_s: float) -> RateResult:
    """Open a fresh USB session, stream for ``duration_s``, return stats."""
    period_s = 1.0 / target_fps
    frames_ok = 0
    busy_count = 0
    empty_count = 0
    other_err = 0
    first_busy_frame: int | None = None
    first_busy_t: float | None = None
    last_frame_t: float | None = None

    bridge = USBBridge.find_sigma_fp_l()
    bridge.open()
    try:
        bridge.open_session()
        bridge.sigma_init()
        # Tiny settle — the very first frame after init is the most
        # reliable, but we don't want to count "first-frame-after-cold-
        # start" specially in the metric. 200 ms is below 1 frame at
        # any tested rate.
        time.sleep(0.2)

        t0 = time.monotonic()
        while time.monotonic() - t0 < duration_s:
            loop_start = time.monotonic()

            try:
                jpeg = bridge.sigma_get_view_frame()
            except PTPError as e:
                # PTPError carries the raw code in str(e). Match by
                # 0x2019 substring rather than parsing structured field
                # to stay defensive against PTPError shape changes.
                msg = str(e)
                if "0x2019" in msg:
                    busy_count += 1
                    if first_busy_frame is None:
                        first_busy_frame = frames_ok
                        first_busy_t = time.monotonic() - t0
                else:
                    other_err += 1
                # Back off so we don't hammer a busy camera.
                time.sleep(0.5)
                continue
            except USBBridgeError:
                # Bulk read timeout or short — count as severe failure
                # and bail; subsequent reads probably all fail too.
                other_err += 1
                break
            except Exception:  # noqa: BLE001
                other_err += 1
                break

            if jpeg:
                frames_ok += 1
                last_frame_t = time.monotonic() - t0
            else:
                empty_count += 1

            # Pace
            elapsed = time.monotonic() - loop_start
            sleep_for = period_s - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)

        elapsed_total = time.monotonic() - t0
        actual_fps = frames_ok / elapsed_total if elapsed_total > 0 else 0.0

        return RateResult(
            target_fps=target_fps,
            frames_ok=frames_ok,
            busy_count=busy_count,
            empty_count=empty_count,
            other_error_count=other_err,
            first_busy_at_frame=first_busy_frame,
            first_busy_at_s=first_busy_t,
            last_frame_at_s=last_frame_t,
            sustained=(busy_count == 0 and other_err == 0),
            actual_fps=actual_fps,
        )
    finally:
        try:
            bridge.close_session()
        except Exception:
            pass
        try:
            bridge.close()
        except Exception:
            pass


def main() -> int:
    results: list[RateResult] = []

    for i, fps in enumerate(TEST_RATES_FPS):
        if i > 0:
            print(f"  [pause {INTER_RATE_PAUSE_S:.1f}s — letting camera settle]")
            time.sleep(INTER_RATE_PAUSE_S)

        print()
        print(f"===== target_fps = {fps} ({DURATION_S:.0f}s) =====")
        try:
            r = run_one_rate(fps, DURATION_S)
        except (USBBridgeError, Exception) as e:  # noqa: BLE001
            print(f"  ✗ session failed before first frame: {e}")
            print("    — this likely means the camera is still recovering")
            print("    — try unplug + replug if all subsequent rates also fail")
            continue
        results.append(r)
        print(
            f"  frames_ok={r.frames_ok}  "
            f"actual_fps={r.actual_fps:.2f}  "
            f"busy={r.busy_count}  empty={r.empty_count}  "
            f"other_err={r.other_error_count}"
        )
        if r.first_busy_at_frame is not None:
            print(
                f"  ✗ first DeviceBusy after {r.first_busy_at_frame} frames "
                f"({r.first_busy_at_s:.1f}s)"
            )
        else:
            print("  ✓ no DeviceBusy in window")

    print()
    print("===== summary =====")
    print(f"  {'fps':>5}  {'actual':>8}  {'frames':>7}  {'busy':>5}  "
          f"{'1st_busy':>10}  {'sustained':>10}")
    for r in results:
        fb = f"{r.first_busy_at_frame}@{r.first_busy_at_s:.1f}s" \
            if r.first_busy_at_frame is not None else "-"
        sus = "YES" if r.sustained else "no"
        print(f"  {r.target_fps:>5}  {r.actual_fps:>8.2f}  "
              f"{r.frames_ok:>7}  {r.busy_count:>5}  "
              f"{fb:>10}  {sus:>10}")

    # Find highest sustained rate
    sustained_rates = [r.target_fps for r in results if r.sustained]
    if sustained_rates:
        print(f"\n  highest sustained: {max(sustained_rates)} fps")
    else:
        print("\n  ✗ NO rate sustained — camera may need different setup")
    return 0


if __name__ == "__main__":
    sys.exit(main())

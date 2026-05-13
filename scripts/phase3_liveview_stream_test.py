#!/usr/bin/env python3
"""Phase 3.2 — Background live-view + interleaved snaps.

Boots a USBBridge, runs ``sigma_init()``, starts a LiveViewStream
sharing a PTP RLock with the main thread, and then over ~30 seconds:

  - logs rolling fps and average frame size every ~2 s
  - fires 5 ``sigma_snap()`` calls (with the lock held) interleaved
    with the streaming, ~5 s apart

What we're verifying:
  1. The lock serialises view-frame fetches against snaps so neither
     side gets corrupted bulk data
  2. Live view keeps streaming while snaps land and drain
  3. fps stays roughly at ``target_fps`` outside of snap windows
  4. The last frame saved opens cleanly in Preview.app

Run::

    sudo killall ptpcamerad 2>/dev/null
    sudo venv/bin/python scripts/phase3_liveview_stream_test.py

The script does NOT use TetherDaemon — it talks to the bridge directly
so we can isolate "is the lock + stream correct" from "is the daemon
state machine correct".
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fp_l_tether.camera.usb_bridge import (  # noqa: E402
    PTPError,
    USBBridge,
    USBBridgeError,
)
from fp_l_tether.transfer.liveview import (  # noqa: E402
    LiveViewFrame,
    LiveViewStream,
)


DURATION_S = 30.0
SNAP_COUNT = 5
SNAP_INTERVAL_S = 5.0
LOG_INTERVAL_S = 2.0
# Match the production default (LiveViewConfig.target_fps). The rate
# ceiling test on FW V90 showed 10 fps sustains and 15 fps stalls.
TARGET_FPS = 10
OUT_PATH = Path.home() / "Desktop" / "last_frame.jpg"


def main() -> int:
    last_frame: dict[str, LiveViewFrame | None] = {"frame": None}
    frame_count = {"n": 0}

    def on_frame(frame: LiveViewFrame) -> None:
        last_frame["frame"] = frame
        frame_count["n"] += 1

    ptp_lock = threading.RLock()
    bridge = USBBridge.find_sigma_fp_l()
    bridge.open()
    print("  USB opened")

    stream: LiveViewStream | None = None
    try:
        with ptp_lock:
            bridge.open_session()
            print("  PTP session opened")
            bridge.sigma_init()
            print("  Sigma init complete (PC capture mode)")

        stream = LiveViewStream(
            bridge,
            ptp_lock,
            target_fps=TARGET_FPS,
            on_frame=on_frame,
        )
        stream.start()
        print(f"  LiveViewStream started @ {TARGET_FPS} fps target")

        # Snap schedule: SNAP_COUNT shots over the run window, spaced
        # by SNAP_INTERVAL_S. First snap fires after one interval so we
        # see fps without any snap contention first.
        t0 = time.monotonic()
        next_snap_at = t0 + SNAP_INTERVAL_S
        next_log_at = t0 + LOG_INTERVAL_S
        snaps_fired = 0
        snaps_ok = 0
        snaps_failed = 0

        while True:
            now = time.monotonic()
            if now - t0 >= DURATION_S:
                break

            # Periodic stream-health log
            if now >= next_log_at:
                fps = stream.current_fps()
                kb = stream.current_frame_kb()
                print(
                    f"  [t={now - t0:5.1f}s] "
                    f"frames={frame_count['n']:4d}  "
                    f"fps={fps:5.2f}  "
                    f"avg_size={kb:6.1f} KB  "
                    f"snaps={snaps_fired}/{SNAP_COUNT}"
                )
                next_log_at = now + LOG_INTERVAL_S

            # Snap if scheduled
            if snaps_fired < SNAP_COUNT and now >= next_snap_at:
                snaps_fired += 1
                t_snap = time.monotonic()
                # Mirror the daemon's full snap→poll→download→clear
                # transaction inside one pause window. The previous
                # iteration of this test paused only around
                # ``sigma_snap()`` itself (~7 ms) and resumed before
                # the camera had finished committing the capture —
                # which is why SNAP #1 still saw a 5 s busy storm
                # even with pause/resume wired. The commit window is
                # what triggers 0x2019; covering it with the pause is
                # the whole point of pause_during_snap.
                stream.pause()
                snap_ok = False
                try:
                    with ptp_lock:
                        bridge.sigma_set_datagroup_3_pc_capture()
                        # Re-sync the target slot from camera state
                        # (image_db_tail = next-write slot, per the
                        # 2026-05-13 hardware traces — head is the
                        # oldest-unread pointer and lags commit).
                        pre = bridge.sigma_get_capture_status(0)
                        target_slot = pre.image_db_tail
                        bridge.sigma_snap(mode=1, amount=1)
                        snap_fired_dt = time.monotonic() - t_snap

                        # Poll until image is ready or timeout (~30 s
                        # to allow a worst-case long exposure).
                        status = None
                        for _ in range(150):
                            status = bridge.sigma_get_capture_status(
                                target_slot,
                            )
                            if status.capt_status in (0x0002, 0x0005):
                                break
                            time.sleep(0.2)
                        else:
                            raise TimeoutError(
                                f"snap timed out, last status="
                                f"0x{status.capt_status:04X}"
                                if status else "snap timed out"
                            )

                        # Drain (download + clear) — same call the
                        # daemon makes. This is what makes the camera
                        # release its commit-window busy state.
                        info, data = bridge.sigma_download_current(
                            status, clear_strategy="image_db_head",
                        )
                    snap_dt = time.monotonic() - t_snap
                    snap_ok = True
                    snaps_ok += 1
                    print(
                        f"  [t={now - t0:5.1f}s] SNAP #{snaps_fired} "
                        f"fired+drained "
                        f"(snap={snap_fired_dt * 1000:.0f} ms, "
                        f"total={snap_dt * 1000:.0f} ms, "
                        f"size={len(data) / 1024:.0f} KB, "
                        f"slot=0x{target_slot:02X})"
                    )
                except (PTPError, USBBridgeError, TimeoutError) as e:
                    snaps_failed += 1
                    print(
                        f"  [t={now - t0:5.1f}s] SNAP #{snaps_fired} FAILED: {e}"
                    )
                finally:
                    # Resume after drain (or failure) — matches the
                    # production watcher.py code path where
                    # _resume_liveview() fires right after
                    # sigma_download_current() returns.
                    stream.resume()
                next_snap_at = now + SNAP_INTERVAL_S

            time.sleep(0.05)

        print()
        print("===== summary =====")
        print(f"  duration:     {DURATION_S:.1f} s")
        print(f"  frames seen:  {frame_count['n']}")
        print(f"  avg fps:      {frame_count['n'] / DURATION_S:.2f}")
        print(f"  snaps fired:  {snaps_fired}")
        print(f"  snaps ok:     {snaps_ok}")
        print(f"  snaps failed: {snaps_failed}")

        f = last_frame["frame"]
        if f is not None:
            OUT_PATH.write_bytes(f.jpeg)
            print(f"  last frame:   {OUT_PATH} ({f.width}x{f.height}, "
                  f"{len(f.jpeg) / 1024:.1f} KB)")
        else:
            print("  ✗ no frames received — stream may have failed")
            return 2

        return 0

    finally:
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
        try:
            with ptp_lock:
                bridge.close_session()
        except Exception:
            pass
        try:
            bridge.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())

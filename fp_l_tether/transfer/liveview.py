"""Background live-view streaming for the Sigma fp L.

A dedicated thread pulls one JPEG frame at a time via
``USBBridge.sigma_get_view_frame()`` (0x902B) and hands it off to a
caller-supplied callback (typically the floating panel's NSImageView).

Concurrency:
    The Sigma camera's PTP endpoint is single-threaded — concurrent
    bulk transfers (e.g. one for live view, another for snap) will
    interleave and corrupt both. Callers MUST share a
    ``threading.Lock`` between this stream and the daemon's snap /
    download / DataGroup code paths. This module acquires the lock
    around every ``sigma_get_view_frame()`` call, so the daemon's
    snap drain naturally serialises against live view as long as the
    daemon takes the same lock.

Pacing:
    ``target_fps`` is an upper bound (frame rate is capped by both the
    camera's frame production and the time it takes the lock to
    become available). We compute the post-fetch sleep as
    ``max(0, frame_period - actual_fetch_time)`` and never sleep
    backward, so the loop self-throttles on slow links.

Failure handling:
    Transient PTP / USB errors → log a warning, brief back-off, retry.
    Persistent failure → bail out and log an error; the daemon can
    restart the stream after a reconnect.
"""

from __future__ import annotations

import struct
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, TYPE_CHECKING

from fp_l_tether.camera.usb_bridge import PTPError, USBBridgeError
from fp_l_tether.telemetry.logger import get_logger

if TYPE_CHECKING:
    from fp_l_tether.camera.usb_bridge import USBBridge


@dataclass
class LiveViewFrame:
    """One decoded live-view frame.

    ``width`` / ``height`` are best-effort: extracted from the JPEG
    SOF marker. They may be 0 if the marker wasn't found (the JPEG
    bytes are still valid for display in either case).
    """

    jpeg: bytes
    width: int
    height: int
    fps_avg: float
    frame_kb: float


# Module-level default — callers may override per-stream.
DEFAULT_BACKOFF_S = 0.5
MAX_CONSECUTIVE_FAILURES = 20  # ~10 seconds at the default back-off

# Phase 3.6 (2026-05-13): when the camera enters power-save the first
# symptom on the LV path is "PTP read too short: 0 bytes" (the header
# read itself returns 0 bytes — distinct from a 0-byte data phase
# which is the symptom on the status-poll path).
#
# Hammering the endpoint after one of these is bad: empirically the
# next bulk transaction tips it from "half-stalled" into Errno 60
# territory, which on the fp L is only recoverable by a physical
# power cycle. So when we detect this pattern we self-pause the
# stream with an exponential back-off — the daemon's recovery path
# owns the wake / reconnect decision, we just stop contributing
# 0-byte reads to the wedge.
#
# Patterns we treat as "endpoint is dozing, stop hammering":
LV_IDLE_PATTERNS: tuple[str, ...] = (
    "PTP read too short",  # raised by USBBridge._read_container
    "Operation timed out",  # Errno 60 wedge symptom
    "Errno 60",
)
# Exponential back-off ladder (seconds). Each consecutive idle hit
# advances one rung; a successful frame resets to rung 0.
LV_IDLE_BACKOFF_LADDER_S: tuple[float, ...] = (5.0, 15.0, 30.0, 60.0)

# fp L rate-test (2026-05-13) findings:
#   - Sustained ceiling is 10 fps; 15 fps reliably degrades to 0x2019
#     DeviceBusy and stalls the bulk endpoint.
#   - The very first GetCamViewFrame after sigma_init() frequently
#     busies out — the camera needs ~half a second to start producing
#     frames. We wait that long once up front rather than waste a
#     back-off cycle on a known transient.
WARMUP_S = 0.5

# PTP_RC_DeviceBusy (ISO 15740 §11.4.16). Sigma fp L returns this when
# its view-frame producer can't keep up with the request rate. Treat
# specially in logs so it's distinguishable from real PTP failures.
PTP_RC_DEVICE_BUSY = 0x2019


def _jpeg_dimensions(jpeg: bytes) -> tuple[int, int]:
    """Walk JPEG segments to find SOF (start-of-frame) and extract WxH.

    Returns (0, 0) if no SOF was found or the JPEG is malformed.
    Mirrors the helper in ``scripts/phase3_liveview_one_frame.py``.
    """
    if len(jpeg) < 4 or not jpeg.startswith(b"\xFF\xD8"):
        return 0, 0
    i = 2
    end = len(jpeg) - 8
    while i < end:
        if jpeg[i] != 0xFF:
            return 0, 0
        marker = jpeg[i + 1]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", jpeg[i + 5 : i + 9])
            return w, h
        if marker == 0xD9:
            return 0, 0
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", jpeg[i + 2 : i + 4])[0]
        i += 2 + seg_len
    return 0, 0


class LiveViewStream:
    """Background thread that emits live-view JPEG frames.

    Usage::

        stream = LiveViewStream(bridge, ptp_lock, target_fps=15,
                                on_frame=panel.update_frame)
        stream.start()
        ...
        stream.stop()

    All public methods are thread-safe. ``on_frame`` is invoked from
    the streaming thread; callers that need main-thread delivery
    should marshal inside the callback (e.g.
    ``performSelectorOnMainThread:``).
    """

    def __init__(
        self,
        bridge: "USBBridge",
        ptp_lock: threading.Lock,
        *,
        target_fps: int = 15,
        on_frame: Callable[[LiveViewFrame], None] | None = None,
        backoff_s: float = DEFAULT_BACKOFF_S,
        max_consecutive_busy: int = 5,
        promote_after_consecutive_ok: int = 10,
        post_snap_busy_grace_s: float = 1.5,
        first_storm_grace_s: float = 30.0,
    ) -> None:
        if target_fps <= 0:
            raise ValueError(f"target_fps must be positive, got {target_fps}")
        self._bridge = bridge
        self._ptp_lock = ptp_lock
        self._target_fps = target_fps         # configured ceiling
        self._effective_fps = target_fps      # adaptive — may be lower
        self._on_frame = on_frame
        self._backoff_s = backoff_s
        self._max_consecutive_busy = max_consecutive_busy
        self._promote_after_consecutive_ok = promote_after_consecutive_ok
        self._post_snap_busy_grace_s = post_snap_busy_grace_s
        self._first_storm_grace_s = first_storm_grace_s

        self._stop_event = threading.Event()
        # _resume_event reflects "stream is running" — set = run, clear = paused.
        # Initially set so start() begins streaming immediately.
        self._resume_event = threading.Event()
        self._resume_event.set()
        self._thread: threading.Thread | None = None
        # Phase 3.6 (2026-05-13): self-pause state. Set in the future
        # when we detect an idle pattern (see LV_IDLE_PATTERNS); the
        # main loop will not attempt a fetch while now < this value.
        # An external resume() clears this — the daemon's recovery
        # path owns the wake signal, not us.
        self._self_paused_until: float = 0.0
        # How far up the LV_IDLE_BACKOFF_LADDER_S we currently sit.
        # Reset to 0 on a successful frame.
        self._idle_rung: int = 0
        # Snap-grace window — busies inside this window are treated as
        # commit-cycle settling, not a rate problem (so they don't
        # tick the demote counter). resume() extends this window.
        self._post_snap_window_until: float = 0.0
        # When the stream started — used by first_storm_grace logic
        # to give the very first capture cycle of a session a longer
        # tolerance for 0x2019 without demoting.
        self._stream_started_at: float = 0.0
        self.log = get_logger("liveview")

        # Rolling fps / frame-size window — last ~1 s of arrivals.
        self._recent: deque[tuple[float, int]] = deque(maxlen=64)
        self._metrics_lock = threading.Lock()

    # ----- lifecycle ---------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._stream_started_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._run, name="LiveViewStream", daemon=True
        )
        self._thread.start()
        self.log.info("liveview_started", target_fps=self._target_fps)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        # If we're paused, the loop is sitting on resume_event.wait().
        # Set it so the loop checks stop_event and returns.
        self._resume_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self.log.info("liveview_stopped")

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_paused(self) -> bool:
        return not self._resume_event.is_set()

    @property
    def effective_fps(self) -> int:
        """Current adaptive target fps (may be below configured target)."""
        return self._effective_fps

    def pause(self) -> None:
        """Suspend the streaming loop without killing the thread.

        Idempotent. Used by the daemon to free the PTP bulk endpoint
        for the duration of a snap+download transaction so the camera
        doesn't busy out (see Phase 3.2 rate-test traces).
        """
        if self._resume_event.is_set():
            self._resume_event.clear()
            self.log.debug("liveview_paused")

    def resume(self) -> None:
        """Resume the streaming loop. Idempotent.

        Extends the post-snap grace window: for ``post_snap_busy_grace_s``
        after this call, any 0x2019 busies from the camera are treated
        as expected commit-cycle settling rather than a rate problem,
        so they don't count toward effective_fps demotion. (Back-off
        and warning logs still happen — just no demote tick.)
        """
        # Always (re)arm the grace window even if we weren't paused —
        # the daemon may call resume() defensively at exit points
        # where pause() was never reached, and that's harmless.
        self._post_snap_window_until = (
            time.monotonic() + self._post_snap_busy_grace_s
        )
        # Clear any in-progress self-pause: an explicit resume() means
        # the daemon believes the endpoint is healthy again, so we
        # should retry on the next loop iteration (the daemon's
        # status poll has just succeeded by the time it reaches us).
        if self._self_paused_until > 0.0:
            self._self_paused_until = 0.0
            self._idle_rung = 0
            self.log.debug("liveview_self_pause_cleared")
        if not self._resume_event.is_set():
            self._resume_event.set()
            self.log.debug("liveview_resumed")

    # ----- metrics -----------------------------------------------------

    def current_fps(self) -> float:
        """Rolling average fps over the last ~1 s of frames."""
        now = time.monotonic()
        with self._metrics_lock:
            window = [t for t, _ in self._recent if now - t <= 1.0]
        if len(window) < 2:
            return 0.0
        # Number of intervals in the window.
        return (len(window) - 1) / max(1e-6, window[-1] - window[0])

    def current_frame_kb(self) -> float:
        """Average frame size in the last ~1 s of frames."""
        now = time.monotonic()
        with self._metrics_lock:
            sizes = [s for t, s in self._recent if now - t <= 1.0]
        if not sizes:
            return 0.0
        return sum(sizes) / len(sizes) / 1024.0

    # ----- main loop ---------------------------------------------------

    def _run(self) -> None:
        consecutive_failures = 0
        # Adaptive-rate state.
        consecutive_busy = 0
        consecutive_success = 0

        # Warm-up: skip the well-known first-call busy by waiting once.
        if self._stop_event.wait(WARMUP_S):
            return

        while not self._stop_event.is_set():
            # ---- Honor pause -------------------------------------
            # When the daemon pauses us (e.g. for the snap+download
            # transaction), block until resumed. Polled in 50 ms
            # increments so stop() still takes effect promptly.
            while not self._resume_event.is_set():
                if self._stop_event.wait(0.05):
                    return

            # ---- Honor self-pause (Phase 3.6) --------------------
            # If we previously detected an idle pattern, sit on the
            # back-off until the deadline (or resume() clears it).
            # 50 ms polling keeps stop_event / resume() responsive.
            while True:
                if self._stop_event.is_set():
                    return
                if self._self_paused_until <= 0.0:
                    break
                remaining = self._self_paused_until - time.monotonic()
                if remaining <= 0:
                    # Back-off expired; clear and try a fetch.
                    self._self_paused_until = 0.0
                    self.log.info(
                        "liveview_self_pause_expired",
                        rung=self._idle_rung,
                    )
                    break
                if self._stop_event.wait(min(remaining, 0.05)):
                    return

            loop_start = time.monotonic()
            period_s = 1.0 / max(1, self._effective_fps)

            # Acquire the lock for just the PTP fetch — release before
            # the callback so heavy panel work doesn't block snaps.
            jpeg: bytes = b""
            try:
                with self._ptp_lock:
                    jpeg = self._bridge.sigma_get_view_frame()
            except (PTPError, USBBridgeError) as e:
                msg = str(e)
                is_busy = f"0x{PTP_RC_DEVICE_BUSY:04X}" in msg
                consecutive_failures += 1
                # Phase 3.6: idle-pattern self-pause. A short read or
                # Errno 60 means the camera is going / has gone into
                # power-save and the bulk endpoint is half-stalled.
                # We don't try to recover ourselves — we just step
                # back so we stop contributing to the wedge, and let
                # the daemon's passive_wait + reconnect logic decide
                # how to proceed. resume() called by the daemon
                # clears this state when the camera is healthy again.
                is_idle_pattern = any(p in msg for p in LV_IDLE_PATTERNS)
                if is_idle_pattern:
                    rung = min(
                        self._idle_rung, len(LV_IDLE_BACKOFF_LADDER_S) - 1,
                    )
                    backoff_s = LV_IDLE_BACKOFF_LADDER_S[rung]
                    self._self_paused_until = (
                        time.monotonic() + backoff_s
                    )
                    self.log.warning(
                        "liveview_self_paused",
                        rung=self._idle_rung,
                        backoff_s=backoff_s,
                        error=msg,
                    )
                    self._idle_rung = min(
                        self._idle_rung + 1,
                        len(LV_IDLE_BACKOFF_LADDER_S) - 1,
                    )
                    # Don't tick into "giving up" on idle patterns —
                    # self-pause handles back-off and we want the
                    # stream to survive a long doze so it resumes
                    # cleanly once the daemon recovers the camera.
                    consecutive_failures = 0
                    # Skip the post-except backoff/giving-up logic
                    # below — go straight back to the loop top so
                    # the self-pause gate engages.
                    continue
                if is_busy:
                    # Decide whether this busy counts toward demotion.
                    # Two grace windows exempt commit-cycle artifacts
                    # from being treated as a sustained rate problem:
                    #   1. post-snap window (set by resume()): a few
                    #      busies are normal as the camera settles
                    #      after a capture commit.
                    #   2. first-storm window: the very first capture
                    #      cycle of a session is the worst — give it
                    #      a wider tolerance so one bad event doesn't
                    #      drag effective_fps for the whole session.
                    now_t = time.monotonic()
                    in_post_snap_grace = now_t < self._post_snap_window_until
                    in_first_storm_grace = (
                        self._stream_started_at > 0
                        and (now_t - self._stream_started_at)
                            < self._first_storm_grace_s
                    )
                    in_grace = in_post_snap_grace or in_first_storm_grace
                    consecutive_success = 0
                    if not in_grace:
                        consecutive_busy += 1
                        # Adaptive: after sustained busy, halve effective
                        # fps so we stop hammering. Floors at 1 fps so
                        # the stream never goes fully silent.
                        if (consecutive_busy >= self._max_consecutive_busy
                                and self._effective_fps > 1):
                            new_fps = max(1, self._effective_fps // 2)
                            self.log.warning(
                                "liveview_rate_demoted",
                                from_fps=self._effective_fps,
                                to_fps=new_fps,
                                consecutive_busy=consecutive_busy,
                            )
                            self._effective_fps = new_fps
                            consecutive_busy = 0  # reset for next demotion
                self.log.warning(
                    "liveview_busy" if is_busy else "liveview_failed",
                    error=msg,
                    consecutive=consecutive_failures,
                    grace=("post_snap" if in_post_snap_grace
                           else "first_storm" if in_first_storm_grace
                           else None) if is_busy else None,
                )
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    self.log.error(
                        "liveview_giving_up",
                        consecutive=consecutive_failures,
                    )
                    return
                # Brief back-off, then retry — but honour stop_event.
                if self._stop_event.wait(self._backoff_s):
                    return
                continue
            except Exception as e:  # noqa: BLE001
                # Unexpected exception — log loudly but keep streaming.
                self.log.error("liveview_unexpected", error=str(e))
                if self._stop_event.wait(self._backoff_s):
                    return
                continue

            # Empty frame (no SOI/EOI in payload). Could be transient
            # — back off briefly and retry without counting as a hard
            # failure.
            if not jpeg:
                if self._stop_event.wait(0.1):
                    return
                continue

            consecutive_failures = 0
            consecutive_busy = 0
            consecutive_success += 1
            # A clean frame means the endpoint is healthy — reset the
            # self-pause back-off ladder so the next idle dip starts
            # fresh at rung 0 instead of escalating to a long sleep.
            self._idle_rung = 0
            # Adaptive: after sustained success, claw back 1 fps at a
            # time toward the configured target. The threshold is
            # configurable via promote_after_consecutive_ok — default
            # 10 frames, which at 5 fps is ~2 s clean, fast enough to
            # recover from a transient hiccup within a few snaps.
            if (consecutive_success >= self._promote_after_consecutive_ok
                    and self._effective_fps < self._target_fps):
                old = self._effective_fps
                self._effective_fps = min(
                    self._target_fps, self._effective_fps + 1
                )
                self.log.info(
                    "liveview_rate_promoted",
                    from_fps=old, to_fps=self._effective_fps,
                )
                consecutive_success = 0

            # Update rolling metrics.
            now = time.monotonic()
            with self._metrics_lock:
                self._recent.append((now, len(jpeg)))

            # Dispatch to the consumer.
            if self._on_frame is not None:
                w, h = _jpeg_dimensions(jpeg)
                frame = LiveViewFrame(
                    jpeg=jpeg,
                    width=w,
                    height=h,
                    fps_avg=self.current_fps(),
                    frame_kb=len(jpeg) / 1024.0,
                )
                try:
                    self._on_frame(frame)
                except Exception as e:  # noqa: BLE001
                    self.log.warning("liveview_callback_raised", error=str(e))

            # Pace: only sleep for the remaining slice of this frame's
            # period. If fetch took longer than period, don't sleep —
            # the loop is already self-throttled by the camera.
            elapsed = time.monotonic() - loop_start
            sleep_for = period_s - elapsed
            if sleep_for > 0:
                if self._stop_event.wait(sleep_for):
                    return

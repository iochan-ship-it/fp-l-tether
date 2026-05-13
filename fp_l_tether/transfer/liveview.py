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
    ) -> None:
        if target_fps <= 0:
            raise ValueError(f"target_fps must be positive, got {target_fps}")
        self._bridge = bridge
        self._ptp_lock = ptp_lock
        self._target_fps = target_fps
        self._on_frame = on_frame
        self._backoff_s = backoff_s

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.log = get_logger("liveview")

        # Rolling fps / frame-size window — last ~1 s of arrivals.
        self._recent: deque[tuple[float, int]] = deque(maxlen=64)
        self._metrics_lock = threading.Lock()

    # ----- lifecycle ---------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="LiveViewStream", daemon=True
        )
        self._thread.start()
        self.log.info("liveview_started", target_fps=self._target_fps)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self.log.info("liveview_stopped")

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

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
        period_s = 1.0 / self._target_fps
        consecutive_failures = 0

        # Warm-up: skip the well-known first-call busy by waiting once.
        if self._stop_event.wait(WARMUP_S):
            return

        while not self._stop_event.is_set():
            loop_start = time.monotonic()

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
                # Busy is the camera's "I can't keep up" signal — not
                # a real failure. Log it under a separate event so it
                # doesn't get conflated with USB / PTP errors.
                self.log.warning(
                    "liveview_busy" if is_busy else "liveview_failed",
                    error=msg,
                    consecutive=consecutive_failures,
                )
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    self.log.error(
                        "liveview_giving_up",
                        consecutive=consecutive_failures,
                    )
                    return
                # Brief back-off, then retry — but honour stop_event.
                # 500 ms recovers from DeviceBusy in our rate tests.
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

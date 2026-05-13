"""Periodic USB keep-alive ping for the Sigma fp L.

The fp L slips into an internal power-saving state after roughly five
minutes of bus idle, even while it is still in PC capture mode. Once
asleep, vendor get-* opcodes return a 0-byte data phase ("response OK,
no data") and the daemon's status poll explodes when it tries to parse
the empty buffer.

A trivial periodic ping (``sigma_get_camera_info``) keeps the camera
awake and avoids the wake-recovery dance entirely. This is what
commercial tethering software (Capture One et al.) does under the
hood.

Failure handling here is deliberately quiet — a heartbeat failure is
not fatal, since the main poll loop in :mod:`watcher` has its own
:class:`CameraIdleError` recovery. We just log a warning and move on;
if the camera is genuinely gone the watcher will see it next poll and
trigger a reconnect.

Usage::

    hb = HeartbeatThread(bridge, ptp_lock, interval_s=60.0)
    hb.start()
    ...
    hb.stop()
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Callable

from fp_l_tether.camera.usb_bridge import (
    CameraIdleError,
    PTPError,
    USBBridgeError,
)
from fp_l_tether.telemetry.logger import get_logger

if TYPE_CHECKING:
    from fp_l_tether.camera.usb_bridge import USBBridge


class HeartbeatThread:
    """Background thread that pings the camera every ``interval_s``.

    Shares the daemon's PTP lock so the ping serialises against
    snaps, downloads, and live-view fetches on the single USB bulk
    endpoint.
    """

    def __init__(
        self,
        bridge: "USBBridge",
        ptp_lock: threading.RLock,
        interval_s: float = 60.0,
        bus_quiet_check: Callable[[], float] | None = None,
    ) -> None:
        self._bridge = bridge
        self._ptp_lock = ptp_lock
        self._interval_s = interval_s
        # Optional callable returning seconds remaining in a
        # post-capture quiet window. If positive, we skip the
        # scheduled ping — the camera is mid-commit and any
        # extra PTP traffic risks pushing the bulk endpoint
        # into Errno 60.
        self._bus_quiet_check = bus_quiet_check
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.log = get_logger("heartbeat")

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="CameraHeartbeat", daemon=True
        )
        self._thread.start()
        self.log.info(
            "heartbeat_started", interval_s=self._interval_s,
        )

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_s):
            # Honour the daemon's burst-aware quiet window: if a
            # post-capture commit tail is in progress, skip this
            # ping (the heartbeat is for the 5-min idle case, not
            # the 5-second commit case, so a single skip is fine).
            if self._bus_quiet_check is not None:
                remaining = self._bus_quiet_check()
                if remaining > 0.0:
                    self.log.debug(
                        "heartbeat_skip_quiet_window",
                        remaining_s=round(remaining, 2),
                    )
                    continue
            try:
                with self._ptp_lock:
                    self._bridge.sigma_get_camera_info()
                self.log.debug("heartbeat_ok")
            except CameraIdleError as e:
                # Camera already dozing — main loop's recovery will
                # handle it on the next status poll. Logged so we can
                # confirm the heartbeat is the right cadence (if we
                # see this regularly, drop interval_s).
                self.log.warning("heartbeat_camera_idle", error=str(e))
            except (PTPError, USBBridgeError) as e:
                # Quiet warning — the watcher owns recovery / reconnect.
                self.log.warning("heartbeat_failed", error=str(e))
        self.log.info("heartbeat_stopped")

"""Periodic USB keep-alive ping for the Sigma fp L.

The fp L slips into an internal power-saving state after roughly 1-2
minutes of "passive" USB activity, even while it is still in PC
capture mode. Once asleep, vendor get-* opcodes return a 0-byte
data phase ("response OK, no data") and the daemon's status poll
explodes when it tries to parse the empty buffer.

**Phase 3.6 (2026-05-13) findings — none of these keep the fp L awake**:

1. Continuous ``sigma_get_capture_status`` polling (~10 Hz) — dozed.
2. Continuous ``sigma_get_view_frame`` streaming (10 fps) — dozed.
3. ``sigma_get_camera_info`` heartbeat at 30 s / 60 s — dozed at
   75-107 s after last shot.
4. ``sigma_set_datagroup_3_pc_capture`` heartbeat at 30 s —
   accelerated the failure: 21 s after last shot the camera went
   into 0x2019 DeviceBusy and immediately to Errno 60. The
   hypothesis is that SetDataGroup3 outside a snap context tells
   the camera "a snap is coming"; when none arrives it falls into
   a bad state.

Plan O (current): try ``sigma_get_cam_op_permission`` (0x9039) at
30 s. The opcode name "Operation Permission" suggests a control-
plane probe rather than a data query, which could plausibly count
as "alive" in the camera's doze accounting.

If Plan O also fails, the next fall-back is fast-fail UX: surface
"Camera asleep — toggle power switch" as soon as the first 0-byte
read arrives, and skip the 95 s recovery retry that empirically
never succeeds.

Failure handling here is deliberately quiet — a heartbeat failure is
not fatal, since the main poll loop in :mod:`watcher` has its own
:class:`CameraIdleError` recovery. We just log a warning and move on;
if the camera is genuinely gone the watcher will see it next poll and
trigger a reconnect.

Usage::

    hb = HeartbeatThread(bridge, ptp_lock, interval_s=30.0)
    hb.start()
    ...
    hb.stop()
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Callable, Literal

import usb.core  # type: ignore[import-not-found]

from fp_l_tether.camera.sigma_datagroup import read_focus_point
from fp_l_tether.camera.usb_bridge import (
    CameraIdleError,
    PTPError,
    USBBridgeError,
)
from fp_l_tether.telemetry.logger import get_logger

if TYPE_CHECKING:
    from fp_l_tether.camera.usb_bridge import USBBridge


KeepaliveStrategy = Literal["info", "af_drive_only", "af_point_jiggle"]


class HeartbeatThread:
    """Background thread that pings the camera every ``interval_s``.

    Shares the daemon's PTP lock so the ping serialises against
    snaps, downloads, and live-view fetches on the single USB bulk
    endpoint.

    Strategy selection (Phase 3.7 expansion):
      - ``"info"``: passive ``sigma_get_camera_info`` query. Doesn't
        prevent doze (Phase 3.6 baseline: 75-107 s until idle) but
        is cheap and produces zero side-effects.
      - ``"af_drive_only"``: ``SnapCommand(mode=3)`` AF-only shutter.
        Best public-opcode result (Plan T: 153 s until doze) but
        triggers a brief AF motor whirr at every interval.
      - ``"af_point_jiggle"``: 0x9032 SetCamDataGroupFocus write
        twice — once shifted by ±1 px, once back to the original
        coordinate. Hypothesis: a DataGroup-write that the camera
        treats as a "user touch" might count toward the doze timer
        the same way a Snap does, without the AF motor cost.
        Untested as of 2026-05-13; observed side effect is a single-
        pixel flicker of the AF reticle on the LV viewport.
    """

    def __init__(
        self,
        bridge: "USBBridge",
        ptp_lock: threading.RLock,
        interval_s: float = 60.0,
        bus_quiet_check: Callable[[], float] | None = None,
        strategy: KeepaliveStrategy = "info",
        af_jiggle_delta_px: int = 1,
        # Legacy parameter — translates to strategy="af_drive_only" if set.
        # Kept so older callers / configs don't break.
        aggressive: bool = False,
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
        # Resolve effective strategy. New ``strategy`` parameter wins;
        # legacy ``aggressive=True`` is treated as af_drive_only.
        if strategy == "info" and aggressive:
            strategy = "af_drive_only"
        self._strategy: KeepaliveStrategy = strategy
        self._jiggle_delta = max(1, int(af_jiggle_delta_px))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Tracks the last known AF point for the jiggle strategy.
        # Refreshed every heartbeat so user clicks aren't clobbered:
        # we read fresh just before each jiggle, then return to that
        # freshly-read value at the end.
        self._last_af: tuple[int, int] | None = None
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
            "heartbeat_started",
            interval_s=self._interval_s,
            strategy=self._strategy,
        )

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    # ----- strategy implementations ------------------------------------

    def _ping_info(self) -> None:
        """Passive query — least-bad of the public read opcodes tested in Phase 3.6."""
        self._bridge.sigma_get_camera_info()
        self.log.debug("heartbeat_info_ok")

    def _ping_af_drive_only(self) -> None:
        """SnapCommand AF_DRIVE_ONLY — Plan T (153 s until doze)."""
        # Phase 3.6 Plan T-2: amount=1 is the value confirmed by experiment
        # to work cleanly. The amount=0 variant returned PTP 0x201D but the
        # camera was still responsive — confirming SnapCommand touches the
        # doze timer even when rejected.
        self._bridge.sigma_snap(mode=3, amount=1)
        self.log.debug("heartbeat_af_drive_ok")

    def _ping_af_point_jiggle(self) -> None:
        """Phase 3.7 Plan U: SetCamDataGroupFocus jiggle.

        Reads the current AF point, writes (x + delta, y), brief sleep,
        writes (x, y) back. The hypothesis is that a DataGroupFocus
        write counts toward the doze timer like a Snap does — without
        the AF motor cost.

        Falls back to the passive info ping if the AF point can't be
        read (e.g., camera in a transient state where DataGroupFocus
        isn't valid yet) so we don't leave the camera completely
        un-pinged.
        """
        pt = read_focus_point(self._bridge)
        if pt is None:
            # Couldn't read — fall back to info to avoid leaving the
            # camera totally un-pinged this cycle.
            self._ping_info()
            self.log.debug("heartbeat_jiggle_fallback_info")
            return

        x, y = pt
        # Phase 3.15 (A7): the fp L's AF grid is X ∈ [96, 928],
        # Y ∈ [85, 597] (CamCanSetInfo5 tag 0x0265) — NOT the ~9520 px
        # sensor space this code originally assumed. The old
        # ``x < 8000`` direction test always jiggled +delta, so an AF
        # point parked on the right edge (x=928, a documented preset)
        # produced x=929 → the range guard in
        # ``sigma_set_cam_datagroup_focus`` raised ValueError → the
        # heartbeat thread died silently and the camera dozed minutes
        # later with no visible cause. Jiggle inward from the midpoint
        # and clamp into the valid grid so the write can never be
        # rejected.
        dx = self._jiggle_delta if x < 512 else -self._jiggle_delta
        x_jig = max(96, min(928, x + dx))
        if x_jig == x:
            # Degenerate delta (e.g. delta=0 config) — nothing to write.
            self._ping_info()
            return

        # Two writes: shift, then restore. Short sleep between so the
        # camera registers the change rather than treating the pair
        # as a single instantaneous setting.
        self._bridge.sigma_set_cam_datagroup_focus(x_jig, y)
        time.sleep(0.05)
        self._bridge.sigma_set_cam_datagroup_focus(x, y)
        self._last_af = (x, y)
        self.log.debug("heartbeat_jiggle_ok", x=x, y=y, dx=dx)

    # ----- main loop ---------------------------------------------------

    def _run(self) -> None:
        ping_fn: Callable[[], None]
        if self._strategy == "af_drive_only":
            ping_fn = self._ping_af_drive_only
        elif self._strategy == "af_point_jiggle":
            ping_fn = self._ping_af_point_jiggle
        else:
            ping_fn = self._ping_info

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
                    ping_fn()
            except CameraIdleError as e:
                # Camera already dozing — main loop's recovery will
                # handle it on the next status poll. Logged so we can
                # confirm the heartbeat is the right cadence (if we
                # see this regularly, drop interval_s).
                self.log.warning("heartbeat_camera_idle", error=str(e))
            except (PTPError, USBBridgeError, usb.core.USBError, ValueError) as e:
                # Quiet warning — the watcher owns recovery / reconnect.
                # Phase 3.6 (2026-05-13): added usb.core.USBError to
                # the catch list. Previously the heartbeat thread
                # crashed with an unhandled USBTimeoutError ([Errno 60])
                # when the bulk endpoint wedged, leaving no heartbeat
                # at all once the camera came back. Catch it here so
                # the thread survives — the daemon's status-poll path
                # owns the actual recovery decision.
                # Phase 3.15 (A7): ValueError added — a rejected
                # parameter (e.g. AF coordinate outside the camera's
                # grid) must degrade to a skipped ping, never kill
                # the keep-alive thread.
                self.log.warning("heartbeat_failed", error=str(e))
        self.log.info("heartbeat_stopped")

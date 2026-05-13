"""TetherDaemon — the heart of the app.

Polls the camera for new images (whether triggered by the physical shutter
button or by an explicit PC-side Snap) and routes each shot through the
configured destination (Lightroom watch folder or session folder).

Architecture::

    ┌─────────────────────────────────────────────────────────────────┐
    │  TetherDaemon (background thread)                                │
    │                                                                  │
    │  1. open USB + sigma_init() (10 PTP calls, PC capture mode)      │
    │                                                                  │
    │  2. loop:                                                        │
    │     a. drain PC-trigger queue → call sigma_snap()                │
    │     b. sigma_get_capture_status(p1=next_slot)                    │
    │     c. if status reached 0x0005:                                 │
    │          download → atomic write → clear → emit ShotEvent        │
    │          next_slot += 1                                          │
    │     d. sleep(poll_interval)                                      │
    │                                                                  │
    │  3. on stop(): close session, release USB                        │
    └─────────────────────────────────────────────────────────────────┘

The daemon publishes ``ShotEvent`` callbacks so a UI (CLI or floating panel)
can update without polling daemon state.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Callable

import usb.core  # type: ignore[import-not-found]

from fp_l_tether.camera.sigma_datagroup import (
    CanSetInfo,
    ExposureSettings,
    read_can_set_info,
    read_exposure,
    read_focus_point,
)
from fp_l_tether.camera.usb_bridge import (
    CameraIdleError,
    PTPError,
    USBBridge,
    USBBridgeError,
)
from fp_l_tether.config import AppConfig
from fp_l_tether.lightroom import build_destination
from fp_l_tether.telemetry import get_logger, log_shot
from fp_l_tether.transfer.atomic import write_atomic
from fp_l_tether.transfer.heartbeat import HeartbeatThread
from fp_l_tether.transfer.liveview import LiveViewFrame, LiveViewStream


# ---------------------------------------------------------------------------
# Event dataclasses (for UI callbacks)
# ---------------------------------------------------------------------------


@dataclass
class ShotEvent:
    """Emitted after each successful download."""

    shot_index: int
    saved_path: Path
    size: int
    elapsed_s: float
    image_id: int
    db_head: int
    db_tail: int
    trigger: str  # "camera_button" | "pc_snap"
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def mbps(self) -> float:
        return (self.size / 1024 / 1024) / self.elapsed_s if self.elapsed_s > 0 else 0.0


@dataclass
class StatusEvent:
    """Emitted on connection / state changes."""

    state: str  # "connecting" | "ready" | "shooting" | "downloading" | "error" | "stopped"
    message: str = ""


@dataclass
class ExposureEvent:
    """Emitted when the camera reports new exposure dial values.

    Fired once on ``ready`` after init and once after every successful
    shot, so the floating panel can show the live SS/ISO/Aperture/WB.
    """

    settings: ExposureSettings


@dataclass
class CanSetInfoEvent:
    """Emitted once per connection — what each dial *may* be set to."""

    info: CanSetInfo


@dataclass
class FocusPointEvent:
    """Emitted with the camera's reported AF point (or None if unknown)."""

    x: int | None
    y: int | None


@dataclass
class LiveViewEvent:
    """Emitted for each live-view frame.

    ``fps_avg`` and ``frame_kb`` are rolling 1-second metrics from the
    stream so the UI can show stream health without computing it itself.
    """

    jpeg: bytes
    width: int
    height: int
    fps_avg: float
    frame_kb: float


@dataclass
class _SetExposureRequest:
    """Internal queue item: change a single exposure dial."""

    # ``group`` is 1 or 2 — which SetCamDataGroup to use.
    group: int
    # Single {field_name: int_value} pair. Field names match sigma-ptpy
    # schema (ShutterSpeed, Aperture, ISOSpeed, ISOAuto, WhiteBalance, …).
    values: dict[str, int]


@dataclass
class _SetFocusRequest:
    """Internal queue item: move the AF point."""

    x: int
    y: int


ShotCallback = Callable[[ShotEvent], None]
StatusCallback = Callable[[StatusEvent], None]
ExposureCallback = Callable[[ExposureEvent], None]
CanSetInfoCallback = Callable[[CanSetInfoEvent], None]
FocusPointCallback = Callable[[FocusPointEvent], None]
LiveFrameCallback = Callable[[LiveViewEvent], None]


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


class TetherDaemon:
    """Background thread that reconciles camera shots with the filesystem.

    Thread-safe API:
        - ``start()`` — boot the worker (non-blocking)
        - ``stop()``  — signal shutdown, join the worker
        - ``request_snap()`` — enqueue a PC-side shutter trigger
        - ``on_shot`` / ``on_status`` — register callbacks (set before start())

    Use as context manager::

        with TetherDaemon(cfg) as daemon:
            daemon.on_shot = lambda e: print(f"new shot: {e.saved_path}")
            daemon.request_snap()
            time.sleep(10)
    """

    def __init__(self, cfg: AppConfig, session_name: str | None = None):
        self.cfg = cfg
        self.session_name = session_name or datetime.now().strftime("%Y%m%d_session")
        self.log = get_logger("daemon")

        self._stop_event = threading.Event()
        self._snap_queue: Queue[None] = Queue()
        self._af_queue: Queue[None] = Queue()
        self._set_exposure_queue: Queue[_SetExposureRequest] = Queue()
        self._set_focus_queue: Queue[_SetFocusRequest] = Queue()
        self._thread: threading.Thread | None = None
        self._shot_count = 0
        self._next_slot = 0  # camera's db_head — advances after each capture
        # Per-item shot counter: keeps filenames sane when switching items mid-session.
        # e.g. vase_0001..vase_0003, then bowl_0001..bowl_0003.
        self._current_item: str = cfg.output.default_item
        self._shots_by_item: dict[str, int] = {}
        self._item_lock = threading.Lock()
        # PTP bulk endpoint serialiser. The Sigma camera handles ONE
        # bulk transfer at a time — concurrent snap + view-frame fetches
        # interleave on the wire and corrupt both. Every bridge.sigma_*
        # call (from daemon AND live-view thread) must hold this lock.
        # RLock so functions that nest (e.g. drain → emit) don't deadlock.
        self._ptp_lock = threading.RLock()
        # Live-view stream — created on connect, torn down on stop.
        self._liveview: LiveViewStream | None = None
        # USB keep-alive heartbeat — same lifecycle as the LV stream.
        self._heartbeat: HeartbeatThread | None = None

        # ----- Burst-aware quiet-window state -----
        # After a burst settles (snap queue drains), the camera's
        # internal ImageDB consolidation needs quiet time on the
        # bulk endpoint to avoid getting wedged into Errno 60 by
        # retries. We enforce a hard no-PTP window whose size
        # scales with the just-finished burst length.
        # ``_bus_quiet_until`` is the monotonic deadline; until
        # then the main loop suppresses status polling, LV resume,
        # heartbeat pings, and snap-queue drain. Updated only via
        # _arm_quiet_window under ``_bus_quiet_lock`` to avoid
        # races between the daemon thread and any consumer
        # (heartbeat).
        self._bus_quiet_until: float = 0.0
        self._bus_quiet_lock = threading.Lock()
        # Burst counter — incremented on every successful download,
        # reset to 0 by _arm_quiet_window. Counts shots in the
        # current contiguous run (between two quiet windows).
        # NOTE: the earlier "shots in last N seconds" heuristic
        # always returned 1 because fp L per-shot processing time
        # (~5 s) exceeded any sane lookback window. Counting
        # explicit run length is unambiguous.
        self._burst_in_progress: int = 0

        self.on_shot: ShotCallback | None = None
        self.on_status: StatusCallback | None = None
        self.on_exposure: ExposureCallback | None = None
        self.on_can_set_info: CanSetInfoCallback | None = None
        self.on_focus_point: FocusPointCallback | None = None
        self.on_live_frame: LiveFrameCallback | None = None

    # ----- lifecycle ---------------------------------------------------

    def __enter__(self) -> "TetherDaemon":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="TetherDaemon", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    # ----- public API --------------------------------------------------

    def request_snap(self) -> None:
        """Queue a PC-side shutter trigger. Non-blocking."""
        self._snap_queue.put(None)
        self.log.info("snap_requested", source="pc")

    def request_af(self) -> None:
        """Queue a PC-side AF-only drive (no capture). Non-blocking.

        Uses SnapCommand mode 3 (AF_DRIVE_ONLY) which drives the AF motor
        once and stops, without producing an image. Useful for re-focusing
        in a pre-focus + space-spam workflow.
        """
        self._af_queue.put(None)
        self.log.info("af_requested", source="pc")

    def request_set_exposure(self, group: int, values: dict[str, int]) -> None:
        """Queue a SetCamDataGroup{1,2} write. Non-blocking.

        ``values`` is keyed by sigma-ptpy field names. The daemon writes
        the field, waits briefly for the camera to settle, re-reads
        DG1+DG2, and emits a fresh ExposureEvent so the UI shows what
        actually took effect (not the requested value).
        """
        if group not in (1, 2):
            raise ValueError(f"group must be 1 or 2, got {group}")
        self._set_exposure_queue.put(_SetExposureRequest(group=group, values=values))
        self.log.info("set_exposure_requested", group=group, fields=list(values))

    def request_set_focus_point(self, x: int, y: int) -> None:
        """Queue a SetCamDataGroupFocus write to move the AF point."""
        self._set_focus_queue.put(_SetFocusRequest(x=x, y=y))
        self.log.info("set_focus_requested", x=x, y=y)

    @property
    def current_item(self) -> str:
        with self._item_lock:
            return self._current_item

    def start_new_session(self, name: str | None = None) -> None:
        """Reset session state without restarting the daemon.

        Updates ``session_name``, resets the global shot counter and
        per-item counters. Use this when moving from one shoot to another
        (e.g. finished a batch of vases, starting a batch of bowls).

        If ``name`` is None or blank, a timestamp-based name is generated.
        """
        cleaned = (name or "").strip()
        if not cleaned:
            cleaned = datetime.now().strftime("%Y%m%d_%H%M%S_session")
        with self._item_lock:
            self.session_name = cleaned
            self._shot_count = 0
            self._shots_by_item.clear()
        self.log.info("session_started", session=cleaned)
        self._emit_status("ready", f"Session: {cleaned}")

    def set_current_item(self, name: str) -> None:
        """Change the active item name. Filenames written from now on use
        this as ``{item}`` and the per-item shot counter resets to 1 the
        first time we shoot under this name.

        Sanitises whitespace; empty / blank values fall back to the
        configured ``default_item`` so we never produce ``..__shot_0001``.
        """
        cleaned = (name or "").strip()
        if not cleaned:
            cleaned = self.cfg.output.default_item
        with self._item_lock:
            self._current_item = cleaned
        self.log.info("item_changed", item=cleaned)

    @property
    def shot_count(self) -> int:
        return self._shot_count

    # ----- internal helpers --------------------------------------------

    def _emit_status(self, state: str, message: str = "") -> None:
        if self.on_status is not None:
            try:
                self.on_status(StatusEvent(state=state, message=message))
            except Exception as e:  # noqa: BLE001
                self.log.warning("status_callback_raised", error=str(e))
        self.log.debug("state", state=state, message=message)

    def _emit_shot(self, event: ShotEvent) -> None:
        if self.on_shot is not None:
            try:
                self.on_shot(event)
            except Exception as e:  # noqa: BLE001
                self.log.warning("shot_callback_raised", error=str(e))

    # ----- Burst-aware quiet-window helpers ----------------------------

    def bus_quiet_remaining(self) -> float:
        """Seconds until the post-capture quiet window expires, or 0.

        Public (no leading underscore) so external threads — notably
        :class:`HeartbeatThread` — can consult the same gate the
        main loop uses. Read is lock-protected because the daemon
        thread updates ``_bus_quiet_until`` from
        :meth:`_arm_quiet_window`.
        """
        with self._bus_quiet_lock:
            return max(0.0, self._bus_quiet_until - time.monotonic())

    def _arm_quiet_window(self) -> None:
        """Set the quiet-window deadline for the just-completed burst.

        Reads ``_burst_in_progress`` (number of contiguous shots
        since the previous quiet window) to compute window length:
        ``base + per_shot * max(0, burst - 1)``. Always EXTENDS,
        never shortens — a freshly observed burst that overlaps an
        existing window won't clip the quiet time prematurely.
        Resets ``_burst_in_progress`` so the next burst starts at 0.
        """
        burst = self._burst_in_progress
        self._burst_in_progress = 0
        base = self.cfg.camera.commit_window_base_s
        per = self.cfg.camera.commit_window_per_shot_s
        window_s = base + per * max(0, burst - 1)
        with self._bus_quiet_lock:
            self._bus_quiet_until = max(
                self._bus_quiet_until, time.monotonic() + window_s,
            )
        self.log.info(
            "quiet_window_armed",
            burst_count=burst,
            window_s=round(window_s, 2),
        )

    def _record_shot(self) -> None:
        """Tick the burst counter for a just-completed download."""
        self._burst_in_progress += 1

    # ----- LV pause/resume helpers ------------------------------------

    def _pause_liveview(self) -> None:
        """Suspend live-view for the duration of a snap+download.

        No-op if LV is disabled, not running, or pause_during_snap is
        off. Idempotent — safe to call from multiple code paths (PC
        snap fire, image-found, etc.) within one capture cycle.
        """
        if (self._liveview is not None
                and self.cfg.liveview.pause_during_snap):
            self._liveview.pause()

    def _resume_liveview(self) -> None:
        """Re-arm the LV stream once the capture cycle is done.

        Called from every code path that ends a capture: download
        completed, slot failure cleared, watchdog reset, PC snap
        rejection. Idempotent.
        """
        if (self._liveview is not None
                and self.cfg.liveview.pause_during_snap):
            self._liveview.resume()

    def _drain_set_exposure(self, bridge: USBBridge) -> None:
        """Apply queued exposure dial writes, then re-read for ground truth.

        Per the user's request: do NOT optimistically update the UI from
        the requested value. Always send → wait → read → emit, so the
        panel reflects what the camera actually accepted.

        **Bulk-endpoint protection (added 2026-05-13 after a live wedge):**
        Rapidly flipping a dial (e.g. 6 ImageQuality clicks in 19 s)
        produced a 0-byte data phase → Errno 60 endpoint wedge that
        only a body power-cycle could recover. Root cause was the LV
        thread interleaving frame fetches between each
        SetCamDataGroup2 write — each Set fights with each LV bulk
        read on the camera's single bulk endpoint, and the camera
        eventually gives up. Mitigation here mirrors the snap path:

          1. Pause LV up front so frame requests stop.
          2. Hold ``_ptp_lock`` across the entire drain — write,
             inter-write delay, settle, read-back, emit — so the
             heartbeat thread also can't sneak between writes.
          3. Insert a small inter-write delay so each Set has time
             to commit before the next is pushed.
          4. Resume LV in ``finally`` so a mid-drain exception
             can't leave the stream stuck.
        """
        if self._set_exposure_queue.empty():
            return

        # Snapshot whether LV was running so we only re-arm what we
        # actually paused (don't fight any other pause source).
        lv_was_running = (
            self._liveview is not None and not self._liveview.is_paused
        )
        if lv_was_running:
            self._liveview.pause()

        applied_any = False
        try:
            with self._ptp_lock:
                while True:
                    try:
                        req = self._set_exposure_queue.get_nowait()
                    except Empty:
                        break
                    try:
                        if req.group == 1:
                            bridge.sigma_set_datagroup_1(req.values)
                        else:
                            bridge.sigma_set_datagroup_2(req.values)
                        applied_any = True
                        self.log.info(
                            "set_exposure_sent",
                            group=req.group, fields=req.values,
                        )
                        # Small inter-write breather so the camera
                        # commits each DataGroup change before the
                        # next arrives. 80 ms is well below human
                        # click cadence (single clicks still feel
                        # instant) but throttles rapid-fire enough
                        # to prevent the busy → 0-byte → wedge
                        # cascade observed without it.
                        time.sleep(0.08)
                    except PTPError as e:
                        # Camera rejected. Re-read so the UI snaps
                        # back to the old (still-current) value —
                        # that's the "revert" path.
                        self.log.error(
                            "set_exposure_failed",
                            group=req.group, fields=req.values,
                            error=str(e),
                        )
                        self._emit_status("error", f"設定変更失敗: {e}")
                        applied_any = True  # still re-emit for revert
                if applied_any:
                    # 200 ms settle then read-back inside the same
                    # lock so LV / heartbeat can't grab the bus
                    # before the read completes.
                    time.sleep(0.2)
                    self._emit_exposure(bridge)
        finally:
            if lv_was_running:
                self._liveview.resume()

    def _drain_set_focus(self, bridge: USBBridge) -> None:
        """Apply queued AF-point moves, then publish the new position."""
        latest: _SetFocusRequest | None = None
        while True:
            try:
                latest = self._set_focus_queue.get_nowait()
            except Empty:
                break
        if latest is None:
            return
        try:
            with self._ptp_lock:
                bridge.sigma_set_cam_datagroup_focus(latest.x, latest.y)
            self.log.info("set_focus_sent", x=latest.x, y=latest.y)
        except PTPError as e:
            self.log.error(
                "set_focus_failed", x=latest.x, y=latest.y, error=str(e),
            )
            self._emit_status("error", f"AF点送信失敗: {e}")
            return
        # Re-publish; the camera's Get is a static cache (per memory) so
        # this just reflects "last commanded" — that's fine for the dot.
        self._emit_focus_point(bridge)

    def _emit_can_set_info(self, bridge: USBBridge) -> None:
        """Read and publish CamCanSetInfo5 — list of allowed dial values."""
        if self.on_can_set_info is None:
            return
        try:
            with self._ptp_lock:
                info = read_can_set_info(bridge)
        except (PTPError, CameraIdleError, ValueError) as e:
            # CameraIdleError tolerated here — best-effort UI read,
            # the next poll-loop iteration will see it again and the
            # main loop's recovery path handles the wake-up.
            self.log.warning("can_set_info_read_failed", error=str(e))
            return
        try:
            self.on_can_set_info(CanSetInfoEvent(info=info))
        except Exception as e:  # noqa: BLE001
            self.log.warning("can_set_info_callback_raised", error=str(e))

    def _emit_focus_point(self, bridge: USBBridge) -> None:
        """Read and publish the current AF point coordinates."""
        if self.on_focus_point is None:
            return
        try:
            with self._ptp_lock:
                xy = read_focus_point(bridge)
        except (PTPError, CameraIdleError) as e:
            self.log.warning("focus_point_read_failed", error=str(e))
            return
        x, y = (xy if xy else (None, None))
        try:
            self.on_focus_point(FocusPointEvent(x=x, y=y))
        except Exception as e:  # noqa: BLE001
            self.log.warning("focus_point_callback_raised", error=str(e))

    def _emit_live_frame(self, frame: LiveViewFrame) -> None:
        """Bridge LiveViewStream frames to the panel callback.

        Called from the LiveViewStream's background thread. Drops the
        callback if none is registered; never raises into the stream
        thread (would kill the stream).
        """
        if self.on_live_frame is None:
            return
        try:
            self.on_live_frame(
                LiveViewEvent(
                    jpeg=frame.jpeg,
                    width=frame.width,
                    height=frame.height,
                    fps_avg=frame.fps_avg,
                    frame_kb=frame.frame_kb,
                )
            )
        except Exception as e:  # noqa: BLE001
            self.log.warning("live_frame_callback_raised", error=str(e))

    def _passive_idle_wait(
        self,
        reason: str,
        attempt: int,
        sleep_s: float,
    ) -> None:
        """Passively wait out a 0-byte read, no active PTP calls.

        Initial recovery design (2026-05-13 morning) actively pinged
        the camera with ``sigma_get_camera_info`` +
        ``sigma_set_datagroup_3_pc_capture``. The first live test
        showed that's the WRONG move: a 0-byte data phase often
        precedes a half-stalled bulk endpoint, and writing more
        commands into it just escalates to Errno 60 (OS USB timeout)
        and forces a session reset.

        The passive strategy is: pause LV (so its read loop doesn't
        keep hammering a sick endpoint), sleep, and let the outer
        loop retry the status poll. If the camera was just
        commit-settling, the next poll succeeds. If it was genuinely
        idle, the heartbeat (or the next poll itself) wakes it.
        Three consecutive failures still escalate to a reconnect.
        """
        self._emit_status(
            "recovering",
            f"Camera quiet — waiting… (attempt {attempt})",
        )
        self.log.warning(
            "camera_idle_passive_wait",
            attempt=attempt,
            reason=reason,
            sleep_s=sleep_s,
        )
        # Pause LV during the wait so its background fetch loop
        # doesn't keep slamming a possibly-half-stalled endpoint.
        # We don't resume here — the LV resume gate at the top of
        # the main loop handles resume timing centrally so we
        # don't accidentally double-fire it during a sequence of
        # alternating wait/poll cycles.
        self._pause_liveview()
        self._stop_event.wait(sleep_s)

    def _emit_exposure(self, bridge: USBBridge) -> None:
        """Read DG1+DG2 from the camera and publish an ExposureEvent.

        Swallows PTPError (camera transient state) — exposure display is
        best-effort, not flow-critical.
        """
        if self.on_exposure is None:
            return
        try:
            with self._ptp_lock:
                settings = read_exposure(bridge)
        except (PTPError, CameraIdleError) as e:
            self.log.warning("exposure_read_failed", error=str(e))
            return
        try:
            self.on_exposure(ExposureEvent(settings=settings))
        except Exception as e:  # noqa: BLE001
            self.log.warning("exposure_callback_raised", error=str(e))

    # ----- main loop ---------------------------------------------------

    def _run(self) -> None:
        """Worker thread entry point.

        Outer reconnect loop: if the USB bridge goes away (camera unplugged,
        powered off, or any libusb I/O error), close the bridge cleanly,
        wait, and try to re-establish. Loops until ``stop()`` is called.

        Reconnect cap: once the bulk endpoint truly stalls (Errno 60 on
        the fp L, observed when LV / status poll collides with a post-
        snap deep commit), no amount of re-claim restores it without a
        physical cable reseat. Spinning on session_lost forever is
        bad UX — we cap the consecutive failures and give up with a
        clear message.
        """
        reconnect_delay_s = 2.0
        reconnect_failure_cap = 5
        reconnect_failures = 0
        first_attempt = True

        while not self._stop_event.is_set():
            if first_attempt:
                first_attempt = False
            else:
                # Wait between reconnect attempts; bail early if stopped
                if self._stop_event.wait(reconnect_delay_s):
                    break

            session_start_at = time.monotonic()
            try:
                self._run_session()
            except (USBBridgeError, usb.core.USBError) as e:
                session_lifetime_s = time.monotonic() - session_start_at
                # Sessions that ran for a while before dying represent
                # "real" disconnects (e.g. user unplugged) where a
                # retry makes sense. Sessions that die in <10 s
                # repeatedly mean the endpoint is wedged — count those
                # toward the cap.
                if session_lifetime_s < 10.0:
                    reconnect_failures += 1
                else:
                    reconnect_failures = 0
                self.log.warning(
                    "session_lost",
                    error=str(e),
                    lifetime_s=round(session_lifetime_s, 2),
                    consecutive_short_failures=reconnect_failures,
                )
                if reconnect_failures >= reconnect_failure_cap:
                    self.log.error(
                        "reconnect_cap_reached",
                        cap=reconnect_failure_cap,
                    )
                    self._emit_status(
                        "error",
                        f"カメラ wedge ({reconnect_failures} 回連続失敗) — "
                        f"fp L 本体の電源を OFF→ON してから再起動してください "
                        f"(USB 抜き差しだけでは復旧しません)",
                    )
                    break
                self._emit_status(
                    "disconnected",
                    f"接続切れ ({e}). 再接続を試行中… "
                    f"({reconnect_failures}/{reconnect_failure_cap})",
                )
                # loop continues → reconnect attempt after delay
            except Exception as e:  # noqa: BLE001
                # Unexpected; log and exit cleanly so we don't spin
                self.log.exception("unexpected_fatal", error=str(e))
                self._emit_status("error", f"crash: {e}")
                break

        self._emit_status("stopped")
        self.log.info("daemon_stopped", shots=self._shot_count)

    def _run_session(self) -> None:
        """One connected session: find camera, open, init, run main poll loop.

        Raises on USB-level failures so the outer loop can reconnect.
        """
        bridge: USBBridge | None = None
        try:
            self._emit_status("connecting", "Looking for Sigma fp / fp L…")
            bridge = USBBridge.find_sigma_fp_l()
            bridge.open()
            with self._ptp_lock:
                bridge.open_session()
            self.log.info("session_opened")

            self._emit_status("initializing", "Running Sigma init sequence (10 PTP calls)…")
            with self._ptp_lock:
                bridge.sigma_init()
            self.log.info("init_complete")

            # Discover starting slot from camera state.
            # Note: we reset _next_slot from the camera's authoritative state
            # on every (re)connect, so resuming after an unplug picks up at
            # the correct slot rather than carrying stale state forward.
            with self._ptp_lock:
                pre = bridge.sigma_get_capture_status(0)
            self._next_slot = pre.image_db_head
            self.log.info("ready",
                          slot=self._next_slot,
                          db_head=pre.image_db_head,
                          db_tail=pre.image_db_tail)
            self._emit_status("ready", f"Watching slot 0x{self._next_slot:02X}")
            # Populate the UI dropdowns + AF popover from camera capabilities.
            self._emit_can_set_info(bridge)
            # Initial exposure read so the panel populates before the first shot.
            self._emit_exposure(bridge)
            self._emit_focus_point(bridge)

            # Start the USB keep-alive heartbeat. Prevents the camera
            # from drifting into its idle power-saving state during
            # long quiet stretches between shots. Shares the PTP lock
            # so its periodic ping serialises against everything else.
            if self.cfg.camera.keep_alive_enabled:
                self._heartbeat = HeartbeatThread(
                    bridge,
                    self._ptp_lock,
                    interval_s=self.cfg.camera.keep_alive_interval_s,
                    bus_quiet_check=self.bus_quiet_remaining,
                )
                self._heartbeat.start()

            # Start background live view if enabled. The stream shares
            # the PTP lock so its view-frame fetches serialise against
            # snaps/downloads on the single USB bulk endpoint.
            if self.cfg.liveview.enabled:
                self._liveview = LiveViewStream(
                    bridge,
                    self._ptp_lock,
                    target_fps=self.cfg.liveview.target_fps,
                    on_frame=self._emit_live_frame,
                    backoff_s=self.cfg.liveview.busy_backoff_ms / 1000.0,
                    max_consecutive_busy=self.cfg.liveview.max_consecutive_busy,
                    promote_after_consecutive_ok=(
                        self.cfg.liveview.promote_after_consecutive_ok
                    ),
                    post_snap_busy_grace_s=(
                        self.cfg.liveview.post_snap_busy_grace_s
                    ),
                    first_storm_grace_s=(
                        self.cfg.liveview.first_storm_grace_s
                    ),
                )
                self._liveview.start()

            poll_idle_s = self.cfg.camera.poll_idle_ms / 1000.0
            poll_active_s = self.cfg.camera.poll_active_ms / 1000.0

            # Idle-recovery counter. After each successful poll we reset
            # to 0; on a CameraIdleError we passively wait (no active
            # PTP — see _passive_idle_wait for why). Three consecutive
            # failures escalate to a session-level disconnect. The
            # quiet-window guard at the top of the loop prevents this
            # path from triggering during a post-capture commit tail
            # (which used to manifest as benign 0-bytes here), so a
            # 0-byte that reaches this branch genuinely indicates the
            # camera has slipped into 5-min power-save.
            idle_recovery_attempts = 0
            idle_recovery_max = 3

            # Per-iteration trigger source. Starts as "camera_button" since
            # we begin in "watching" mode; flips to "pc_snap" the iteration
            # after a PC-side request is fired.
            pending_trigger: str = "camera_button"
            t_shot_start: float | None = None  # time the current pending shot started
            # Watchdog: if a pending shot doesn't produce a downloadable
            # frame within this window, treat it as stuck and reset so
            # AF/snap drains can fire again. Slightly longer than the
            # worst long-exposure case (30s shutter + buffer write).
            snap_watchdog_s = 35.0
            # Throttle the diagnostic "still polling" log so we don't
            # spam — every N seconds at most while a shot is pending.
            polling_log_interval_s = 3.0
            t_last_polling_log: float = 0.0

            while not self._stop_event.is_set():
                # ----- 1a-pre0. Bus quiet-window guard -----
                # After a successful download, the camera enters a
                # deep ImageDB consolidation that lasts longer the
                # bigger the just-completed burst. Polling the
                # status endpoint, fetching LV frames, or pinging
                # heartbeat DURING this window can push a
                # half-stalled endpoint into Errno 60 — which on
                # the fp L is only recoverable by physically
                # power-cycling the camera body.
                #
                # So we enforce a hard no-PTP gate at the top of
                # the loop: while ``bus_quiet_remaining()`` is
                # positive, we sleep in short ticks and do nothing
                # else. This blocks status polls AND snap-queue
                # drain AND LV resume — pending snaps queue up
                # and fire once the window expires.
                quiet_remaining = self.bus_quiet_remaining()
                if quiet_remaining > 0.0:
                    # Sleep in <=500 ms slices so stop_event is
                    # checked promptly. Don't continue past this
                    # point — the window must be PTP-quiet.
                    if self._stop_event.wait(min(quiet_remaining, 0.5)):
                        continue
                    continue

                # ----- 1a-pre1. LV resume gate -----
                # The window just expired (or no capture has
                # happened yet). Lift any pending LV pause now
                # — central place, so individual capture paths
                # never have to worry about LV state.
                if (
                    self._liveview is not None
                    and self._liveview.is_paused
                    and t_shot_start is None
                ):
                    self._resume_liveview()

                # ----- 1a-pre. Drain exposure/focus queues -----
                # Skip while a shot is in flight — between snap fire
                # and quiet-window arm, the camera is in commit phase
                # and set_* writes can wedge the FW (2026-05-13 incident:
                # rapid set_focus + AF + snap during commit window
                # produced an unrecoverable bulk endpoint wedge at
                # shot #27 / ~12 min). AF and snap drains below are
                # already gated on t_shot_start; this makes set_*
                # symmetric with that contract. Queue items survive —
                # they drain on the next iteration after the quiet
                # window closes.
                if t_shot_start is None:
                    self._drain_set_exposure(bridge)
                    self._drain_set_focus(bridge)

                # ----- 1a. Drain AF queue (only when no shot is in flight) ---
                # AF-only drive (SnapCommand mode 3) produces no image,
                # so we don't set t_shot_start / pending_trigger after it.
                if t_shot_start is None:
                    try:
                        self._af_queue.get_nowait()
                    except Empty:
                        pass
                    else:
                        self._emit_status("focusing", "AF駆動中")
                        try:
                            with self._ptp_lock:
                                bridge.sigma_set_datagroup_3_pc_capture()
                                bridge.sigma_snap(mode=3, amount=1)  # AF_DRIVE_ONLY
                            self.log.info("af_completed")
                            self._emit_status(
                                "ready",
                                f"Watching slot 0x{self._next_slot:02X}",
                            )
                        except PTPError as e:
                            self.log.error("af_failed", error=str(e))
                            self._emit_status("error", f"AF失敗: {e}")
                        # USBBridgeError / usb.core.USBError propagates → reconnect

                # ----- 1b. Drain snap queue ONLY if no shot is in flight ---
                # If we already fired a PC snap and haven't seen its image
                # land yet (``t_shot_start`` is set), don't fire another —
                # the camera rejects rapid-fire snaps with status 0x6004
                # and the second snap's slot ends up in permanent failure.
                pc_snap_requested = False
                if t_shot_start is None:
                    try:
                        self._snap_queue.get_nowait()
                        pc_snap_requested = True
                    except Empty:
                        pass

                if pc_snap_requested:
                    self._emit_status("shooting", "Snap (PC trigger)")
                    # Suspend LV for the entire snap+download cycle.
                    # The Phase 3.2 stream test showed that running LV
                    # through a PC snap triggers a ~5 s busy storm on
                    # the first capture (and shorter blips on later
                    # ones). Pausing here lets the camera dedicate the
                    # bulk endpoint to capture and download. Resumed
                    # below after download / failure / watchdog.
                    self._pause_liveview()
                    try:
                        # Re-arm + sync + snap as one PTP transaction so
                        # the live-view thread can't slot a view-frame
                        # fetch between sub-steps and confuse the
                        # capture state machine.
                        with self._ptp_lock:
                            # Re-arm PC capture mode (per fp trace) then fire
                            bridge.sigma_set_datagroup_3_pc_capture()

                            # Re-sync next_slot from camera state before snap.
                            #
                            # Per the 2026-05-13 hardware traces, the camera
                            # writes the next snap to ``image_db_tail`` (the
                            # next-write slot), NOT ``image_db_head``. Head is
                            # the oldest-unread pointer; it sits at 0 when
                            # nothing is pending and only catches up after
                            # the capture commits ~hundreds of ms later. So
                            # polling head pre-snap finds an empty slot and
                            # the loop spins until the watchdog fires.
                            try:
                                sync_state = bridge.sigma_get_capture_status(0)
                                target = sync_state.image_db_tail
                                if target != self._next_slot:
                                    self.log.info(
                                        "slot_resync",
                                        cached=self._next_slot,
                                        actual=target,
                                        db_head=sync_state.image_db_head,
                                        db_tail=sync_state.image_db_tail,
                                    )
                                    self._next_slot = target
                            except (PTPError, USBBridgeError) as e:
                                self.log.warning(
                                    "slot_resync_failed", error=str(e)
                                )

                            bridge.sigma_snap(
                                mode=self.cfg.camera.snap_mode,
                                amount=1,
                            )
                        pending_trigger = "pc_snap"
                        t_shot_start = time.monotonic()
                        # Force the first polling log to fire ASAP
                        # (otherwise we wait polling_log_interval_s).
                        t_last_polling_log = 0.0
                    except PTPError as e:
                        # Camera-side error (e.g. 0x6001 capture failure).
                        # Camera is still alive, just rejected this snap.
                        self.log.error("pc_snap_failed", error=str(e))
                        self._emit_status("error", str(e))
                        # Snap never fired → no download cycle to wait
                        # for, so resume LV immediately.
                        self._resume_liveview()
                        time.sleep(poll_idle_s)
                        continue
                    # USBBridgeError / usb.core.USBError propagates → reconnect

                # ----- 2. Single status poll for the active slot -----
                try:
                    with self._ptp_lock:
                        status = bridge.sigma_get_capture_status(self._next_slot)
                except CameraIdleError as e:
                    # 0-byte data phase reaching here means the
                    # camera has genuinely slipped into 5-min
                    # power-save (commit-tail 0-bytes are absorbed
                    # by the quiet-window guard above). Passive
                    # wait — no active PTP, since the previous
                    # active-recovery design empirically pushed a
                    # half-stalled endpoint into Errno 60. Counts
                    # toward cap; three failures → reconnect.
                    idle_recovery_attempts += 1
                    if idle_recovery_attempts >= idle_recovery_max:
                        self.log.error(
                            "camera_idle_recovery_exhausted",
                            attempts=idle_recovery_attempts,
                        )
                        raise USBBridgeError(
                            "camera_idle_recovery_exhausted"
                        ) from e
                    self._passive_idle_wait(
                        reason=str(e),
                        attempt=idle_recovery_attempts,
                        sleep_s=5.0,
                    )
                    continue
                except PTPError as e:
                    self.log.error("status_poll_failed", error=str(e))
                    self._emit_status("error", f"poll error: {e}")
                    time.sleep(poll_idle_s)
                    continue
                else:
                    # Any successful poll resets the idle-recovery counter.
                    idle_recovery_attempts = 0
                # USBBridgeError / usb.core.USBError propagates → reconnect

                # Failure status (0x6XXX range). This is sticky — the camera
                # remembers a failed slot across PTP sessions, so on startup
                # we may find the current slot already in 0x6004 from a
                # previous run. Clear it and advance.
                if (status.capt_status & 0xF000) == 0x6000:
                    self.log.warning(
                        "capture_failure_skipping",
                        status=f"0x{status.capt_status:04X}",
                        slot=self._next_slot,
                    )
                    try:
                        with self._ptp_lock:
                            bridge.sigma_clear_image_db_single(self._next_slot)
                        self.log.info(
                            "cleared_failed_slot",
                            slot=self._next_slot,
                        )
                    except PTPError as e:
                        # Non-fatal: camera rejected the clear. Move on.
                        self.log.warning(
                            "clear_failed_slot_failed",
                            slot=self._next_slot,
                            error=str(e),
                        )
                    # USBBridgeError propagates → reconnect
                    # Advance past the broken slot
                    self._next_slot = (self._next_slot + 1) & 0xFF
                    pending_trigger = "camera_button"
                    t_shot_start = None
                    # Capture cycle ended (in failure) → resume LV so it
                    # doesn't stay paused after a stuck-slot recovery.
                    self._resume_liveview()
                    continue

                # Image ready → download + write
                if status.capt_status in (0x0002, 0x0005):
                    self._emit_status("downloading", f"Slot 0x{self._next_slot:02X}")
                    try:
                        with self._ptp_lock:
                            entries = bridge.sigma_download_current(
                                status,
                                clear_strategy="image_db_head",
                            )
                    except PTPError as e:
                        self.log.error("download_failed", error=str(e))
                        self._emit_status("error", f"download error: {e}")
                        # Download failed → cycle is over, re-arm LV.
                        self._resume_liveview()
                        time.sleep(poll_idle_s)
                        continue
                    except ValueError as e:
                        # PictFileInfo2 parse sanity check tripped — the
                        # response from the camera failed the per-entry
                        # ext / size whitelist. The bridge has NOT
                        # initiated a GetBigPartialPictFile, so the bulk
                        # endpoint is intact. Log, advance past this
                        # slot, re-arm LV, and continue.
                        self.log.error(
                            "download_parse_failed",
                            error=str(e),
                            slot=self._next_slot,
                            image_id=status.image_id,
                            db_head=status.image_db_head,
                            db_tail=status.image_db_tail,
                        )
                        self._emit_status(
                            "error",
                            f"画像情報の解析失敗: {e}",
                        )
                        self._next_slot = (self._next_slot + 1) & 0xFF
                        self._resume_liveview()
                        time.sleep(poll_idle_s)
                        continue
                    # USBBridgeError propagates → reconnect

                    # NOTE: We deliberately do NOT resume LV here.
                    # The camera's internal "deep commit" (ImageDB
                    # consolidation) continues for several seconds
                    # after sigma_download_current() returns — the
                    # bigger the just-completed burst, the longer it
                    # takes (~12 s observed for 11 shots). Resuming
                    # LV (or polling status) during that window
                    # collides with the deep commit on the single
                    # bulk endpoint and stalls it into Errno 60 — a
                    # state only recoverable by power-cycling the
                    # camera body.
                    #
                    # Arming of the quiet window happens AFTER the
                    # post-download bookkeeping below (which
                    # includes one last PTP read to refresh the
                    # exposure dials). Once armed, the main loop's
                    # top-of-loop guard suppresses all PTP traffic
                    # until the window expires.

                    # entries is len 1 (JPG-only / DNG-only) or len 2
                    # (DNG+JPG). All files in one entries list share
                    # the same shutter — same shot_index, same
                    # item_idx, same base filename, only the
                    # extension differs.
                    self._shot_count += 1
                    with self._item_lock:
                        item = self._current_item
                        item_idx = self._shots_by_item.get(item, 0) + 1
                        self._shots_by_item[item] = item_idx

                    elapsed = (
                        (time.monotonic() - t_shot_start)
                        if t_shot_start else 0.0
                    )
                    saved_paths: list[Path] = []
                    total_size = 0
                    for entry_idx, (info, data) in enumerate(entries):
                        dest = build_destination(
                            self.cfg,
                            shot_index=item_idx,
                            session_name=self.session_name,
                            item_name=item,
                            image_id=status.image_id,
                            camera_name="fpL",
                            file_ext=(info.fileext or "jpg").lstrip("."),
                        )
                        if dest.session_dir is not None:
                            dest.session_dir.mkdir(
                                parents=True, exist_ok=True
                            )
                        saved = write_atomic(
                            dest.path,
                            data,
                            on_conflict=self.cfg.output.on_conflict,
                        )
                        saved_paths.append(saved)
                        total_size += len(data)
                        # Per-file log so the user sees both halves of
                        # a DNG+JPG pair in the log stream.
                        log_shot(
                            self.log,
                            filename=saved.name,
                            size=len(data),
                            elapsed_s=elapsed,
                            image_id=status.image_id,
                            slot=(self._next_slot + entry_idx) & 0xFF,
                            image_db_head=status.image_db_head,
                            image_db_tail=status.image_db_tail,
                            trigger=pending_trigger,
                        )

                    # Emit a single ShotEvent representing the shutter
                    # (the primary file — DNG comes first in DNG+JPG
                    # mode, so saved_paths[0] is the "main" image).
                    # size is the sum so the UI's running total is
                    # accurate.
                    event = ShotEvent(
                        shot_index=self._shot_count,
                        saved_path=saved_paths[0],
                        size=total_size,
                        elapsed_s=elapsed,
                        image_id=status.image_id,
                        db_head=status.image_db_head,
                        db_tail=status.image_db_tail,
                        trigger=pending_trigger,
                    )
                    self._emit_shot(event)
                    if len(saved_paths) == 1:
                        ready_msg = (
                            f"Saved #{self._shot_count}: "
                            f"{saved_paths[0].name}"
                        )
                    else:
                        names = " + ".join(p.name for p in saved_paths)
                        ready_msg = (
                            f"Saved #{self._shot_count}: {names}"
                        )
                    self._emit_status("ready", ready_msg)
                    # Refresh exposure display — the user may have rolled a
                    # dial between shots.
                    self._emit_exposure(bridge)

                    # Advance past every slot we just consumed, then
                    # reset trigger marker.
                    self._next_slot = (
                        self._next_slot + len(entries)
                    ) & 0xFF
                    pending_trigger = "camera_button"
                    t_shot_start = None

                    # ----- Arm the burst-aware quiet window -----
                    # Tick the burst counter once per file written —
                    # DNG+JPG counts as 2 ticks because the camera
                    # commits twice the image_db state per shutter,
                    # roughly doubling deep-commit work. If no
                    # further snap is queued, arm a window scaled to
                    # the (file-aware) burst length and reset the
                    # counter.
                    for _ in range(len(entries)):
                        self._record_shot()
                    if self._snap_queue.empty():
                        self._arm_quiet_window()
                    # Tight loop — image may already be there for next shot
                    continue

                # No image yet → sleep & loop.
                # Diagnostic: while a shot is pending, log the capt_status
                # every few seconds so we can see what the camera is
                # returning when shots get "stuck" mid-flight.
                if t_shot_start is not None:
                    now = time.monotonic()
                    if now - t_last_polling_log >= polling_log_interval_s:
                        self.log.debug(
                            "snap_polling",
                            slot=self._next_slot,
                            capt_status=f"0x{status.capt_status:04X}",
                            image_db_head=status.image_db_head,
                            image_db_tail=status.image_db_tail,
                            image_id=getattr(status, "image_id", None),
                            elapsed_s=round(now - t_shot_start, 2),
                        )
                        t_last_polling_log = now

                    # Watchdog: if the shot has been pending too long,
                    # reset so the next snap can fire. The camera will
                    # eventually catch up (or the user can re-trigger).
                    if now - t_shot_start > snap_watchdog_s:
                        self.log.warning(
                            "snap_watchdog_timeout",
                            slot=self._next_slot,
                            capt_status=f"0x{status.capt_status:04X}",
                            image_db_head=status.image_db_head,
                            image_db_tail=status.image_db_tail,
                            elapsed_s=round(now - t_shot_start, 2),
                        )
                        self._emit_status(
                            "error",
                            f"Snap timeout — status 0x{status.capt_status:04X}",
                        )
                        t_shot_start = None
                        pending_trigger = "camera_button"
                        t_last_polling_log = 0.0
                        # Snap is being abandoned → resume LV so the
                        # user still sees the viewfinder.
                        self._resume_liveview()

                # Use active interval when we're waiting on a pending shot,
                # idle interval when just watching for a manual button press.
                interval = poll_active_s if t_shot_start else poll_idle_s
                time.sleep(interval)

        finally:
            # Always release the bridge so the next reconnect attempt
            # starts from a clean USB state. Errors here are swallowed
            # because the device may already be gone.
            # Stop heartbeat first — it's the lowest-priority worker
            # and we want it gone before any bridge teardown so its
            # in-flight ping can drain cleanly.
            if self._heartbeat is not None:
                try:
                    self._heartbeat.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._heartbeat = None
            # Stop the live-view thread BEFORE closing the bridge so
            # its in-flight fetch can complete (or fail cleanly) while
            # the USB endpoint is still alive.
            if self._liveview is not None:
                try:
                    self._liveview.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._liveview = None
            if bridge is not None:
                try:
                    with self._ptp_lock:
                        bridge.close_session()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    bridge.close()
                except Exception:  # noqa: BLE001
                    pass

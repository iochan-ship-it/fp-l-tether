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

from fp_l_tether.camera.usb_bridge import (
    PTPError,
    USBBridge,
    USBBridgeError,
)
from fp_l_tether.config import AppConfig
from fp_l_tether.lightroom import build_destination
from fp_l_tether.telemetry import get_logger, log_shot
from fp_l_tether.transfer.atomic import write_atomic


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


ShotCallback = Callable[[ShotEvent], None]
StatusCallback = Callable[[StatusEvent], None]


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
        self._thread: threading.Thread | None = None
        self._shot_count = 0
        self._next_slot = 0  # camera's db_head — advances after each capture
        # Per-item shot counter: keeps filenames sane when switching items mid-session.
        # e.g. vase_0001..vase_0003, then bowl_0001..bowl_0003.
        self._current_item: str = cfg.output.default_item
        self._shots_by_item: dict[str, int] = {}
        self._item_lock = threading.Lock()

        self.on_shot: ShotCallback | None = None
        self.on_status: StatusCallback | None = None

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

    # ----- main loop ---------------------------------------------------

    def _run(self) -> None:
        """Worker thread entry point.

        Outer reconnect loop: if the USB bridge goes away (camera unplugged,
        powered off, or any libusb I/O error), close the bridge cleanly,
        wait, and try to re-establish. Loops until ``stop()`` is called.
        """
        reconnect_delay_s = 2.0
        first_attempt = True

        while not self._stop_event.is_set():
            if first_attempt:
                first_attempt = False
            else:
                # Wait between reconnect attempts; bail early if stopped
                if self._stop_event.wait(reconnect_delay_s):
                    break

            try:
                self._run_session()
            except (USBBridgeError, usb.core.USBError) as e:
                # USB-level error → camera gone or I/O broken. Reconnect.
                self.log.warning("session_lost", error=str(e))
                self._emit_status(
                    "disconnected",
                    f"接続切れ ({e}). 再接続を試行中…",
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
            bridge.open_session()
            self.log.info("session_opened")

            self._emit_status("initializing", "Running Sigma init sequence (10 PTP calls)…")
            bridge.sigma_init()
            self.log.info("init_complete")

            # Discover starting slot from camera state.
            # Note: we reset _next_slot from the camera's authoritative state
            # on every (re)connect, so resuming after an unplug picks up at
            # the correct slot rather than carrying stale state forward.
            pre = bridge.sigma_get_capture_status(0)
            self._next_slot = pre.image_db_head
            self.log.info("ready",
                          slot=self._next_slot,
                          db_head=pre.image_db_head,
                          db_tail=pre.image_db_tail)
            self._emit_status("ready", f"Watching slot 0x{self._next_slot:02X}")

            poll_idle_s = self.cfg.camera.poll_idle_ms / 1000.0
            poll_active_s = self.cfg.camera.poll_active_ms / 1000.0

            # Per-iteration trigger source. Starts as "camera_button" since
            # we begin in "watching" mode; flips to "pc_snap" the iteration
            # after a PC-side request is fired.
            pending_trigger: str = "camera_button"
            t_shot_start: float | None = None  # time the current pending shot started

            while not self._stop_event.is_set():
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
                    try:
                        # Re-arm PC capture mode (per fp trace) then fire
                        bridge.sigma_set_datagroup_3_pc_capture()
                        bridge.sigma_snap(
                            mode=self.cfg.camera.snap_mode,
                            amount=1,
                        )
                        pending_trigger = "pc_snap"
                        t_shot_start = time.monotonic()
                    except PTPError as e:
                        # Camera-side error (e.g. 0x6001 capture failure).
                        # Camera is still alive, just rejected this snap.
                        self.log.error("pc_snap_failed", error=str(e))
                        self._emit_status("error", str(e))
                        time.sleep(poll_idle_s)
                        continue
                    # USBBridgeError / usb.core.USBError propagates → reconnect

                # ----- 2. Single status poll for the active slot -----
                try:
                    status = bridge.sigma_get_capture_status(self._next_slot)
                except PTPError as e:
                    self.log.error("status_poll_failed", error=str(e))
                    self._emit_status("error", f"poll error: {e}")
                    time.sleep(poll_idle_s)
                    continue
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
                    continue

                # Image ready → download + write
                if status.capt_status in (0x0002, 0x0005):
                    self._emit_status("downloading", f"Slot 0x{self._next_slot:02X}")
                    try:
                        info, data = bridge.sigma_download_current(
                            status,
                            clear_strategy="image_db_head",
                        )
                    except PTPError as e:
                        self.log.error("download_failed", error=str(e))
                        self._emit_status("error", f"download error: {e}")
                        time.sleep(poll_idle_s)
                        continue
                    # USBBridgeError propagates → reconnect

                    self._shot_count += 1
                    # Per-item shot counter for the filename template's
                    # ``{shot}``. Resets implicitly when the user types a
                    # new item name in the panel.
                    with self._item_lock:
                        item = self._current_item
                        item_idx = self._shots_by_item.get(item, 0) + 1
                        self._shots_by_item[item] = item_idx
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
                        dest.session_dir.mkdir(parents=True, exist_ok=True)
                    saved = write_atomic(
                        dest.path,
                        data,
                        on_conflict=self.cfg.output.on_conflict,
                    )

                    elapsed = (time.monotonic() - t_shot_start) if t_shot_start else 0.0
                    event = ShotEvent(
                        shot_index=self._shot_count,
                        saved_path=saved,
                        size=len(data),
                        elapsed_s=elapsed,
                        image_id=status.image_id,
                        db_head=status.image_db_head,
                        db_tail=status.image_db_tail,
                        trigger=pending_trigger,
                    )
                    log_shot(
                        self.log,
                        filename=saved.name,
                        size=event.size,
                        elapsed_s=event.elapsed_s,
                        image_id=event.image_id,
                        slot=self._next_slot,
                        image_db_head=event.db_head,
                        image_db_tail=event.db_tail,
                        trigger=pending_trigger,
                    )
                    self._emit_shot(event)
                    self._emit_status("ready", f"Saved #{self._shot_count}: {saved.name}")

                    # Advance to next slot, reset trigger marker
                    self._next_slot = (self._next_slot + 1) & 0xFF
                    pending_trigger = "camera_button"
                    t_shot_start = None
                    # Tight loop — image may already be there for next shot
                    continue

                # No image yet → sleep & loop
                # Use active interval when we're waiting on a pending shot,
                # idle interval when just watching for a manual button press.
                interval = poll_active_s if t_shot_start else poll_idle_s
                time.sleep(interval)

        finally:
            # Always release the bridge so the next reconnect attempt
            # starts from a clean USB state. Errors here are swallowed
            # because the device may already be gone.
            if bridge is not None:
                try:
                    bridge.close_session()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    bridge.close()
                except Exception:  # noqa: BLE001
                    pass

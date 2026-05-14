"""Floating tether panel that hovers over Lightroom — even in fullscreen.

This is the Lightroom-internal-tether-bar replacement. A small always-on-top
NSPanel that shows current status and lets the user fire the shutter from
the keyboard.

Why NSPanel and not rumps/menubar?
  Lightroom in fullscreen mode hides the menubar. A regular NSWindow on a
  different macOS Space won't be visible. The fix is to use NSPanel with
  ``NSWindowCollectionBehaviorCanJoinAllSpaces`` +
  ``NSWindowCollectionBehaviorFullScreenAuxiliary`` so the panel "follows"
  whichever Space the user is on, including Lightroom's fullscreen Space.

UI:

    ┌────────────────────────────────────────────────┐
    │  ● Sigma fp L          ready                   │  ← status row
    │  Session: still_life_001                       │
    │  #42  SDIM0042.JPG  26.7 MB  5.1 MB/s         │  ← last-shot row
    │                                                │
    │           [ Shoot ]      [ Stop ]              │
    │                                                │
    │  Hotkey: Space = shoot  •  ⌘Q = quit          │
    └────────────────────────────────────────────────┘

Drag the title to reposition. Position persists in user prefs (future work).

Keyboard shortcuts:
  - Space — request_snap()
  - ⌘Q    — stop daemon + quit app

Run as part of ``fp-l-tether start`` (the CLI brings up the panel on the
main thread; daemon runs in a background thread).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import objc
from AppKit import (
    NSAlert,
    NSAlertFirstButtonReturn,
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSButton,
    NSColor,
    NSEvent,
    NSEventMaskKeyDown,
    NSFloatingWindowLevel,
    NSFont,
    NSImage,
    NSImageScaleProportionallyUpOrDown,
    NSImageView,
    NSMakeRect,
    NSMakeSize,
    NSPanel,
    NSPopUpButton,
    NSScreen,
    NSStatusWindowLevel,
    NSTextAlignmentCenter,
    NSTextField,
    NSTextView,
    NSTitledWindowMask,
    NSUtilityWindowMask,
    NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskNonactivatingPanel,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
    NSWindowStyleMaskUtilityWindow,
)
from Foundation import NSData, NSObject, NSTimer

from fp_l_tether.camera.sigma_datagroup import (
    apex_to_aperture,
    apex_to_iso,
    apex_to_shutter,
    image_quality_label,
    resolution_label,
    wb_label,
)

if TYPE_CHECKING:
    from fp_l_tether.config import AppConfig
    from fp_l_tether.transfer import TetherDaemon

logger = logging.getLogger(__name__)


PANEL_WIDTH = 320
# CONTROLS_HEIGHT is the height of the original (pre-LV) control area.
# Existing widget Y offsets are computed against this constant so that
# growing the panel to add a live-view viewport at the top doesn't
# require touching every NSMakeRect below.
#
# Bumped from 280 → 308 in Phase 3.5d to make room for a third
# exposure-dropdown row (Format + Size). All widgets anchored to the
# top of the controls area use ``CONTROLS_HEIGHT - N`` so they shift
# down with the bump; widgets anchored to the bottom (item field,
# buttons, hint) use absolute y values and stay put. The shot label
# at ``CONTROLS_HEIGHT - 166`` lands at the same absolute pixel row
# as before so its alignment with the bottom-anchored widgets is
# preserved.
CONTROLS_HEIGHT = 308
# Live-view viewport — placed above the controls area. Sized for the
# fp L's 3:2 capture ratio so frames don't distort when scaled to fit.
LV_WIDTH = 220
LV_HEIGHT = 140  # 220x140 ≈ 3:1.9 — close to 3:2, avoids letterboxing
LV_PAD_TOP = 12
LV_PAD_BOTTOM = 10
PANEL_HEIGHT = CONTROLS_HEIGHT + LV_PAD_BOTTOM + LV_HEIGHT + LV_PAD_TOP

# AF marker dimensions (px) — the on-LV reticle shown at the current
# AF point. The camera's AF coord bounds are dynamic (populated from
# CamCanSetInfo5 on connect, fp L V90 defaults via FloatingTetherPanel.af_bounds).
AF_MARKER_SIZE = 28


class FloatingTetherPanel(NSObject):
    """A small NSPanel that floats above Lightroom (even in fullscreen).

    Construct on the **main thread** AFTER NSApplication has been initialized
    (Typer's CLI does this for us via ``run_forever``).
    """

    # PyObjC requires __new__ for NSObject subclasses; init done in _init_with_daemon
    def __new__(cls, daemon=None, cfg=None):  # type: ignore[no-untyped-def]
        if daemon is None:  # bare alloc-init path (called by ObjC runtime)
            self = objc.super(FloatingTetherPanel, cls).new()
            return self
        self = objc.super(FloatingTetherPanel, cls).new()
        self._init_with_daemon(daemon, cfg)
        return self

    # ----- python-side setup ------------------------------------------

    def _init_with_daemon(self, daemon: "TetherDaemon", cfg: "AppConfig") -> None:
        self._daemon = daemon
        self._cfg = cfg
        self._app = NSApplication.sharedApplication()
        # Accessory app: doesn't show a Dock icon, no main menu — perfect
        # for an overlay tool.
        self._app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

        # Controls-busy gate (Fix 4, 2026-05-13): while the daemon
        # reports state in ("shooting", "downloading"), Shoot / AF /
        # AF Point buttons + all exposure dropdowns are greyed out
        # and the LV overlay is force-shown. Pressing controls during
        # commit window queues requests that fire into a busy camera
        # — the 2026-05-13 endurance run showed this cascading into
        # FW wedge. Dimming gives the user visual feedback to wait.
        self._controls_busy = False

        self._build_window()
        self._wire_callbacks()
        self._install_hotkeys()
        self._install_lv_staleness_watch()

    # ----- window construction ----------------------------------------

    def _build_window(self) -> None:
        screen = NSScreen.mainScreen()
        frame = screen.visibleFrame()
        # Position top-right by default
        origin_x = frame.origin.x + frame.size.width - PANEL_WIDTH - 20
        origin_y = frame.origin.y + frame.size.height - PANEL_HEIGHT - 20

        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskUtilityWindow
            | NSWindowStyleMaskNonactivatingPanel
        )
        rect = NSMakeRect(origin_x, origin_y, PANEL_WIDTH, PANEL_HEIGHT)
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False
        )
        panel.setTitle_("fp L tether")
        panel.setLevel_(NSStatusWindowLevel)  # higher than floating, beats fullscreen
        panel.setOpaque_(False)
        panel.setHasShadow_(True)
        panel.setMovableByWindowBackground_(True)

        # Make it follow the user across Spaces and stay over fullscreen apps
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorStationary
        )

        content = panel.contentView()

        # Live-view viewport (top of the panel, above the controls area).
        # 3.3a only constructs the view and wires the callback; the
        # JPEG → NSImage conversion + setImage_ marshalling lands in
        # 3.3b. Until then the view shows an empty frame, but the
        # callback fires (verifiable via logs) so we know the pipe is
        # connected end-to-end.
        lv_x = (PANEL_WIDTH - LV_WIDTH) // 2
        lv_y = CONTROLS_HEIGHT + LV_PAD_BOTTOM
        # LiveViewImageView is a click-aware NSImageView subclass. Click
        # anywhere on the rendered LV → maps view-local (x, y) to camera
        # AF coordinates via panel.af_bounds() and pushes through
        # panel.commit_focus_point(). This replaces the AF popover as
        # the primary way to set the AF point (the popover stays for
        # users who want the numeric coord readout / 3×3 snap mode).
        self._live_view = LiveViewImageView.alloc().initWithFrame_(
            NSMakeRect(lv_x, lv_y, LV_WIDTH, LV_HEIGHT)
        )
        self._live_view.setOwner_(self)
        self._live_view.setEditable_(False)
        # Proportional scaling so non-3:2 frames (e.g. cropped sensor
        # modes) don't distort.
        self._live_view.setImageScaling_(NSImageScaleProportionallyUpOrDown)
        # Dark backdrop so the viewport reads as "screen" before the
        # first frame arrives (and during pause windows in 3.3c).
        self._live_view.setWantsLayer_(True)
        layer = self._live_view.layer()
        if layer is not None:
            layer.setBackgroundColor_(
                NSColor.colorWithCalibratedWhite_alpha_(0.08, 1.0).CGColor()
            )
        content.addSubview_(self._live_view)

        # Pause overlay — a semi-transparent dark veil with a centered
        # "Saving…" label, sitting exactly on top of the LV image.
        # Initially hidden; a 250 ms staleness watchdog (installed in
        # _install_lv_staleness_watch) toggles it based on how long
        # since the last frame arrived. This is what tells the user
        # "the camera is mid-snap, the freeze is expected" so they
        # don't think the app crashed during the ~5 s DNG drain.
        self._lv_overlay = NSView.alloc().initWithFrame_(
            NSMakeRect(lv_x, lv_y, LV_WIDTH, LV_HEIGHT)
        )
        self._lv_overlay.setWantsLayer_(True)
        overlay_layer = self._lv_overlay.layer()
        if overlay_layer is not None:
            overlay_layer.setBackgroundColor_(
                NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.55).CGColor()
            )
        self._lv_overlay.setHidden_(True)
        content.addSubview_(self._lv_overlay)

        # "Saving…" label — vertically centered inside the overlay.
        # Coords are relative to the overlay view (its origin is
        # (lv_x, lv_y), so the label rect is in overlay-local space).
        self._lv_overlay_label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(0, (LV_HEIGHT - 24) // 2, LV_WIDTH, 24)
        )
        _make_label(self._lv_overlay_label, "Saving…", bold=True, size=15)
        self._lv_overlay_label.setAlignment_(NSTextAlignmentCenter)
        self._lv_overlay_label.setTextColor_(NSColor.whiteColor())
        self._lv_overlay.addSubview_(self._lv_overlay_label)

        # AF marker — a small yellow-amber reticle drawn on top of the
        # LV showing the camera's current AF point. Added AFTER the
        # _lv_overlay so it sits higher in z-order; we hide it
        # explicitly whenever the overlay shows (the pause-state
        # semantic is "stale info, don't trust this view"). Initial
        # frame is placed at the LV centre as a sensible default
        # until the first focus-point event arrives.
        self._af_marker_view = NSView.alloc().initWithFrame_(
            NSMakeRect(
                lv_x + (LV_WIDTH - AF_MARKER_SIZE) // 2,
                lv_y + (LV_HEIGHT - AF_MARKER_SIZE) // 2,
                AF_MARKER_SIZE,
                AF_MARKER_SIZE,
            )
        )
        self._af_marker_view.setWantsLayer_(True)
        marker_layer = self._af_marker_view.layer()
        if marker_layer is not None:
            marker_layer.setBorderColor_(
                NSColor.colorWithCalibratedRed_green_blue_alpha_(
                    1.0, 0.78, 0.0, 1.0
                ).CGColor()
            )
            marker_layer.setBorderWidth_(2.0)
            marker_layer.setBackgroundColor_(NSColor.clearColor().CGColor())
            marker_layer.setCornerRadius_(2.0)
        # Hidden until the first focus-point event lands — avoids a
        # stray reticle at the LV centre before we know where the
        # camera actually has its AF point.
        self._af_marker_view.setHidden_(True)
        content.addSubview_(self._af_marker_view)

        # Staleness watchdog state — overlay shows when no LV frame
        # arrived for ``_lv_stale_threshold_s``. At 10 fps target the
        # frame interval is ~100 ms, so 500 ms is roughly "5 missed
        # frames" — generous enough to not flash on a single hiccup.
        self._lv_last_frame_at: float = 0.0
        self._lv_stale_threshold_s: float = 0.5
        self._lv_stale_timer = None  # set in _install_lv_staleness_watch

        # Status indicator label (top of the controls area — LV viewport
        # sits ABOVE this and uses its own y range so all existing
        # widgets stay at their original on-panel coordinates).
        self._status_label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(12, CONTROLS_HEIGHT - 38, PANEL_WIDTH - 24, 20)
        )
        _make_label(self._status_label, "● connecting…", bold=True, size=13)
        content.addSubview_(self._status_label)

        # Session label
        self._session_label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(12, CONTROLS_HEIGHT - 60, PANEL_WIDTH - 24, 18)
        )
        _make_label(self._session_label, f"Session: {self._daemon.session_name}", size=11)
        self._session_label.setTextColor_(NSColor.secondaryLabelColor())
        content.addSubview_(self._session_label)

        # Exposure dropdowns row 1: ISO + Shutter
        # Each NSPopUpButton's menu is populated on first CanSetInfoEvent.
        # Selection fires _dropdown_changed which queues a SetDataGroup
        # write via the daemon; the UI is then re-synced from the
        # subsequent ExposureEvent (no optimistic update).
        iso_caption = NSTextField.alloc().initWithFrame_(
            NSMakeRect(12, CONTROLS_HEIGHT - 82, 28, 18)
        )
        _make_label(iso_caption, "ISO", size=10)
        iso_caption.setTextColor_(NSColor.secondaryLabelColor())
        content.addSubview_(iso_caption)
        self._iso_dropdown = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(42, CONTROLS_HEIGHT - 86, 110, 24), False
        )
        self._iso_dropdown.setTarget_(self)
        self._iso_dropdown.setAction_("isoChanged:")
        content.addSubview_(self._iso_dropdown)

        ss_caption = NSTextField.alloc().initWithFrame_(
            NSMakeRect(160, CONTROLS_HEIGHT - 82, 28, 18)
        )
        _make_label(ss_caption, "SS", size=10)
        ss_caption.setTextColor_(NSColor.secondaryLabelColor())
        content.addSubview_(ss_caption)
        self._ss_dropdown = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(186, CONTROLS_HEIGHT - 86, 122, 24), False
        )
        self._ss_dropdown.setTarget_(self)
        self._ss_dropdown.setAction_("ssChanged:")
        content.addSubview_(self._ss_dropdown)

        # Exposure dropdowns row 2: Aperture + WB
        av_caption = NSTextField.alloc().initWithFrame_(
            NSMakeRect(12, CONTROLS_HEIGHT - 110, 28, 18)
        )
        _make_label(av_caption, "Av", size=10)
        av_caption.setTextColor_(NSColor.secondaryLabelColor())
        content.addSubview_(av_caption)
        self._av_dropdown = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(42, CONTROLS_HEIGHT - 114, 110, 24), False
        )
        self._av_dropdown.setTarget_(self)
        self._av_dropdown.setAction_("avChanged:")
        content.addSubview_(self._av_dropdown)

        wb_caption = NSTextField.alloc().initWithFrame_(
            NSMakeRect(160, CONTROLS_HEIGHT - 110, 28, 18)
        )
        _make_label(wb_caption, "WB", size=10)
        wb_caption.setTextColor_(NSColor.secondaryLabelColor())
        content.addSubview_(wb_caption)
        self._wb_dropdown = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(186, CONTROLS_HEIGHT - 114, 122, 24), False
        )
        self._wb_dropdown.setTarget_(self)
        self._wb_dropdown.setAction_("wbChanged:")
        content.addSubview_(self._wb_dropdown)

        # Exposure dropdowns row 3: Format (DG2.ImageQuality) + Size
        # (DG2.Resolution). Same two-column layout + spacing as the
        # first two rows. Both write through ``request_set_exposure
        # (group=2, ...)`` so the existing read-back / re-emit cycle
        # in the daemon validates whatever the camera actually accepts.
        fmt_caption = NSTextField.alloc().initWithFrame_(
            NSMakeRect(12, CONTROLS_HEIGHT - 138, 36, 18)
        )
        _make_label(fmt_caption, "Fmt", size=10)
        fmt_caption.setTextColor_(NSColor.secondaryLabelColor())
        content.addSubview_(fmt_caption)
        self._fmt_dropdown = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(42, CONTROLS_HEIGHT - 142, 110, 24), False
        )
        self._fmt_dropdown.setTarget_(self)
        self._fmt_dropdown.setAction_("fmtChanged:")
        content.addSubview_(self._fmt_dropdown)

        size_caption = NSTextField.alloc().initWithFrame_(
            NSMakeRect(160, CONTROLS_HEIGHT - 138, 28, 18)
        )
        _make_label(size_caption, "Size", size=10)
        size_caption.setTextColor_(NSColor.secondaryLabelColor())
        content.addSubview_(size_caption)
        self._size_dropdown = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(186, CONTROLS_HEIGHT - 142, 122, 24), False
        )
        self._size_dropdown.setTarget_(self)
        self._size_dropdown.setAction_("sizeChanged:")
        content.addSubview_(self._size_dropdown)

        # Last-shot label
        self._shot_label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(12, CONTROLS_HEIGHT - 166, PANEL_WIDTH - 24, 18)
        )
        _make_label(self._shot_label, "No shots yet", size=11)
        self._shot_label.setTextColor_(NSColor.secondaryLabelColor())
        content.addSubview_(self._shot_label)

        # CanSetInfo (allowed values) — captured on connect. Used to
        # populate dropdowns and to bound the AF popover's coord mapping.
        self._can_set_info = None
        # Track latest exposure bytes so we can re-select the matching
        # dropdown item without firing the action accidentally.
        self._exposure_raw: dict[str, int] = {}
        # Current AF point (for the on-LV marker). None ⇒ unknown.
        self._focus_xy: tuple[int, int] | None = None
        # Whether the user is currently changing a dropdown — guards
        # against re-syncing-back during the action callback.
        self._suppress_action = False

        # Item row: "Item:" label + editable text field.
        # Typing here changes ``{item}`` in the filename template and resets
        # the per-item shot counter to 1 on the next shot.
        item_caption = NSTextField.alloc().initWithFrame_(
            NSMakeRect(12, 96, 40, 22)
        )
        _make_label(item_caption, "Item:", size=11)
        item_caption.setTextColor_(NSColor.secondaryLabelColor())
        content.addSubview_(item_caption)

        self._item_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(56, 94, PANEL_WIDTH - 68, 22)
        )
        self._item_field.setStringValue_(self._daemon.current_item)
        self._item_field.setFont_(NSFont.systemFontOfSize_(12))
        self._item_field.setBezeled_(True)
        self._item_field.setEditable_(True)
        self._item_field.setSelectable_(True)
        # Fires on Return / Enter — commits text to the daemon.
        self._item_field.setTarget_(self)
        self._item_field.setAction_("itemCommitted:")
        content.addSubview_(self._item_field)

        # Shoot button
        self._shoot_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(12, 58, 80, 28)
        )
        self._shoot_btn.setTitle_("Shoot ⎵")
        self._shoot_btn.setBezelStyle_(NSBezelStyleRounded)
        self._shoot_btn.setTarget_(self)
        self._shoot_btn.setAction_("shootClicked:")
        content.addSubview_(self._shoot_btn)

        # AF button (drive AF only, no capture — SnapCommand mode 3).
        # Phase 3.9 (2026-05-13): widened to fill the slot vacated by
        # the removed "AF Point ⊞" popover button — AF point is now
        # set by clicking on the LV, with the current point shown as
        # a yellow reticle (see _af_marker_view).
        self._af_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(98, 58, 132, 28)
        )
        self._af_btn.setTitle_("AF")
        self._af_btn.setBezelStyle_(NSBezelStyleRounded)
        self._af_btn.setTarget_(self)
        self._af_btn.setAction_("afClicked:")
        content.addSubview_(self._af_btn)

        # Bottom row: New Session + Stop
        self._new_session_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(12, 28, 72, 26)
        )
        self._new_session_btn.setTitle_("New…")
        self._new_session_btn.setBezelStyle_(NSBezelStyleRounded)
        self._new_session_btn.setTarget_(self)
        self._new_session_btn.setAction_("newSessionClicked:")
        content.addSubview_(self._new_session_btn)

        self._quit_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(248, 28, 60, 26)
        )
        self._quit_btn.setTitle_("Stop")
        self._quit_btn.setBezelStyle_(NSBezelStyleRounded)
        self._quit_btn.setTarget_(self)
        self._quit_btn.setAction_("quitClicked:")
        content.addSubview_(self._quit_btn)

        # Hint footer
        hint = NSTextField.alloc().initWithFrame_(
            NSMakeRect(12, 6, PANEL_WIDTH - 24, 16)
        )
        _make_label(hint, "Space = shoot  •  A = AF  •  ⌘Q = quit", size=10)
        hint.setTextColor_(NSColor.tertiaryLabelColor())
        content.addSubview_(hint)

        panel.orderFrontRegardless()
        self._panel = panel

    # ----- daemon callback wiring -------------------------------------

    def _wire_callbacks(self) -> None:
        # All callbacks are invoked from the daemon's background thread,
        # so we marshal back to the main thread with performSelectorOnMainThread.
        self._daemon.on_status = self._on_status_threadsafe
        self._daemon.on_shot = self._on_shot_threadsafe
        self._daemon.on_exposure = self._on_exposure_threadsafe
        self._daemon.on_can_set_info = self._on_can_set_info_threadsafe
        self._daemon.on_focus_point = self._on_focus_point_threadsafe
        self._daemon.on_live_frame = self._on_live_frame_threadsafe

    def _on_status_threadsafe(self, event) -> None:  # type: ignore[no-untyped-def]
        # Cross-thread call → marshal to main thread
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "updateStatus:",
            (event.state, event.message),
            False,
        )

    def _on_shot_threadsafe(self, event) -> None:  # type: ignore[no-untyped-def]
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "updateShot:",
            (event.shot_index, event.saved_path.name, event.size, event.mbps),
            False,
        )

    def _on_exposure_threadsafe(self, event) -> None:  # type: ignore[no-untyped-def]
        s = event.settings
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "updateExposure:",
            (
                s.iso_raw,
                s.iso_auto_raw,
                s.shutter_raw,
                s.aperture_raw,
                s.wb_raw,
                s.file_format_raw,
                s.image_size_raw,
            ),
            False,
        )

    def _on_can_set_info_threadsafe(self, event) -> None:  # type: ignore[no-untyped-def]
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "updateCanSetInfo:",
            (event.info,),
            False,
        )

    def _on_focus_point_threadsafe(self, event) -> None:  # type: ignore[no-untyped-def]
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "updateFocusPoint:",
            (event.x, event.y),
            False,
        )

    def _on_live_frame_threadsafe(self, event) -> None:  # type: ignore[no-untyped-def]
        """Marshal a live-view frame from the LV thread to the main thread.

        Called from ``LiveViewStream`` (via ``TetherDaemon._emit_live_frame``)
        every 100 ms or so. We hand a single-element tuple to
        ``performSelectorOnMainThread_`` because ObjC selectors take
        exactly one ``id``-typed argument.

        3.3a wiring: ``updateLiveFrame_`` is a stub. 3.3b will turn
        ``event.jpeg`` into an NSImage and call ``setImage_`` on
        ``self._live_view``.
        """
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "updateLiveFrame:",
            (event.jpeg, event.width, event.height),
            False,
        )

    # ----- ObjC-callable updates (main thread only) -------------------

    @objc.signature(b"v@:@")
    def updateStatus_(self, tup) -> None:
        state, message = tup
        symbol = {
            "connecting": "○",
            "initializing": "◐",
            "ready": "●",
            "shooting": "◉",
            "downloading": "◑",
            "recovering": "◍",
            "error": "✕",
            "stopped": "○",
            "disconnected": "✕",
            "focusing": "◐",
        }.get(state, "●")
        text = f"{symbol} {state}"
        if message:
            text += f"  —  {message}"
        self._status_label.setStringValue_(text)

        # Colour priority: ready=green, error/disconnected=red,
        # recovering=yellow (mid-wake-up), everything else=label colour.
        if state == "ready":
            color = NSColor.systemGreenColor()
        elif state in ("error", "disconnected"):
            color = NSColor.systemRedColor()
        elif state == "recovering":
            color = NSColor.systemYellowColor()
        else:
            color = NSColor.labelColor()
        self._status_label.setTextColor_(color)

        # ----- Fix 4: dim controls during commit window -----
        # shooting / downloading / recovering = camera is mid-cycle
        # or quiet, button presses just queue. ready / error /
        # stopped / disconnected = safe to interact again. Other
        # states (connecting / initializing / focusing) leave the
        # current dim state alone — focusing is sub-second so we
        # don't flash on every AF.
        #
        # Phase 3.6 (2026-05-13): "recovering" added to the busy set
        # so the LV overlay forces up and dropdowns gray out while
        # _passive_idle_wait is running. Without this the user sees
        # a stale LV frame and can keep mashing buttons into a
        # half-stalled endpoint.
        if state in ("shooting", "downloading", "recovering"):
            self._set_controls_busy(True)
        elif state in ("ready", "error", "stopped", "disconnected"):
            self._set_controls_busy(False)

    def _set_controls_busy(self, busy: bool) -> None:
        """Toggle interactive controls based on capture-busy state.

        Disables Shoot / AF + all six exposure dropdowns when
        ``busy=True`` (state in shooting/downloading) and re-enables
        them on ``busy=False``. Also force-shows the LV overlay
        during busy so the user sees "Saving…" instantly instead of
        waiting ~500 ms for the staleness watchdog, and hides the
        AF reticle (its position info is stale until LV resumes).

        When un-busying, we DON'T force-hide the overlay — the
        staleness watchdog still owns the post-snap visual transition
        (overlay stays until a fresh LV frame lands, which is the
        right "camera is back online" cue).
        """
        if self._controls_busy == busy:
            return
        self._controls_busy = busy
        enabled = not busy
        for btn in (self._shoot_btn, self._af_btn):
            btn.setEnabled_(enabled)
        for dd in (
            self._iso_dropdown,
            self._ss_dropdown,
            self._av_dropdown,
            self._wb_dropdown,
            self._fmt_dropdown,
            self._size_dropdown,
        ):
            dd.setEnabled_(enabled)
        if busy:
            # Force the overlay up immediately so the user gets
            # feedback without waiting for the 500 ms staleness
            # threshold. Reticle hides with the overlay — re-shown
            # on the next focus-point event or when LV resumes.
            self._lv_overlay.setHidden_(False)
            self._af_marker_view.setHidden_(True)

    @objc.signature(b"v@:@")
    def updateShot_(self, tup) -> None:
        idx, name, size, mbps = tup
        size_mb = size / 1024 / 1024
        self._shot_label.setStringValue_(
            f"#{idx}  {name}  ·  {size_mb:.1f} MB  ·  {mbps:.1f} MB/s"
        )
        self._shot_label.setTextColor_(NSColor.labelColor())

    @objc.signature(b"v@:@")
    def updateExposure_(self, tup) -> None:
        """Sync the dropdowns to the camera's reported exposure.

        The action callbacks set ``_suppress_action`` so this method
        doesn't recursively re-queue writes when it programmatically
        selects items.
        """
        iso_raw, iso_auto, ss_raw, av_raw, wb_raw, fmt_raw, size_raw = tup
        self._exposure_raw = {
            "ISOSpeed": iso_raw,
            "ISOAuto": iso_auto,
            "ShutterSpeed": ss_raw,
            "Aperture": av_raw,
            "WhiteBalance": wb_raw,
            "ImageQuality": fmt_raw,
            "Resolution": size_raw,
        }
        self._suppress_action = True
        try:
            # ISO dropdown — represented value is the raw byte; "Auto"
            # is a sentinel string. We store the code in the menu item's
            # representedObject.
            if iso_auto:
                self._select_dropdown_by_repr(self._iso_dropdown, "auto")
            else:
                self._select_dropdown_by_repr(self._iso_dropdown, iso_raw)
            self._select_dropdown_by_repr(self._ss_dropdown, ss_raw)
            self._select_dropdown_by_repr(self._av_dropdown, av_raw)
            self._select_dropdown_by_repr(self._wb_dropdown, wb_raw)
            self._select_dropdown_by_repr(self._fmt_dropdown, fmt_raw)
            self._select_dropdown_by_repr(self._size_dropdown, size_raw)
        finally:
            self._suppress_action = False

    @objc.signature(b"v@:@")
    def updateCanSetInfo_(self, tup) -> None:
        """Populate dropdown menus from a CamCanSetInfo5 snapshot."""
        (info,) = tup
        self._can_set_info = info
        self._suppress_action = True
        try:
            # ISO: prepend "Auto" sentinel, then manual ISO codes (descending
            # so the largest comes first — matches camera dial direction).
            iso_codes = list(info.iso_manual_codes) or [
                0x18, 0x20, 0x28, 0x30, 0x38, 0x40, 0x48, 0x50,
            ]
            iso_items: list[tuple[str, object]] = [("Auto", "auto")]
            for code in iso_codes:
                iso_items.append((apex_to_iso(code).replace("ISO ", ""), code))
            self._fill_dropdown(self._iso_dropdown, iso_items)

            # Shutter speed
            ss_codes = info.shutter_codes or list(range(0x18, 0xA8, 2))
            self._fill_dropdown(
                self._ss_dropdown,
                [(apex_to_shutter(c), c) for c in ss_codes],
            )

            # Aperture
            self._fill_dropdown(
                self._av_dropdown,
                [(apex_to_aperture(c), c) for c in info.aperture_codes],
            )

            # White balance
            wb_codes = info.wb_codes or list(range(0, 12))
            self._fill_dropdown(
                self._wb_dropdown,
                [(wb_label(c), c) for c in wb_codes],
            )

            # Format (ImageQuality) + Size (Resolution). Lists default
            # to the full sigma_ptpy enum when the camera reports
            # empty CamCanSetInfo5 entries (fp L V90 observed).
            self._fill_dropdown(
                self._fmt_dropdown,
                [(image_quality_label(c), c) for c in info.image_quality_codes],
            )
            self._fill_dropdown(
                self._size_dropdown,
                [(resolution_label(c), c) for c in info.resolution_codes],
            )
        finally:
            self._suppress_action = False

        # Re-sync selection to the last known exposure now that items exist.
        if self._exposure_raw:
            self.updateExposure_(
                (
                    self._exposure_raw.get("ISOSpeed", 0),
                    self._exposure_raw.get("ISOAuto", 0),
                    self._exposure_raw.get("ShutterSpeed", 0),
                    self._exposure_raw.get("Aperture", 0),
                    self._exposure_raw.get("WhiteBalance", 0),
                    self._exposure_raw.get("ImageQuality", 0),
                    self._exposure_raw.get("Resolution", 0),
                )
            )

    @objc.signature(b"v@:@")
    def updateFocusPoint_(self, tup) -> None:
        x, y = tup
        if x is None or y is None:
            self._focus_xy = None
            self._af_marker_view.setHidden_(True)
            return
        self._focus_xy = (int(x), int(y))
        self._reposition_af_marker()

    def _reposition_af_marker(self) -> None:
        """Move the AF reticle to the current ``_focus_xy`` on the LV.

        Hides the marker if the LV overlay is up (camera mid-snap),
        otherwise sets the frame to centre on the mapped view coord
        and shows it. Called from ``updateFocusPoint_`` and from the
        LV resume path.
        """
        if self._focus_xy is None:
            self._af_marker_view.setHidden_(True)
            return
        if self._controls_busy:
            # Stale-info gate — reticle reappears once LV resumes.
            self._af_marker_view.setHidden_(True)
            return
        cam_x, cam_y = self._focus_xy
        vx, vy = self.cam_to_view(cam_x, cam_y)
        frame = NSMakeRect(
            vx - AF_MARKER_SIZE / 2.0,
            vy - AF_MARKER_SIZE / 2.0,
            AF_MARKER_SIZE,
            AF_MARKER_SIZE,
        )
        self._af_marker_view.setFrame_(frame)
        self._af_marker_view.setHidden_(False)

    def cam_to_view(self, cam_x: int, cam_y: int) -> tuple[float, float]:
        """Map a camera AF coord to LV-superview coordinates.

        Returns (x, y) in the panel's content-view coordinate space
        (so the marker view's frame can be set directly to a rect
        centred there). AppKit Y goes up from bottom-left; camera Y
        goes down from top-left → we flip Y.
        """
        x_min, x_max, y_min, y_max = self.af_bounds()
        nx = (cam_x - x_min) / max(1, (x_max - x_min))
        ny = (cam_y - y_min) / max(1, (y_max - y_min))
        nx = max(0.0, min(1.0, nx))
        ny = max(0.0, min(1.0, ny))
        # LV frame origin in superview coords:
        lv_x = (PANEL_WIDTH - LV_WIDTH) // 2
        lv_y = CONTROLS_HEIGHT + LV_PAD_BOTTOM
        vx = lv_x + nx * LV_WIDTH
        vy = lv_y + (1.0 - ny) * LV_HEIGHT  # flip Y
        return vx, vy

    @objc.signature(b"v@:@")
    def updateLiveFrame_(self, tup) -> None:
        """Decode a JPEG live-view frame and push it into the viewport.

        Runs on the main thread (marshalled via
        ``performSelectorOnMainThread_``). NSImage construction and
        setImage_ are both main-thread-only on AppKit.

        Decoding is cheap (~1–2 ms for 760 KB JPEG on Apple Silicon)
        so we do it inline rather than offloading. If profiling later
        shows it's too heavy, switch to a CIImage / CGImageSource
        pipeline or pre-decode on the LV thread.
        """
        jpeg, width, height = tup
        # Cheap aliveness counter — useful from the debugger / a future
        # debug overlay.
        self._lv_frame_count = getattr(self, "_lv_frame_count", 0) + 1
        # Record arrival time for the staleness watchdog. As soon as
        # a frame lands we know LV is alive, so hide the overlay
        # immediately rather than waiting for the next timer tick.
        self._lv_last_frame_at = time.monotonic()
        # Fix 4: don't hide while the daemon is mid-snap — busy state
        # owns the overlay during commit window. The LV pause should
        # mean no frames arrive anyway, but a stray late frame from
        # before the pause shouldn't flicker the "Saving…" off.
        if not self._lv_overlay.isHidden() and not self._controls_busy:
            self._lv_overlay.setHidden_(True)
            # Overlay just hid → restore the AF reticle if we have
            # a known focus point.
            self._reposition_af_marker()

        # Wrap the Python bytes in an NSData. PyObjC can usually pass
        # bytes through transparently, but going via NSData avoids a
        # bytes-copy ambiguity in some PyObjC versions and is what
        # Apple's sample code uses.
        ns_data = NSData.dataWithBytes_length_(jpeg, len(jpeg))
        image = NSImage.alloc().initWithData_(ns_data)
        if image is None:
            # Malformed JPEG (rare — the camera occasionally emits a
            # truncated frame during mode transitions). Keep the
            # previous image up so the viewport doesn't flicker.
            return
        self._live_view.setImage_(image)

    # ----- dropdown helpers -------------------------------------------

    def _fill_dropdown(
        self,
        dropdown,  # type: ignore[no-untyped-def]
        items: list[tuple[str, object]],
    ) -> None:
        """Replace dropdown's items with ``(title, representedObject)`` pairs."""
        dropdown.removeAllItems()
        for title, repr_obj in items:
            dropdown.addItemWithTitle_(title)
            menu_item = dropdown.lastItem()
            menu_item.setRepresentedObject_(repr_obj)

    def _select_dropdown_by_repr(self, dropdown, target) -> None:  # type: ignore[no-untyped-def]
        """Select the first menu item whose representedObject == target.

        No-op if no item matches — keeps current selection so the UI
        doesn't jump while the dropdown is being populated.
        """
        for i in range(dropdown.numberOfItems()):
            item = dropdown.itemAtIndex_(i)
            if item.representedObject() == target:
                dropdown.selectItemAtIndex_(i)
                return

    def _selected_repr(self, dropdown):  # type: ignore[no-untyped-def]
        item = dropdown.selectedItem()
        if item is None:
            return None
        return item.representedObject()

    # ----- button actions ---------------------------------------------

    @objc.signature(b"v@:@")
    def shootClicked_(self, sender) -> None:
        self._daemon.request_snap()

    @objc.signature(b"v@:@")
    def afClicked_(self, sender) -> None:
        self._daemon.request_af()

    # --- exposure dropdown actions ---------------------------------

    @objc.signature(b"v@:@")
    def isoChanged_(self, sender) -> None:
        if self._suppress_action:
            return
        repr_obj = self._selected_repr(sender)
        if repr_obj == "auto":
            # Engage auto ISO. ISOAuto is in DG1.
            self._daemon.request_set_exposure(1, {"ISOAuto": 1})
        elif isinstance(repr_obj, int):
            # Manual ISO — switch off auto + set the byte. Send both in
            # one DG1 write so the camera doesn't transiently use the
            # previous manual ISO with auto disabled.
            self._daemon.request_set_exposure(
                1, {"ISOAuto": 0, "ISOSpeed": int(repr_obj)}
            )

    @objc.signature(b"v@:@")
    def ssChanged_(self, sender) -> None:
        if self._suppress_action:
            return
        repr_obj = self._selected_repr(sender)
        if isinstance(repr_obj, int):
            self._daemon.request_set_exposure(
                1, {"ShutterSpeed": int(repr_obj)}
            )

    @objc.signature(b"v@:@")
    def avChanged_(self, sender) -> None:
        if self._suppress_action:
            return
        repr_obj = self._selected_repr(sender)
        if isinstance(repr_obj, int):
            self._daemon.request_set_exposure(
                1, {"Aperture": int(repr_obj)}
            )

    @objc.signature(b"v@:@")
    def wbChanged_(self, sender) -> None:
        if self._suppress_action:
            return
        repr_obj = self._selected_repr(sender)
        if isinstance(repr_obj, int):
            # WhiteBalance lives in DG2.
            self._daemon.request_set_exposure(
                2, {"WhiteBalance": int(repr_obj)}
            )

    @objc.signature(b"v@:@")
    def fmtChanged_(self, sender) -> None:
        """Picture file format (DG2.ImageQuality). DNG variants ignore Size.

        The daemon's read-back / re-emit cycle (200 ms after the write)
        will refresh the dropdown to the value the camera actually
        committed — so if the camera rejects a particular code (e.g.
        the lens is in a state where DNG isn't allowed) the UI snaps
        back to the previous value rather than silently lying.
        """
        if self._suppress_action:
            return
        repr_obj = self._selected_repr(sender)
        if isinstance(repr_obj, int):
            self._daemon.request_set_exposure(
                2, {"ImageQuality": int(repr_obj)}
            )

    @objc.signature(b"v@:@")
    def sizeChanged_(self, sender) -> None:
        """Image size (DG2.Resolution: L/M/S, applies to JPG modes)."""
        if self._suppress_action:
            return
        repr_obj = self._selected_repr(sender)
        if isinstance(repr_obj, int):
            self._daemon.request_set_exposure(
                2, {"Resolution": int(repr_obj)}
            )

    # --- AF point (LV click → camera) -------------------------------

    def commit_focus_point(self, cam_x: int, cam_y: int) -> None:
        """Push a new AF point to the camera (called from LV mouse handler)."""
        # Clamp to the camera-reported bounds so we never send out-of-range.
        info = self._can_set_info
        if info is not None:
            cam_x = max(info.af_x_min, min(info.af_x_max, cam_x))
            cam_y = max(info.af_y_min, min(info.af_y_max, cam_y))
        self._focus_xy = (cam_x, cam_y)
        self._daemon.request_set_focus_point(cam_x, cam_y)
        # Move the reticle immediately to give feedback before the
        # focus-point event round-trips back from the camera.
        self._reposition_af_marker()

    def af_bounds(self) -> tuple[int, int, int, int]:
        """Return ``(x_min, x_max, y_min, y_max)`` for the AF coordinate system."""
        info = self._can_set_info
        if info is None:
            return 96, 928, 85, 597
        return info.af_x_min, info.af_x_max, info.af_y_min, info.af_y_max

    def current_focus_xy(self) -> tuple[int, int] | None:
        return self._focus_xy

    @objc.signature(b"v@:@")
    def newSessionClicked_(self, sender) -> None:
        """Prompt for a new session name; reset counters on confirm."""
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Start new session")
        alert.setInformativeText_(
            "Resets shot counters. Leave blank for an auto timestamp name."
        )
        alert.addButtonWithTitle_("Start")
        alert.addButtonWithTitle_("Cancel")

        input_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(0, 0, 240, 24)
        )
        input_field.setStringValue_("")
        alert.setAccessoryView_(input_field)

        # Make sure the modal sits on top of the floating panel
        response = alert.runModal()
        if response == NSAlertFirstButtonReturn:
            name = str(input_field.stringValue())
            self._daemon.start_new_session(name)
            # Update the session label so the user sees confirmation
            self._session_label.setStringValue_(
                f"Session: {self._daemon.session_name}"
            )
            # Reset shot label since counter restarts
            self._shot_label.setStringValue_("No shots yet")
            self._shot_label.setTextColor_(NSColor.secondaryLabelColor())

    @objc.signature(b"v@:@")
    def itemCommitted_(self, sender) -> None:
        """Called when the user presses Return in the item text field."""
        name = str(sender.stringValue())
        self._daemon.set_current_item(name)
        # Normalise the displayed value so the user can see what got saved
        # (e.g. trimmed whitespace, fallback to default for blanks).
        sender.setStringValue_(self._daemon.current_item)
        # Drop focus so subsequent space/A keys go back to triggers.
        self._panel.makeFirstResponder_(None)

    @objc.signature(b"v@:@")
    def quitClicked_(self, sender) -> None:
        self.stop()

    # ----- live-view staleness watchdog -------------------------------

    def _install_lv_staleness_watch(self) -> None:
        """Start an NSTimer that shows the pause overlay when LV freezes.

        Fires every 250 ms on the main run loop. The overlay is shown
        when no LV frame has arrived for ``_lv_stale_threshold_s``
        (default 500 ms ≈ 5 missed frames at 10 fps target) and hidden
        as soon as a fresh frame lands (also handled inline in
        ``updateLiveFrame_`` for instant resume).

        We keep a reference to the timer so ``stop()`` can invalidate
        it — otherwise the timer keeps the panel alive past quit.
        """
        self._lv_stale_timer = (
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.25, self, "checkLiveFrameStale:", None, True
            )
        )

    @objc.signature(b"v@:@")
    def checkLiveFrameStale_(self, timer) -> None:
        """Toggle the pause overlay based on how stale the LV feed is."""
        if self._lv_last_frame_at == 0.0:
            # No frame yet — keep overlay hidden (stream is still
            # warming up; we don't want a "Saving…" before the first
            # frame ever lands).
            return
        stale = (
            time.monotonic() - self._lv_last_frame_at
            > self._lv_stale_threshold_s
        )
        is_hidden = bool(self._lv_overlay.isHidden())
        if stale and is_hidden:
            self._lv_overlay.setHidden_(False)
        elif not stale and not is_hidden:
            # Fix 4: busy gate wins — if the daemon is in commit
            # window, hold the overlay up even when frames look
            # fresh (e.g. tail-end stream queue draining).
            if not self._controls_busy:
                self._lv_overlay.setHidden_(True)
                self._reposition_af_marker()

    # ----- hotkeys (global within app) --------------------------------

    def _install_hotkeys(self) -> None:
        """Catch keystrokes while ANY app is focused — limited to space/quit.

        We use a local monitor on NSEventMaskKeyDown that fires when the
        panel app is active. Since the panel is a non-activating utility
        panel, focus stays on Lightroom, so we ALSO install a global
        monitor for when Lightroom has focus.
        """

        def _handle(event) -> object | None:  # noqa: ANN001
            responder = self._panel.firstResponder()
            in_text_field = (
                responder is not None and responder.isKindOfClass_(NSTextView)
            )

            key = event.charactersIgnoringModifiers()
            key_code = event.keyCode()

            # Phase 3.9 Fix 2: Escape (keyCode 53) always drops focus when
            # the user is editing a field — gives them an "oops" out
            # without committing.
            if key_code == 53 and in_text_field:
                self._panel.makeFirstResponder_(None)
                return None  # consume

            if in_text_field:
                # Space inside the Item field: commit + drop focus + shoot.
                # One-keystroke fast path so the user isn't trapped after
                # editing the item name.
                if key == " ":
                    field = self._item_field
                    name = str(field.stringValue())
                    self._daemon.set_current_item(name)
                    field.setStringValue_(self._daemon.current_item)
                    self._panel.makeFirstResponder_(None)
                    self._daemon.request_snap()
                    return None  # consume
                # All other keys (letters, digits, Return, Tab, arrows)
                # pass through to the field normally.
                return event

            # Outside text field: original hotkey behavior.
            if key == " ":
                self._daemon.request_snap()
                return None  # consume
            if key == "a":
                self._daemon.request_af()
                return None  # consume
            return event

        # Local monitor (when our panel has focus) — returns event or None
        self._local_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            NSEventMaskKeyDown, _handle
        )
        # NOTE: a true global hotkey requires Accessibility permission.
        # We don't install a global monitor yet to keep MVP permission-free.
        # User clicks the Shoot button when LR has focus, or the panel.

    # ----- lifecycle --------------------------------------------------

    def run_forever(self) -> None:
        """Block the main thread on the Cocoa event loop."""
        self._app.run()

    def stop(self) -> None:
        # Tear down the LV staleness watchdog first — otherwise the
        # repeating timer keeps a strong ref to self and prevents the
        # panel from being released after quit.
        if getattr(self, "_lv_stale_timer", None) is not None:
            try:
                self._lv_stale_timer.invalidate()
            except Exception:  # noqa: BLE001
                pass
            self._lv_stale_timer = None
        try:
            self._daemon.stop()
        except Exception as e:  # noqa: BLE001
            logger.warning("daemon stop raised: %s", e)
        try:
            self._panel.close()
        except Exception:  # noqa: BLE001
            pass
        # End the NSApp run loop
        try:
            self._app.terminate_(None)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_label(field: NSTextField, text: str, *, bold: bool = False, size: float = 12) -> None:
    field.setStringValue_(text)
    field.setBezeled_(False)
    field.setDrawsBackground_(False)
    field.setEditable_(False)
    field.setSelectable_(False)
    if bold:
        field.setFont_(NSFont.boldSystemFontOfSize_(size))
    else:
        field.setFont_(NSFont.systemFontOfSize_(size))


# ---------------------------------------------------------------------------
# Live-view image view — click to set AF point
# ---------------------------------------------------------------------------
#
# Tiny NSImageView subclass whose only job is to turn mouseDown_ events
# into camera AF coords and forward them to the FloatingTetherPanel via
# its commit_focus_point() entry point.
#
# Coordinate mapping:
#  - The whole LV view rect maps to the camera's AF coord range
#    (af_bounds() = X∈[x_min..x_max], Y∈[y_min..y_max], default 96..928
#    / 85..597 on fp L V90).
#  - AppKit Y goes up from bottom-left, camera Y goes down from
#    top-left → we flip Y when going view → cam.
#  - The image is scaled proportionally inside the view, so non-3:2
#    frames will pillarbox/letterbox slightly. We intentionally don't
#    correct for that here — the camera's AF coord system already
#    covers the cropped active area (the fp L's AF range stays inside
#    the full sensor frame regardless of LV aspect), and the reverse
#    mapping in panel.cam_to_view() uses the same scale factors so
#    click-in and reticle-out are symmetric.


class LiveViewImageView(NSImageView):
    """NSImageView subclass that maps clicks → camera AF coordinates.

    Holds a back-ref to the FloatingTetherPanel (set via setOwner_)
    so it can read the current AF bounds and push new points via
    ``commit_focus_point``. The on-LV reticle is positioned by the
    panel itself from ``updateFocusPoint_``.
    """

    def initWithFrame_(self, frame):  # type: ignore[no-untyped-def]
        self = objc.super(LiveViewImageView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._owner = None
        return self

    def setOwner_(self, owner) -> None:  # type: ignore[no-untyped-def]
        self._owner = owner

    def mouseDown_(self, event) -> None:  # type: ignore[no-untyped-def]
        if self._owner is None:
            return
        local = self.convertPoint_fromView_(event.locationInWindow(), None)
        bounds = self.bounds()
        w = bounds.size.width
        h = bounds.size.height
        x_min, x_max, y_min, y_max = self._owner.af_bounds()
        # Clamp normalised view coords to [0..1] so a click exactly on
        # the border (or a stray off-by-one) still produces a valid
        # in-range AF point.
        nx = max(0.0, min(1.0, local.x / max(1.0, w)))
        ny = max(0.0, min(1.0, 1.0 - local.y / max(1.0, h)))  # flip Y
        cam_x = int(round(nx * (x_max - x_min) + x_min))
        cam_y = int(round(ny * (y_max - y_min) + y_min))
        self._owner.commit_focus_point(cam_x, cam_y)



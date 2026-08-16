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
    NSAppearance,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSButton,
    NSColor,
    NSEvent,
    NSEventMaskKeyDown,
    NSEventModifierFlagCommand,
    NSEventModifierFlagControl,
    NSEventModifierFlagOption,
    NSEventModifierFlagShift,
    NSFloatingWindowLevel,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSImage,
    NSImageScaleProportionallyUpOrDown,
    NSImageView,
    NSKernAttributeName,
    NSMakeRect,
    NSMakeSize,
    NSMenu,
    NSMenuItem,
    NSMutableParagraphStyle,
    NSPanel,
    NSParagraphStyleAttributeName,
    NSPopUpButton,
    NSScreen,
    NSStatusWindowLevel,
    NSTextAlignmentCenter,
    NSTextAlignmentLeft,
    NSTextAlignmentRight,
    NSTextField,
    NSTextView,
    NSTitledWindowMask,
    NSUtilityWindowMask,
    NSView,
    NSWorkspace,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskNonactivatingPanel,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
    NSWindowStyleMaskUtilityWindow,
)
from pathlib import Path

from Foundation import (
    NSAffineTransform,
    NSAttributedString,
    NSData,
    NSObject,
    NSPointInRect,
    NSTimer,
    NSURL,
)

# Font-weight constants — usually exported from AppKit, but the names
# vary by PyObjC version. Use the documented literal values so the
# import survives regardless of bridge metadata.
NSFontWeightRegular = 0.0
NSFontWeightMedium = 0.23
NSFontWeightSemibold = 0.3
NSFontWeightBold = 0.4

# NSPopUpArrowPosition.noArrow — hide the dropdown chevron on the
# hero exposure popups (the title text itself is the affordance).
_NS_POPUP_NO_ARROW = 0

# Compositing op for the focus-zoom crop blit (Phase 3.22). The name
# moved across AppKit versions (NSCompositeCopy → NSCompositingOperationCopy)
# so import defensively and fall back to the documented raw value.
try:
    from AppKit import NSCompositingOperationCopy as _NS_COMPOSITE_COPY
except ImportError:  # pragma: no cover — old PyObjC metadata
    _NS_COMPOSITE_COPY = 1

# Autoresizing masks (documented literal values — the names aren't
# exported by every PyObjC build). Pin-top-right = flexible left +
# flexible bottom margin; pin-top-left = flexible right + bottom.
_NS_PIN_TOP_RIGHT = 1 | 8   # NSViewMinXMargin | NSViewMinYMargin
_NS_PIN_TOP_LEFT = 4 | 8    # NSViewMaxXMargin | NSViewMinYMargin

from fp_l_tether.camera.sigma_datagroup import (
    EXP_COMP_CODES,
    EXPOSURE_MODE_CODES,
    apex_to_aperture,
    apex_to_iso,
    apex_to_shutter,
    battery_describe,
    battery_label,
    battery_level_class,
    expcomp_label,
    exposure_mode_label,
    image_quality_label,
    resolution_label,
    wb_label,
)
from fp_l_tether.storage import (
    LVWindowState,
    SettingsCache,
    load_settings_cache,
    save_settings_cache,
)
from fp_l_tether.ui.grid_overlay import GridOverlayView, cycle_mode as _grid_cycle
from fp_l_tether.ui.histogram_view import HistogramView
from fp_l_tether.ui.lv_attached_pane import LVDetachedPlaceholder
from fp_l_tether.ui.lv_geometry import fit_rect, rotate_forward, rotate_inverse
from fp_l_tether.ui.lv_window import LVDetachedWindow
from fp_l_tether.ui.preferences_window import PreferencesWindow

if TYPE_CHECKING:
    from fp_l_tether.config import AppConfig
    from fp_l_tether.transfer import TetherDaemon

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Phase 3.10 — design tokens (anodized black + amber, mono hero values).
# These are intentionally module-level so the build_window method reads
# as layout-only and tweaking the palette doesn't require editing the
# widget tree. See docs/PHASE_3_10_UI_POLISH.md for the source spec.
# ---------------------------------------------------------------------


def _c(r: int, g: int, b: int, a: float = 1.0):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(
        r / 255.0, g / 255.0, b / 255.0, a
    )


# Backgrounds
C_BG_PANEL = _c(26, 26, 28)
C_BG_ELEVATED = _c(38, 38, 41)
C_BG_INPUT = _c(14, 14, 16)
C_BG_LV_FRAME = _c(10, 10, 12)

# Foregrounds
C_FG_PRIMARY = _c(240, 240, 242)
C_FG_SECONDARY = _c(144, 144, 160)
C_FG_TERTIARY = _c(106, 106, 120)
C_FG_INVERSE = _c(26, 26, 28)

# Accents
C_AMBER = _c(232, 161, 58)
C_AMBER_BRIGHT = _c(247, 180, 69)

# State colours (used by the status-row dot + LV chrome)
C_STATE_READY = _c(52, 199, 89)
C_STATE_BUSY = _c(255, 159, 10)
C_STATE_RECOVER = _c(255, 204, 0)
C_STATE_ERROR = _c(255, 69, 58)

# Strokes
C_STROKE_SUBTLE = _c(46, 46, 52)
C_STROKE_DEFAULT = _c(58, 58, 64)
C_STROKE_STRONG = _c(82, 82, 92)

# Typography. Mono for numeric exposure values so the digits don't
# bounce around as the user spins through dial codes; SF Pro for
# everything else.
F_HERO = NSFont.monospacedSystemFontOfSize_weight_(18, NSFontWeightBold)
F_NUMERIC = NSFont.monospacedSystemFontOfSize_weight_(11, NSFontWeightMedium)
F_LABEL = NSFont.systemFontOfSize_weight_(10, NSFontWeightMedium)
F_BODY = NSFont.systemFontOfSize_weight_(12, NSFontWeightRegular)
F_BUTTON = NSFont.systemFontOfSize_weight_(13, NSFontWeightSemibold)
F_HINT = NSFont.systemFontOfSize_weight_(10, NSFontWeightRegular)
# Phase 3.22 — tiny mono font for the on-LV badges (battery pill,
# focus-zoom "100%" marker).
F_BADGE = NSFont.monospacedSystemFontOfSize_weight_(10, NSFontWeightMedium)

# Status-row presets — keep the daemon's internal state names (these
# match the keys the daemon emits through on_status) but render with
# the clean human-readable strings the spec asks for.
STATUS_PRESETS = {
    "ready":        (C_STATE_READY,   "Ready"),
    "shooting":     (C_STATE_BUSY,    "Capturing…"),
    "downloading":  (C_STATE_BUSY,    "Saving…"),
    "recovering":   (C_STATE_RECOVER, "USB recovery"),
    "error":        (C_STATE_ERROR,   "Camera unresponsive"),
    "focusing":     (C_AMBER,         "Focusing…"),
    "connecting":   (C_FG_TERTIARY,   "Connecting…"),
    "initializing": (C_FG_TERTIARY,   "Warming up…"),
    "stopped":      (C_FG_TERTIARY,   "Stopped"),
    "disconnected": (C_STATE_ERROR,   "Disconnected"),
}

# ---------------------------------------------------------------------
# Layout constants
# ---------------------------------------------------------------------
PANEL_WIDTH = 320
# Phase 3.13 — two-state panel height. Attached = LV slot + controls;
# detached = controls only (LV lives in its own window so the slot is
# removed, not just hidden). PANEL_HEIGHT keeps its historical value
# as an alias for layout math + the initial NSPanel rect.
PANEL_HEIGHT_ATTACHED = 480
PANEL_HEIGHT_DETACHED = 270  # STATUS_Y + STATUS_H + PAD_TOP = 238 + 18 + 14
PANEL_HEIGHT = PANEL_HEIGHT_ATTACHED
PAD_X = 16
PAD_TOP = 14
PAD_BOTTOM = 14

# Live-view viewport
LV_WIDTH = PANEL_WIDTH - 2 * PAD_X  # 288
LV_HEIGHT = 200
LV_X = PAD_X  # 16
LV_Y = PANEL_HEIGHT - PAD_TOP - LV_HEIGHT  # 266

# Status row (dot + state + shot count) — between LV and exposure block
STATUS_H = 18
STATUS_Y = LV_Y - 10 - STATUS_H  # 238

# Exposure hero block (top stroke, label row, value row, bottom stroke)
EXP_TOP_STROKE_Y = STATUS_Y - 14            # 224
EXP_LABEL_H = 14
EXP_LABEL_Y = EXP_TOP_STROKE_Y - 6 - EXP_LABEL_H   # 204
EXP_VALUE_H = 26
EXP_VALUE_Y = EXP_LABEL_Y - 4 - EXP_VALUE_H        # 174
EXP_BOT_STROKE_Y = EXP_VALUE_Y - 8                 # 166

# Secondary dropdown row (WB / Format / Size)
SEC_H = 24
SEC_Y = EXP_BOT_STROKE_Y - 14 - SEC_H              # 128

# Subject text field
SUB_H = 26
SUB_Y = SEC_Y - 14 - SUB_H                         # 88

# Buttons (Shoot 2/3 + AF 1/3)
BTN_H = 32
BTN_Y = SUB_Y - 14 - BTN_H                         # 42
BTN_GAP = 6
SHOOT_W = round(LV_WIDTH * 2 / 3) - BTN_GAP // 2   # 189
AF_W = LV_WIDTH - SHOOT_W - BTN_GAP                # 93

# Footer hint
HINT_H = 12
HINT_Y = 14  # PAD_BOTTOM

# AF reticle on the LV — 4 amber corner ticks
AF_MARKER_SIZE = 40
AF_TICK_ARM = 8
AF_TICK_STROKE = 1.5

# Phase 3.11 — bottom-strip RGB histogram inside the LV viewport.
# 50pt high per spec, full LV width, hugs the LV bottom edge.
HIST_STRIP_H = 50

# Vertical LV padding values kept for backwards compatibility with
# cam_to_view callers — these mirror the new LV_Y / LV_HEIGHT layout.
LV_PAD_TOP = PAD_TOP
LV_PAD_BOTTOM = STATUS_Y + STATUS_H + 10  # how much room is under the LV
# CONTROLS_HEIGHT is no longer meaningful with the new top-down layout,
# but is referenced by the historic cam_to_view math. We map it to the
# top edge of the status row so cam→view conversion still resolves to
# the LV rect.
CONTROLS_HEIGHT = LV_Y - LV_PAD_BOTTOM  # = LV_Y - (STATUS_Y + STATUS_H + 10)


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

        # Phase 3.12 — detachable LV window state. Defaults to attached.
        # ``_user_settings`` is the panel's own load of the on-disk cache;
        # we read lv_window state from it at startup (to decide whether
        # to detach immediately) and merge our lv_window writes back into
        # it so subsequent saves don't blow away dg1/dg2 that the watcher
        # populated. The watcher keeps its own copy — separate races are
        # fine because we re-load before writing in ``_save_lv_window_state``.
        try:
            self._user_settings: SettingsCache | None = load_settings_cache()
        except Exception:  # noqa: BLE001
            self._user_settings = None
        self._lv_window: LVDetachedWindow | None = None
        self._placeholder: LVDetachedPlaceholder | None = None
        # Phase 3.14 — Preferences window singleton (lazy-created on first
        # ⌘, press). Stays alive across hide/show so its form state and
        # focused field survive closing the window.
        self._prefs_controller: PreferencesWindow | None = None
        # Token-based debounce for window-move/resize → settings save.
        # ``_pending_save_token`` is bumped on every event; the queued
        # dispatch_after closure only writes if its captured token still
        # matches the latest, coalescing rapid drag/resize bursts.
        self._pending_save_token: int = 0
        # Phase 3.20d: while > now, the LV staleness watchdog leaves the
        # veil down (post-save quiet window = frames expected to stall).
        self._suppress_stale_until: float = 0.0
        # Phase 3.21: save-confirmation footer flash + last raw status
        # message (surfaced for error states instead of being dropped).
        self._flash_text: str = ""
        self._flash_until: float = 0.0
        self._last_status_message: str = ""
        # Phase 3.22: focus-zoom state, battery cache, review overlay.
        # _lv_zoom is the magnification factor (0 = off, 4, 8 — same
        # ladder as the body's MF assist); truthiness = "zoom active".
        # _zoom_hist_was_visible remembers the histogram strip so
        # exiting zoom restores the user's own H toggle, not the cfg
        # default. _review_view is lazily created on the first shot.
        self._lv_zoom: int = 0
        self._zoom_hist_was_visible: bool = False
        # Phase 3.23: manual LV rotation, clockwise degrees applied to
        # the displayed frame (0 / 90 / 180 / 270). The fp L exposes no
        # attitude data over PTP (confirmed Phase 3.11c) AND its LV
        # stream is always sensor-native landscape, so a body turned to
        # portrait shows the subject lying on its side. Nothing can
        # detect that for us — the user picks the rotation with R.
        # AF click / reticle mapping is transformed to match.
        self._lv_rotation: int = 0
        self._review_view = None
        self._review_token: int = 0
        self._last_battery_raw: int | None = None

        self._build_window()
        self._install_context_menu()
        self._wire_callbacks()
        self._install_hotkeys()
        self._install_lv_staleness_watch()

        # If the user quit last time with LV detached, restore that state
        # right after window construction (per Phase 3.12 spec section 7).
        try:
            cached_lv = self._user_settings.lv_window if self._user_settings else None
        except AttributeError:
            cached_lv = None
        if cached_lv is not None and cached_lv.detached:
            try:
                self._detach_lv()
            except Exception as e:  # noqa: BLE001
                # Best-effort restore — fall back to attached mode if any
                # AppKit call raises (e.g. all NSScreens transient at app
                # launch on display reconfiguration).
                logger.warning("LV detach restore failed: %s", e)

    # ----- window construction ----------------------------------------

    def _build_window(self) -> None:
        """Build the floating panel — Phase 3.10 layout.

        Anodized-black panel, amber accent, mono hero exposure values.
        Layout constants are at module-top (LV_*, EXP_*, SEC_Y, SUB_Y,
        BTN_Y, HINT_Y) so this method reads as widget placement only —
        no magic numbers buried in the call sites.
        """
        screen = NSScreen.mainScreen()
        frame = screen.visibleFrame()
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
        panel.setLevel_(NSStatusWindowLevel)
        panel.setOpaque_(True)
        panel.setBackgroundColor_(C_BG_PANEL)
        panel.setHasShadow_(True)
        panel.setMovableByWindowBackground_(True)

        # Dark appearance so NSPopUpButton + system controls render
        # readable text on our anodized-black background without each
        # control needing an explicit attributedTitle.
        try:
            panel.setAppearance_(
                NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua")
            )
        except Exception:  # noqa: BLE001
            # appearanceNamed_ on very old macOS versions may fail;
            # the panel still renders, just with default chrome.
            pass

        # Wire the close button → graceful daemon shutdown.
        panel.setDelegate_(self)

        # Follow the user across Spaces, sit over Lightroom fullscreen.
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorStationary
        )

        content = panel.contentView()
        content.setWantsLayer_(True)
        if content.layer() is not None:
            content.layer().setBackgroundColor_(C_BG_PANEL.CGColor())

        # ----- Live-view viewport ----------------------------------
        self._live_view = LiveViewImageView.alloc().initWithFrame_(
            NSMakeRect(LV_X, LV_Y, LV_WIDTH, LV_HEIGHT)
        )
        self._live_view.setOwner_(self)
        self._live_view.setEditable_(False)
        self._live_view.setImageScaling_(NSImageScaleProportionallyUpOrDown)
        self._live_view.setWantsLayer_(True)
        lv_layer = self._live_view.layer()
        if lv_layer is not None:
            lv_layer.setBackgroundColor_(C_BG_LV_FRAME.CGColor())
            lv_layer.setCornerRadius_(4.0)
            lv_layer.setMasksToBounds_(True)
        content.addSubview_(self._live_view)

        # ----- LV badges (Phase 3.22) ------------------------------
        # Battery pill (top-right) + focus-zoom marker (top-left).
        # Both are subviews of the LV view itself, so they travel with
        # detach/reattach for free and autoresize against the detached
        # window. _ClickThroughView keeps the click-to-AF surface
        # intact underneath.
        self._battery_pill = _make_lv_badge(
            LV_WIDTH - _BADGE_W_BAT - _BADGE_M,
            LV_HEIGHT - _BADGE_H - _BADGE_M,
            _BADGE_W_BAT,
            mask=_NS_PIN_TOP_RIGHT,
        )
        self._battery_label = self._battery_pill.subviews()[0]
        self._battery_pill.setHidden_(True)  # until DG1 reports a value
        self._live_view.addSubview_(self._battery_pill)

        # Exposure-mode chip (Phase 3.22c) — top-left. The body's own
        # mode display is unreachable mid-tether (controls locked, LCD
        # dark), and the PC-mode template + cache replay clobber any
        # mode dialled while the app was down, so the app must both
        # SHOW the live mode and (via right-click menu) SET it.
        self._mode_chip = _make_lv_badge(
            _BADGE_M,
            LV_HEIGHT - _BADGE_H - _BADGE_M,
            _BADGE_W_MODE,
            mask=_NS_PIN_TOP_LEFT,
        )
        self._mode_chip_label = self._mode_chip.subviews()[0]
        self._mode_chip.setToolTip_(
            "Exposure mode — right-click the panel to change"
        )
        self._mode_chip.setHidden_(True)  # until DG2 reports a mode
        self._live_view.addSubview_(self._mode_chip)

        self._zoom_badge = _make_lv_badge(
            _BADGE_M + _BADGE_W_MODE + 4,
            LV_HEIGHT - _BADGE_H - _BADGE_M,
            _BADGE_W_ZOOM,
            mask=_NS_PIN_TOP_LEFT,
        )
        _zoom_lbl = self._zoom_badge.subviews()[0]
        _zoom_lbl.setStringValue_("×4")  # placeholder — set per level
        _zoom_lbl.setTextColor_(C_AMBER)
        self._zoom_badge.setHidden_(True)  # only while Z-zoom active
        self._live_view.addSubview_(self._zoom_badge)

        # ----- Composition grid (Phase 3.11) -----------------------
        # Full-LV-area overlay; drawRect is a no-op when mode == "off"
        # so cycling through hotkey G is free when the grid is hidden.
        # Added BEFORE the histogram strip so the strip's translucent
        # background tints any grid lines underneath without obscuring
        # them — the spec calls for the histogram strip to feel like
        # an integral layer of the LV rather than a sticker on top.
        self._grid_view = GridOverlayView.alloc().initWithFrame_(
            NSMakeRect(LV_X, LV_Y, LV_WIDTH, LV_HEIGHT)
        )
        self._grid_view.setMode_(self._cfg.liveview.grid_mode)
        content.addSubview_(self._grid_view)

        # ----- Histogram strip (Phase 3.11) ------------------------
        # Bottom 50pt inside the LV viewport. Hidden by default if
        # the cfg says so; the daemon's compute side respects the
        # same flag so a hidden strip costs zero CPU.
        self._hist_view = HistogramView.alloc().initWithFrame_(
            NSMakeRect(LV_X, LV_Y, LV_WIDTH, HIST_STRIP_H)
        )
        self._hist_view.setHidden_(not self._cfg.liveview.show_histogram)
        content.addSubview_(self._hist_view)

        # ----- LV pause overlay (Saving…) --------------------------
        self._lv_overlay = NSView.alloc().initWithFrame_(
            NSMakeRect(LV_X, LV_Y, LV_WIDTH, LV_HEIGHT)
        )
        self._lv_overlay.setWantsLayer_(True)
        overlay_layer = self._lv_overlay.layer()
        if overlay_layer is not None:
            overlay_layer.setBackgroundColor_(
                NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.62).CGColor()
            )
            overlay_layer.setCornerRadius_(4.0)
        self._lv_overlay.setHidden_(True)
        content.addSubview_(self._lv_overlay)

        # "Saving…" label centred inside the overlay (overlay-local coords).
        self._lv_overlay_label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(0, (LV_HEIGHT - 22) // 2, LV_WIDTH, 22)
        )
        _make_label(self._lv_overlay_label, "Saving…", bold=False, size=13)
        self._lv_overlay_label.setFont_(F_BUTTON)
        self._lv_overlay_label.setAlignment_(NSTextAlignmentCenter)
        self._lv_overlay_label.setTextColor_(C_FG_PRIMARY)
        self._lv_overlay.addSubview_(self._lv_overlay_label)

        # ----- AF reticle (4 amber corner ticks) -------------------
        self._af_marker_view = _build_corner_reticle()
        self._af_marker_view.setFrame_(
            NSMakeRect(
                LV_X + (LV_WIDTH - AF_MARKER_SIZE) // 2,
                LV_Y + (LV_HEIGHT - AF_MARKER_SIZE) // 2,
                AF_MARKER_SIZE,
                AF_MARKER_SIZE,
            )
        )
        # Hidden until a focus-point event lands — no stray reticle
        # at the LV centre before we know where the camera is aimed.
        self._af_marker_view.setHidden_(True)
        content.addSubview_(self._af_marker_view)

        # Staleness watchdog state — preserved verbatim from 3.9.
        self._lv_last_frame_at: float = 0.0
        self._lv_stale_threshold_s: float = 0.5
        self._lv_stale_timer = None

        # ----- Status row ------------------------------------------
        # Dot (small coloured circle) + state label (left) + shot
        # counter (right-aligned). All three sit on the same baseline.
        dot_size = 8
        dot_y = STATUS_Y + (STATUS_H - dot_size) // 2
        self._status_dot = NSView.alloc().initWithFrame_(
            NSMakeRect(LV_X, dot_y, dot_size, dot_size)
        )
        self._status_dot.setWantsLayer_(True)
        dot_layer = self._status_dot.layer()
        if dot_layer is not None:
            dot_layer.setBackgroundColor_(C_FG_TERTIARY.CGColor())
            dot_layer.setCornerRadius_(dot_size / 2.0)
        content.addSubview_(self._status_dot)

        self._status_label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(LV_X + dot_size + 8, STATUS_Y, 180, STATUS_H)
        )
        _make_label(self._status_label, "Connecting…")
        self._status_label.setFont_(F_BODY)
        self._status_label.setTextColor_(C_FG_PRIMARY)
        content.addSubview_(self._status_label)

        # Shot counter — right-aligned, mono digits, tertiary colour.
        # Phase 3.21: narrowed to make room for the gear button.
        self._shot_label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(LV_X + LV_WIDTH - 24 - 86, STATUS_Y, 86, STATUS_H)
        )
        _make_label(self._shot_label, "0 shots")
        self._shot_label.setFont_(F_NUMERIC)
        self._shot_label.setTextColor_(C_FG_TERTIARY)
        self._shot_label.setAlignment_(NSTextAlignmentRight)
        content.addSubview_(self._shot_label)

        # Phase 3.21: a VISIBLE entrance to Preferences. Hotkeys are a
        # power-user layer — a fresh install must be explorable with a
        # mouse alone. Monochrome SF Symbol, tertiary tint, tooltip
        # teaches the shortcut.
        gear = NSButton.alloc().initWithFrame_(
            NSMakeRect(LV_X + LV_WIDTH - 18, STATUS_Y - 1, 18, STATUS_H + 2)
        )
        gear.setBordered_(False)
        _gear_img = None
        try:
            _gear_img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                "gearshape", "Preferences"
            )
        except Exception:  # noqa: BLE001 — pre-11 AppKit
            _gear_img = None
        if _gear_img is not None:
            gear.setImage_(_gear_img)
            try:
                gear.setContentTintColor_(C_FG_TERTIARY)
            except Exception:  # noqa: BLE001
                pass
        else:
            gear.setTitle_("\u2699\ufe0e")  # text-presentation gear glyph
        gear.setTarget_(self)
        gear.setAction_("prefsButtonClicked:")
        gear.setToolTip_("Preferences (\u2318,)")
        content.addSubview_(gear)
        self._gear_btn = gear

        # ----- Exposure hero block ---------------------------------
        # Top + bottom 1pt strokes; label row (uppercase) above value
        # row (mono hero).
        top_stroke = _make_stroke(LV_X, EXP_TOP_STROKE_Y, LV_WIDTH, C_STROKE_SUBTLE)
        content.addSubview_(top_stroke)
        bot_stroke = _make_stroke(LV_X, EXP_BOT_STROKE_Y, LV_WIDTH, C_STROKE_SUBTLE)
        content.addSubview_(bot_stroke)

        # 3-column grid — equal widths, no gutters (the label/value
        # alignment carries the visual rhythm).
        col_w = LV_WIDTH // 3  # 96
        col_xs = [LV_X, LV_X + col_w, LV_X + 2 * col_w]

        # Uppercase labels
        for col_x, text in zip(col_xs, ("ISO", "SHUTTER", "AV")):
            lbl = NSTextField.alloc().initWithFrame_(
                NSMakeRect(col_x, EXP_LABEL_Y, col_w, EXP_LABEL_H)
            )
            _make_label(lbl, text)
            lbl.setAttributedStringValue_(
                _uppercase_attr(text, C_FG_SECONDARY, font=F_LABEL, tracking=1.0,
                                alignment=NSTextAlignmentCenter)
            )
            content.addSubview_(lbl)

        # Hero popups (bezel-less, centred, mono 18pt bold).
        self._iso_dropdown = _make_hero_popup(
            col_xs[0], EXP_VALUE_Y, col_w, EXP_VALUE_H, self, "isoChanged:"
        )
        content.addSubview_(self._iso_dropdown)
        self._ss_dropdown = _make_hero_popup(
            col_xs[1], EXP_VALUE_Y, col_w, EXP_VALUE_H, self, "ssChanged:"
        )
        content.addSubview_(self._ss_dropdown)
        self._av_dropdown = _make_hero_popup(
            col_xs[2], EXP_VALUE_Y, col_w, EXP_VALUE_H, self, "avChanged:"
        )
        content.addSubview_(self._av_dropdown)

        # ----- Secondary row (WB / Format / Size / EV) -------------
        # Phase 3.22 adds a 4th popup for exposure compensation.
        # Widths are asymmetric — sized to each popup's longest label
        # ("Fluorescent", "DNG+JPG", "S", "−3.0") — and still span
        # LV_WIDTH exactly: 84+78+48+60 + 3×6 gaps = 288.
        sec_gap = 6
        sec_ws = (84, 78, 48, 60)
        sec_xs = []
        _sx = LV_X
        for _sw in sec_ws:
            sec_xs.append(_sx)
            _sx += _sw + sec_gap
        self._wb_dropdown = _make_secondary_popup(
            sec_xs[0], SEC_Y, sec_ws[0], SEC_H, self, "wbChanged:"
        )
        self._wb_dropdown.setToolTip_("White balance")
        content.addSubview_(self._wb_dropdown)
        self._fmt_dropdown = _make_secondary_popup(
            sec_xs[1], SEC_Y, sec_ws[1], SEC_H, self, "fmtChanged:"
        )
        self._fmt_dropdown.setToolTip_("File format")
        content.addSubview_(self._fmt_dropdown)
        self._size_dropdown = _make_secondary_popup(
            sec_xs[2], SEC_Y, sec_ws[2], SEC_H, self, "sizeChanged:"
        )
        self._size_dropdown.setToolTip_("Image size (JPG)")
        content.addSubview_(self._size_dropdown)
        self._ev_dropdown = _make_secondary_popup(
            sec_xs[3], SEC_Y, sec_ws[3], SEC_H, self, "evChanged:"
        )
        self._ev_dropdown.setToolTip_("Exposure compensation (EV)")
        content.addSubview_(self._ev_dropdown)

        # ----- Subject field ---------------------------------------
        # Composite: a dark inset container holds an inline uppercase
        # "SUBJECT" label (left) and the editable text field (right),
        # both vertically centred so the value doesn't hang in the
        # top-left corner of an empty-looking box.
        sub_container = NSView.alloc().initWithFrame_(
            NSMakeRect(LV_X, SUB_Y, LV_WIDTH, SUB_H)
        )
        sub_container.setWantsLayer_(True)
        sub_layer = sub_container.layer()
        if sub_layer is not None:
            sub_layer.setBackgroundColor_(C_BG_INPUT.CGColor())
            sub_layer.setBorderColor_(C_STROKE_DEFAULT.CGColor())
            sub_layer.setBorderWidth_(1.0)
            sub_layer.setCornerRadius_(4.0)

        # Inline "SUBJECT" label — non-editable, uppercase, tertiary
        SUB_PAD_X = 10
        SUB_LABEL_W = 58   # enough for "SUBJECT" at F_LABEL with tracking
        SUB_GAP = 10
        SUB_TEXT_H = 16    # single-line height for F_BODY (12pt)
        sub_text_y = (SUB_H - SUB_TEXT_H) / 2   # vertical centre
        sub_caption = NSTextField.alloc().initWithFrame_(
            NSMakeRect(SUB_PAD_X, sub_text_y, SUB_LABEL_W, SUB_TEXT_H)
        )
        sub_caption.setAttributedStringValue_(
            _uppercase_attr(
                "Subject", C_FG_TERTIARY, font=F_LABEL, tracking=1.0,
                alignment=NSTextAlignmentLeft,
            )
        )
        sub_caption.setBezeled_(False)
        sub_caption.setDrawsBackground_(False)
        sub_caption.setEditable_(False)
        sub_caption.setSelectable_(False)
        sub_container.addSubview_(sub_caption)

        # Editable text field — centred next to the label
        edit_x = SUB_PAD_X + SUB_LABEL_W + SUB_GAP
        edit_w = LV_WIDTH - edit_x - SUB_PAD_X
        self._item_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(edit_x, sub_text_y, edit_w, SUB_TEXT_H)
        )
        self._item_field.setStringValue_(self._daemon.current_item)
        self._item_field.setFont_(F_BODY)
        self._item_field.setTextColor_(C_FG_PRIMARY)
        self._item_field.setBezeled_(False)
        self._item_field.setDrawsBackground_(False)
        self._item_field.setEditable_(True)
        self._item_field.setSelectable_(True)
        self._item_field.setTarget_(self)
        self._item_field.setAction_("itemCommitted:")
        sub_container.addSubview_(self._item_field)

        content.addSubview_(sub_container)

        # ----- Buttons (Shoot 2/3 + AF 1/3) ------------------------
        self._shoot_btn = _make_amber_button(
            NSMakeRect(LV_X, BTN_Y, SHOOT_W, BTN_H),
            "Shoot",
            target=self,
            action="shootClicked:",
        )
        content.addSubview_(self._shoot_btn)

        self._af_btn = _make_dark_button(
            NSMakeRect(LV_X + SHOOT_W + BTN_GAP, BTN_Y, AF_W, BTN_H),
            "AF",
            target=self,
            action="afClicked:",
        )
        content.addSubview_(self._af_btn)

        # ----- Footer hint -----------------------------------------
        self._hint_label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(LV_X, HINT_Y, LV_WIDTH, HINT_H)
        )
        _make_label(self._hint_label, "")
        self._hint_label.setFont_(F_HINT)
        self._hint_label.setTextColor_(C_FG_TERTIARY)
        self._hint_label.setAlignment_(NSTextAlignmentCenter)
        self._hint_label.setStringValue_(
            "␣ shoot · A/⌘A focus · H/⌘H hist · G/⌘G grid · ⌘D detach · ⌘Q quit"
        )
        content.addSubview_(self._hint_label)

        # ----- internal state --------------------------------------
        self._can_set_info = None
        self._exposure_raw: dict[str, int] = {}
        self._focus_xy: tuple[int, int] | None = None
        self._suppress_action = False
        # Latest shot count — driven by updateShot_.
        self._shot_count = 0

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
            (
                event.shot_index,
                event.saved_path.name,
                event.size,
                event.mbps,
                # Phase 3.22 — full path string for the review overlay.
                str(event.saved_path),
            ),
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
                # Phase 3.22 — exp-comp + battery ride the same event;
                # 3.22c appends the exposure mode.
                getattr(s, "exp_comp_raw", 0),
                getattr(s, "battery_raw", -1),
                getattr(s, "exposure_mode_raw", 0),
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
            (event.jpeg, event.width, event.height, event.histogram),
            False,
        )

    # ----- ObjC-callable updates (main thread only) -------------------

    @objc.signature(b"v@:@")
    def updateStatus_(self, tup) -> None:
        """Drive the status-row dot + label + per-state visual treatment.

        Maps the daemon's state name through STATUS_PRESETS to (dot
        colour, display text), then flips the busy gate (which owns
        the opacity treatment + LV overlay) for capture-cycle states.
        Unknown / transient states fall back to a neutral preset so
        the UI never shows the raw machine name to the user.
        """
        state, message = tup
        self._last_status_message = str(message or "")
        # Phase 3.20: remember the raw daemon state — the busy gate is
        # binary, but snap-queueing during busy is only safe in the
        # active-capture states (see _may_queue_snap).
        self._last_daemon_state = state
        dot_color, label_text = STATUS_PRESETS.get(
            state, (C_FG_TERTIARY, state.title() if state else "—")
        )
        self._status_label.setStringValue_(label_text)
        self._status_label.setTextColor_(C_FG_PRIMARY)
        dot_layer = self._status_dot.layer()
        if dot_layer is not None:
            dot_layer.setBackgroundColor_(dot_color.CGColor())

        # Busy gate — shooting / downloading / recovering all dim
        # the controls and force the LV overlay. Phase 3.10 also
        # rewrites the hint footer with per-state guidance.
        # Phase 3.15 (A10): error / disconnected keep the controls
        # DISABLED (spec 3.10 "Wedged" row). Previously they re-enabled
        # everything, so clicks against a dead camera queued requests
        # that fired unexpectedly the moment recovery succeeded —
        # and dial changes silently no-opped.
        if state in ("shooting", "downloading", "recovering",
                     "error", "disconnected"):
            self._set_controls_busy(True, state=state)
        elif state in ("ready", "stopped"):
            self._set_controls_busy(False, state=state)
        else:
            # Transient states (focusing, connecting, initializing) —
            # keep current dim state, just refresh the hint label.
            self._refresh_hint(state)

    def _may_queue_snap(self) -> bool:
        """Phase 3.20: may a Space press QUEUE during busy?

        Only with pipelined capture enabled, and only in the
        active-capture states — a queued snap there feeds the
        pipeline (bounded to 2 by the daemon). Never in
        error/disconnected/recovering, where a queued press would
        recreate the phantom-shutter-after-recovery bug (A16).
        """
        try:
            if not self._cfg.camera.pipelined_capture:
                return False
        except AttributeError:
            return False
        return getattr(self, "_last_daemon_state", "") in (
            "shooting", "downloading", "focusing",
        )

    @objc.python_method
    def _install_context_menu(self) -> None:
        """Right-click menu over the whole panel (Phase 3.21).

        Every action lists its keyboard shortcut, so the menu doubles
        as the discoverability layer for the hotkey set — a fresh
        install can drive everything by mouse and LEARN the keys in
        passing. macOS-native NSMenu, no custom chrome (質実剛健).
        """
        menu = NSMenu.alloc().initWithTitle_("fp-l-tether")

        def add(title, action, key="", mask=0):  # noqa: ANN001
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, action, key
            )
            if key:
                item.setKeyEquivalentModifierMask_(mask)
            item.setTarget_(self)
            menu.addItem_(item)
            return item

        add("Shoot  (Space)", "shootClicked:")
        add("Focus  (A)", "afClicked:")
        menu.addItem_(NSMenuItem.separatorItem())

        # Exposure-mode submenu (Phase 3.22c). The body's MODE button
        # is locked during tether AND the PC-mode template + settings
        # cache clobber any mode dialled while the app was down — so
        # the app is the authoritative place to pick P/A/S/M. The
        # selection round-trips through the camera (checkmark follows
        # the re-read) and lands in the settings cache, so it survives
        # reconnects.
        mode_root = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Exposure Mode", None, ""
        )
        mode_menu = NSMenu.alloc().initWithTitle_("Exposure Mode")
        self._mode_items = {}
        for label, code in EXPOSURE_MODE_CODES:
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                label, "menuExposureModeSelect:", ""
            )
            it.setTarget_(self)
            it.setTag_(code)
            mode_menu.addItem_(it)
            self._mode_items[code] = it
        mode_root.setSubmenu_(mode_menu)
        menu.addItem_(mode_root)
        menu.addItem_(NSMenuItem.separatorItem())

        add("Histogram", "menuToggleHistogram:", "h", 0)
        add("Grid", "menuCycleGrid:", "g", 0)
        add("Focus Zoom ×4 / ×8", "menuToggleZoom:", "z", 0)

        # LV rotation submenu (Phase 3.23). The fp L streams live view
        # sensor-native landscape whatever way the body is held, and
        # publishes no attitude data over PTP — so shooting portrait
        # puts the subject on its side and only the user can say which
        # way is up. R cycles; the submenu is for picking directly.
        rot_root = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "LV Rotation", None, ""
        )
        rot_menu = NSMenu.alloc().initWithTitle_("LV Rotation")
        self._rotation_items = {}
        for label, deg in (
            ("0°  (landscape)", 0),
            ("90° CW  (R)", 90),
            ("180°", 180),
            ("270° CW", 270),
        ):
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                label, "menuRotationSelect:", ""
            )
            it.setTarget_(self)
            it.setTag_(deg)
            rot_menu.addItem_(it)
            self._rotation_items[deg] = it
        self._rotation_items[0].setState_(1)
        rot_root.setSubmenu_(rot_menu)
        menu.addItem_(rot_root)

        add("Detach / Reattach LV", "menuToggleDetach:", "d",
            NSEventModifierFlagCommand)
        menu.addItem_(NSMenuItem.separatorItem())

        # Interval shooting submenu (Phase 3.21) — a tether staple for
        # product / repro work. Daemon-side timer feeds the normal
        # snap queue, so every safety gate applies unchanged.
        interval_root = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Interval Shooting", None, ""
        )
        interval_menu = NSMenu.alloc().initWithTitle_("Interval")
        self._interval_items = {}
        for label, secs in (
            ("Off", 0), ("5 s", 5), ("10 s", 10), ("30 s", 30), ("60 s", 60),
        ):
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                label, "menuIntervalSelect:", ""
            )
            it.setTarget_(self)
            it.setTag_(secs)
            interval_menu.addItem_(it)
            self._interval_items[secs] = it
        self._interval_items[0].setState_(1)
        interval_root.setSubmenu_(interval_menu)
        menu.addItem_(interval_root)
        menu.addItem_(NSMenuItem.separatorItem())
        add("Reveal Shots Folder in Finder", "menuRevealShots:")
        add("Preferences\u2026", "menuOpenPrefs:", ",",
            NSEventModifierFlagCommand)
        menu.addItem_(NSMenuItem.separatorItem())
        add("Quit fp-l-tether", "menuQuit:", "q", NSEventModifierFlagCommand)

        self._panel.contentView().setMenu_(menu)
        self._context_menu = menu

    def prefsButtonClicked_(self, sender) -> None:  # type: ignore[no-untyped-def]
        self._open_preferences()

    def menuToggleHistogram_(self, sender) -> None:  # type: ignore[no-untyped-def]
        self._toggle_histogram()

    def menuCycleGrid_(self, sender) -> None:  # type: ignore[no-untyped-def]
        self._cycle_grid()

    def menuToggleZoom_(self, sender) -> None:  # type: ignore[no-untyped-def]
        self._toggle_focus_zoom()

    def menuRotationSelect_(self, sender) -> None:  # type: ignore[no-untyped-def]
        self._set_lv_rotation(int(sender.tag()))

    def menuToggleDetach_(self, sender) -> None:  # type: ignore[no-untyped-def]
        self._toggle_lv_detached()

    def menuOpenPrefs_(self, sender) -> None:  # type: ignore[no-untyped-def]
        self._open_preferences()

    def menuQuit_(self, sender) -> None:  # type: ignore[no-untyped-def]
        self.stop()

    def menuExposureModeSelect_(self, sender) -> None:  # type: ignore[no-untyped-def]
        """Write DG2.ExposureMode from the context menu (Phase 3.22c).

        Optimistically checkmarks the pick for instant feedback; the
        daemon's post-write re-read emits a fresh ExposureEvent that
        settles the checkmark + chip on what the camera actually
        accepted (a rejected write snaps them back).
        """
        if self._controls_busy:
            return
        code = int(sender.tag())
        self._sync_exposure_mode(code)
        self._daemon.request_set_exposure(2, {"ExposureMode": code})

    def menuIntervalSelect_(self, sender) -> None:  # type: ignore[no-untyped-def]
        secs = int(sender.tag())
        try:
            self._daemon.set_interval(float(secs))
        except Exception as e:  # noqa: BLE001
            logger.warning("set_interval failed: %s", e)
            return
        for s_key, item in self._interval_items.items():
            item.setState_(1 if s_key == secs else 0)

    def menuRevealShots_(self, sender) -> None:  # type: ignore[no-untyped-def]
        """Open the folder the NEXT shot will land in (Phase 3.21)."""
        try:
            if self._cfg.lightroom.mode == "watch":
                folder = Path(str(self._cfg.lightroom.watch_folder)).expanduser()
            else:
                folder = Path(str(self._cfg.output.root)).expanduser()
            folder.mkdir(parents=True, exist_ok=True)
            NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_(
                [NSURL.fileURLWithPath_(str(folder))]
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("reveal_in_finder_failed: %s", e)

    def _set_overlay_label(self, text: str) -> None:
        """Set the LV pause-overlay caption (Phase 3.15).

        The overlay used to be hard-coded "Saving…" for every pause
        cause; the label now tracks the actual state (Saving… /
        Reconnecting… / Camera unresponsive / LV paused).
        """
        try:
            self._lv_overlay_label.setStringValue_(text)
        except Exception:  # noqa: BLE001
            pass

    def _refresh_hint(self, state: str) -> None:
        """Update the footer hint per state.

        Default reads ``␣ shoot · A/⌘A focus · ⌘Q quit`` (the keymap).
        During capture / recovery it switches to a guidance string.
        """
        # Phase 3.21: a just-saved flash outranks the keymap (but
        # never outranks error / recovery guidance).
        if (
            time.monotonic() < getattr(self, "_flash_until", 0.0)
            and state not in ("error", "disconnected", "recovering")
        ):
            self._hint_label.setStringValue_(self._flash_text)
            self._hint_label.setTextColor_(C_AMBER)
            return
        # Phase 3.22: active focus-zoom announces itself + the way out
        # (also outranked by error / recovery guidance).
        zoom_f = getattr(self, "_lv_zoom", 0)
        if zoom_f and state not in ("error", "disconnected", "recovering"):
            nxt = "×8" if zoom_f == 4 else "off"
            self._hint_label.setStringValue_(
                f"Focus zoom ×{zoom_f} — Z: {nxt} · click LV: exit"
            )
            self._hint_label.setTextColor_(C_AMBER)
            return
        if state in ("shooting", "downloading"):
            text = "Camera busy — release space to wait"
            color = C_STATE_BUSY
        elif state == "recovering":
            text = "Settings preserved · auto-resume"
            color = C_STATE_RECOVER
        elif state == "error" or state == "disconnected":
            # Phase 3.21: show the daemon's actual message — the
            # generic power-cycle line hid WHAT went wrong.
            msg = getattr(self, "_last_status_message", "")
            if msg:
                text = msg if len(msg) <= 64 else (msg[:63] + "\u2026")
            else:
                text = "Quit and relaunch after power cycle"
            color = C_STATE_ERROR
        else:
            detach_word = "reattach" if self._lv_window is not None else "detach"
            text = (
                "␣ shoot · A/⌘A focus · H/⌘H hist · G/⌘G grid · "
                f"⌘D {detach_word} · ⌘, prefs · ⌘Q quit"
            )
            color = C_FG_TERTIARY
        self._hint_label.setStringValue_(text)
        self._hint_label.setTextColor_(color)

    def _set_controls_busy(self, busy: bool, *, state: str = "") -> None:
        """Toggle interactive controls + per-state opacity per Phase 3.10.

        Beyond the original enable/disable + force-overlay behaviour,
        this method now dims the exposure block, secondary row, and
        subject field per the spec opacity table (0.4 during capture,
        0.3 during recovery). Hint text is rewritten via _refresh_hint
        so the footer carries state guidance instead of the keymap.

        When un-busying, opacity returns to 1.0; the LV overlay is
        NOT force-hidden (the staleness watchdog owns that transition
        — overlay stays until a fresh frame lands).
        """
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
            self._ev_dropdown,
        ):
            dd.setEnabled_(enabled)

        # Per-state opacity treatment. Lists which views get dimmed
        # together — the LV stays fully visible (its overlay carries
        # the "Saving…" cue), and the status row stays sharp so the
        # state itself remains legible.
        dim_views = (
            self._iso_dropdown,
            self._ss_dropdown,
            self._av_dropdown,
            self._wb_dropdown,
            self._fmt_dropdown,
            self._size_dropdown,
            self._ev_dropdown,
            self._item_field,
            self._shoot_btn,
            self._af_btn,
        )
        if state in ("recovering", "error", "disconnected"):
            alpha = 0.3
        elif state in ("shooting", "downloading"):
            alpha = 0.4
        else:
            alpha = 1.0
        for v in dim_views:
            try:
                v.setAlphaValue_(alpha)
            except Exception:  # noqa: BLE001
                pass

        # Hint footer per state.
        self._refresh_hint(state)

        # Phase 3.15 (D): per-state overlay wording. "Saving…" during a
        # ~4 s USB recovery was a lie (spec 3.10: "Reconnecting…"),
        # and a wedged camera deserves the honest label.
        if state in ("shooting", "downloading"):
            self._set_overlay_label("Saving…")
        elif state in ("recovering", "disconnected"):
            self._set_overlay_label("Reconnecting…")
        elif state == "error":
            self._set_overlay_label("Camera unresponsive")
        elif state == "ready":
            self._set_overlay_label("LV resuming…")

        if busy:
            # Force the overlay up immediately so the user gets
            # feedback without waiting for the 500 ms staleness
            # threshold. Reticle hides with the overlay — re-shown
            # on the next focus-point event or when LV resumes.
            self._lv_overlay.setHidden_(False)
            self._af_marker_view.setHidden_(True)
        elif state == "ready":
            # Phase 3.20d: the save is DONE — drop the dark veil NOW
            # and let the frozen last LV frame show through while the
            # post-capture quiet window runs out (LV frames resume a
            # few seconds later). A camera freezing its preview
            # between shots is natural; a black "Saving…" veil over a
            # Ready panel read as a hang. The staleness watchdog is
            # suppressed for the window's worst case so it doesn't
            # slam the veil back over the frozen frame; if LV is
            # genuinely dead it re-engages after the suppression
            # lapses.
            self._suppress_stale_until = time.monotonic() + 8.0
            self._lv_overlay.setHidden_(True)
            self._reposition_af_marker()

    @objc.signature(b"v@:@")
    def updateShot_(self, tup) -> None:
        """Bump the right-aligned shot counter in the status row.

        The per-shot detail (filename / size / speed) is dropped from
        the default view per the v2 mockup — only the count remains.
        The detail still flows through the daemon's structured log.
        """
        # Phase 3.22 grew the tuple 4→5 (saved-path string for the
        # review overlay). Tolerate the old shape.
        if len(tup) >= 5:
            idx, name, size, _mbps, path_str = tup[:5]
        else:
            idx, name, size, _mbps = tup
            path_str = None
        self._shot_count = max(int(idx), self._shot_count + 1)
        plural = "shot" if self._shot_count == 1 else "shots"
        self._shot_label.setStringValue_(f"{self._shot_count} {plural}")
        self._shot_label.setTextColor_(C_FG_SECONDARY)

        # Phase 3.21: save-confirmation flash. For a few seconds the
        # footer shows exactly WHAT was saved — the "did that actually
        # take?" glance no longer needs Lightroom or a log window.
        try:
            size_mb = float(size) / (1024 * 1024)
            self._flash_text = f"\u2713 #{int(idx)}  {name}  \u00b7  {size_mb:.1f} MB"
        except Exception:  # noqa: BLE001
            self._flash_text = f"\u2713 #{int(idx)}  {name}"
        self._flash_until = time.monotonic() + 4.0
        self._refresh_hint(getattr(self, "_last_daemon_state", "ready"))
        try:
            NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                4.2, False,
                lambda _t: self._refresh_hint(
                    getattr(self, "_last_daemon_state", "")
                ),
            )
        except Exception:  # noqa: BLE001
            pass

        # Phase 3.22 — post-shot review overlay.
        if path_str:
            self._show_review(str(path_str))

    # ----- post-shot review (Phase 3.22) ------------------------------

    def _show_review(self, path_str: str) -> None:
        """Flash the just-saved frame over the LV for a beat.

        Tether-style capture review: after every shutter the viewport
        shows WHAT was captured, full-size, for 2.5 s — the sharpness
        / framing glance no longer needs Lightroom. The JPG half of a
        DNG+JPG pair is used (identical basename per the B5 pair
        guarantee); DNG-only shots skip review, since decoding a
        60 MB raw on the main thread would stall the LV for ~1 s.
        """
        try:
            p = Path(path_str)
            if p.suffix.lower() != ".jpg":
                for cand in (p.with_suffix(".JPG"), p.with_suffix(".jpg")):
                    if cand.exists():
                        p = cand
                        break
                else:
                    return  # DNG-only — no cheap preview to show
            if not p.exists():
                return
            image = NSImage.alloc().initWithContentsOfFile_(str(p))
            if image is None:
                return
        except Exception:  # noqa: BLE001
            return

        try:
            view = self._ensure_review_view()
            if view is None:
                return
            view.setImage_(image)
            view.setToolTip_(p.name)
            view.setHidden_(False)
            # Token-guarded auto-hide: a burst of shots keeps replacing
            # the image and pushes the hide-out, instead of the first
            # shot's timer chopping the last shot's review short.
            self._review_token += 1
            token = self._review_token
            NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                2.5, False,
                lambda _t: (
                    self._hide_review()
                    if self._review_token == token
                    else None
                ),
            )
        except Exception:  # noqa: BLE001
            pass

    def _ensure_review_view(self):  # type: ignore[no-untyped-def]
        """(Re)parent the lazily-created review view over the LV.

        The LV's superview changes across detach/reattach, so the
        review view re-homes itself to wherever the LV currently
        lives and mirrors its frame on every show.
        """
        superview = self._live_view.superview()
        if superview is None:
            return None
        view = self._review_view
        if view is None:
            view = _ReviewImageView.alloc().initWithFrame_(
                self._live_view.frame()
            )
            view.setEditable_(False)
            view.setImageScaling_(NSImageScaleProportionallyUpOrDown)
            view.setWantsLayer_(True)
            layer = view.layer()
            if layer is not None:
                layer.setBackgroundColor_(C_BG_LV_FRAME.CGColor())
                layer.setCornerRadius_(4.0)
                layer.setMasksToBounds_(True)
                # Amber hairline = "this is a capture, not live view".
                layer.setBorderColor_(C_AMBER.CGColor())
                layer.setBorderWidth_(1.0)
            self._review_view = view
        if view.superview() is not superview:
            try:
                view.removeFromSuperview()
            except Exception:  # noqa: BLE001
                pass
            superview.addSubview_(view)
        view.setFrame_(self._live_view.frame())
        return view

    def _hide_review(self) -> bool:
        """Hide the review overlay. Returns True if it was visible."""
        view = self._review_view
        if view is None or view.isHidden():
            return False
        self._review_token += 1  # cancels any pending auto-hide
        view.setHidden_(True)
        return True

    @objc.signature(b"v@:@")
    def updateExposure_(self, tup) -> None:
        """Sync the dropdowns to the camera's reported exposure.

        The action callbacks set ``_suppress_action`` so this method
        doesn't recursively re-queue writes when it programmatically
        selects items.
        """
        # Phase 3.22 grew the tuple 7→9 (exp-comp + battery); 3.22c
        # 9→10 (exposure mode). Tolerate older shapes for any marshal
        # in flight across a reload.
        mode_raw = None
        if len(tup) >= 10:
            (iso_raw, iso_auto, ss_raw, av_raw, wb_raw, fmt_raw,
             size_raw, ec_raw, bat_raw, mode_raw) = tup[:10]
        elif len(tup) >= 9:
            (iso_raw, iso_auto, ss_raw, av_raw, wb_raw, fmt_raw,
             size_raw, ec_raw, bat_raw) = tup[:9]
        else:
            iso_raw, iso_auto, ss_raw, av_raw, wb_raw, fmt_raw, size_raw = tup
            ec_raw, bat_raw = 0, None
        self._exposure_raw = {
            "ISOSpeed": iso_raw,
            "ISOAuto": iso_auto,
            "ShutterSpeed": ss_raw,
            "Aperture": av_raw,
            "WhiteBalance": wb_raw,
            "ImageQuality": fmt_raw,
            "Resolution": size_raw,
            "ExpComp": ec_raw,
        }
        if mode_raw is not None:
            self._exposure_raw["ExposureMode"] = mode_raw
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
            self._select_dropdown_by_repr(self._ev_dropdown, ec_raw)
        finally:
            self._suppress_action = False

        # Battery pill (Phase 3.22) — refresh outside the suppress
        # block; it's display-only and never fires an action.
        if bat_raw is not None:
            self._update_battery(int(bat_raw))
        # Exposure-mode chip + menu checkmarks (Phase 3.22c).
        if mode_raw is not None:
            self._sync_exposure_mode(int(mode_raw))

    def _sync_exposure_mode(self, mode_raw: int) -> None:
        """Reflect DG2.ExposureMode into the LV chip + context menu."""
        letter = exposure_mode_label(mode_raw)
        try:
            self._mode_chip_label.setStringValue_(letter)
            self._mode_chip.setHidden_(letter == "—")
            for code, item in getattr(self, "_mode_items", {}).items():
                item.setState_(1 if code == mode_raw else 0)
        except Exception:  # noqa: BLE001
            pass

    def _update_battery(self, raw: int) -> None:
        """Render DG1.BatteryState into the LV pill (Phase 3.22b).

        3-segment gauge matching the body's own indicator, coloured by
        severity (white = ok, amber = low, red = empty). Unknown raw
        values hide the pill rather than guess. The tooltip headline
        is a plain reading ("Battery level 1/3") with the raw byte in
        parentheses for ongoing field calibration.
        """
        if raw == self._last_battery_raw:
            return
        self._last_battery_raw = raw
        text = battery_label(raw)
        try:
            self._battery_label.setStringValue_(text)
            tip = battery_describe(raw)
            self._battery_pill.setToolTip_(tip)
            self._battery_label.setToolTip_(tip)
            klass = battery_level_class(raw)
            if klass == "critical":
                self._battery_label.setTextColor_(C_STATE_ERROR)
            elif klass == "low":
                self._battery_label.setTextColor_(C_AMBER_BRIGHT)
            else:
                self._battery_label.setTextColor_(C_FG_PRIMARY)
            self._battery_pill.setHidden_(not text)
        except Exception:  # noqa: BLE001
            pass

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

            # Exposure compensation (Phase 3.22). CamCanSetInfo5 does
            # NOT advertise ExpComp codes on fp L V90, so the ladder
            # is static: ±3 EV in 1/3 steps, same as the body dial.
            self._fill_dropdown(
                self._ev_dropdown,
                [(expcomp_label(c), c) for c in EXP_COMP_CODES],
            )
            # Default to ±0 until the first exposure event lands —
            # _fill_dropdown's index-0 selection would show "+3.0".
            self._select_dropdown_by_repr(self._ev_dropdown, 0)
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
                    self._exposure_raw.get("ExpComp", 0),
                    None,  # battery unchanged — pill keeps last value
                    self._exposure_raw.get("ExposureMode", None),
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
        if self._lv_zoom:
            # Phase 3.22 — the reticle's mapping assumes the full
            # frame; on a zoom crop the AF point IS the crop centre,
            # so the corner ticks would just mislead.
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

        Returns (x, y) in the LV view's *superview* coordinate space
        — that's panel content while attached, and the detached
        window's content view while detached (Phase 3.12). We read
        ``_live_view.frame()`` rather than hard-coding LV_X/LV_Y so
        the mapping works in both states; the AF marker shares the
        same superview as the LV view in both cases, so its frame
        can be set directly to a rect centred on the returned coord.
        AppKit Y goes up from bottom-left; camera Y goes down from
        top-left → we flip Y.
        """
        x_min, x_max, y_min, y_max = self.af_bounds()
        nx = (cam_x - x_min) / max(1, (x_max - x_min))
        ny = (cam_y - y_min) / max(1, (y_max - y_min))
        nx = max(0.0, min(1.0, nx))
        ny = max(0.0, min(1.0, ny))
        # Phase 3.23 — the displayed frame may be turned 90/180/270°;
        # the reticle has to travel with the picture, not the sensor.
        nx, ny = self._rot_fwd(nx, ny)
        lv = self._live_view.frame()
        ox, oy, fw, fh = self.lv_fit_rect(lv.size.width, lv.size.height)
        vx = lv.origin.x + ox + nx * fw
        vy = lv.origin.y + oy + (1.0 - ny) * fh  # flip Y (AppKit Y goes up)
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
        # 4-tuple since Phase 3.11 — histogram trails the jpeg/w/h.
        # Tolerate the old 3-tuple shape for any in-flight performSelector
        # marshalled before the panel rebuilt (e.g. across a config reload).
        if len(tup) == 4:
            jpeg, width, height, histogram = tup
        else:
            jpeg, width, height = tup
            histogram = None
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
        # Phase 3.22 — focus zoom: swap in a ×4/×8 crop around the AF
        # point. Falls back to the full frame on any crop failure.
        if self._lv_zoom:
            zoomed = self._zoom_crop(image, width, height)
            if zoomed is not None:
                image = zoomed
        # Phase 3.23 — manual rotation LAST, so the zoom crop above
        # keeps working in unrotated camera coords (where _focus_xy
        # lives) and only the presentation is turned.
        image = self._rotate_image(image)
        self._live_view.setImage_(image)

        # Push the side-channel histogram snapshot into the bottom-strip
        # view. ``setData_`` is cheap (stash + setNeedsDisplay) so it's
        # fine to call on every frame even when the strip is hidden.
        if histogram is not None and getattr(self, "_hist_view", None) is not None:
            self._hist_view.setData_(histogram)

    # ----- dropdown helpers -------------------------------------------

    def _fill_dropdown(
        self,
        dropdown,  # type: ignore[no-untyped-def]
        items: list[tuple[str, object]],
    ) -> None:
        """Replace dropdown's items with ``(title, representedObject)`` pairs.

        Phase 3.15 (A8): items are appended as explicit ``NSMenuItem``s
        on the popup's menu. The previous ``addItemWithTitle_`` path
        uses AppKit's title-dedup semantics — an existing item with the
        same title is silently REMOVED — which made shutter stops
        disappear whenever two codes rendered the same label (0.6"/0.8"
        both showed "1/2" before apex_to_shutter learned sub-second
        decimals, and the fp-L-V90 fallback list produces legitimate
        near-duplicate labels). Every code keeps its own row now, even
        if two labels tie after rounding.
        """
        dropdown.removeAllItems()
        menu = dropdown.menu()
        for title, repr_obj in items:
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                str(title), None, ""
            )
            item.setRepresentedObject_(repr_obj)
            menu.addItem_(item)
        if dropdown.numberOfItems() > 0:
            dropdown.selectItemAtIndex_(0)
            dropdown.synchronizeTitleAndSelectedItem()

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

    @objc.signature(b"v@:@")
    def evChanged_(self, sender) -> None:
        """Exposure compensation (DG1.ExpComp, APEX 1/8-stop, 2's-comp).

        Phase 3.22. In M mode without auto-ISO the camera treats
        ExpComp as a metering readout, so a write may be ignored — the
        daemon's post-write re-read emits what actually took effect
        and the dropdown snaps back, which is the honest behaviour.
        """
        if self._suppress_action:
            return
        repr_obj = self._selected_repr(sender)
        if isinstance(repr_obj, int):
            self._daemon.request_set_exposure(
                1, {"ExpComp": int(repr_obj)}
            )

    # --- AF point (LV click → camera) -------------------------------

    def commit_focus_point(self, cam_x: int, cam_y: int) -> None:
        """Push a new AF point to the camera (called from LV mouse handler)."""
        # Phase 3.22 click precedence: a review thumbnail on top of
        # the LV means the click-point isn't the live scene — dismiss
        # the review instead of blindly moving AF. Likewise while
        # focus-zoomed the click mapping assumes the full frame, so a
        # click exits the zoom rather than jumping the AF point.
        if self._hide_review():
            return
        if self._lv_zoom:
            self._set_zoom_factor(0)
            return
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
            # Reset shot counter since the session restarts.
            self._shot_count = 0
            self._shot_label.setStringValue_("0 shots")
            self._shot_label.setTextColor_(C_FG_TERTIARY)

    # --- panel delegate -----------------------------------------------

    def windowShouldClose_(self, sender) -> bool:
        """Wire the panel's close button (×) → graceful daemon shutdown.

        Returning False keeps AppKit from auto-closing the window mid-
        shutdown; ``self.stop()`` calls panel.close() + app.terminate_
        in the right order.
        """
        try:
            self.stop()
        except Exception as e:  # noqa: BLE001
            logger.warning("stop() raised from windowShouldClose_: %s", e)
        return False

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

    # ----- Phase 3.11 overlay toggles ---------------------------------

    def _toggle_histogram(self) -> None:
        """Hotkey H — flip the bottom-strip histogram visibility.

        Also tells the daemon (→ LV stream) to stop / start computing
        so a hidden strip doesn't waste CPU. The compute state and the
        view-hidden state are kept in sync via this single entry point.
        """
        view = getattr(self, "_hist_view", None)
        if view is None:
            return
        new_visible = bool(view.isHidden())  # toggle
        view.setHidden_(not new_visible)
        # Phase 3.22: an explicit H during focus-zoom overrides the
        # zoom's force-hide — keep the zoom-exit restore in sync with
        # the user's latest intent rather than the pre-zoom snapshot.
        if getattr(self, "_lv_zoom", False):
            self._zoom_hist_was_visible = new_visible
        try:
            self._daemon.set_histogram_enabled(new_visible)
        except Exception as e:  # noqa: BLE001
            logger.warning("histogram toggle: daemon side raised: %s", e)
        # Persist into the in-memory config so a recovery / restart
        # picks up the user's choice. (Disk-level persistence is a
        # future enhancement — see config.toml docs.)
        try:
            self._cfg.liveview.show_histogram = new_visible
        except Exception:  # noqa: BLE001
            pass

    def _cycle_grid(self) -> None:
        """Hotkey G — cycle composition grid mode off → thirds → golden → full.

        Pure presentation: the grid view's drawRect is a no-op when
        mode == "off", so cycling through "off" does NOT cost any
        redraw beyond a single setNeedsDisplay tick.
        """
        view = getattr(self, "_grid_view", None)
        if view is None:
            return
        current = str(view.mode())
        new_mode = _grid_cycle(current)  # type: ignore[arg-type]
        view.setMode_(new_mode)
        try:
            self._cfg.liveview.grid_mode = new_mode  # type: ignore[assignment]
        except Exception:  # noqa: BLE001
            pass
        # Briefly surface the new mode in the hint footer so the user
        # has visual confirmation the keystroke registered.
        try:
            detach_word = "reattach" if self._lv_window is not None else "detach"
            self._hint_label.setStringValue_(
                f"Grid: {new_mode}    ␣ shoot · A/⌘A focus · H/⌘H hist · "
                f"G/⌘G grid · ⌘D {detach_word} · ⌘Q quit"
            )
        except Exception:  # noqa: BLE001
            pass

    # ----- focus zoom (Phase 3.22) ------------------------------------

    def _toggle_focus_zoom(self) -> None:
        """Hotkey Z — cycle AF-point magnification off → ×4 → ×8 → off.

        Same ladder as the body's own MF assist. Repro / product work
        needs pixel-level focus confirmation that a proportional fit
        can't show. While zoomed: ``updateLiveFrame_`` crops 1/f of
        the frame around ``_focus_xy`` (so the magnification is
        relative to the full view and works at ANY viewport size,
        attached or detached); grid + reticle hide (their geometry is
        meaningless on a crop); an amber ×4/×8 badge marks the state;
        Z cycles onward, a click on the LV exits.

        (v1 defined zoom as a 1:1 device-pixel crop — on a large
        detached LV window the full frame is already ≥ native, so
        the zoom visibly did nothing. Factor-based is what a
        photographer means by "zoom".)
        """
        self._set_zoom_factor({0: 4, 4: 8}.get(self._lv_zoom, 0))

    def _set_zoom_factor(self, factor: int) -> None:
        if factor == self._lv_zoom:
            return
        was_off = not self._lv_zoom
        self._lv_zoom = factor
        try:
            if factor:
                self._zoom_badge.subviews()[0].setStringValue_(f"×{factor}")
                self._zoom_badge.setHidden_(False)
                self._grid_view.setHidden_(True)
                if was_off:
                    self._zoom_hist_was_visible = (
                        not self._hist_view.isHidden()
                    )
                self._hist_view.setHidden_(True)
                self._af_marker_view.setHidden_(True)
            else:
                self._zoom_badge.setHidden_(True)
                self._grid_view.setHidden_(False)
                if self._zoom_hist_was_visible:
                    self._hist_view.setHidden_(False)
                self._zoom_hist_was_visible = False
                self._reposition_af_marker()
        except Exception:  # noqa: BLE001
            pass
        self._refresh_hint(getattr(self, "_last_daemon_state", "ready"))

    def _zoom_crop(self, image, src_w: int, src_h: int):  # type: ignore[no-untyped-def]
        """Crop 1/factor of ``image`` around the AF point (Phase 3.22b).

        The crop fills the viewport afterwards, so the net effect is
        ×factor magnification relative to the full-frame fit —
        independent of viewport size, which is what fixed the
        "detached window zoom does nothing" report. Returns None on
        any failure so the caller falls back to the plain fit.
        """
        factor = self._lv_zoom
        if not factor:
            return None
        try:
            # Prefer the image's own coordinate space over the parsed
            # SOF dims — identical for the fp L's 72-dpi LV JPEGs, but
            # drawInRect fromRect is defined in image-size units.
            isz = image.size()
            iw = float(isz.width) or float(src_w)
            ih = float(isz.height) or float(src_h)
            if iw <= 0 or ih <= 0:
                return None
            cw = max(1.0, iw / factor)
            ch = max(1.0, ih / factor)
            # Centre on the AF point (camera coords → normalised).
            if self._focus_xy is not None:
                x_min, x_max, y_min, y_max = self.af_bounds()
                nx = (self._focus_xy[0] - x_min) / max(1, x_max - x_min)
                ny = (self._focus_xy[1] - y_min) / max(1, y_max - y_min)
                nx = max(0.0, min(1.0, nx))
                ny = max(0.0, min(1.0, ny))
            else:
                nx = ny = 0.5
            cx = nx * iw
            cy_td = ny * ih          # camera Y is top-down
            x0 = max(0.0, min(iw - cw, cx - cw / 2.0))
            y0 = max(0.0, min(ih - ch, (ih - cy_td) - ch / 2.0))  # flip Y
            out = NSImage.alloc().initWithSize_(NSMakeSize(cw, ch))
            out.lockFocus()
            try:
                image.drawInRect_fromRect_operation_fraction_(
                    NSMakeRect(0, 0, cw, ch),
                    NSMakeRect(x0, y0, cw, ch),
                    _NS_COMPOSITE_COPY,
                    1.0,
                )
            finally:
                out.unlockFocus()
            return out
        except Exception:  # noqa: BLE001
            return None

    # ----- LV rotation (Phase 3.23) -----------------------------------

    def _cycle_lv_rotation(self) -> None:
        """Hotkey R — rotate the LV 90° clockwise, cycling back to 0°.

        Why manual: the fp L's live-view stream is always delivered in
        sensor-native landscape and the body publishes no attitude /
        level data over PTP (Phase 3.11c settled that — it isn't in
        any DataGroup), so there is nothing to auto-detect from. One
        keystroke per quarter-turn is the honest UI.
        """
        self._set_lv_rotation((self._lv_rotation + 90) % 360)

    def _set_lv_rotation(self, degrees: int) -> None:
        degrees = int(degrees) % 360
        if degrees not in (0, 90, 180, 270) or degrees == self._lv_rotation:
            return
        self._lv_rotation = degrees
        # Menu checkmarks + reticle follow immediately; the image
        # itself picks it up on the next LV frame (≤100 ms at 10 fps).
        for deg, item in getattr(self, "_rotation_items", {}).items():
            item.setState_(1 if deg == degrees else 0)
        self._reposition_af_marker()
        try:
            self._hint_label.setStringValue_(
                f"LV rotation: {degrees}°    R rotate · ␣ shoot · "
                f"A focus · Z zoom · ⌘D detach"
            )
        except Exception:  # noqa: BLE001
            pass

    def _rotate_image(self, image):  # type: ignore[no-untyped-def]
        """Return ``image`` rotated ``self._lv_rotation`` degrees clockwise.

        Returns the input unchanged at 0° (the common case — no
        per-frame allocation when rotation is off) and on any failure,
        so a bad rotate degrades to an unrotated LV rather than a
        frozen one.
        """
        deg = self._lv_rotation
        if not deg:
            return image
        try:
            isz = image.size()
            w = float(isz.width)
            h = float(isz.height)
            if w <= 0 or h <= 0:
                return image
            swapped = deg in (90, 270)
            out_w, out_h = (h, w) if swapped else (w, h)
            out = NSImage.alloc().initWithSize_(NSMakeSize(out_w, out_h))
            out.lockFocus()
            try:
                xform = NSAffineTransform.transform()
                # Rotate about the destination centre, then step back
                # by half the SOURCE size so the drawn rect lands
                # centred. AppKit's rotateByDegrees_ is CCW-positive,
                # so a clockwise turn is a negative angle.
                xform.translateXBy_yBy_(out_w / 2.0, out_h / 2.0)
                xform.rotateByDegrees_(-deg)
                xform.translateXBy_yBy_(-w / 2.0, -h / 2.0)
                xform.concat()
                image.drawInRect_fromRect_operation_fraction_(
                    NSMakeRect(0, 0, w, h),
                    NSMakeRect(0, 0, w, h),
                    _NS_COMPOSITE_COPY,
                    1.0,
                )
            finally:
                out.unlockFocus()
            return out
        except Exception:  # noqa: BLE001
            return image

    def _rot_fwd(self, nx: float, ny: float) -> tuple[float, float]:
        """Image-normalised (x right, y down) → displayed-normalised."""
        return rotate_forward(self._lv_rotation, nx, ny)

    def _rot_inv(self, dx: float, dy: float) -> tuple[float, float]:
        """Displayed-normalised → image-normalised. Inverse of _rot_fwd."""
        return rotate_inverse(self._lv_rotation, dx, dy)

    def lv_fit_rect(
        self, container_w: float, container_h: float
    ) -> tuple[float, float, float, float]:
        """Where the LV image actually lands inside a container rect.

        ``NSImageScaleProportionallyUpOrDown`` centres the frame and
        letterboxes the remainder, so view coords are NOT image coords
        whenever the aspect ratios differ. That was a small error
        before (LV JPEG vs the 288×200 viewport); with a 90° rotation
        it becomes a large one — a portrait frame in a landscape box
        is mostly black bar, and an AF click in the bar used to map to
        a bogus sensor coord. Returns (ox, oy, w, h) in the
        container's own coordinate space, origin bottom-left.
        """
        fallback = (0.0, 0.0, container_w, container_h)
        try:
            img = self._live_view.image()
            if img is None:
                return fallback
            isz = img.size()
            return fit_rect(
                container_w, container_h, float(isz.width), float(isz.height)
            )
        except Exception:  # noqa: BLE001
            return fallback

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
            # Phase 3.20d: post-save grace — the quiet window means
            # frames are EXPECTED to stall for a few seconds; keep
            # showing the frozen frame instead of slamming the veil
            # back. Re-engages automatically once the grace lapses.
            if time.monotonic() < getattr(self, "_suppress_stale_until", 0.0):
                return
            # Phase 3.15 (D): a stalled stream is not a save. Busy
            # states own their own label (set in _set_controls_busy);
            # a watchdog-triggered pause reads "LV paused".
            if not self._controls_busy:
                self._set_overlay_label("LV paused")
            self._lv_overlay.setHidden_(False)
        elif not stale and not is_hidden:
            # Fix 4: busy gate wins — if the daemon is in commit
            # window, hold the overlay up even when frames look
            # fresh (e.g. tail-end stream queue draining).
            if not self._controls_busy:
                self._lv_overlay.setHidden_(True)
                self._reposition_af_marker()

    # ----- Phase 3.12 — LV detach / reattach --------------------------

    def _toggle_lv_detached(self) -> None:
        """Single ⌘D entry point — detach if attached, reattach if detached."""
        if self._lv_window is None:
            self._detach_lv()
        else:
            self._reattach_lv()

    def _resize_panel_for_detach_state(self, detached: bool) -> None:
        """Resize the host panel for the given detach state (Phase 3.13).

        ``PANEL_HEIGHT_ATTACHED`` / ``PANEL_HEIGHT_DETACHED`` are
        **content-area** heights (matching the initWithContentRect_
        contract used in ``_build_window``). The actual NSWindow frame
        adds title-bar chrome on top, so we ask AppKit to convert via
        ``frameRectForContentRect_`` before applying.

        The top edge is anchored to the same screen-y across the resize:
        AppKit uses a bottom-left origin, so we compute the current top
        edge first, then place the new frame so its top still hits that
        coordinate.

        Uses ``setFrame_display_(frame, True)`` (no animate flag) — the
        resize is instant by design (質実剛健 / no UI transitions).
        """
        new_content_height = (
            PANEL_HEIGHT_DETACHED if detached else PANEL_HEIGHT_ATTACHED
        )
        current_window = self._panel.frame()
        current_content_height = self._panel.contentView().frame().size.height
        if current_content_height == new_content_height:
            return

        # Ask AppKit how big the WINDOW must be to host this content rect.
        # The chrome (title bar etc.) gets added automatically.
        desired_content_rect = NSMakeRect(
            0, 0, PANEL_WIDTH, new_content_height
        )
        desired_window_frame = self._panel.frameRectForContentRect_(
            desired_content_rect
        )

        # Keep the top edge anchored: derive new origin.y so that
        # origin.y + size.height == old top edge.
        top_y = current_window.origin.y + current_window.size.height
        new_frame = NSMakeRect(
            current_window.origin.x,
            top_y - desired_window_frame.size.height,
            desired_window_frame.size.width,
            desired_window_frame.size.height,
        )

        try:
            self._panel.setFrame_display_(new_frame, True)
        except Exception:  # noqa: BLE001
            # Resize failure is non-fatal — the LV ownership move
            # already succeeded; a stuck-large panel is recoverable
            # via reattach, a stuck-small panel via detach toggle.
            pass

    def _detach_lv(self) -> None:
        """Lift the 5 LV-area views into a new ``LVDetachedWindow``.

        Ownership move (not redraw) — histogram thread pool, grid mode,
        AF reticle, and pause overlay all keep their state.
        """
        if self._lv_window is not None:
            return  # already detached, no-op

        # Phase 3.22: drop any visible review overlay first — it's
        # parented to the LV's *current* superview and would otherwise
        # linger over the shrunken panel. It re-homes on next show.
        self._hide_review()

        content = self._panel.contentView()

        # Step 1: lift the 5 LV-area views off the panel content. We
        # keep strong refs via self._* already, so removeFromSuperview
        # just unparents them — the views themselves remain alive.
        for view in (
            self._live_view,
            self._grid_view,
            self._hist_view,
            self._lv_overlay,
            self._af_marker_view,
        ):
            try:
                view.removeFromSuperview()
            except Exception:  # noqa: BLE001
                pass

        # Step 2: build & show the detached window with the lifted views.
        # Phase 3.15 (A4): the resolver reports whether the rect is a
        # cached WINDOW frame (title bar included) or a content-sized
        # default — the factory needs to know which coordinate system
        # it's holding, otherwise the window grows by the title-bar
        # height on every detach cycle.
        initial_frame, frame_is_window = self._resolve_lv_window_frame()
        self._lv_window = LVDetachedWindow.make(
            self,
            initial_frame,
            self._live_view,
            self._grid_view,
            self._hist_view,
            self._lv_overlay,
            self._af_marker_view,
            frame_is_window_frame=frame_is_window,
        )
        if self._lv_window is None:
            # Construction failed — put the views back and bail. Better
            # to leave the user attached than half-detached.
            content.addSubview_(self._live_view)
            content.addSubview_(self._grid_view)
            content.addSubview_(self._hist_view)
            content.addSubview_(self._lv_overlay)
            content.addSubview_(self._af_marker_view)
            return
        self._lv_window.makeKeyAndOrderFront_(None)

        # Step 3 (Phase 3.13): no more placeholder. Instead, shrink the
        # host panel so only the control surface (status + exposure +
        # dropdowns + subject + buttons + hint) remains visible. Top
        # edge is preserved so the user's panel position doesn't jump.
        self._placeholder = None
        self._resize_panel_for_detach_state(detached=True)

        # Step 4: reposition AF reticle relative to the detached window's
        # LV bounds (cam_to_view now reads _live_view.frame()).
        try:
            self._reposition_af_marker()
        except Exception:  # noqa: BLE001
            pass

        # Step 5: persist (detached=True, frame=actual WINDOW frame).
        # Phase 3.15 (A4): read the frame back from the window we just
        # made rather than echoing ``initial_frame`` — the cache field
        # holds window-frame coordinates exclusively now (every other
        # save site uses ``window.frame()``), so mixed content/window
        # rects can no longer accumulate title-bar drift.
        # NSRect → tuple conversion required: settings_cache.LVWindowState
        # expects a 4-tuple of floats, never a raw NSRect/CGPoint (those
        # are opaque C structs that json.dumps can't serialise).
        actual = self._lv_window.frame()
        initial_tuple: tuple[float, float, float, float] = (
            float(actual.origin.x),
            float(actual.origin.y),
            float(actual.size.width),
            float(actual.size.height),
        )
        self._save_lv_window_state(detached=True, frame=initial_tuple)

        # Step 6: refresh hint so the footer shows "⌘D reattach".
        try:
            self._refresh_hint(getattr(self._daemon, "state", ""))
        except Exception:  # noqa: BLE001
            pass

    def _reattach_lv(self) -> None:
        """Reverse of ``_detach_lv``. Idempotent if already attached."""
        if self._lv_window is None:
            return

        # Phase 3.22: same review-overlay hygiene as _detach_lv.
        self._hide_review()

        content = self._panel.contentView()

        # Capture final frame for cache before we close the window —
        # NSWindow.frame() may return zeroes after orderOut.
        try:
            final_frame = self._lv_window.frame()
            final_tuple: tuple[float, float, float, float] | None = (
                float(final_frame.origin.x),
                float(final_frame.origin.y),
                float(final_frame.size.width),
                float(final_frame.size.height),
            )
        except Exception:  # noqa: BLE001
            final_tuple = None

        # Step 1: defensive placeholder cleanup. Phase 3.13 no longer
        # constructs LVDetachedPlaceholder, but a stale instance could
        # exist if some earlier code path attached one — clear it so
        # the panel content view doesn't keep an orphaned subview.
        if self._placeholder is not None:
            try:
                self._placeholder.removeFromSuperview()
            except Exception:  # noqa: BLE001
                pass
            self._placeholder = None

        # Step 2: lift the 5 LV-area views off the detached window.
        for view in (
            self._live_view,
            self._grid_view,
            self._hist_view,
            self._lv_overlay,
            self._af_marker_view,
        ):
            try:
                view.removeFromSuperview()
            except Exception:  # noqa: BLE001
                pass

        # Step 2.5 (Phase 3.13): expand the panel back to attached
        # height BEFORE we write the canonical view frames. Top edge
        # stays anchored so the panel grows down, not up.
        self._resize_panel_for_detach_state(detached=False)

        # Step 3: restore each view's original panel-relative frame.
        # The detached window resized them to its content bounds; we
        # need to write the canonical panel slot back so they fit.
        self._live_view.setFrame_(NSMakeRect(LV_X, LV_Y, LV_WIDTH, LV_HEIGHT))
        self._grid_view.setFrame_(NSMakeRect(LV_X, LV_Y, LV_WIDTH, LV_HEIGHT))
        self._hist_view.setFrame_(NSMakeRect(LV_X, LV_Y, LV_WIDTH, HIST_STRIP_H))
        # Phase 3.13: revert hist to bottom-strip rendering on the
        # compact panel. setMode_ is no-op if already bottom_strip.
        try:
            self._hist_view.setMode_("bottom_strip")
        except Exception:  # noqa: BLE001
            pass
        self._lv_overlay.setFrame_(NSMakeRect(LV_X, LV_Y, LV_WIDTH, LV_HEIGHT))
        # Pause overlay label needs its panel-slot centre too.
        try:
            self._lv_overlay_label.setFrame_(
                NSMakeRect(0, (LV_HEIGHT - 22) // 2, LV_WIDTH, 22)
            )
        except Exception:  # noqa: BLE001
            pass
        # AF marker frame is positioned by _reposition_af_marker below;
        # initialise to a centred default so the first paint isn't junk.
        self._af_marker_view.setFrame_(
            NSMakeRect(
                LV_X + (LV_WIDTH - AF_MARKER_SIZE) // 2,
                LV_Y + (LV_HEIGHT - AF_MARKER_SIZE) // 2,
                AF_MARKER_SIZE,
                AF_MARKER_SIZE,
            )
        )

        # Step 4: re-add to panel content in the original z-order.
        content.addSubview_(self._live_view)
        content.addSubview_(self._grid_view)
        content.addSubview_(self._hist_view)
        content.addSubview_(self._lv_overlay)
        content.addSubview_(self._af_marker_view)

        # Step 5: drop the window. orderOut_ + drop the ref. Window has
        # ``releasedWhenClosed=False`` so the Python ref is what keeps
        # it alive; clearing it lets ARC free everything.
        try:
            self._lv_window.orderOut_(None)
        except Exception:  # noqa: BLE001
            pass
        self._lv_window = None

        # Step 6: reposition AF reticle relative to panel coords.
        try:
            self._reposition_af_marker()
        except Exception:  # noqa: BLE001
            pass

        # Step 7: persist (detached=False, frame=last-known so we remember
        # the user's preferred geometry next detach).
        self._save_lv_window_state(detached=False, frame=final_tuple)

        # Step 8: refresh hint so footer shows "⌘D detach".
        try:
            self._refresh_hint(getattr(self._daemon, "state", ""))
        except Exception:  # noqa: BLE001
            pass

    # ----- Phase 3.12 — frame resolution / persistence ----------------

    def _resolve_lv_window_frame(self):  # type: ignore[no-untyped-def]
        """Return ``(NSRect, frame_is_window_frame)`` for the next detach.

        Order of preference:
        1. Cached frame, if its origin lies inside any current NSScreen
           visibleFrame (display still attached).
        2. Cached frame's *size*, re-centred on the main screen
           (display disappeared since last save — fall back gracefully).
        3. Default 720×480 centred on main screen (no cache yet).

        Phase 3.15 (A4): the bool tells the caller which coordinate
        system the rect is in — cached values are WINDOW frames
        (``window.frame()``), the first-run default is a CONTENT size.
        """
        cached_lv = None
        try:
            cached_lv = self._user_settings.lv_window if self._user_settings else None
        except AttributeError:
            cached_lv = None

        if cached_lv is None or cached_lv.frame is None:
            return self._default_lv_window_frame(), False

        x, y, w, h = cached_lv.frame
        saved = NSMakeRect(x, y, w, h)
        if self._frame_origin_on_any_screen(saved):
            return saved, True

        # Display disappeared — keep size, re-centre on main.
        try:
            main = NSScreen.mainScreen().visibleFrame()
            cx = main.origin.x + (main.size.width - w) / 2.0
            cy = main.origin.y + (main.size.height - h) / 2.0
            return NSMakeRect(cx, cy, w, h), True
        except Exception:  # noqa: BLE001
            return self._default_lv_window_frame(), False

    def _default_lv_window_frame(self):  # type: ignore[no-untyped-def]
        """Default detached frame — 720×480 centred on main screen.

        720×480 is 2.5× the original 288×200 panel slot; comfortably
        below the LV source's native 1620×1080 so no aliasing.
        """
        # Pull dimensions from the static config so a future user-side
        # tweak doesn't require touching panel code.
        try:
            cfg_lv = self._cfg.liveview.lv_window
            w = float(cfg_lv.default_width)
            h = float(cfg_lv.default_height)
        except AttributeError:
            w, h = 720.0, 480.0
        try:
            main = NSScreen.mainScreen().visibleFrame()
            cx = main.origin.x + (main.size.width - w) / 2.0
            cy = main.origin.y + (main.size.height - h) / 2.0
        except Exception:  # noqa: BLE001
            cx, cy = 100.0, 100.0
        return NSMakeRect(cx, cy, w, h)

    def _frame_origin_on_any_screen(self, rect) -> bool:  # type: ignore[no-untyped-def]
        """Return True if the rect's origin is inside any NSScreen.visibleFrame.

        Origin-only check — macOS ``constrainFrameRect`` handles the
        oversize-vs-screen case for us, so we only need to ensure the
        window won't open in a void.
        """
        try:
            screens = NSScreen.screens()
        except Exception:  # noqa: BLE001
            return False
        if screens is None:
            return False
        origin = rect.origin
        for screen in screens:
            try:
                if NSPointInRect(origin, screen.visibleFrame()):
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _on_lv_window_frame_changed(self, frame) -> None:  # type: ignore[no-untyped-def]
        """Debounced save: detached window moved or resized.

        Bumps the pending-save token; schedules a dispatch_after that
        only writes if the token still matches when it fires. Rapid
        drag bursts coalesce to a single disk write.
        """
        self._pending_save_token += 1
        my_token = self._pending_save_token
        try:
            debounce_ms = int(self._cfg.liveview.lv_window.save_debounce_ms)
        except AttributeError:
            debounce_ms = 250

        # Capture frame as a tuple now (frame is NSRect by-value; safe
        # to read after the dispatch_after fires too, but explicit).
        captured: tuple[float, float, float, float] = (
            float(frame.origin.x),
            float(frame.origin.y),
            float(frame.size.width),
            float(frame.size.height),
        )

        def _flush() -> None:
            if my_token != self._pending_save_token:
                return  # superseded by a newer event
            self._save_lv_window_state(detached=True, frame=captured)

        # We're already on the main thread (delegate callback). Schedule
        # via NSTimer rather than libdispatch — NSTimer is the simplest
        # PyObjC-friendly path that doesn't need a Foundation dispatch
        # import dance, and the main-thread runloop dispatches it.
        try:
            NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                debounce_ms / 1000.0, False, lambda _t: _flush()
            )
        except Exception:  # noqa: BLE001
            # Fallback: write immediately if NSTimer.block_ API is not
            # available (older PyObjC). Misses the debounce but the
            # disk write is cheap.
            _flush()

    def _save_lv_window_state(
        self,
        *,
        detached: bool,
        frame: tuple[float, float, float, float] | None,
    ) -> None:
        """Persist (detached, frame) into ~/.fp-l-tether/user_settings.json.

        Re-loads the cache first so the watcher's dg1/dg2 fields aren't
        clobbered by our lv_window-only write. If the cache file doesn't
        exist yet, a fresh ``SettingsCache`` is created with empty dg1/dg2.
        """
        try:
            on_disk = load_settings_cache()
        except Exception:  # noqa: BLE001
            on_disk = None
        cache = on_disk if on_disk is not None else SettingsCache()
        cache.lv_window = LVWindowState(detached=bool(detached), frame=frame)
        try:
            save_settings_cache(cache)
        except Exception as e:  # noqa: BLE001
            logger.warning("lv_window cache save failed: %s", e)
            return
        # Update our in-memory copy so subsequent loads (e.g. on quit)
        # see the latest state without a re-read.
        self._user_settings = cache

    # ----- preferences window (Phase 3.14) ----------------------------

    def _open_preferences(self) -> None:
        """Open (or re-show) the Preferences window — ⌘, handler.

        Singleton: the controller is constructed on first call and
        reused thereafter. ``show()`` recenters on first display only,
        so reopening after the user dragged the window keeps the
        position they chose.
        """
        if self._prefs_controller is None:
            try:
                self._prefs_controller = PreferencesWindow(self)
            except Exception as e:  # noqa: BLE001
                logger.warning("Preferences window construction failed: %s", e)
                return
        try:
            self._prefs_controller.show()
        except Exception as e:  # noqa: BLE001
            logger.warning("Preferences window show failed: %s", e)

    # ----- hotkeys (global within app) --------------------------------

    def _install_hotkeys(self) -> None:
        """Catch keystrokes while ANY app is focused — limited to space/quit.

        We use a local monitor on NSEventMaskKeyDown that fires when the
        panel app is active. Since the panel is a non-activating utility
        panel, focus stays on Lightroom, so we ALSO install a global
        monitor for when Lightroom has focus.
        """

        # Modifier-mask helper for the pure-⌘ overlay shortcuts. We
        # only fire when ⌘ is the SOLE active modifier (Shift / Option
        # / Ctrl all off) so we don't claim ⌘⇧H, ⌘⌥H, etc. — those
        # remain available for future bindings.
        _MOD_MASK = (
            NSEventModifierFlagCommand
            | NSEventModifierFlagShift
            | NSEventModifierFlagOption
            | NSEventModifierFlagControl
        )

        def _handle(event) -> object | None:  # noqa: ANN001
            # Phase 3.15 (A1): this is an app-wide LOCAL monitor — it
            # sees key events for EVERY window in the app, including
            # the Preferences window and NSOpenPanel/NSAlert sheets.
            # Only the tether panel and the detached LV window are
            # hotkey surfaces; typing a path in Preferences must never
            # fire the shutter. Events for any other window pass
            # through untouched.
            ev_window = event.window()
            hotkey_windows = [self._panel]
            if getattr(self, "_lv_window", None) is not None:
                hotkey_windows.append(self._lv_window)
            if ev_window is None or all(
                ev_window is not w for w in hotkey_windows
            ):
                return event

            # Phase 3.15 (A2): ignore key-autorepeat. Holding Space a
            # beat too long used to queue snap requests at ~15/s —
            # the exact "rapid requests into a busy camera" pattern
            # behind the 2026-05-13 FW wedge. One keydown = one action.
            if event.isARepeat():
                return None

            # Text-input detection belongs to the window the event is
            # for (field editors are NSTextView), not to the panel's
            # responder — the old panel-only check made keystrokes in
            # other windows look like "not typing".
            responder = ev_window.firstResponder()
            in_text_field = (
                responder is not None and responder.isKindOfClass_(NSTextView)
            )

            key = event.charactersIgnoringModifiers()
            key_code = event.keyCode()

            # ---- Pure-⌘ overlay shortcuts (Phase 3.11 polish) -----
            # Fire BEFORE the in-text-field gate so the user can
            # toggle histogram / grid even while editing the SUBJECT
            # field. ⌘H / ⌘G only — anything with additional
            # modifiers falls through to the normal dispatch.
            mods = event.modifierFlags() & _MOD_MASK
            if mods == NSEventModifierFlagCommand:
                k_lower = (key or "").lower()
                if k_lower == "h":
                    self._toggle_histogram()
                    return None  # consume
                if k_lower == "g":
                    self._cycle_grid()
                    return None  # consume
                if k_lower == "d":
                    # Phase 3.12 — detach/reattach LV viewport.
                    self._toggle_lv_detached()
                    return None  # consume
                if k_lower == "a":
                    # AF trigger that works even when SUBJECT (or any
                    # text field) has focus. Consumes ⌘A inside the
                    # SUBJECT field — Select All is a minor loss in a
                    # one-line item-name input. The bare ``a`` binding
                    # below still works outside text fields.
                    # Phase 3.15 (A2): honour the busy gate — the AF
                    # button is disabled during commit/recovery for
                    # wedge-safety; the hotkey must not bypass that.
                    if not self._controls_busy:
                        self._daemon.request_af()
                    return None  # consume
                if k_lower == "q":
                    # Phase 3.15 (A10): ⌘Q — advertised in the footer
                    # hint (and required by PHASE_3_12) but previously
                    # unimplemented: as an accessory app with no main
                    # menu there is no default Quit menu item to
                    # inherit, so the shortcut was dead.
                    self.stop()
                    return None  # consume
                if key == ",":
                    # Phase 3.14 — Preferences window. Use the raw
                    # character (not lowercased) so we match the actual
                    # comma key without colliding with letters.
                    self._open_preferences()
                    return None  # consume

            # Phase 3.9 Fix 2: Escape (keyCode 53) always drops focus when
            # the user is editing a field — gives them an "oops" out
            # without committing. Phase 3.15 (A9): restore the last
            # COMMITTED value into the field too — spec 3.9 requires
            # "field shows previous committed value"; leaving the
            # abandoned text visible made the UI show "bowl" while the
            # daemon still filed shots under "vase".
            if key_code == 53 and in_text_field:
                try:
                    self._item_field.setStringValue_(
                        self._daemon.current_item
                    )
                except Exception:  # noqa: BLE001
                    pass
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
                    # Phase 3.15 (A2): the commit half always runs, but
                    # the shoot half honours the busy gate (same rule
                    # as the disabled Shoot button). Phase 3.20: with
                    # pipelined capture, busy presses queue instead.
                    if not self._controls_busy or self._may_queue_snap():
                        self._daemon.request_snap()
                    return None  # consume
                # All other keys (letters, digits, Return, Tab, arrows)
                # pass through to the field normally.
                return event

            # Outside text field: original hotkey behavior.
            # Phase 3.15 (A2): Space/A respect the busy gate — the
            # buttons they mirror are disabled during commit/recovery
            # precisely because queued requests into a busy camera
            # cascade into FW wedge (2026-05-13). Phase 3.20: with
            # pipelined capture enabled, a Space during the capture
            # cycle QUEUES (bounded) instead of being swallowed —
            # that queue is what feeds the pipeline.
            if key == " ":
                if not self._controls_busy or self._may_queue_snap():
                    self._daemon.request_snap()
                return None  # consume
            if key == "a":
                if not self._controls_busy:
                    self._daemon.request_af()
                return None  # consume
            # Phase 3.11 — overlay toggles.
            if key == "h":
                self._toggle_histogram()
                return None  # consume
            if key == "g":
                self._cycle_grid()
                return None  # consume
            # Phase 3.22 — focus zoom (1:1 crop around the AF point).
            if key == "z":
                self._toggle_focus_zoom()
                return None  # consume
            # Phase 3.23 — rotate LV 90° CW per press (portrait shooting).
            if key == "r":
                self._cycle_lv_rotation()
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

        # Phase 3.12 — final flush of LV window state, then close the
        # detached window if it's still up. We capture frame BEFORE
        # orderOut_ because NSWindow.frame() may return zeros after
        # the window leaves the screen list.
        if getattr(self, "_lv_window", None) is not None:
            try:
                f = self._lv_window.frame()
                frame_tup: tuple[float, float, float, float] | None = (
                    float(f.origin.x),
                    float(f.origin.y),
                    float(f.size.width),
                    float(f.size.height),
                )
            except Exception:  # noqa: BLE001
                frame_tup = None
            try:
                self._save_lv_window_state(detached=True, frame=frame_tup)
            except Exception as e:  # noqa: BLE001
                logger.warning("lv_window final save failed: %s", e)
            try:
                self._lv_window.orderOut_(None)
            except Exception:  # noqa: BLE001
                pass
            self._lv_window = None

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
# Phase 3.10 helpers — corner-tick reticle, attributed strings, styled
# buttons / popups. All return ready-to-add NSViews so _build_window
# reads as widget placement rather than configuration.
# ---------------------------------------------------------------------------


def _make_stroke(x: float, y: float, width: float, color) -> "NSView":
    """Return a 1pt horizontal stroke view (used for exposure dividers)."""
    v = NSView.alloc().initWithFrame_(NSMakeRect(x, y, width, 1))
    v.setWantsLayer_(True)
    v.layer().setBackgroundColor_(color.CGColor())
    return v


def _uppercase_attr(
    text: str,
    color,
    *,
    font=None,
    tracking: float = 1.0,
    alignment=None,
) -> "NSAttributedString":
    """Build an uppercase, slightly-tracked attributed string.

    Used for the small ISO/SHUTTER/AV labels above the hero values and
    for the Subject field placeholder.
    """
    if font is None:
        font = F_LABEL
    style = NSMutableParagraphStyle.alloc().init()
    if alignment is not None:
        style.setAlignment_(alignment)
    attrs = {
        NSFontAttributeName: font,
        NSForegroundColorAttributeName: color,
        NSKernAttributeName: tracking,
        NSParagraphStyleAttributeName: style,
    }
    return NSAttributedString.alloc().initWithString_attributes_(
        text.upper(), attrs
    )


def _button_title_attr(
    text: str,
    color,
    *,
    font=None,
    alignment=None,
) -> "NSAttributedString":
    """Build a centred button-title attributed string (no uppercase)."""
    if font is None:
        font = F_BUTTON
    style = NSMutableParagraphStyle.alloc().init()
    if alignment is not None:
        style.setAlignment_(alignment)
    attrs = {
        NSFontAttributeName: font,
        NSForegroundColorAttributeName: color,
        NSParagraphStyleAttributeName: style,
    }
    return NSAttributedString.alloc().initWithString_attributes_(text, attrs)


class _ClickThroughView(NSView):
    """NSView that never intercepts mouse events (Phase 3.15, D).

    Used for overlay chrome that sits ON TOP of the click-to-AF
    surface. The AF reticle in particular is parked exactly where the
    user most often wants to click again (the current AF point) — its
    40×40 container used to swallow those clicks. Same pattern as
    ``GridOverlayView.hitTest_``.
    """

    def hitTest_(self, point):  # type: ignore[no-untyped-def]
        return None


class _ReviewImageView(NSImageView):
    """Post-shot review overlay — never intercepts clicks (Phase 3.22).

    hitTest → None lets a click fall through to the LiveViewImageView
    underneath; its ``commit_focus_point`` path dismisses the review
    first (a blind AF move under a covered LV would be a surprise).
    """

    def hitTest_(self, point):  # type: ignore[no-untyped-def]
        return None


# On-LV badge geometry (Phase 3.22) — shared by the battery pill, the
# exposure-mode chip, and the focus-zoom marker so the LV corners stay
# visually symmetric.
_BADGE_H = 16
_BADGE_M = 6
_BADGE_W_BAT = 56
_BADGE_W_ZOOM = 34
_BADGE_W_MODE = 24


def _make_lv_badge(x: float, y: float, w: float, *, mask: int) -> "NSView":
    """Small dark pill with a centred mono label, for LV corner chrome.

    Returns a ``_ClickThroughView`` whose single subview is the
    NSTextField label (callers reach it via ``subviews()[0]``).
    """
    pill = _ClickThroughView.alloc().initWithFrame_(
        NSMakeRect(x, y, w, _BADGE_H)
    )
    pill.setWantsLayer_(True)
    layer = pill.layer()
    if layer is not None:
        layer.setBackgroundColor_(
            NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.45).CGColor()
        )
        layer.setCornerRadius_(_BADGE_H / 2.0)
    try:
        pill.setAutoresizingMask_(mask)
    except Exception:  # noqa: BLE001
        pass
    label = NSTextField.alloc().initWithFrame_(
        NSMakeRect(0, 1, w, _BADGE_H - 2)
    )
    _make_label(label, "")
    label.setFont_(F_BADGE)
    label.setTextColor_(C_FG_PRIMARY)
    label.setAlignment_(NSTextAlignmentCenter)
    pill.addSubview_(label)
    return pill


def _build_corner_reticle(
    size: int = AF_MARKER_SIZE,
    arm: int = AF_TICK_ARM,
    stroke: float = AF_TICK_STROKE,
    color=None,
) -> "NSView":
    """4 amber L-shape corner ticks, drawn as 8 thin sub-NSViews.

    Visually identical to the CAShapeLayer approach in the spec, but
    skips the NSBezierPath → CGPath conversion. Each "L" is two thin
    rectangles (one horizontal arm, one vertical arm) anchored to the
    corresponding corner.
    """
    col = color if color is not None else C_AMBER
    container = _ClickThroughView.alloc().initWithFrame_(
        NSMakeRect(0, 0, size, size)
    )
    container.setWantsLayer_(True)
    container.layer().setBackgroundColor_(NSColor.clearColor().CGColor())

    s = stroke
    # In AppKit y goes up, so y≈0 is the BOTTOM of the view.
    rects = [
        # bottom-left L
        (0, 0, arm, s),  (0, 0, s, arm),
        # bottom-right L
        (size - arm, 0, arm, s),  (size - s, 0, s, arm),
        # top-left L
        (0, size - s, arm, s),  (0, size - arm, s, arm),
        # top-right L
        (size - arm, size - s, arm, s),  (size - s, size - arm, s, arm),
    ]
    for x, y, w, h in rects:
        tick = NSView.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        tick.setWantsLayer_(True)
        tick.layer().setBackgroundColor_(col.CGColor())
        container.addSubview_(tick)
    return container


def _make_hero_popup(
    x: float, y: float, w: float, h: float, target, action: str
) -> "NSPopUpButton":
    """ISO / SHUTTER / AV hero popup — bezel-less, centred, mono 18pt bold.

    The selected menu-item title is what gets drawn as the value.
    Setting font on the button (and centred paragraph alignment via the
    cell) makes the popup render as a static-looking hero number; the
    click still pops the menu, so the affordance is intact.
    """
    btn = NSPopUpButton.alloc().initWithFrame_pullsDown_(
        NSMakeRect(x, y, w, h), False
    )
    btn.setBordered_(False)
    btn.setFont_(F_HERO)
    try:
        btn.cell().setArrowPosition_(_NS_POPUP_NO_ARROW)
    except Exception:  # noqa: BLE001
        pass
    cell = btn.cell()
    if cell is not None:
        try:
            cell.setAlignment_(NSTextAlignmentCenter)
        except Exception:  # noqa: BLE001
            pass
    btn.setTarget_(target)
    btn.setAction_(action)
    return btn


def _make_secondary_popup(
    x: float, y: float, w: float, h: float, target, action: str
) -> "NSPopUpButton":
    """Compact dropdown for the WB / Format / Size row.

    Layer-backed dark background with a 1pt subtle stroke and rounded
    corners. The selected-item title renders in F_BODY (12pt).
    """
    btn = NSPopUpButton.alloc().initWithFrame_pullsDown_(
        NSMakeRect(x, y, w, h), False
    )
    btn.setBordered_(False)
    btn.setFont_(F_BODY)
    btn.setWantsLayer_(True)
    layer = btn.layer()
    if layer is not None:
        layer.setBackgroundColor_(C_BG_ELEVATED.CGColor())
        layer.setBorderColor_(C_STROKE_DEFAULT.CGColor())
        layer.setBorderWidth_(1.0)
        layer.setCornerRadius_(4.0)
    btn.setTarget_(target)
    btn.setAction_(action)
    return btn


def _make_amber_button(
    rect, title: str, *, target, action: str
) -> "NSButton":
    """Amber-filled primary action button (Shoot)."""
    btn = NSButton.alloc().initWithFrame_(rect)
    btn.setBordered_(False)
    btn.setTitle_(title)
    btn.setWantsLayer_(True)
    layer = btn.layer()
    if layer is not None:
        layer.setBackgroundColor_(C_AMBER.CGColor())
        layer.setCornerRadius_(4.0)
    btn.setAttributedTitle_(
        _button_title_attr(title, C_FG_INVERSE, alignment=NSTextAlignmentCenter)
    )
    btn.setTarget_(target)
    btn.setAction_(action)
    return btn


def _make_dark_button(
    rect, title: str, *, target, action: str
) -> "NSButton":
    """Dark-elevated secondary action button (AF)."""
    btn = NSButton.alloc().initWithFrame_(rect)
    btn.setBordered_(False)
    btn.setTitle_(title)
    btn.setWantsLayer_(True)
    layer = btn.layer()
    if layer is not None:
        layer.setBackgroundColor_(C_BG_ELEVATED.CGColor())
        layer.setBorderColor_(C_STROKE_DEFAULT.CGColor())
        layer.setBorderWidth_(1.0)
        layer.setCornerRadius_(4.0)
    btn.setAttributedTitle_(
        _button_title_attr(title, C_FG_PRIMARY, alignment=NSTextAlignmentCenter)
    )
    btn.setTarget_(target)
    btn.setAction_(action)
    return btn


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
        # Phase 3.23 — undo any manual LV rotation before handing the
        # point to the camera: the user clicks what they SEE, the
        # camera only understands sensor-native coords.
        try:
            nx, ny = self._owner._rot_inv(nx, ny)
        except Exception:  # noqa: BLE001
            pass
        nx = max(0.0, min(1.0, nx))
        ny = max(0.0, min(1.0, ny))
        cam_x = int(round(nx * (x_max - x_min) + x_min))
        cam_y = int(round(ny * (y_max - y_min) + y_min))
        self._owner.commit_focus_point(cam_x, cam_y)



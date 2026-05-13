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
    NSMakeRect,
    NSPanel,
    NSScreen,
    NSStatusWindowLevel,
    NSTextField,
    NSTextView,
    NSTitledWindowMask,
    NSUtilityWindowMask,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskNonactivatingPanel,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
    NSWindowStyleMaskUtilityWindow,
)
from Foundation import NSObject

if TYPE_CHECKING:
    from fp_l_tether.config import AppConfig
    from fp_l_tether.transfer import TetherDaemon

logger = logging.getLogger(__name__)


PANEL_WIDTH = 320
PANEL_HEIGHT = 190


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

        self._build_window()
        self._wire_callbacks()
        self._install_hotkeys()

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

        # Status indicator label (top)
        self._status_label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(12, PANEL_HEIGHT - 38, PANEL_WIDTH - 24, 20)
        )
        _make_label(self._status_label, "● connecting…", bold=True, size=13)
        content.addSubview_(self._status_label)

        # Session label
        self._session_label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(12, PANEL_HEIGHT - 60, PANEL_WIDTH - 24, 18)
        )
        _make_label(self._session_label, f"Session: {self._daemon.session_name}", size=11)
        self._session_label.setTextColor_(NSColor.secondaryLabelColor())
        content.addSubview_(self._session_label)

        # Last-shot label
        self._shot_label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(12, PANEL_HEIGHT - 84, PANEL_WIDTH - 24, 18)
        )
        _make_label(self._shot_label, "No shots yet", size=11)
        self._shot_label.setTextColor_(NSColor.secondaryLabelColor())
        content.addSubview_(self._shot_label)

        # Item row: "Item:" label + editable text field.
        # Typing here changes ``{item}`` in the filename template and resets
        # the per-item shot counter to 1 on the next shot.
        item_caption = NSTextField.alloc().initWithFrame_(
            NSMakeRect(12, 68, 40, 22)
        )
        _make_label(item_caption, "Item:", size=11)
        item_caption.setTextColor_(NSColor.secondaryLabelColor())
        content.addSubview_(item_caption)

        self._item_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(56, 66, PANEL_WIDTH - 68, 22)
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
            NSMakeRect(12, 30, 100, 28)
        )
        self._shoot_btn.setTitle_("Shoot ⎵")
        self._shoot_btn.setBezelStyle_(NSBezelStyleRounded)
        self._shoot_btn.setTarget_(self)
        self._shoot_btn.setAction_("shootClicked:")
        content.addSubview_(self._shoot_btn)

        # AF button (drive AF only, no capture — SnapCommand mode 3)
        self._af_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(118, 30, 46, 28)
        )
        self._af_btn.setTitle_("AF")
        self._af_btn.setBezelStyle_(NSBezelStyleRounded)
        self._af_btn.setTarget_(self)
        self._af_btn.setAction_("afClicked:")
        content.addSubview_(self._af_btn)

        # New Session button — resets shot counters and session name.
        self._new_session_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(170, 30, 72, 28)
        )
        self._new_session_btn.setTitle_("New…")
        self._new_session_btn.setBezelStyle_(NSBezelStyleRounded)
        self._new_session_btn.setTarget_(self)
        self._new_session_btn.setAction_("newSessionClicked:")
        content.addSubview_(self._new_session_btn)

        # Quit button
        self._quit_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(248, 30, 60, 28)
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
        # Both callbacks are invoked from the daemon's background thread,
        # so we marshal back to the main thread with performSelectorOnMainThread.
        self._daemon.on_status = self._on_status_threadsafe
        self._daemon.on_shot = self._on_shot_threadsafe

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
            "error": "✕",
            "stopped": "○",
        }.get(state, "●")
        text = f"{symbol} {state}"
        if message:
            text += f"  —  {message}"
        self._status_label.setStringValue_(text)

        color = NSColor.systemRedColor() if state == "error" else NSColor.labelColor()
        if state == "ready":
            color = NSColor.systemGreenColor()
        self._status_label.setTextColor_(color)

    @objc.signature(b"v@:@")
    def updateShot_(self, tup) -> None:
        idx, name, size, mbps = tup
        size_mb = size / 1024 / 1024
        self._shot_label.setStringValue_(
            f"#{idx}  {name}  ·  {size_mb:.1f} MB  ·  {mbps:.1f} MB/s"
        )
        self._shot_label.setTextColor_(NSColor.labelColor())

    # ----- button actions ---------------------------------------------

    @objc.signature(b"v@:@")
    def shootClicked_(self, sender) -> None:
        self._daemon.request_snap()

    @objc.signature(b"v@:@")
    def afClicked_(self, sender) -> None:
        self._daemon.request_af()

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

    # ----- hotkeys (global within app) --------------------------------

    def _install_hotkeys(self) -> None:
        """Catch keystrokes while ANY app is focused — limited to space/quit.

        We use a local monitor on NSEventMaskKeyDown that fires when the
        panel app is active. Since the panel is a non-activating utility
        panel, focus stays on Lightroom, so we ALSO install a global
        monitor for when Lightroom has focus.
        """

        def _handle(event) -> object | None:  # noqa: ANN001
            # If the user is typing in a text field (item input), let the
            # event through — don't hijack space / "a" as triggers.
            responder = self._panel.firstResponder()
            if responder is not None and responder.isKindOfClass_(NSTextView):
                return event
            key = event.charactersIgnoringModifiers()
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

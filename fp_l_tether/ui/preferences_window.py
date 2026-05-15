"""Preferences window (Phase 3.14).

A modeless NSWindow that exposes the most commonly tuned settings:
Lightroom mode + folders, output naming, log directory. Edits commit
live on every field change — there is no Apply button. Closing via
the red button or ⌘W hides the window; the next ⌘, brings it back.

Why not an NSPanel?
  The floating tether is an NSPanel (utility, non-activating, joins
  all Spaces) because it has to ride above Lightroom fullscreen.
  Preferences is a one-shot configuration surface — a regular
  NSWindow at ``NSNormalWindowLevel`` is the conventional macOS
  pattern and avoids the "always on top" footprint.

Camera-side settings (keepalive strategy, USB recovery knobs, etc.)
are deliberately omitted — see ``docs/PHASE_3_14_SETTINGS_PANEL.md``
section 1. Those stay in ``config.toml``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

import objc
from AppKit import (
    NSAlert,
    NSAlertFirstButtonReturn,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSButton,
    NSColor,
    NSFont,
    NSMakeRect,
    NSMakeSize,
    NSModalResponseOK,
    NSNormalWindowLevel,
    NSOpenPanel,
    NSPopUpButton,
    NSTextAlignmentLeft,
    NSTextField,
    NSView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)

# Raw AppKit constants for setButtonType_ / setState_. PyObjC has
# dropped the named ``NSOnState`` and ``NSSwitchButton`` aliases on
# modern macOS (those were deprecated in 10.14 in favour of
# NSControlStateValueOn / NSButtonTypeSwitch), but the underlying
# integer values are stable. We use the integers directly so the code
# works across PyObjC versions without conditional imports.
_NS_BTN_RADIO = 4      # NSButtonTypeRadio
_NS_BTN_SWITCH = 3     # NSButtonTypeSwitch
_NS_STATE_ON = 1       # NSControlStateValueOn
_NS_STATE_OFF = 0      # NSControlStateValueOff
from Foundation import NSObject, NSURL

from fp_l_tether.storage import (
    AppPrefs,
    SettingsCache,
    load_settings_cache,
    save_settings_cache,
)

if TYPE_CHECKING:
    from fp_l_tether.config import AppConfig
    from fp_l_tether.ui.floating_panel import FloatingTetherPanel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Layout constants — Preferences is a fixed-size form, so all sizing
# lives here for easy review without searching widget code.
# ---------------------------------------------------------------------

WIN_W = 420
WIN_H = 540

PAD_X = 20
PAD_TOP = 18
PAD_BOTTOM = 16

# Form geometry: label on the left, control on the right.
LABEL_X = PAD_X
LABEL_W = 130
CONTROL_X = LABEL_X + LABEL_W + 8
CONTROL_W = WIN_W - CONTROL_X - PAD_X         # full-width control
BROWSE_W = 80
PATH_FIELD_W = CONTROL_W - BROWSE_W - 6       # leave room for Browse…

ROW_H = 22
ROW_GAP = 8
SECTION_GAP = 14
SECTION_HEADER_H = 16

# Helpers ---------------------------------------------------------------


def _make_label(
    text: str,
    frame,
    *,
    bold: bool = False,
    secondary: bool = False,
) -> NSTextField:
    """Build a non-editable, transparent NSTextField at ``frame``."""
    f = NSTextField.alloc().initWithFrame_(frame)
    f.setStringValue_(text)
    f.setEditable_(False)
    f.setBezeled_(False)
    f.setDrawsBackground_(False)
    f.setSelectable_(False)
    f.setAlignment_(NSTextAlignmentLeft)
    if bold:
        f.setFont_(NSFont.boldSystemFontOfSize_(12.0))
    else:
        f.setFont_(NSFont.systemFontOfSize_(12.0))
    if secondary:
        f.setTextColor_(NSColor.secondaryLabelColor())
    return f


def _make_text_field(text: str, frame, placeholder: str = "") -> NSTextField:
    """Editable NSTextField with a placeholder."""
    f = NSTextField.alloc().initWithFrame_(frame)
    f.setStringValue_(text or "")
    if placeholder:
        f.setPlaceholderString_(placeholder)
    f.setFont_(NSFont.systemFontOfSize_(12.0))
    return f


def _display_path(p: str | Path | None) -> str:
    """Render an absolute path with ``$HOME`` collapsed to ``~``."""
    if p is None:
        return ""
    s = str(p)
    home = str(Path.home())
    if s.startswith(home + "/"):
        return "~/" + s[len(home) + 1 :]
    if s == home:
        return "~"
    return s


def _from_display_path(text: str) -> str:
    """Reverse of ``_display_path`` for storage purposes.

    We keep the user's typed string (preserves the ``~``) — only the
    overrides path in ``AppPrefs.to_config_overrides`` actually
    expands it before handing to filesystem code.
    """
    return (text or "").strip()


# Conflict popup ordering. Index 0 = default (rename).
_CONFLICT_OPTIONS = [
    ("Rename", "rename"),
    ("Overwrite", "overwrite"),
    ("Skip", "skip"),
]


# ---------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------


class PreferencesWindow(NSObject):
    """Controller (NSObject) that owns the Preferences NSWindow.

    Singleton lifecycle is managed by the floating panel: it keeps a
    single instance in ``self._prefs_controller`` and calls
    :meth:`show` on each ⌘, press. Closing the window calls
    ``orderOut_(None)`` but leaves the controller alive for reuse.
    """

    # ----- construction / lifecycle ---------------------------------

    def __new__(cls, panel=None):  # type: ignore[no-untyped-def]
        # PyObjC requires __new__ for NSObject subclasses; bare
        # alloc-init goes through the no-arg path that the ObjC
        # runtime uses internally.
        self = objc.super(PreferencesWindow, cls).new()
        if panel is None:
            return self
        self._panel = panel
        self._build_window()
        self._populate_from_cache()
        return self

    def _build_window(self) -> None:
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
        # Centred initially — first call to show() positions it.
        rect = NSMakeRect(0, 0, WIN_W, WIN_H)
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False
        )
        window.setTitle_("fp-l-tether Preferences")
        window.setLevel_(NSNormalWindowLevel)
        window.setReleasedWhenClosed_(False)
        # Close button hides instead of destroying — we want to reuse
        # the same controller + widget tree across ⌘, presses.
        window.setDelegate_(self)
        self._window = window

        content = window.contentView()
        self._build_form(content)

    # ----- form ----------------------------------------------------

    @objc.python_method
    def _build_form(self, content: NSView) -> None:
        """Stack form rows top-to-bottom inside the window content view."""
        y = WIN_H - PAD_TOP

        # ----- Lightroom section ---------------------------------
        y -= SECTION_HEADER_H
        content.addSubview_(_make_label(
            "Lightroom",
            NSMakeRect(PAD_X, y, WIN_W - 2 * PAD_X, SECTION_HEADER_H),
            bold=True,
        ))
        y -= ROW_GAP

        # Mode (radio pair)
        y -= ROW_H
        content.addSubview_(_make_label(
            "Mode", NSMakeRect(LABEL_X, y, LABEL_W, ROW_H), secondary=True,
        ))
        # Two NSButtons wired as a radio group via target/action.
        self._mode_watch_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(CONTROL_X, y, 80, ROW_H)
        )
        self._mode_watch_btn.setButtonType_(4)  # NSRadioButton
        self._mode_watch_btn.setTitle_("Watch")
        self._mode_watch_btn.setTarget_(self)
        self._mode_watch_btn.setAction_("modeChanged:")
        content.addSubview_(self._mode_watch_btn)

        self._mode_session_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(CONTROL_X + 88, y, 90, ROW_H)
        )
        self._mode_session_btn.setButtonType_(4)
        self._mode_session_btn.setTitle_("Session")
        self._mode_session_btn.setTarget_(self)
        self._mode_session_btn.setAction_("modeChanged:")
        content.addSubview_(self._mode_session_btn)

        y -= ROW_GAP
        # Watch folder + Browse
        y -= ROW_H
        content.addSubview_(_make_label(
            "Watch folder",
            NSMakeRect(LABEL_X, y, LABEL_W, ROW_H), secondary=True,
        ))
        self._watch_field = _make_text_field(
            "", NSMakeRect(CONTROL_X, y, PATH_FIELD_W, ROW_H),
            placeholder="~/Pictures/Tether/_watch",
        )
        self._watch_field.setTarget_(self)
        self._watch_field.setAction_("watchFolderChanged:")
        content.addSubview_(self._watch_field)
        self._watch_browse_btn = self._make_browse_button(
            NSMakeRect(CONTROL_X + PATH_FIELD_W + 6, y, BROWSE_W, ROW_H),
            "chooseWatchFolder:",
        )
        content.addSubview_(self._watch_browse_btn)

        y -= ROW_GAP
        # Session root + Browse
        y -= ROW_H
        content.addSubview_(_make_label(
            "Session root",
            NSMakeRect(LABEL_X, y, LABEL_W, ROW_H), secondary=True,
        ))
        self._session_field = _make_text_field(
            "", NSMakeRect(CONTROL_X, y, PATH_FIELD_W, ROW_H),
            placeholder="~/Pictures/Tether",
        )
        self._session_field.setTarget_(self)
        self._session_field.setAction_("sessionRootChanged:")
        content.addSubview_(self._session_field)
        self._session_browse_btn = self._make_browse_button(
            NSMakeRect(CONTROL_X + PATH_FIELD_W + 6, y, BROWSE_W, ROW_H),
            "chooseSessionRoot:",
        )
        content.addSubview_(self._session_browse_btn)

        # ----- Output section ------------------------------------
        y -= SECTION_GAP
        y -= SECTION_HEADER_H
        content.addSubview_(_make_label(
            "Output",
            NSMakeRect(PAD_X, y, WIN_W - 2 * PAD_X, SECTION_HEADER_H),
            bold=True,
        ))

        y -= ROW_GAP + ROW_H
        content.addSubview_(_make_label(
            "Default subject",
            NSMakeRect(LABEL_X, y, LABEL_W, ROW_H), secondary=True,
        ))
        self._subject_field = _make_text_field(
            "", NSMakeRect(CONTROL_X, y, CONTROL_W, ROW_H),
            placeholder="untitled",
        )
        self._subject_field.setTarget_(self)
        self._subject_field.setAction_("subjectChanged:")
        content.addSubview_(self._subject_field)

        y -= ROW_GAP + ROW_H
        content.addSubview_(_make_label(
            "Filename pattern",
            NSMakeRect(LABEL_X, y, LABEL_W, ROW_H), secondary=True,
        ))
        self._pattern_field = _make_text_field(
            "", NSMakeRect(CONTROL_X, y, CONTROL_W, ROW_H),
            placeholder="{session}_{shot:04d}.{ext}",
        )
        self._pattern_field.setTarget_(self)
        self._pattern_field.setAction_("patternChanged:")
        content.addSubview_(self._pattern_field)

        y -= ROW_GAP + ROW_H
        content.addSubview_(_make_label(
            "On conflict",
            NSMakeRect(LABEL_X, y, LABEL_W, ROW_H), secondary=True,
        ))
        self._conflict_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(CONTROL_X, y, 160, ROW_H), False
        )
        for title, _key in _CONFLICT_OPTIONS:
            self._conflict_popup.addItemWithTitle_(title)
        self._conflict_popup.setTarget_(self)
        self._conflict_popup.setAction_("conflictChanged:")
        content.addSubview_(self._conflict_popup)

        # ----- Logs section --------------------------------------
        y -= SECTION_GAP
        y -= SECTION_HEADER_H
        content.addSubview_(_make_label(
            "Logs",
            NSMakeRect(PAD_X, y, WIN_W - 2 * PAD_X, SECTION_HEADER_H),
            bold=True,
        ))

        y -= ROW_GAP + ROW_H
        content.addSubview_(_make_label(
            "Log directory",
            NSMakeRect(LABEL_X, y, LABEL_W, ROW_H), secondary=True,
        ))
        self._log_dir_field = _make_text_field(
            "", NSMakeRect(CONTROL_X, y, PATH_FIELD_W, ROW_H),
            placeholder="~/Library/Logs/fp-l-tether",
        )
        self._log_dir_field.setTarget_(self)
        self._log_dir_field.setAction_("logDirChanged:")
        content.addSubview_(self._log_dir_field)
        self._log_browse_btn = self._make_browse_button(
            NSMakeRect(CONTROL_X + PATH_FIELD_W + 6, y, BROWSE_W, ROW_H),
            "chooseLogDir:",
        )
        content.addSubview_(self._log_browse_btn)

        y -= ROW_GAP + ROW_H
        self._json_log_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(CONTROL_X, y, CONTROL_W, ROW_H)
        )
        self._json_log_btn.setButtonType_(_NS_BTN_SWITCH)
        self._json_log_btn.setTitle_("Save structured JSON log")
        self._json_log_btn.setTarget_(self)
        self._json_log_btn.setAction_("jsonLogToggled:")
        content.addSubview_(self._json_log_btn)

        # Inline "Restart to apply" hint — shown when log_dir or
        # json_log_enabled diverges from the value loaded at open time.
        y -= ROW_H
        self._restart_hint = _make_label(
            "",
            NSMakeRect(CONTROL_X, y, CONTROL_W, ROW_H),
            secondary=True,
        )
        self._restart_hint.setFont_(NSFont.systemFontOfSize_(11.0))
        self._restart_hint.setHidden_(True)
        content.addSubview_(self._restart_hint)

        # ----- Camera info (read-only) ---------------------------
        y -= SECTION_GAP
        y -= SECTION_HEADER_H
        content.addSubview_(_make_label(
            "Camera",
            NSMakeRect(PAD_X, y, WIN_W - 2 * PAD_X, SECTION_HEADER_H),
            bold=True,
        ))
        y -= ROW_GAP + ROW_H
        info = _make_label(
            "Camera settings (DG1/DG2 cache) are auto-managed in\n"
            "~/.fp-l-tether/user_settings.json and not editable here.",
            NSMakeRect(LABEL_X, y - ROW_H, WIN_W - 2 * PAD_X, ROW_H * 2 + 4),
            secondary=True,
        )
        info.setFont_(NSFont.systemFontOfSize_(11.0))
        # Allow the two-line message to wrap inside the field.
        try:
            info.cell().setWraps_(True)
        except Exception:  # noqa: BLE001
            pass
        content.addSubview_(info)

        # ----- Reset to defaults (bottom-right) ------------------
        reset_w = 160
        reset_h = 24
        self._reset_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(
                WIN_W - PAD_X - reset_w,
                PAD_BOTTOM,
                reset_w,
                reset_h,
            )
        )
        self._reset_btn.setTitle_("Reset to defaults")
        self._reset_btn.setBezelStyle_(NSBezelStyleRounded)
        self._reset_btn.setTarget_(self)
        self._reset_btn.setAction_("resetToDefaults:")
        content.addSubview_(self._reset_btn)

    @objc.python_method
    def _make_browse_button(self, frame, action: str) -> NSButton:
        btn = NSButton.alloc().initWithFrame_(frame)
        btn.setTitle_("Choose…")
        btn.setBezelStyle_(NSBezelStyleRounded)
        btn.setTarget_(self)
        btn.setAction_(action)
        return btn

    # ----- populate / snapshot --------------------------------------

    def _populate_from_cache(self) -> None:
        """Fill widgets from current cache + live AppConfig.

        Precedence on display:
          - if ``app_prefs.<field>`` is set → show it
          - else → show the value from the live AppConfig (which
            reflects ``config.toml`` + built-in defaults)
        """
        cache = self._load_or_init_cache()
        prefs = cache.app_prefs or AppPrefs()
        cfg = self._panel._cfg

        # Mode radio
        current_mode = prefs.lightroom_mode or cfg.lightroom.mode
        self._mode_watch_btn.setState_(
            _NS_STATE_ON if current_mode == "watch" else 0
        )
        self._mode_session_btn.setState_(
            _NS_STATE_ON if current_mode == "session" else 0
        )

        # Path fields
        self._watch_field.setStringValue_(
            prefs.watch_folder or _display_path(cfg.lightroom.watch_folder)
        )
        self._session_field.setStringValue_(
            prefs.session_root or _display_path(cfg.output.root)
        )

        # Output
        self._subject_field.setStringValue_(
            prefs.default_subject or cfg.output.default_item
        )
        self._pattern_field.setStringValue_(
            prefs.filename_template or cfg.output.filename_template
        )
        current_conflict = prefs.on_conflict or cfg.output.on_conflict
        for idx, (_title, key) in enumerate(_CONFLICT_OPTIONS):
            if key == current_conflict:
                self._conflict_popup.selectItemAtIndex_(idx)
                break

        # Logs
        self._log_dir_field.setStringValue_(
            prefs.log_dir or _display_path(
                Path("~/Library/Logs/fp-l-tether").expanduser()
            )
        )
        json_enabled = (
            prefs.json_log_enabled
            if prefs.json_log_enabled is not None
            else True
        )
        self._json_log_btn.setState_(_NS_STATE_ON if json_enabled else 0)

        # Snapshot the restart-required values so we can flag divergence.
        self._snapshot_log_dir = self._log_dir_field.stringValue()
        self._snapshot_json_log = bool(json_enabled)
        self._restart_hint.setHidden_(True)

    def _load_or_init_cache(self) -> SettingsCache:
        """Load the on-disk cache, or return a fresh one if absent.

        Always returns a usable ``SettingsCache``; callers can mutate
        ``.app_prefs`` and call :func:`save_settings_cache`.
        """
        cache = load_settings_cache()
        if cache is None:
            return SettingsCache(app_prefs=AppPrefs())
        if cache.app_prefs is None:
            cache.app_prefs = AppPrefs()
        return cache

    # ----- show / close --------------------------------------------

    def show(self) -> None:
        """Bring the window to front, centring on first call."""
        # Re-populate so the form reflects any external changes that
        # happened while the window was hidden (e.g. config.toml edit).
        self._populate_from_cache()
        if not self._window.isVisible():
            self._window.center()
        self._window.makeKeyAndOrderFront_(None)

    def windowShouldClose_(self, sender) -> bool:  # type: ignore[no-untyped-def]
        # Hide rather than destroy — keep the singleton alive for the
        # next ⌘, press.
        try:
            self._window.orderOut_(None)
        except Exception:  # noqa: BLE001
            pass
        return False

    # ----- action handlers -----------------------------------------

    def modeChanged_(self, sender) -> None:  # type: ignore[no-untyped-def]
        # Mutex: clicking one radio clears the other.
        is_watch = sender is self._mode_watch_btn
        self._mode_watch_btn.setState_(_NS_STATE_ON if is_watch else 0)
        self._mode_session_btn.setState_(0 if is_watch else _NS_STATE_ON)
        new_mode = "watch" if is_watch else "session"
        self._commit({"lightroom_mode": new_mode})
        try:
            self._panel._cfg.lightroom.mode = new_mode
        except Exception:  # noqa: BLE001
            pass

    def watchFolderChanged_(self, sender) -> None:  # type: ignore[no-untyped-def]
        value = _from_display_path(self._watch_field.stringValue())
        if not value:
            return
        self._commit({"watch_folder": value})
        try:
            self._panel._cfg.lightroom.watch_folder = Path(value).expanduser()
        except Exception:  # noqa: BLE001
            pass

    def sessionRootChanged_(self, sender) -> None:  # type: ignore[no-untyped-def]
        value = _from_display_path(self._session_field.stringValue())
        if not value:
            return
        self._commit({"session_root": value})
        try:
            self._panel._cfg.output.root = Path(value).expanduser()
        except Exception:  # noqa: BLE001
            pass

    def subjectChanged_(self, sender) -> None:  # type: ignore[no-untyped-def]
        value = (self._subject_field.stringValue() or "").strip()
        if not value:
            value = "untitled"
            self._subject_field.setStringValue_(value)
        self._commit({"default_subject": value})
        try:
            self._panel._cfg.output.default_item = value
        except Exception:  # noqa: BLE001
            pass

    def patternChanged_(self, sender) -> None:  # type: ignore[no-untyped-def]
        value = (self._pattern_field.stringValue() or "").strip()
        if not self._validate_pattern(value):
            # Don't persist garbage; revert to the cached value.
            self._populate_from_cache()
            return
        self._commit({"filename_template": value})
        try:
            self._panel._cfg.output.filename_template = value
        except Exception:  # noqa: BLE001
            pass

    def conflictChanged_(self, sender) -> None:  # type: ignore[no-untyped-def]
        idx = self._conflict_popup.indexOfSelectedItem()
        if idx < 0 or idx >= len(_CONFLICT_OPTIONS):
            return
        _title, key = _CONFLICT_OPTIONS[idx]
        self._commit({"on_conflict": key})
        try:
            self._panel._cfg.output.on_conflict = key
        except Exception:  # noqa: BLE001
            pass

    def logDirChanged_(self, sender) -> None:  # type: ignore[no-untyped-def]
        value = _from_display_path(self._log_dir_field.stringValue())
        if not value:
            return
        self._commit({"log_dir": value})
        self._refresh_restart_hint()

    def jsonLogToggled_(self, sender) -> None:  # type: ignore[no-untyped-def]
        enabled = bool(self._json_log_btn.state())
        self._commit({"json_log_enabled": enabled})
        self._refresh_restart_hint()

    def resetToDefaults_(self, sender) -> None:  # type: ignore[no-untyped-def]
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Reset all Preferences?")
        alert.setInformativeText_(
            "Watch folder, default subject, filename pattern, and other "
            "Preferences values revert to config.toml / built-in "
            "defaults. Camera settings (DG1/DG2 cache) are not "
            "affected."
        )
        alert.addButtonWithTitle_("Reset")
        alert.addButtonWithTitle_("Cancel")
        if alert.runModal() != NSAlertFirstButtonReturn:
            return
        cache = self._load_or_init_cache()
        cache.app_prefs = None
        save_settings_cache(cache)
        self._populate_from_cache()

    # ----- Browse… folder pickers ----------------------------------

    def chooseWatchFolder_(self, sender) -> None:  # type: ignore[no-untyped-def]
        self._pick_folder(self._watch_field, "Choose watch folder",
                          self.watchFolderChanged_)

    def chooseSessionRoot_(self, sender) -> None:  # type: ignore[no-untyped-def]
        self._pick_folder(self._session_field, "Choose session root",
                          self.sessionRootChanged_)

    def chooseLogDir_(self, sender) -> None:  # type: ignore[no-untyped-def]
        self._pick_folder(self._log_dir_field, "Choose log directory",
                          self.logDirChanged_)

    @objc.python_method
    def _pick_folder(self, target_field, title: str, on_commit) -> None:
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(False)
        panel.setCanChooseDirectories_(True)
        panel.setAllowsMultipleSelection_(False)
        panel.setCanCreateDirectories_(True)
        panel.setTitle_(title)
        panel.setPrompt_("Select")
        # Pre-populate to the current value if it exists.
        current_str = _from_display_path(target_field.stringValue())
        if current_str:
            current = Path(current_str).expanduser()
            if current.exists():
                panel.setDirectoryURL_(NSURL.fileURLWithPath_(str(current)))
        if panel.runModal() != NSModalResponseOK:
            return
        url = panel.URL()
        if url is None:
            return
        chosen = Path(url.path())
        target_field.setStringValue_(_display_path(chosen))
        on_commit(target_field)

    # ----- validation + persistence --------------------------------

    @objc.python_method
    def _validate_pattern(self, template: str) -> bool:
        """Reject patterns that would produce extension-less files."""
        if not template:
            return False
        # Must reference {ext} so write_atomic knows the suffix.
        if "{ext}" not in template:
            return False
        # Trial-format with sentinel values so format keyword errors
        # surface before the user shoots.
        try:
            template.format(
                date="20260515",
                time="120000",
                shot=1,
                subject="x",
                session="s",
                ext="dng",
                name="x",
            )
        except (KeyError, IndexError, ValueError):
            return False
        return True

    @objc.python_method
    def _commit(self, updates: dict) -> None:
        """Merge updates into ``app_prefs`` and persist atomically."""
        cache = self._load_or_init_cache()
        prefs = cache.app_prefs or AppPrefs()
        for key, value in updates.items():
            setattr(prefs, key, value)
        cache.app_prefs = prefs
        if not save_settings_cache(cache):
            logger.warning("Preferences: cache save failed")

    def _refresh_restart_hint(self) -> None:
        """Show the inline 'Restart to apply' note if log_dir / json
        differ from the values snapshotted at window-open time."""
        log_dir_changed = (
            self._log_dir_field.stringValue() != self._snapshot_log_dir
        )
        json_changed = (
            bool(self._json_log_btn.state()) != self._snapshot_json_log
        )
        if log_dir_changed or json_changed:
            self._restart_hint.setStringValue_("Restart to apply")
            self._restart_hint.setHidden_(False)
        else:
            self._restart_hint.setHidden_(True)

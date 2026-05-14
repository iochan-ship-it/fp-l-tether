"""Detached LV viewport — owns the lifted LV view stack (Phase 3.12).

When the user hits ⌘D, ``FloatingTetherPanel`` removes its five LV-area
subviews (``_live_view``, ``_grid_view``, ``_hist_view``, ``_lv_overlay``,
``_af_marker_view``) and hands them to this window. Ownership move means
the histogram thread pool, grid mode state, AF reticle, and pause overlay
all keep working without any rewiring — that's the explicit MVP guarantee
in ``docs/PHASE_3_12_LV_DETACH.md``.

Window style:
  - ``NSWindowStyleMaskTitled`` + ``NSWindowStyleMaskResizable`` only.
    No ``Closable`` (no red close dot) and no ``Miniaturizable`` (no
    yellow minimize dot). This is a hard guarantee against accidental
    closure — the panel's ⌘Q is the only path that tears the window
    down. ⌘D toggles reattach.
  - ``setContentAspectRatio_(3:2)`` matches the fp L LV source (1620×1080)
    so the user can't squeeze the viewport into a non-native ratio and
    introduce black bars or anisotropic scaling.
  - ``NSStatusWindowLevel`` + ``CanJoinAllSpaces | FullScreenAuxiliary``
    so the floated viewport rides over Lightroom fullscreen exactly
    the way the panel itself does.

Resize layout: all five views snap to the content bounds in
``windowDidResize_``. The histogram remains a bottom 50pt strip; the
overlay label re-centres; AF reticle is re-positioned by the panel's
``_reposition_af_marker`` (which reads ``_live_view.frame()`` so it
naturally tracks whichever superview the marker is currently parented
to).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import objc
from AppKit import (
    NSAppearance,
    NSBackingStoreBuffered,
    NSColor,
    NSMakeRect,
    NSMakeSize,
    NSStatusWindowLevel,
    NSWindow,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)

if TYPE_CHECKING:
    from fp_l_tether.ui.floating_panel import FloatingTetherPanel

# Bottom histogram strip height — duplicated locally from
# floating_panel.HIST_STRIP_H to avoid an import cycle (floating_panel
# imports lv_window during _toggle_lv_detached). If the panel reskins
# and the strip height changes, update both sides together.
_HIST_STRIP_H = 50

# Pause overlay label height — kept in sync with the panel's
# _lv_overlay_label construction (NSMakeRect(0, (h-22)//2, w, 22)).
_OVERLAY_LABEL_H = 22

# Window title — short, lowercase product name. Sigma's own UI uses
# "Sigma fp L"; we shorten to "fp L" to match the panel title.
_WINDOW_TITLE = "fp L · Live View"

# Anodized-black background. Same numeric tokens as
# floating_panel.C_BG_PANEL (26,26,28). Duplicated to avoid import cycle.
_C_BG_PANEL = NSColor.colorWithCalibratedRed_green_blue_alpha_(
    26 / 255.0, 26 / 255.0, 28 / 255.0, 1.0
)


class LVDetachedWindow(NSWindow):
    """NSWindow that hosts the lifted LV view stack while detached.

    Construct via :py:meth:`make` (the class-level factory) — never
    alloc/init directly. The factory wires the panel back-ref and the
    five LV views, sets style/level/aspect ratio, and returns a window
    ready for ``makeKeyAndOrderFront_``.
    """

    @objc.python_method
    @classmethod
    def make(  # type: ignore[no-untyped-def]
        cls,
        panel,
        initial_frame,
        lv_view,
        grid_view,
        hist_view,
        lv_overlay,
        af_marker,
    ):
        """Build the detached window and adopt the five LV subviews.

        Caller's responsibility:
          - Call ``removeFromSuperview`` on each of the five views
            *before* invoking this factory (they will be re-added to
            this window's content view here).
          - Pass ``initial_frame`` already resolved (e.g. cached frame
            falling back to main-screen-centred default).
          - Retain the returned window in ``panel._lv_window`` so the
            collection behaviour doesn't release it on the next event
            loop spin.
        """
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskResizable
        # NB: deliberately no Closable / Miniaturizable. See module docstring.
        window = cls.alloc().initWithContentRect_styleMask_backing_defer_(
            initial_frame, style, NSBackingStoreBuffered, False
        )
        if window is None:
            return None

        window._panel = panel
        window._live_view = lv_view
        window._grid_view = grid_view
        window._hist_view = hist_view
        window._lv_overlay = lv_overlay
        window._af_marker_view = af_marker

        window.setTitle_(_WINDOW_TITLE)
        window.setLevel_(NSStatusWindowLevel)
        window.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        window.setHidesOnDeactivate_(False)
        # We close the window ourselves on quit; don't let Cocoa release
        # it when an orderOut_ happens to coincide with the runloop tick.
        window.setReleasedWhenClosed_(False)

        # 3:2 aspect ratio (LV source is 1620×1080 = 3:2). Min size
        # matches the spec — 360×240 is 1.25× the original 288×200 panel
        # slot, large enough to be a meaningful detach.
        window.setContentAspectRatio_(NSMakeSize(3, 2))
        window.setContentMinSize_(NSMakeSize(360, 240))

        window.setOpaque_(True)
        window.setBackgroundColor_(_C_BG_PANEL)
        try:
            window.setAppearance_(
                NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua")
            )
        except Exception:  # noqa: BLE001
            # appearanceNamed_ can fail on very old macOS — non-fatal.
            pass

        # Window owns its own delegate; we route the resize/move callbacks
        # back to the panel through it.
        window.setDelegate_(window)

        content = window.contentView()
        content.setWantsLayer_(True)
        if content.layer() is not None:
            content.layer().setBackgroundColor_(_C_BG_PANEL.CGColor())

        # Subview z-order mirrors the panel's _build_window stacking
        # (LV → grid → hist → overlay → AF marker). Insertion order
        # determines z-order in AppKit (later = on top).
        content.addSubview_(lv_view)
        content.addSubview_(grid_view)
        content.addSubview_(hist_view)
        content.addSubview_(lv_overlay)
        content.addSubview_(af_marker)

        # Initial layout — set frames to the content bounds so the
        # views aren't stuck at their old panel-relative origins.
        window._layout_for_bounds(content.bounds())
        return window

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    @objc.python_method
    def _layout_for_bounds(self, bounds):  # type: ignore[no-untyped-def]
        """Snap LV / grid / hist / overlay / overlay-label to ``bounds``.

        Called both at construction (from ``make``) and on every
        ``windowDidResize_`` notification. AF marker is repositioned
        separately via the panel (it needs camera-coord context).
        """
        w = bounds.size.width
        h = bounds.size.height
        full = NSMakeRect(bounds.origin.x, bounds.origin.y, w, h)

        self._live_view.setFrame_(full)
        self._grid_view.setFrame_(full)
        try:
            # GridOverlayView caches a path keyed to its frame; force a
            # rebuild so the lines snap to the new bounds rather than
            # being scaled from a stale path.
            self._grid_view.setNeedsDisplay_(True)
        except Exception:  # noqa: BLE001
            pass

        # Histogram = bottom 50pt strip; width follows.
        self._hist_view.setFrame_(NSMakeRect(
            bounds.origin.x,
            bounds.origin.y,
            w,
            _HIST_STRIP_H,
        ))

        # Pause overlay covers the whole viewport; its centred label
        # also needs re-centring (label uses overlay-local coords).
        self._lv_overlay.setFrame_(full)
        try:
            label = getattr(self._panel, "_lv_overlay_label", None)
            if label is not None:
                label.setFrame_(NSMakeRect(
                    0,
                    (h - _OVERLAY_LABEL_H) / 2.0,
                    w,
                    _OVERLAY_LABEL_H,
                ))
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # NSWindowDelegate callbacks (self-as-delegate — setDelegate_(self))
    # ------------------------------------------------------------------

    def windowDidResize_(self, notification) -> None:  # type: ignore[no-untyped-def]
        try:
            self._layout_for_bounds(self.contentView().bounds())
        except Exception:  # noqa: BLE001
            pass
        try:
            self._panel._reposition_af_marker()
        except Exception:  # noqa: BLE001
            pass
        self._notify_panel_frame_changed()

    def windowDidMove_(self, notification) -> None:  # type: ignore[no-untyped-def]
        self._notify_panel_frame_changed()

    # NB: with no Closable in the style mask the user *can't* close
    # this window via UI. We still wire a defensive windowShouldClose_
    # → False so a programmatic close (e.g. ⌘W via some accelerator we
    # didn't anticipate) doesn't kill the LV mid-session. The panel's
    # _reattach_lv / stop is the only legitimate teardown path.
    def windowShouldClose_(self, sender) -> bool:  # type: ignore[no-untyped-def]
        return False

    # ------------------------------------------------------------------
    # Panel notification (debounced save lives on the panel side)
    # ------------------------------------------------------------------

    @objc.python_method
    def _notify_panel_frame_changed(self) -> None:
        cb = getattr(self._panel, "_on_lv_window_frame_changed", None)
        if cb is None:
            return
        try:
            cb(self.frame())
        except Exception:  # noqa: BLE001
            # Don't propagate UI-thread exceptions into AppKit's
            # notification dispatch — log silently and continue.
            pass

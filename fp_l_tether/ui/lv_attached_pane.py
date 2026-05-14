"""Panel-side LV slot placeholder while the viewport is detached.

When Phase 3.12 ⌘D floats the LV view stack into a separate window,
the panel's original LV rect is left empty. We drop this small NSView
into that slot so the panel doesn't have a black hole; it shows a
single ⤢ glyph + two lines of helper text and, as a redundant
affordance, reattaches when clicked.

The placeholder is intentionally chrome-light: same background colour
as the panel (anodized black), no separator, no border. The amber ⤢
glyph is the only visual weight — same accent we use for the AF
reticle / status busy dot, so it reads as "this is where the live
view *will* return to" rather than as a new control.

Click target = the full view bounds. The panel's ``_toggle_lv_detached``
gets called via the back-ref passed in ``setOwner_`` (mirroring the
``LiveViewImageView`` pattern in ``floating_panel``).
"""

from __future__ import annotations

import objc
from AppKit import (
    NSAttributedString,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSMakePoint,
    NSMakeRect,
    NSMakeSize,
    NSMutableParagraphStyle,
    NSParagraphStyleAttributeName,
    NSTextAlignmentCenter,
    NSView,
)


# Colours / fonts are duplicated as literal NSColor values instead of
# imported from ``floating_panel`` to avoid a UI-module import cycle
# (the panel imports this module during _build_window). The numbers
# match the tokens in ``floating_panel.C_BG_PANEL`` / ``C_AMBER`` /
# ``C_FG_SECONDARY`` / ``C_FG_TERTIARY`` 1:1 — if the panel reskins,
# update both sides together.
_BG = NSColor.colorWithCalibratedRed_green_blue_alpha_(
    26 / 255.0, 26 / 255.0, 28 / 255.0, 1.0
)
_AMBER = NSColor.colorWithCalibratedRed_green_blue_alpha_(
    232 / 255.0, 161 / 255.0, 58 / 255.0, 1.0
)
_FG_SECONDARY = NSColor.colorWithCalibratedRed_green_blue_alpha_(
    144 / 255.0, 144 / 255.0, 160 / 255.0, 1.0
)
_FG_TERTIARY = NSColor.colorWithCalibratedRed_green_blue_alpha_(
    106 / 255.0, 106 / 255.0, 120 / 255.0, 1.0
)

# Glyph: SF Symbols ``arrow.up.left.and.arrow.down.right`` would be
# nice but requires AppKit's SF Symbols API (macOS 11+ + extra
# plumbing). Unicode ⤢ (U+2922) renders identically in the dark
# panel context at 28pt and works back to macOS 10.13. The trade-off
# is intentional: we keep the placeholder font-only so it inherits
# the panel's appearance changes without bespoke asset wiring.
_GLYPH = "\u2922"  # ⤢


class LVDetachedPlaceholder(NSView):
    """Static slot view shown when LV is in the detached window.

    The view is purely presentational — it stores a back-ref to the
    panel so ``mouseDown_`` can drive the same toggle the ⌘D hotkey
    does (redundant affordance for users who haven't found the
    keybinding yet).
    """

    def initWithFrame_(self, frame):  # type: ignore[no-untyped-def]
        self = objc.super(LVDetachedPlaceholder, self).initWithFrame_(frame)
        if self is None:
            return None
        self._owner = None
        # Match the panel background so the slot feels like dead space
        # rather than a separate widget. We pick layer-backing so the
        # background colour is a single fillRect on the GPU.
        self.setWantsLayer_(True)
        layer = self.layer()
        if layer is not None:
            layer.setBackgroundColor_(_BG.CGColor())
            # Mirror the LV image view's 4pt corner radius so reattach
            # is visually seamless (placeholder and LV occupy the same
            # bounds — sharing the corner radius keeps the eye stable).
            layer.setCornerRadius_(4.0)
            layer.setMasksToBounds_(True)
        return self

    def setOwner_(self, owner) -> None:  # type: ignore[no-untyped-def]
        self._owner = owner

    def mouseDown_(self, event) -> None:  # type: ignore[no-untyped-def]
        owner = self._owner
        if owner is None:
            return
        # Single entry point — the panel decides whether to detach or
        # reattach based on ``_lv_window`` state.
        toggle = getattr(owner, "_toggle_lv_detached", None)
        if toggle is not None:
            toggle()

    def isOpaque(self) -> bool:  # type: ignore[override]
        return True

    def drawRect_(self, dirty_rect) -> None:  # type: ignore[no-untyped-def]
        bounds = self.bounds()
        w = bounds.size.width
        h = bounds.size.height
        if w <= 0 or h <= 0:
            return

        # ---- ⤢ glyph (centered, amber, 28pt) ----------------------
        glyph_font = NSFont.systemFontOfSize_(28.0)
        glyph_style = NSMutableParagraphStyle.alloc().init()
        glyph_style.setAlignment_(NSTextAlignmentCenter)
        glyph_attrs = {
            NSFontAttributeName: glyph_font,
            NSForegroundColorAttributeName: _AMBER,
            NSParagraphStyleAttributeName: glyph_style,
        }
        glyph = NSAttributedString.alloc().initWithString_attributes_(
            _GLYPH, glyph_attrs
        )
        glyph_size = glyph.size()
        # Vertically position the glyph slightly above centre so the
        # two text lines underneath share the same optical centre as
        # the view as a whole.
        glyph_y = (h / 2.0) + 6.0
        glyph.drawAtPoint_(
            NSMakePoint((w - glyph_size.width) / 2.0, glyph_y)
        )

        # ---- "LV detached" + hint --------------------------------
        body_style = NSMutableParagraphStyle.alloc().init()
        body_style.setAlignment_(NSTextAlignmentCenter)

        title_attrs = {
            NSFontAttributeName: NSFont.systemFontOfSize_(11.0),
            NSForegroundColorAttributeName: _FG_SECONDARY,
            NSParagraphStyleAttributeName: body_style,
        }
        hint_attrs = {
            NSFontAttributeName: NSFont.systemFontOfSize_(10.0),
            NSForegroundColorAttributeName: _FG_TERTIARY,
            NSParagraphStyleAttributeName: body_style,
        }
        title = NSAttributedString.alloc().initWithString_attributes_(
            "LV detached", title_attrs
        )
        hint = NSAttributedString.alloc().initWithString_attributes_(
            "\u2318D to reattach", hint_attrs  # ⌘D
        )

        # Two-line stack below the glyph. 14pt + 12pt line boxes give
        # comfortable breathing room without spilling into the bottom
        # edge of the 200pt-tall slot.
        title_rect = NSMakeRect(0, (h / 2.0) - 18.0, w, 14.0)
        hint_rect = NSMakeRect(0, (h / 2.0) - 34.0, w, 12.0)
        title.drawInRect_(title_rect)
        hint.drawInRect_(hint_rect)

    # Silence the system "no acceptsFirstMouse" warning — we want the
    # very first click after the panel gains focus to drive reattach
    # (matches macOS norm for utility-panel buttons).
    def acceptsFirstMouse_(self, event) -> bool:  # type: ignore[no-untyped-def]
        return True

    # Convenience for callers that hand-build the placeholder without
    # caring about its exact intrinsic size: returning the current
    # frame size means autoresizing parents don't accidentally
    # compress us below the slot width.
    def intrinsicContentSize(self):  # type: ignore[no-untyped-def]
        return NSMakeSize(self.frame().size.width, self.frame().size.height)

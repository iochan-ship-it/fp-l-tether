"""Composition grid overlay for the Live View viewport.

Thin NSView that draws subtle white guide lines on top of the LV
image. Cycles through four modes:

* ``off``    — no grid (view is hidden externally; drawRect is a no-op)
* ``thirds`` — rule-of-thirds (2 vertical + 2 horizontal at 1/3, 2/3)
* ``golden`` — golden-ratio (φ ≈ 0.618 and complement 0.382)
* ``full``   — 10×6 fine alignment grid for art-repro / product work

Implementation uses ``drawRect_`` + ``NSBezierPath`` instead of the
``CAShapeLayer`` form sketched in the spec, because the panel already
keeps its other overlays (AF reticle, pause veil) as plain NSViews
and the line count is tiny (max 14 strokes in "full" mode) — a
single bezier path is cheaper than the CGPath round-trip would be.
"""

from __future__ import annotations

from typing import Literal

import objc
from AppKit import (
    NSBezierPath,
    NSColor,
    NSMakePoint,
    NSView,
)


# Spec: subtle 30% white. Strong enough to read against most LV content
# but light enough not to compete with the subject.
_LINE_COLOR = NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.30)
_LINE_WIDTH = 1.0


GridMode = Literal["off", "thirds", "golden", "full"]

# Cycle order: hotkey G advances along this list, wrapping to start.
_CYCLE: tuple[GridMode, ...] = ("off", "thirds", "golden", "full")


def cycle_mode(current: GridMode) -> GridMode:
    """Return the next grid mode in the cycle."""
    try:
        idx = _CYCLE.index(current)
    except ValueError:
        return _CYCLE[0]
    return _CYCLE[(idx + 1) % len(_CYCLE)]


# Vertical / horizontal fractional positions per mode. Coordinates are
# fractions of the view's bounds (0 = left/bottom, 1 = right/top).
_POSITIONS: dict[GridMode, tuple[tuple[float, ...], tuple[float, ...]]] = {
    "off":    ((), ()),
    "thirds": ((1 / 3, 2 / 3), (1 / 3, 2 / 3)),
    "golden": ((0.382, 0.618), (0.382, 0.618)),
    "full":   (tuple(i / 10 for i in range(1, 10)),
               tuple(i / 6 for i in range(1, 6))),
}


class GridOverlayView(NSView):
    """Translucent NSView that strokes composition guide lines.

    Use ``setMode_`` from the main thread; the view marks itself dirty
    and AppKit redraws on the next runloop tick. ``isOpaque`` returns
    False so the LV image shows through everywhere except the lines.
    """

    def initWithFrame_(self, frame):  # type: ignore[no-untyped-def]
        self = objc.super(GridOverlayView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._mode: GridMode = "off"
        self.setWantsLayer_(True)
        # Hit-test ignores this view — clicks should fall through to
        # the LV image so the AF-point click-to-set behaviour still
        # works when a grid is drawn on top.
        return self

    def setMode_(self, mode: str) -> None:  # type: ignore[no-untyped-def]
        if mode not in _POSITIONS:
            mode = "off"
        self._mode = mode  # type: ignore[assignment]
        self.setNeedsDisplay_(True)

    def mode(self) -> str:
        return self._mode

    def isOpaque(self) -> bool:  # type: ignore[override]
        return False

    def hitTest_(self, point):  # type: ignore[no-untyped-def]
        # Pass clicks through to whatever's underneath (the LV image
        # view, which handles click-to-focus).
        return None

    def drawRect_(self, dirty_rect) -> None:  # type: ignore[no-untyped-def]
        if self._mode == "off":
            return
        verticals, horizontals = _POSITIONS.get(self._mode, ((), ()))
        if not verticals and not horizontals:
            return

        bounds = self.bounds()
        w = bounds.size.width
        h = bounds.size.height
        if w <= 0 or h <= 0:
            return

        path = NSBezierPath.bezierPath()
        path.setLineWidth_(_LINE_WIDTH)
        for fx in verticals:
            # Snap to half-pixel for crisper 1pt strokes on integer-
            # scale displays. fmod doesn't matter for grids this small.
            x = round(fx * w) + 0.5
            path.moveToPoint_(NSMakePoint(x, 0.0))
            path.lineToPoint_(NSMakePoint(x, h))
        for fy in horizontals:
            y = round(fy * h) + 0.5
            path.moveToPoint_(NSMakePoint(0.0, y))
            path.lineToPoint_(NSMakePoint(w, y))
        _LINE_COLOR.setStroke()
        path.stroke()

"""RGB histogram — bottom strip or top-right overlay inside the LV.

NSView subclass that owns a ``HistogramData`` snapshot and renders it
in ``drawRect:`` using NSBezierPath. Three overlapping line paths
(R / G / B) drawn with additive compositing so the overlap area
brightens toward white — the standard additive-blend RGB histogram look.

Numeric clipping markers (▲ X.X% / ▽ X.X%) are drawn at the corners.

Phase 3.13 adds a second placement mode: a 180×90 rounded box anchored
to the LV's top-right corner. Mode selection lives in
:py:meth:`HistogramView.setMode_`; the caller is responsible for
matching the view's frame to the chosen mode.

The view is purely presentation: it stores the latest data, calls
``setNeedsDisplay_(True)``, and lets AppKit redraw on the next runloop
tick. No timers, no callbacks back into the daemon.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import objc
from AppKit import (
    NSBezierPath,
    NSColor,
    NSCompositingOperationPlusLighter,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSGraphicsContext,
    NSMakePoint,
    NSMakeRect,
    NSMutableParagraphStyle,
    NSParagraphStyleAttributeName,
    NSTextAlignmentLeft,
    NSTextAlignmentRight,
    NSView,
)
from Foundation import NSAttributedString

if TYPE_CHECKING:
    from fp_l_tether.transfer.histogram import HistogramData


# Strip background: semi-transparent dark ink so the LV image shows
# through, but the histogram strokes have enough contrast to read.
_BG_COLOR = NSColor.colorWithCalibratedRed_green_blue_alpha_(
    10 / 255.0, 10 / 255.0, 12 / 255.0, 0.65
)

# Channel stroke colors — lighter than primary RGB so additive blend
# overlap reads as white rather than as garish over-saturated patches.
_R_COLOR = NSColor.colorWithCalibratedRed_green_blue_alpha_(
    255 / 255.0, 90 / 255.0, 90 / 255.0, 0.75
)
_G_COLOR = NSColor.colorWithCalibratedRed_green_blue_alpha_(
    90 / 255.0, 255 / 255.0, 130 / 255.0, 0.75
)
_B_COLOR = NSColor.colorWithCalibratedRed_green_blue_alpha_(
    110 / 255.0, 150 / 255.0, 255 / 255.0, 0.85
)

# Clipping-marker colors — start out subdued white, switch to a strong
# warning tint once the % crosses the spec's 1.0% threshold.
_MARKER_WHITE = NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.95)
_MARKER_RED = NSColor.colorWithCalibratedRed_green_blue_alpha_(
    255 / 255.0, 69 / 255.0, 58 / 255.0, 1.0
)
_MARKER_BLUE = NSColor.colorWithCalibratedRed_green_blue_alpha_(
    110 / 255.0, 180 / 255.0, 255 / 255.0, 1.0
)

# Spec: SF Mono Medium 11pt (F_NUMERIC). Pulled in via constructor
# rather than imported from floating_panel to keep this module free
# of UI cycles.
_MARKER_FONT = NSFont.monospacedSystemFontOfSize_weight_(11, 0.23)

# Compact (top-right) marker font — SF Mono Medium 10pt. The compact
# box is 180×90, so the 11pt label crowds the 14pt top margin; 10pt
# leaves a comfortable optical gap.
_COMPACT_MARKER_FONT = NSFont.monospacedSystemFontOfSize_weight_(10, 0.23)

# Phase 3.13 — compact (top-right) chrome tokens. The layer paints
# the background + border so drawRect_ doesn't repaint them per frame.
_COMPACT_BG_COLOR = NSColor.colorWithCalibratedRed_green_blue_alpha_(
    10 / 255.0, 10 / 255.0, 12 / 255.0, 0.78
)
_COMPACT_BORDER_COLOR = NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.12)
_COMPACT_CORNER_RADIUS = 4.0
_COMPACT_BORDER_WIDTH = 0.5
_COMPACT_PADDING = 6
_COMPACT_TOP_MARGIN = 14

# Padding inside the strip — leaves room for the corner markers.
_PADDING = 8

# Clipping-percentage threshold above which markers turn warning-colored.
_CLIP_WARN_PCT = 1.0

# Log-scale baseline: bins with count = 0 should render as a flat
# bottom line rather than -inf. Add 1 before log10 → smallest non-zero
# value renders at log10(2) ≈ 0.3 above the floor, which is visually
# distinct from "literally zero".
import math

_LOG_FLOOR = math.log10(1.0)


def _log_scale(count: int, max_log: float, height: float) -> float:
    """Map a histogram bin count → strip-relative Y in points."""
    if count <= 0 or max_log <= 0:
        return 0.0
    return (math.log10(count + 1) / max_log) * height


class HistogramView(NSView):
    """RGB histogram with clipping % markers — two render modes.

    Use ``setData_`` from the main thread to push a new
    ``HistogramData`` snapshot — the view marks itself dirty and
    AppKit redraws on the next runloop tick.

    Two modes (Phase 3.13):
      - ``bottom_strip`` (default): full-width band at the LV's
        bottom edge, translucent dark background painted in
        ``drawRect_`` — the historical layout.
      - ``top_right``: 180×90 rounded box with the background and
        border painted by the CALayer (so ``_draw_compact`` only
        renders traces + markers).

    Switch via :py:meth:`setMode_`; the caller is responsible for
    also setting the correct frame.
    """

    _MODE_BOTTOM_STRIP = "bottom_strip"
    _MODE_TOP_RIGHT = "top_right"

    def initWithFrame_(self, frame):  # type: ignore[no-untyped-def]
        self = objc.super(HistogramView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._data: HistogramData | None = None
        self._mode: str = self._MODE_BOTTOM_STRIP
        # Layer-backing keeps the strip composited on top of the LV
        # image without redraw flicker when the LV refreshes at 10 fps.
        self.setWantsLayer_(True)
        if self.layer() is not None:
            self.layer().setBackgroundColor_(NSColor.clearColor().CGColor())
        return self

    def setData_(self, data):  # type: ignore[no-untyped-def]
        """Push the latest histogram. Pass ``None`` to clear."""
        self._data = data
        self.setNeedsDisplay_(True)

    def setMode_(self, mode):  # type: ignore[no-untyped-def]
        """Switch between 'bottom_strip' and 'top_right' rendering.

        Callers must also ``setFrame_`` to the appropriate frame; this
        method only flips the rendering style + layer chrome.
        No-op when the mode is unchanged or unrecognised.
        """
        if mode not in (self._MODE_BOTTOM_STRIP, self._MODE_TOP_RIGHT):
            return
        if getattr(self, "_mode", None) == mode:
            return
        self._mode = mode
        self.setWantsLayer_(True)
        layer = self.layer()
        if layer is None:
            self.setNeedsDisplay_(True)
            return
        if mode == self._MODE_TOP_RIGHT:
            # Layer paints the box chrome — drawRect_ stays out of it.
            layer.setBackgroundColor_(_COMPACT_BG_COLOR.CGColor())
            layer.setCornerRadius_(_COMPACT_CORNER_RADIUS)
            layer.setBorderWidth_(_COMPACT_BORDER_WIDTH)
            layer.setBorderColor_(_COMPACT_BORDER_COLOR.CGColor())
        else:
            # Bottom strip: drawRect_ fills _BG_COLOR manually.
            layer.setBackgroundColor_(NSColor.clearColor().CGColor())
            layer.setCornerRadius_(0.0)
            layer.setBorderWidth_(0.0)
        self.setNeedsDisplay_(True)

    def isOpaque(self) -> bool:  # type: ignore[override]
        # Translucent — the LV image must remain visible behind the strip.
        return False

    def drawRect_(self, dirty_rect) -> None:  # type: ignore[no-untyped-def]
        if self._mode == self._MODE_TOP_RIGHT:
            self._draw_compact(dirty_rect)
        else:
            self._draw_strip(dirty_rect)

    # ----- bottom-strip rendering (existing layout) ----------------

    def _draw_strip(self, dirty_rect) -> None:  # type: ignore[no-untyped-def]
        bounds = self.bounds()
        w = bounds.size.width
        h = bounds.size.height

        # ---- Background fill -------------------------------------
        _BG_COLOR.setFill()
        NSBezierPath.fillRect_(NSMakeRect(0, 0, w, h))

        data = self._data
        if data is None:
            return

        # ---- Histogram traces ------------------------------------
        # Y-axis: log scale, normalized to the strip's highest bin
        # across all three channels so peaks reach near the top edge.
        inner_w = w - 2 * _PADDING
        # Reserve a bit at the top for the markers so the histogram
        # peaks don't crowd them.
        top_margin = 16
        inner_h = max(1.0, h - top_margin - _PADDING)

        max_count = max(
            max(data.r) if data.r else 0,
            max(data.g) if data.g else 0,
            max(data.b) if data.b else 0,
        )
        if max_count <= 0:
            # Flat / empty frame — draw the markers and bail.
            self._draw_markers(data, w, h)
            return
        max_log = math.log10(max_count + 1)

        # Use additive ("plus lighter") compositing so RGB overlap
        # brightens to white. Wrap in saveGraphicsState / restore
        # so subsequent text drawing isn't accidentally additive.
        ctx = NSGraphicsContext.currentContext()
        if ctx is not None:
            ctx.saveGraphicsState()
            ctx.setCompositingOperation_(NSCompositingOperationPlusLighter)
        try:
            for channel, color in (
                (data.r, _R_COLOR),
                (data.g, _G_COLOR),
                (data.b, _B_COLOR),
            ):
                self._stroke_channel(
                    channel, color, max_log, inner_w, inner_h, _PADDING
                )
        finally:
            if ctx is not None:
                ctx.restoreGraphicsState()

        # ---- Clipping markers (drawn AFTER the histogram so they
        #      sit on top, not blended additively into white) -------
        self._draw_markers(data, w, h)

    # ----- compact (top-right) rendering (Phase 3.13) --------------

    def _draw_compact(self, dirty_rect) -> None:  # type: ignore[no-untyped-def]
        """Render the 180×90 top-right overlay.

        Layer-painted background + border, so we only draw the traces
        and the corner markers here. Padding 6pt, top 14pt reserved
        for markers, plot area ≈ 168 × 70.
        """
        data = self._data
        if data is None:
            return
        bounds = self.bounds()
        w = bounds.size.width
        h = bounds.size.height
        pad = _COMPACT_PADDING
        top_margin = _COMPACT_TOP_MARGIN
        inner_w = max(1.0, w - 2 * pad)
        inner_h = max(1.0, h - top_margin - pad)

        max_count = max(
            max(data.r) if data.r else 0,
            max(data.g) if data.g else 0,
            max(data.b) if data.b else 0,
        )
        if max_count <= 0:
            self._draw_markers_compact(data, w, h)
            return
        max_log = math.log10(max_count + 1)

        ctx = NSGraphicsContext.currentContext()
        if ctx is not None:
            ctx.saveGraphicsState()
            ctx.setCompositingOperation_(NSCompositingOperationPlusLighter)
        try:
            for channel, color in (
                (data.r, _R_COLOR),
                (data.g, _G_COLOR),
                (data.b, _B_COLOR),
            ):
                self._stroke_channel(
                    channel, color, max_log, inner_w, inner_h, pad
                )
        finally:
            if ctx is not None:
                ctx.restoreGraphicsState()

        self._draw_markers_compact(data, w, h)

    # ----- helpers ---------------------------------------------------

    def _stroke_channel(
        self,
        bins,
        color,
        max_log: float,
        inner_w: float,
        inner_h: float,
        pad: int,
    ) -> None:
        """Draw one R/G/B channel as a stroked line path.

        ``bins`` is a list of 256 ints. We sample one X step per bin
        (no interpolation needed at 256 buckets across a typical 270px
        strip) and use log-scale Y.
        """
        if not bins:
            return
        path = NSBezierPath.bezierPath()
        path.setLineWidth_(1.0)
        # X step: distribute 256 buckets evenly across inner width.
        n = len(bins)
        step = inner_w / max(1, n - 1)
        # First point
        y0 = _log_scale(bins[0], max_log, inner_h)
        path.moveToPoint_(NSMakePoint(pad, pad + y0))
        for i in range(1, n):
            x = pad + i * step
            y = pad + _log_scale(bins[i], max_log, inner_h)
            path.lineToPoint_(NSMakePoint(x, y))
        color.setStroke()
        path.stroke()

    def _draw_markers(self, data, w: float, h: float) -> None:
        """Top-left ▲ X.X% (highlight) + top-right ▽ X.X% (shadow)."""
        high_pct = max(0.0, float(data.clipped_high_pct))
        low_pct = max(0.0, float(data.clipped_low_pct))

        high_color = _MARKER_RED if high_pct > _CLIP_WARN_PCT else _MARKER_WHITE
        low_color = _MARKER_BLUE if low_pct > _CLIP_WARN_PCT else _MARKER_WHITE

        high_text = f"▲ {high_pct:4.1f}%"
        low_text = f"▽ {low_pct:4.1f}%"

        # Left-aligned (highlight) and right-aligned (shadow) at the
        # top of the strip. 14pt label height keeps them inside the
        # 16pt top margin reserved in drawRect_.
        text_h = 14
        text_y = h - _PADDING - text_h + 2  # nudge baseline a touch

        left_style = NSMutableParagraphStyle.alloc().init()
        left_style.setAlignment_(NSTextAlignmentLeft)
        right_style = NSMutableParagraphStyle.alloc().init()
        right_style.setAlignment_(NSTextAlignmentRight)

        left_attrs = {
            NSFontAttributeName: _MARKER_FONT,
            NSForegroundColorAttributeName: high_color,
            NSParagraphStyleAttributeName: left_style,
        }
        right_attrs = {
            NSFontAttributeName: _MARKER_FONT,
            NSForegroundColorAttributeName: low_color,
            NSParagraphStyleAttributeName: right_style,
        }
        left_str = NSAttributedString.alloc().initWithString_attributes_(
            high_text, left_attrs
        )
        right_str = NSAttributedString.alloc().initWithString_attributes_(
            low_text, right_attrs
        )
        marker_w = 80
        left_str.drawInRect_(
            NSMakeRect(_PADDING, text_y, marker_w, text_h)
        )
        right_str.drawInRect_(
            NSMakeRect(w - _PADDING - marker_w, text_y, marker_w, text_h)
        )

    def _draw_markers_compact(self, data, w: float, h: float) -> None:
        """Compact-mode marker variant — 10pt font, 6pt padding."""
        high_pct = max(0.0, float(data.clipped_high_pct))
        low_pct = max(0.0, float(data.clipped_low_pct))

        high_color = _MARKER_RED if high_pct > _CLIP_WARN_PCT else _MARKER_WHITE
        low_color = _MARKER_BLUE if low_pct > _CLIP_WARN_PCT else _MARKER_WHITE

        high_text = f"\u25b2 {high_pct:4.1f}%"  # ▲
        low_text = f"\u25bd {low_pct:4.1f}%"    # ▽

        pad = _COMPACT_PADDING
        text_h = 12
        text_y = h - pad - text_h + 1

        left_style = NSMutableParagraphStyle.alloc().init()
        left_style.setAlignment_(NSTextAlignmentLeft)
        right_style = NSMutableParagraphStyle.alloc().init()
        right_style.setAlignment_(NSTextAlignmentRight)

        left_attrs = {
            NSFontAttributeName: _COMPACT_MARKER_FONT,
            NSForegroundColorAttributeName: high_color,
            NSParagraphStyleAttributeName: left_style,
        }
        right_attrs = {
            NSFontAttributeName: _COMPACT_MARKER_FONT,
            NSForegroundColorAttributeName: low_color,
            NSParagraphStyleAttributeName: right_style,
        }
        left_str = NSAttributedString.alloc().initWithString_attributes_(
            high_text, left_attrs
        )
        right_str = NSAttributedString.alloc().initWithString_attributes_(
            low_text, right_attrs
        )
        marker_w = 72
        left_str.drawInRect_(
            NSMakeRect(pad, text_y, marker_w, text_h)
        )
        right_str.drawInRect_(
            NSMakeRect(w - pad - marker_w, text_y, marker_w, text_h)
        )

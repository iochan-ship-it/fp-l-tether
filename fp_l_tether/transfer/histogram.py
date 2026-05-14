"""RGB histogram + clipping % computation for Sigma fp L Live View frames.

Pillow-only — no numpy. Pillow's C-backed ``Image.histogram()`` and the
``ImageChops`` lighter/darker ops give us fast per-channel bins and
correct "any-channel clipped" counts.

Called from ``LiveViewStream`` on a side-channel thread, so this module
must NOT touch the PTP endpoint or any AppKit objects — it's pure
image-math.

Performance target: < 40 ms per call on a 1620×1080 JPEG with
``downsample=2`` (≈400k pixels evaluated). If it goes over,
``LiveViewStream``'s "one outstanding compute" pattern naturally skips
frames rather than backing up.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageChops


@dataclass
class HistogramData:
    """One frame's RGB histogram + clipping metrics.

    Counts are plain Python lists of 256 ints (one bin per intensity 0..255)
    rather than ``numpy.ndarray`` so we don't drag numpy in as a hard
    dependency. The view layer iterates them once per frame to build
    NSBezierPath segments — fast enough even at 10 fps.
    """

    r: list[int]
    g: list[int]
    b: list[int]
    clipped_high_pct: float
    clipped_low_pct: float
    width: int
    height: int
    total_pixels: int


# Clipping thresholds — match the spec.
# A pixel counts as "highlight clipped" when ANY channel ≥ this value.
_HIGH_THRESHOLD = 254
# A pixel counts as "shadow crushed" when ANY channel ≤ this value.
_LOW_THRESHOLD = 1


def compute_histogram(jpeg_bytes: bytes, downsample: int = 2) -> HistogramData:
    """Decode JPEG and compute RGB histogram + clipping percentages.

    ``downsample`` is a box-reduction factor: 2 means we sample every
    2x2 block (≈¼ pixel count), which keeps compute under ~30 ms on a
    1620×1080 frame while preserving histogram shape. 3 or 4 are still
    statistically valid if more headroom is needed.

    ``Image.reduce`` is preferred over ``[::n, ::n]`` indexing because
    it's a single C call that averages each NxN block rather than
    picking a single pixel — antialiasing the result so the histogram
    isn't biased by which pixels happened to be sampled.
    """
    img = Image.open(io.BytesIO(jpeg_bytes))
    if img.mode != "RGB":
        img = img.convert("RGB")
    if downsample > 1:
        # Image.reduce takes the box factor; clamp to ≥ 1 so the
        # divide-by-zero branch can't be hit accidentally.
        img = img.reduce(max(1, int(downsample)))

    width, height = img.size
    total = width * height

    r_band, g_band, b_band = img.split()
    hist_r = r_band.histogram()  # list[int] length 256
    hist_g = g_band.histogram()
    hist_b = b_band.histogram()

    # Per-pixel "any channel clipped" counts: lighter() = per-pixel max
    # of two images, darker() = per-pixel min. Histogram of the
    # max-image gives bins of "the brightest channel was X" → the tail
    # ≥ _HIGH_THRESHOLD is the "any channel near 255" count exactly,
    # and symmetric for the dark side.
    max_img = ImageChops.lighter(ImageChops.lighter(r_band, g_band), b_band)
    max_hist = max_img.histogram()
    high_count = sum(max_hist[_HIGH_THRESHOLD:])

    min_img = ImageChops.darker(ImageChops.darker(r_band, g_band), b_band)
    min_hist = min_img.histogram()
    low_count = sum(min_hist[: _LOW_THRESHOLD + 1])

    clipped_high_pct = 100.0 * high_count / total if total else 0.0
    clipped_low_pct = 100.0 * low_count / total if total else 0.0

    return HistogramData(
        r=hist_r,
        g=hist_g,
        b=hist_b,
        clipped_high_pct=clipped_high_pct,
        clipped_low_pct=clipped_low_pct,
        width=width,
        height=height,
        total_pixels=total,
    )

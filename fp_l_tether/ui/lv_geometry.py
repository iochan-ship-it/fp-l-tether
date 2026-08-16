"""Pure geometry helpers for the live-view viewport (Phase 3.23).

Split out of ``floating_panel`` so the maths is testable without
importing AppKit — the unit suite has to stay pure-Python for CI.

Two problems live here:

**Rotation.** The Sigma fp L always streams live view in sensor-native
landscape and publishes no attitude / level data over PTP (settled in
Phase 3.11c — it is in no DataGroup), so a body turned to portrait
shows the subject lying on its side and nothing can detect it for us.
The user picks the rotation manually; these functions keep AF clicks
and the AF reticle consistent with whatever they picked.

Coordinate conventions:

- *image-normalised* ``(nx, ny)`` — origin top-left, x right, y DOWN.
  This is the camera's own AF coordinate sense.
- *display-normalised* ``(dx, dy)`` — same shape, but of the rotated
  picture as the user sees it.
- ``degrees`` is clockwise, one of 0 / 90 / 180 / 270.

**Aspect fit.** ``NSImageScaleProportionallyUpOrDown`` centres the
frame and letterboxes the rest, so view coords are only image coords
when the aspects match. At 0° the error was small (LV JPEG vs a
288×200 viewport); at 90° a portrait frame in a landscape box is
mostly black bar, and a click in the bar would map to a bogus sensor
coordinate.
"""

from __future__ import annotations

ROTATIONS: tuple[int, ...] = (0, 90, 180, 270)


def normalise_rotation(degrees: int) -> int:
    """Clamp an arbitrary angle to the nearest supported quarter-turn."""
    deg = int(degrees) % 360
    return deg if deg in ROTATIONS else 0


def rotate_forward(degrees: int, nx: float, ny: float) -> tuple[float, float]:
    """Image-normalised → display-normalised.

    At 90° clockwise the image's top-left corner ends up at the
    display's top-right, so ``(0, 0) → (1, 0)``.
    """
    deg = normalise_rotation(degrees)
    if deg == 90:
        return 1.0 - ny, nx
    if deg == 180:
        return 1.0 - nx, 1.0 - ny
    if deg == 270:
        return ny, 1.0 - nx
    return nx, ny


def rotate_inverse(degrees: int, dx: float, dy: float) -> tuple[float, float]:
    """Display-normalised → image-normalised. Exact inverse of forward."""
    deg = normalise_rotation(degrees)
    if deg == 90:
        return dy, 1.0 - dx
    if deg == 180:
        return 1.0 - dx, 1.0 - dy
    if deg == 270:
        return 1.0 - dy, dx
    return dx, dy


def fit_rect(
    container_w: float,
    container_h: float,
    image_w: float,
    image_h: float,
) -> tuple[float, float, float, float]:
    """Where an aspect-fitted, centred image lands in its container.

    Returns ``(offset_x, offset_y, width, height)``. Degenerate inputs
    (any dimension ≤ 0) fall back to filling the container, which is
    what the caller wants before the first frame arrives.
    """
    if container_w <= 0 or container_h <= 0 or image_w <= 0 or image_h <= 0:
        return 0.0, 0.0, container_w, container_h
    scale = min(container_w / image_w, container_h / image_h)
    w = image_w * scale
    h = image_h * scale
    return (container_w - w) / 2.0, (container_h - h) / 2.0, w, h

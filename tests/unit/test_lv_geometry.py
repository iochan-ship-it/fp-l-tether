"""Phase 3.23 — LV rotation + aspect-fit geometry.

Pure-Python (no AppKit import) so this runs in CI. The maths is what
keeps an AF click landing where the user pointed once the live view is
manually rotated for portrait shooting.
"""

from __future__ import annotations

import pytest

from fp_l_tether.ui.lv_geometry import (
    ROTATIONS,
    fit_rect,
    normalise_rotation,
    rotate_forward,
    rotate_inverse,
)


@pytest.mark.parametrize("deg", ROTATIONS)
@pytest.mark.parametrize(
    "point", [(0.0, 0.0), (1.0, 0.0), (0.5, 0.25), (0.13, 0.87), (1.0, 1.0)]
)
def test_rotation_roundtrips(deg: int, point: tuple[float, float]) -> None:
    """inverse(forward(p)) == p — a click on the reticle re-selects it."""
    nx, ny = point
    dx, dy = rotate_forward(deg, nx, ny)
    back = rotate_inverse(deg, dx, dy)
    assert back == pytest.approx((nx, ny))


def test_rotation_90_moves_top_left_to_top_right() -> None:
    """The defining case: a clockwise quarter-turn of the picture."""
    assert rotate_forward(90, 0.0, 0.0) == pytest.approx((1.0, 0.0))
    assert rotate_forward(90, 1.0, 0.0) == pytest.approx((1.0, 1.0))
    assert rotate_forward(90, 1.0, 1.0) == pytest.approx((0.0, 1.0))
    assert rotate_forward(90, 0.0, 1.0) == pytest.approx((0.0, 0.0))


def test_rotation_180_is_point_reflection() -> None:
    assert rotate_forward(180, 0.25, 0.75) == pytest.approx((0.75, 0.25))


def test_rotation_270_is_inverse_of_90() -> None:
    """Two named quarter-turns that must compose back to identity."""
    p = (0.3, 0.8)
    once = rotate_forward(90, *p)
    assert rotate_forward(270, *once) == pytest.approx(p)


def test_rotation_zero_is_identity() -> None:
    assert rotate_forward(0, 0.42, 0.17) == (0.42, 0.17)
    assert rotate_inverse(0, 0.42, 0.17) == (0.42, 0.17)


def test_centre_is_fixed_under_every_rotation() -> None:
    for deg in ROTATIONS:
        assert rotate_forward(deg, 0.5, 0.5) == pytest.approx((0.5, 0.5))


@pytest.mark.parametrize(
    "raw,expected",
    [(0, 0), (90, 90), (360, 0), (450, 90), (-90, 270), (45, 0), (17, 0)],
)
def test_normalise_rotation(raw: int, expected: int) -> None:
    """Unsupported angles degrade to 0° rather than skewing the mapping."""
    assert normalise_rotation(raw) == expected


def test_fit_rect_letterboxes_a_portrait_frame_in_a_landscape_box() -> None:
    """The 90°-rotated case: 1080×1620 LV inside the 288×200 viewport."""
    ox, oy, w, h = fit_rect(288.0, 200.0, 1080.0, 1620.0)
    assert h == pytest.approx(200.0)          # height-limited
    assert w == pytest.approx(200.0 * 1080 / 1620)
    assert oy == pytest.approx(0.0)
    assert ox == pytest.approx((288.0 - w) / 2.0)  # centred → real black bars


def test_fit_rect_pillarboxes_a_wide_frame() -> None:
    ox, oy, w, h = fit_rect(288.0, 200.0, 1024.0, 682.0)
    assert w == pytest.approx(288.0)          # width-limited
    assert h == pytest.approx(288.0 * 682 / 1024)
    assert ox == pytest.approx(0.0)
    assert oy == pytest.approx((200.0 - h) / 2.0)


def test_fit_rect_matching_aspect_fills_exactly() -> None:
    assert fit_rect(300.0, 200.0, 1500.0, 1000.0) == pytest.approx(
        (0.0, 0.0, 300.0, 200.0)
    )


@pytest.mark.parametrize(
    "args",
    [(0.0, 200.0, 100.0, 100.0), (288.0, 0.0, 100.0, 100.0),
     (288.0, 200.0, 0.0, 100.0), (288.0, 200.0, 100.0, -1.0)],
)
def test_fit_rect_degenerate_inputs_fill_container(args) -> None:
    """No image yet / bad size → fill, never divide by zero."""
    ox, oy, w, h = fit_rect(*args)
    assert (ox, oy) == (0.0, 0.0)
    assert (w, h) == (args[0], args[1])

"""Unit tests for exposure-value formatting (Phase 3.15, A8).

The sub-second shutter band (0.55–1.0 s) must render as decimal
seconds like the camera LCD. Before the fix, the 1/3-stop codes at
~0.63 s and ~0.8 s both rounded to "1/2" — and because AppKit's
``addItemWithTitle_`` deletes an existing same-titled item, the
duplicated labels silently removed shutter stops from the dropdown.
"""

from __future__ import annotations

from fp_l_tether.camera.sigma_datagroup import apex_to_shutter

# Sigma 8-bit APEX shutter encoding: 0x38 = 1 s, each +8 halves the
# time, 1/3-stop steps at +3/+5/+8 within each octave.
CODE_1S = 0x38
CODE_0_8S = 0x3B   # 2^(-3/8) ≈ 0.771 s → 0.8"
CODE_0_6S = 0x3D   # 2^(-5/8) ≈ 0.648 s → 0.6"
CODE_HALF = 0x40   # 2^(-1)   = 0.5 s   → 1/2
CODE_1_125 = 0x70  # 2^(-7)   ≈ 1/128 s → 1/125
CODE_2S = 0x30     # 2^(+1)   = 2 s     → 2.0"


def test_sub_second_band_renders_decimal_seconds() -> None:
    assert apex_to_shutter(CODE_0_8S) == '0.8"'
    assert apex_to_shutter(CODE_0_6S) == '0.6"'


def test_half_second_stays_a_fraction() -> None:
    """0.5 s keeps the photographic 1/2 notation (threshold is 0.55)."""
    assert apex_to_shutter(CODE_HALF) == "1/2"


def test_sub_second_stops_are_distinct_labels() -> None:
    """The dropdown-eating duplicate: three codes, three labels (A8)."""
    labels = [apex_to_shutter(v) for v in (CODE_0_8S, CODE_0_6S, CODE_HALF)]
    assert len(set(labels)) == 3, labels


def test_whole_second_and_fraction_formats_unchanged() -> None:
    assert apex_to_shutter(CODE_1S) == '1.0"'
    assert apex_to_shutter(CODE_2S) == '2.0"'
    assert apex_to_shutter(CODE_1_125) == "1/125"


def test_zero_code_renders_dash() -> None:
    assert apex_to_shutter(0x00) == "—"

"""Unit tests for Phase 3.22 formatters — ExpComp + BatteryState.

ExpComp is DG1 bit 0x2000: 8-bit APEX in 1/8-stop units, two's
complement, with Sigma's 1/3-stop ladder using offsets 3/5/8 within
each stop. EXP_COMP_CODES is the static dropdown ladder (+3 → −3 EV)
because CamCanSetInfo5 does not advertise ExpComp codes on fp L V90.

BatteryState (DG1 bit 0x0010) has an undocumented scale; battery_label
must stay conservative — render only plausible bar/percent shapes and
return "" for anything else — until live calibration pins it down.
"""

from __future__ import annotations

from fp_l_tether.camera.sigma_datagroup import (
    EXP_COMP_CODES,
    EXPOSURE_MODE_CODES,
    battery_describe,
    battery_label,
    battery_level_class,
    expcomp_label,
    exposure_mode_label,
)

# ---- expcomp_label ---------------------------------------------------


def test_zero_renders_plus_minus_zero() -> None:
    assert expcomp_label(0x00) == "±0"


def test_positive_third_stops() -> None:
    assert expcomp_label(0x03) == "+0.3"
    assert expcomp_label(0x05) == "+0.7"
    assert expcomp_label(0x08) == "+1.0"
    assert expcomp_label(0x0D) == "+1.7"
    assert expcomp_label(0x18) == "+3.0"


def test_negative_third_stops_two_complement() -> None:
    assert expcomp_label(0xFD) == "−0.3"
    assert expcomp_label(0xF8) == "−1.0"
    assert expcomp_label(0xE8) == "−3.0"


def test_half_stop_code() -> None:
    """Half-stop bodies use offset 4 within the stop."""
    assert expcomp_label(0x04) == "+0.5"


def test_non_canonical_eighth_renders_exact_value() -> None:
    """M-mode metering deviations land on raw 1/8 steps — show them
    honestly instead of snapping to the nearest third."""
    assert expcomp_label(0x01) == "+0.12"
    assert expcomp_label(0xFF) == "−0.12"


# ---- EXP_COMP_CODES ladder ------------------------------------------


def test_ladder_is_plus3_to_minus3_in_thirds() -> None:
    labels = [expcomp_label(c) for c in EXP_COMP_CODES]
    assert labels == [
        "+3.0", "+2.7", "+2.3", "+2.0", "+1.7", "+1.3", "+1.0",
        "+0.7", "+0.3", "±0", "−0.3", "−0.7", "−1.0", "−1.3",
        "−1.7", "−2.0", "−2.3", "−2.7", "−3.0",
    ]


def test_ladder_codes_are_valid_bytes_and_unique() -> None:
    assert len(EXP_COMP_CODES) == 19
    assert len(set(EXP_COMP_CODES)) == 19
    assert all(0x00 <= c <= 0xFF for c in EXP_COMP_CODES)


def test_ladder_stays_inside_body_dial_range() -> None:
    """±3 EV — the fp L dial range. 8-bit two's complement: signed
    magnitude must never exceed 24 (= 3 stops × 8)."""
    for code in EXP_COMP_CODES:
        signed = code - 256 if code > 127 else code
        assert -24 <= signed <= 24, hex(code)


def test_ladder_is_strictly_descending() -> None:
    signed = [c - 256 if c > 127 else c for c in EXP_COMP_CODES]
    assert signed == sorted(signed, reverse=True)


# ---- battery_label (3-segment gauge, matches the body icon) ---------


def test_gauge_renders_three_segments() -> None:
    assert battery_label(1) == "▮▯▯"
    assert battery_label(2) == "▮▮▯"
    assert battery_label(3) == "▮▮▮"


def test_gauge_clamps_high_small_values_to_full() -> None:
    """Harmless under either a 3- or 5-level scale hypothesis."""
    assert battery_label(4) == "▮▮▮"
    assert battery_label(5) == "▮▮▮"


def test_empty_battery_renders_empty_gauge() -> None:
    assert battery_label(0) == "▯▯▯"


def test_percent_scale_renders_percent() -> None:
    assert battery_label(6) == "6%"
    assert battery_label(78) == "78%"
    assert battery_label(100) == "100%"


def test_unknown_values_render_empty_string() -> None:
    """Out-of-range and the -1 'not yet read' sentinel hide the pill."""
    assert battery_label(101) == ""
    assert battery_label(255) == ""
    assert battery_label(-1) == ""


# ---- battery_level_class / battery_describe --------------------------


def test_severity_classes() -> None:
    assert battery_level_class(0) == "critical"
    assert battery_level_class(1) == "low"
    assert battery_level_class(2) == "ok"
    assert battery_level_class(3) == "ok"
    assert battery_level_class(8) == "critical"   # ≤10%
    assert battery_level_class(20) == "low"       # ≤25%
    assert battery_level_class(78) == "ok"
    assert battery_level_class(-1) == "unknown"
    assert battery_level_class(255) == "unknown"


def test_describe_keeps_raw_for_calibration() -> None:
    assert battery_describe(1) == "Battery level 1/3 (camera raw: 1)"
    assert battery_describe(78) == "Battery 78% (camera raw: 78)"
    assert "raw: 255" in battery_describe(255)


# ---- exposure mode (Phase 3.22c) ------------------------------------


def test_exposure_mode_letters() -> None:
    assert exposure_mode_label(1) == "P"
    assert exposure_mode_label(2) == "A"
    assert exposure_mode_label(3) == "S"
    assert exposure_mode_label(4) == "M"


def test_exposure_mode_unknown_renders_dash() -> None:
    assert exposure_mode_label(0) == "—"
    assert exposure_mode_label(9) == "—"


def test_exposure_mode_codes_menu_ladder() -> None:
    """Menu order P→A→S→M, codes 1..4, labels start with the letter."""
    assert [c for _l, c in EXPOSURE_MODE_CODES] == [1, 2, 3, 4]
    for label, code in EXPOSURE_MODE_CODES:
        assert label.startswith(exposure_mode_label(code))

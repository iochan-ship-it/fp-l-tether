"""Phase 3.23 — CamCanSetInfo5 aperture range decode.

Regression cover for the 2026-08-16 field report: the panel's aperture
dropdown started at f/8 with an f/2.8 lens mounted. The camera was
reporting the range correctly; the Av*256 → APEX-byte converter
divided by 16 instead of 32, doubling the exponent and squaring every
f-number (f/2.8–f/22 came out f/8–f/512).
"""

from __future__ import annotations

from fp_l_tether.camera.sigma_datagroup import (
    _av256_to_aperture_byte,
    _expand_range_in_thirds,
    apex_to_aperture,
)

# Straight off a live fp L V90 with a 2.8–22 lens (inspect cansetinfo5,
# 2026-08-16): tag 0x00D2 FValue = SSHORT[3].
LIVE_FVALUE = (768, 2304, 85)


def _codes_from(mn: int, mx: int, step: int) -> list[int]:
    codes = [_av256_to_aperture_byte(v) for v in _expand_range_in_thirds(mn, mx, step)]
    return list(dict.fromkeys(codes))


def test_av256_anchors_match_the_documented_byte_scale() -> None:
    """256 units = 1 Av = half an f-number doubling = 8 bytes."""
    assert _av256_to_aperture_byte(0) == 0x08       # Av 0 → f/1.0
    assert _av256_to_aperture_byte(512) == 0x18     # Av 2 → f/2.0
    assert _av256_to_aperture_byte(1024) == 0x28    # Av 4 → f/4.0
    assert _av256_to_aperture_byte(1536) == 0x38    # Av 6 → f/8.0


def test_live_range_decodes_to_the_lens_the_user_actually_has() -> None:
    codes = _codes_from(*LIVE_FVALUE)
    labels = [apex_to_aperture(c) for c in codes]
    assert labels[0] == "f/2.8"     # wide open — the bug hid this
    assert labels[-1] == "f/22"     # minimum aperture
    assert "f/8" in labels
    assert len(labels) == 19        # 1/3 stops across 6 stops, inclusive


def test_live_range_is_the_canonical_third_stop_series() -> None:
    labels = [apex_to_aperture(c) for c in _codes_from(*LIVE_FVALUE)]
    assert labels == [
        "f/2.8", "f/3.2", "f/3.5", "f/4", "f/4.5", "f/5", "f/5.6",
        "f/6.3", "f/7.1", "f/8", "f/9", "f/10", "f/11", "f/13",
        "f/14", "f/16", "f/18", "f/20", "f/22",
    ]


def test_no_absurd_f_numbers() -> None:
    """The old /16 bug topped the list out at f/512. Nothing above f/45."""
    for code in _codes_from(*LIVE_FVALUE):
        assert 2.0 ** ((code - 0x08) / 16.0) <= 45.0


def test_labels_snap_to_photographic_numbers() -> None:
    """Raw powers of two give f/5.7 and f/23; lens barrels say 5.6 / 22."""
    assert apex_to_aperture(0x30) == "f/5.6"
    assert apex_to_aperture(0x50) == "f/22"
    assert apex_to_aperture(0x20) == "f/2.8"
    assert apex_to_aperture(0x00) == "—"


def test_byte_conversion_is_clamped() -> None:
    assert _av256_to_aperture_byte(-100000) == 0
    assert _av256_to_aperture_byte(100000) == 0xFF

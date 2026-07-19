"""Sigma CamDataGroup parsers — extract human-readable exposure settings.

The Sigma PTP vendor protocol packs camera state into "data groups" using
a TIFF-like *FieldPresent* bitmask: a 16-bit big-endian header decides
which following fields are physically present in the byte stream. Field
ordering inside the stream is fixed, but their offsets depend on which
bits are set.

The DataGroup wire format and FieldPresent bit assignments below were
derived by observing live USB traffic produced by a Sigma fp L (run
``fp-l-tether inspect`` to dump the raw bytes against your own camera)
and were cross-referenced against the public ``sigma-ptpy`` Python
project for naming consistency. We decode the fields we display in
the floating panel (ShutterSpeed, Aperture, ISOSpeed, WhiteBalance,
ImageQuality, Resolution); everything else is skipped over by walking
the same field order, which keeps the parser robust to FieldPresent
variations across firmware versions.

APEX 8-bit encoding (Sigma flavour, 1/3 stop steps):

- ShutterSpeed: T = 2^((0x38 - v)/8)  seconds.  0x38=1s, 0x58=1/15s,
  0x70=1/125s, 0x88=1/1000s, 0xA0=1/8000s.
- Aperture:     N = 2^((v - 0x08)/16). 0x08=f/1.0, 0x18=f/2.0,
  0x28=f/4.0, 0x38=f/8.0, 0x48=f/16.
- ISOSpeed:     ISO = 100 * 2^((v - 0x20)/8). 0x20=100, 0x28=200,
  0x30=400, 0x38=800, 0x40=1600.

For whole + 1/3 stops we round to canonical photography stops.

Future fields (per the user's notes — kept here so the offsets don't
need re-derivation): ExpComp (DG1 bit 0x2000), DriveMode (DG2 bit
0x100), ExposureMode (DG2 bit 0x400).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fp_l_tether.camera.sigma_ifd import parse_ifd

if TYPE_CHECKING:
    from fp_l_tether.camera.usb_bridge import USBBridge


# ---------------------------------------------------------------------------
# DG1 / DG2 FieldPresent bitmasks
#
# The bit/name/width tuples below describe the wire layout we observed
# by interacting with a Sigma fp L (toggling each setting on the camera
# body and re-reading the DataGroup over USB). The naming follows the
# convention established by the sigma-ptpy project for cross-project
# readability.
# ---------------------------------------------------------------------------

# DG1 — exposure-related state. Bit value → (struct field name, byte width).
# Order matches the struct's declaration order; FieldPresent only tells us
# *whether* each entry is in the stream, not the order.
_DG1_FIELDS: tuple[tuple[int, str, int], ...] = (
    (0x0100, "ShutterSpeed", 1),
    (0x0200, "Aperture", 1),
    (0x0400, "ProgramShift", 1),
    (0x0800, "ISOAuto", 1),
    (0x1000, "ISOSpeed", 1),
    (0x2000, "ExpComp", 1),
    (0x4000, "ABValue", 1),
    (0x8000, "ABSetting", 1),
    (0x0001, "FrameBufferState", 1),
    (0x0002, "MediaFreeSpace", 2),
    (0x0004, "MediaStatus", 1),
    (0x0008, "CurrentLensFocalLength", 2),
    (0x0010, "BatteryState", 1),
    (0x0020, "ABShotRemainNumber", 1),
    (0x0040, "ExpCompExcludeAB", 1),
    (0x0080, "_Reserved0", 1),
)

# DG2 — drive / WB / flash state.
_DG2_FIELDS: tuple[tuple[int, str, int], ...] = (
    (0x0100, "DriveMode", 1),
    (0x0200, "SpecialMode", 1),
    (0x0400, "ExposureMode", 1),
    (0x0800, "AEMeteringMode", 1),
    (0x1000, "_Reserved0", 1),
    (0x2000, "_Reserved1", 1),
    (0x4000, "_Reserved2", 1),
    (0x8000, "_Reserved3", 1),
    (0x0001, "FlashType", 1),
    (0x0002, "_Reserved4", 1),
    (0x0004, "FlashMode", 1),
    (0x0008, "FlashSetting", 1),
    (0x0010, "_Reserved5", 1),
    (0x0020, "WhiteBalance", 1),
    (0x0040, "Resolution", 1),
    (0x0080, "ImageQuality", 1),
)


# ---------------------------------------------------------------------------
# WhiteBalance enum — labels follow sigma-ptpy's naming convention for
# cross-project compatibility; the underlying byte values were verified
# against a Sigma fp L.
# ---------------------------------------------------------------------------

_WB_LABELS: dict[int, str] = {
    0x0: "Null",
    0x1: "Auto",
    0x2: "Sunlight",
    0x3: "Shade",
    0x4: "Overcast",
    0x5: "Incandescent",
    0x6: "Fluorescent",
    0x7: "Flash",
    0x8: "Custom1",
    0x9: "CustomCapt1",
    0xA: "Custom2",
    0xB: "CustomCapt2",
    0xC: "Custom3",
    0xD: "CustomCapt3",
    0xE: "ColorTemp",
    0xF: "LightSource",
}


def wb_label(value: int) -> str:
    return _WB_LABELS.get(value, f"0x{value:02X}")


# ---------------------------------------------------------------------------
# ImageQuality + Resolution enums (DG2.ImageQuality / DG2.Resolution)
# ---------------------------------------------------------------------------
#
# Per sigma_ptpy/enum.py the fp series uses an IntFlag-style encoding for
# ImageQuality where DNG+JPG is literally ``DNG | JPEGFine`` (0x10 | 0x02 =
# 0x12). Resolution is a small enum L/M/S that only applies to JPG modes
# — the fp L returns 0xff for Resolution while in DNG-only, see
# project_image_quality_resolution_mapping.md memory.
#
# Values here are TENTATIVE for fp L V90: they match the well-documented
# sigma_ptpy schema but have not yet been confirmed by a live diff
# session that rotates the camera's File Format menu and re-inspects
# DG2. If a future inspect-diff reveals a different mapping the labels
# below need adjusting (UI logic does NOT — it uses *_raw codes from
# CamCanSetInfo5 directly, so the dropdown will still be correct).

# DG2.ImageQuality enum (byte value → label, low-to-high quality order)
_IMAGE_QUALITY_LABELS: dict[int, str] = {
    0x02: "JPG Fine",
    0x04: "JPG Normal",
    0x08: "JPG Basic",
    0x10: "DNG",
    0x12: "DNG+JPG",
}

# DG2.Resolution enum (byte value → L/M/S label)
_RESOLUTION_LABELS: dict[int, str] = {
    0x01: "L",
    0x02: "M",
    0x04: "S",
    0xFF: "—",  # fp L reports this in DNG-only states; Resolution is N/A
}


def image_quality_label(value: int) -> str:
    return _IMAGE_QUALITY_LABELS.get(value, f"0x{value:02X}")


def resolution_label(value: int) -> str:
    return _RESOLUTION_LABELS.get(value, f"0x{value:02X}")


# ---------------------------------------------------------------------------
# Generic FieldPresent-aware decoder
# ---------------------------------------------------------------------------


def _parse_datagroup(
    buf: bytes,
    fields: tuple[tuple[int, str, int], ...],
) -> dict[str, int]:
    """Walk a Sigma DataGroup payload using its FieldPresent header.

    Layout::

        [_Header u8][FieldPresent u16be][...conditional fields...][_Parity u8]

    Returns a dict {field_name: int_value}. Missing fields (their FP bit
    cleared) are simply absent from the result.
    """
    if len(buf) < 4:
        raise ValueError(f"datagroup buffer too small: {len(buf)} bytes")
    # byte 0 is _Header; bytes 1..2 are FieldPresent (Int16ub).
    fp = (buf[1] << 8) | buf[2]
    off = 3
    out: dict[str, int] = {"_FieldPresent": fp}
    for bit, name, width in fields:
        if not (fp & bit):
            continue
        end = off + width
        if end > len(buf) - 1:  # leave room for _Parity
            raise ValueError(
                f"datagroup truncated at field {name} (bit 0x{bit:04X}); "
                f"need offset {end}, buf has {len(buf)}"
            )
        # All multibyte numeric fields in DG1/DG2 are Int16ul (little-endian)
        # per sigma_ptpy schema. Single-byte fields are just u8.
        if width == 1:
            out[name] = buf[off]
        else:
            out[name] = int.from_bytes(buf[off:end], "little")
        off = end
    return out


def parse_datagroup1(buf: bytes) -> dict[str, int]:
    """Decode a Sigma CamDataGroup1 payload."""
    return _parse_datagroup(buf, _DG1_FIELDS)


def parse_datagroup2(buf: bytes) -> dict[str, int]:
    """Decode a Sigma CamDataGroup2 payload."""
    return _parse_datagroup(buf, _DG2_FIELDS)


# ---------------------------------------------------------------------------
# APEX → human-readable helpers
# ---------------------------------------------------------------------------


def apex_to_shutter(v: int) -> str:
    """Format Sigma APEX 8-bit shutter speed as ``1/125`` or ``2.0"``.

    Encoding: 0x38 = 1 second; each +8 halves time, each -8 doubles it,
    in 1/3-stop increments (each +1 ≈ 2^(1/8)).
    """
    if v == 0x00:
        return "—"
    # Seconds = 2^((0x38 - v) / 8). 1/3-stop steps are math-exact but the
    # camera LCD shows canonical photography values (15s not 16s, 30s not
    # 32s, 1/15 not 1/16), so we round to that table.
    seconds = 2.0 ** ((0x38 - v) / 8.0)
    # Phase 3.15 (A8): the 0.55–1.0 s band renders as decimal seconds
    # (0.6" / 0.8") like the camera LCD. Previously these fell into the
    # fraction branch and all rounded to "1/2" — and because AppKit's
    # ``addItemWithTitle_`` silently REMOVES an existing same-titled
    # item, the duplicated labels made shutter stops vanish from the
    # dropdown. Threshold 0.55 keeps the familiar 1/2 (=0.5 s) as a
    # fraction while catching the 1/3-stop codes at ~0.63 s and 0.8 s.
    if seconds >= 0.55:
        canonical_s = (
            0.6, 0.8, 1.0, 1.3, 1.6, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0,
            10.0, 13.0, 15.0, 20.0, 25.0, 30.0,
        )
        nearest = min(canonical_s, key=lambda c: abs(c - seconds))
        if nearest >= 10:
            return f'{nearest:.0f}"'
        return f'{nearest:.1f}"'
    # Sub-second: render as 1/N rounded to canonical photography stops.
    denom = 1.0 / seconds
    canonical = (
        2, 3, 4, 5, 6, 8, 10, 13, 15, 20, 25, 30, 40, 50, 60, 80, 100,
        125, 160, 200, 250, 320, 400, 500, 640, 800, 1000, 1250, 1600,
        2000, 2500, 3200, 4000, 5000, 6400, 8000,
    )
    nearest = min(canonical, key=lambda c: abs(c - denom))
    return f"1/{nearest}"


def apex_to_aperture(v: int) -> str:
    """Format Sigma APEX 8-bit aperture as ``f/2.8``.

    Encoding: 0x08 = f/1.0; each +16 doubles the f-number (one full stop),
    1/3-stop increments otherwise.
    """
    if v == 0x00:
        return "—"
    f_number = 2.0 ** ((v - 0x08) / 16.0)
    if f_number < 10:
        return f"f/{f_number:.1f}"
    return f"f/{f_number:.0f}"


def apex_to_iso(v: int) -> str:
    """Format Sigma APEX 8-bit ISO as ``ISO 100``.

    Encoding: 0x20 = ISO 100; each +8 doubles, 1/3-stop increments
    otherwise. Result rounded to canonical photography stops.
    """
    if v == 0x00:
        return "—"
    raw = 100.0 * (2.0 ** ((v - 0x20) / 8.0))
    canonical = (
        6, 12, 25, 50, 100, 125, 160, 200, 250, 320, 400, 500, 640,
        800, 1000, 1250, 1600, 2000, 2500, 3200, 4000, 5000, 6400,
        8000, 10000, 12800, 25600, 51200, 102400,
    )
    nearest = min(canonical, key=lambda c: abs(c - raw))
    return f"ISO {nearest}"


# ---------------------------------------------------------------------------
# Public surface — high-level exposure read
# ---------------------------------------------------------------------------


@dataclass
class ExposureSettings:
    """Snapshot of the four core exposure dials."""

    iso: str
    shutter: str
    aperture: str
    wb: str
    # Raw bytes so callers can re-format / log if needed.
    iso_raw: int
    shutter_raw: int
    aperture_raw: int
    wb_raw: int
    # ISOAuto: 0 = manual, 1 = auto. Lets the UI show "Auto" in the
    # ISO dropdown even though iso_raw still carries whatever value
    # the camera's auto algorithm picked.
    iso_auto_raw: int = 0
    # Picture file format (DG2.ImageQuality + Resolution). Defaults to
    # "—" so the panel renders cleanly on the very first frame, before
    # the daemon's initial DG2 read returns. ``file_format`` is the
    # human label (e.g. "JPG Fine", "DNG", "DNG+JPG"); ``file_format_raw``
    # is the DG2 byte the camera reports; ``image_size`` is the L/M/S
    # selector that applies to JPG modes (DNG is always full-res, so
    # Resolution is reported as 0xff by the fp L in DNG-only states —
    # see project_image_quality_resolution_mapping.md memory).
    file_format: str = "—"
    file_format_raw: int = 0
    image_size: str = "—"
    image_size_raw: int = 0
    # Phase 3.22: exposure compensation (DG1.ExpComp, 8-bit APEX in
    # 1/8-stop units, two's complement). In M mode the camera reports
    # the metered deviation from AE-correct instead of a user dial.
    exp_comp: str = "±0"
    exp_comp_raw: int = 0
    # Phase 3.22: DG1.BatteryState raw byte. Scale is undocumented in
    # public sources — the UI renders conservatively (see
    # battery_label) and logs the raw value for calibration. -1 = not
    # yet read.
    battery_raw: int = -1
    # Phase 3.22c: DG2.ExposureMode (1=P 2=A 3=S 4=M). Surfaced so the
    # panel can show/set the mode — the body's own mode selection gets
    # clobbered by the PC-mode template + cache replay on connect.
    exposure_mode_raw: int = 0

    def short(self) -> str:
        """One-line label, e.g. ``ISO 100 · 1/125 · f/2.8 · WB Auto``."""
        return f"{self.iso} · {self.shutter} · {self.aperture} · WB {self.wb}"


# ---------------------------------------------------------------------------
# Set DG1 / DG2 — build payloads for SetCamDataGroup1 / SetCamDataGroup2
# ---------------------------------------------------------------------------
#
# Wire shape (mirrors the libgphoto2 sigma-fp.txt init traces — verified
# to work on fp L)::
#
#     [_Header u8 = 0x03][FieldPresent u16be][...fields in struct order...]
#     [_Parity u8 = sum_mod_256(everything above)]
#
# - _Header = 0x03 (fp-trace constant; meaning unknown but required).
# - FieldPresent is Int16ub matching the *Get* response (decoded into
#   sigma-ptpy's bitmask names). Bits set ⇒ that field's value follows.
# - Fields are written in *_DG{1,2}_FIELDS* declaration order, skipping
#   any whose bit is 0. Width per field comes from the same table.
# - Trailing parity is sum & 0xFF over the preceding bytes; the bridge
#   helper ``sigma_send_raw_setdatagroup`` appends it for us.


def _build_set_datagroup(
    field_bits: int,
    values: dict[str, int],
    fields: tuple[tuple[int, str, int], ...],
) -> bytes:
    """Build a Sigma SetCamDataGroup1/2 payload (without trailing parity).

    ``values`` keys must cover every name whose bit is set in
    ``field_bits``. Multibyte fields are little-endian.
    """
    out = bytearray()
    out.append(0x03)  # _Header — fp-trace constant
    # FieldPresent is Int16ub (big-endian)
    out.append((field_bits >> 8) & 0xFF)
    out.append(field_bits & 0xFF)
    for bit, name, width in fields:
        if not (field_bits & bit):
            continue
        if name not in values:
            raise ValueError(f"value for field {name!r} missing")
        v = values[name]
        out += int(v).to_bytes(width, "little")
    return bytes(out)


def build_set_datagroup1(values: dict[str, int]) -> bytes:
    """Build a SetCamDataGroup1 payload from {field_name: int} pairs.

    Only the supplied fields are sent — the FieldPresent bitmask is
    derived from the dict's keys, so the camera knows exactly which dials
    are being written.
    """
    bits = 0
    for bit, name, _w in _DG1_FIELDS:
        if name in values:
            bits |= bit
    return _build_set_datagroup(bits, values, _DG1_FIELDS)


def build_set_datagroup2(values: dict[str, int]) -> bytes:
    """Build a SetCamDataGroup2 payload from {field_name: int} pairs."""
    bits = 0
    for bit, name, _w in _DG2_FIELDS:
        if name in values:
            bits |= bit
    return _build_set_datagroup(bits, values, _DG2_FIELDS)


# ---------------------------------------------------------------------------
# CamCanSetInfo5 — list of allowed values for each dial
# ---------------------------------------------------------------------------
#
# Tag mapping (from sigma-ptpy schema.py CamCanSetInfo5.decode())::
#
#     0x00D2 FValue            — aperture range (lens-dependent, often empty)
#     0x00D3 TValue            — alt shutter range (often empty on fp L)
#     0x00D4 ShutterSpeed      — [min*256, max*256, step_third*256]
#     0x00D7 ISOManual         — [min*256, max*256, step_full*256, step_third*256]
#     0x00D8 ISOAuto           — same shape as ISOManual
#     0x012D WhiteBalance      — list of WB enum values
#     0x0265 AF point bounds   — [Y_min, Y_max, X_min, X_max]  (used by AF popover)
#
# Note: sigma-ptpy encodes APEX values as ``Sv * 256`` (1 stop = 256
# units); our DG1 bytes encode the same values as ``8 per stop`` with a
# baseline of 0x20 for ISO 100 / 0x38 for 1s. We do the conversion here
# so callers can compare directly against the bytes they'll send back.
#
# Aperture is often missing from CamCanSetInfo5 because it's lens-driven.
# We fall back to a synthesised list (f/1.0..f/22 in 1/3 stops) when the
# camera doesn't report one.


@dataclass
class CanSetInfo:
    """What the camera says the user is allowed to set right now."""

    shutter_codes: list[int] = field(default_factory=list)
    aperture_codes: list[int] = field(default_factory=list)
    iso_manual_codes: list[int] = field(default_factory=list)
    iso_auto_codes: list[int] = field(default_factory=list)
    wb_codes: list[int] = field(default_factory=list)
    # ImageQuality / Resolution allowed bytes. Default to the full
    # standard sigma_ptpy enum so the UI dropdown is populated even
    # if the camera's CamCanSetInfo5 entries for these tags come back
    # empty (observed on fp L V90 at the time of writing — see
    # project_image_quality_resolution_mapping.md). Once a live
    # diff confirms the tag IDs, the parser below will override.
    image_quality_codes: list[int] = field(
        default_factory=lambda: [0x02, 0x04, 0x08, 0x10, 0x12]
    )
    resolution_codes: list[int] = field(
        default_factory=lambda: [0x01, 0x02, 0x04]
    )
    af_x_min: int = 96
    af_x_max: int = 928
    af_y_min: int = 85
    af_y_max: int = 597


def _expand_range_in_thirds(min_sv: int, max_sv: int, step_sv: int) -> list[int]:
    """Walk an APEX range in 1/3-stop steps and emit sigma 'Sv*256' values."""
    if step_sv <= 0:
        return []
    out: list[int] = []
    v = min_sv
    while v <= max_sv:
        out.append(v)
        v += step_sv
    return out


def _sv256_to_iso_byte(sv256: int) -> int:
    """Convert sigma-ptpy Sv*256 to our DG1 APEX byte (8 per stop, ISO 100=0x20).

    Sv=5 ⇒ ISO 100 ⇒ byte 0x20. byte = sv256/32 - 8.
    """
    return max(0, min(0xFF, round(sv256 / 32 - 8)))


def _tv256_to_shutter_byte(tv256: int) -> int:
    """Convert sigma-ptpy Tv*256 to our DG1 APEX byte (8 per stop, 1s=0x38).

    Tv=0 ⇒ 1s ⇒ byte 0x38. byte = 0x38 + tv256/32.
    """
    return max(0, min(0xFF, round(0x38 + tv256 / 32)))


def _default_aperture_codes() -> list[int]:
    """Synthesised aperture list when the lens/camera doesn't report one.

    f/1.0 to f/22 in 1/3-stop steps. Maps via apex_to_aperture's inverse:
    byte = 0x08 + 16 * log2(f).  We emit every step from f/1.0 (0x08)
    through f/22 (0x50) — that's the full common photography range.
    """
    return list(range(0x08, 0x51, 2))  # step 2 = 1/3 stop


def parse_can_set_info5(buf: bytes) -> CanSetInfo:
    """Parse the IFD blob returned by 0x9030 GetCamCanSetInfo5."""
    info = CanSetInfo()
    ifd = parse_ifd(buf)

    # --- AF point bounds (tag 0x0265 = SHORT[4] = [Y_min, Y_max, X_min, X_max])
    af = ifd.by_tag(0x0265)
    if af is not None and isinstance(af.value, list) and len(af.value) == 4:
        info.af_y_min, info.af_y_max, info.af_x_min, info.af_x_max = af.value

    # --- ShutterSpeed (tag 0x00D4 = SSHORT[3] = [min*256, max*256, step*256])
    ss = ifd.by_tag(0x00D4)
    if ss is not None and isinstance(ss.value, list) and len(ss.value) >= 3:
        mn, mx, step = ss.value[0], ss.value[1], ss.value[2]
        info.shutter_codes = [
            _tv256_to_shutter_byte(v) for v in _expand_range_in_thirds(mn, mx, step)
        ]
        # Deduplicate while preserving order
        info.shutter_codes = list(dict.fromkeys(info.shutter_codes))

    # --- ISO (manual + auto, each [min*256, max*256, step_full, step_third])
    for tag, attr in ((0x00D7, "iso_manual_codes"), (0x00D8, "iso_auto_codes")):
        entry = ifd.by_tag(tag)
        if entry is None or not isinstance(entry.value, list) or len(entry.value) < 3:
            continue
        mn, mx = entry.value[0], entry.value[1]
        # Prefer the 1/3-stop step (last element when count==4) for finer UI.
        step = entry.value[-1] if len(entry.value) >= 4 else entry.value[2]
        codes = [_sv256_to_iso_byte(v) for v in _expand_range_in_thirds(mn, mx, step)]
        setattr(info, attr, list(dict.fromkeys(codes)))

    # --- Aperture (tag 0x00D2 FValue; usually empty → synthesise)
    av = ifd.by_tag(0x00D2)
    if av is not None and isinstance(av.value, list) and len(av.value) >= 3:
        mn, mx = av.value[0], av.value[1]
        step = av.value[-1]
        # Aperture: Av step is the same 256-per-stop scale; our byte uses
        # 16 per stop with baseline 0x08 = f/1.0.
        codes = [
            max(0, min(0xFF, round(0x08 + v / 16)))
            for v in _expand_range_in_thirds(mn, mx, step)
        ]
        info.aperture_codes = list(dict.fromkeys(codes))
    if not info.aperture_codes:
        info.aperture_codes = _default_aperture_codes()

    # --- WhiteBalance (tag 0x012D BYTE[N], each entry an enum value)
    wb = ifd.by_tag(0x012D)
    if wb is not None:
        if isinstance(wb.value, list):
            info.wb_codes = list(wb.value)
        elif isinstance(wb.value, int):
            info.wb_codes = [wb.value]

    # --- ImageQuality / Resolution allowed values.
    # Tag IDs are TENTATIVE for fp L (sigma_ptpy schema documents them
    # but the only fp L baseline we have shows empty lists at 0x012F /
    # tag-15-bytes-of-7-down-to-1 at 0x0015 — clearly fp L specific).
    # For now we only override the dataclass defaults if the camera
    # returns a non-empty list whose values look like valid
    # ImageQuality / Resolution bytes. Otherwise the defaults (full
    # standard sigma_ptpy enum) survive, so the dropdown stays usable.
    iq = ifd.by_tag(0x012F)  # candidate ImageQuality allowed-list
    if (iq is not None
            and isinstance(iq.value, list)
            and iq.value
            and all(v in _IMAGE_QUALITY_LABELS for v in iq.value)):
        info.image_quality_codes = list(iq.value)
    res = ifd.by_tag(0x0014)  # candidate Resolution allowed-list
    if (res is not None
            and isinstance(res.value, list)
            and res.value
            and all(v in _RESOLUTION_LABELS for v in res.value)):
        info.resolution_codes = list(res.value)

    return info


def read_can_set_info(bridge: "USBBridge") -> CanSetInfo:
    """Pull and parse CamCanSetInfo5 from the camera."""
    return parse_can_set_info5(bridge.sigma_get_cam_can_set_info_5())


# ---------------------------------------------------------------------------
# Get focus point (current AF position) for the popover's blue dot
# ---------------------------------------------------------------------------


def read_focus_point(bridge: "USBBridge") -> tuple[int, int] | None:
    """Return ``(X, Y)`` of the current AF point, or None if not parseable.

    DataGroupFocus is a TIFF IFD; tag 0x000D (DMFPos) stores 4 bytes
    in (Y_lo, Y_hi, X_lo, X_hi) order — same byte layout we *write* via
    SetCamDataGroupFocus. Note: per project memory the camera caches this
    statically, so the value here is "last commanded" not "currently
    actively rendered". Good enough as a starting point for the popover
    dot.
    """
    try:
        ifd = parse_ifd(bridge.sigma_get_cam_datagroup_focus())
    except Exception:  # noqa: BLE001
        return None
    entry = ifd.by_tag(0x000D)
    if entry is None:
        return None
    raw = entry.raw_value_bytes
    if len(raw) < 4:
        return None
    y = int.from_bytes(raw[0:2], "little")
    x = int.from_bytes(raw[2:4], "little")
    return x, y


def expcomp_label(v: int) -> str:
    """Format DG1.ExpComp (8-bit APEX, 1/8-stop, two's complement).

    0x00 → "±0", +8 → "+1.0", 0xF8 (−8) → "−1.0". Sigma's 1/3-stop
    ladder uses offsets 3/5/8 within each stop (0.3 / 0.7 / 1.0);
    half-stop bodies use 4 (0.5).
    """
    signed = v - 256 if v > 127 else v
    if signed == 0:
        return "±0"
    sign = "+" if signed > 0 else "−"
    n = abs(signed)
    whole, frac_units = divmod(n, 8)
    frac = {0: 0.0, 3: 0.3, 4: 0.5, 5: 0.7}.get(frac_units)
    if frac is None:
        # Non-canonical step — show the exact eighth-stop value.
        return f"{sign}{n / 8.0:.2f}"
    return f"{sign}{whole + frac:.1f}"


# Standard fp L exposure-compensation ladder: +3 EV → −3 EV in 1/3 steps
# (dropdown top-to-bottom, matching the body's own dial range).
# CamCanSetInfo5 doesn't advertise ExpComp codes, so this list is static.
# Codes are APEX 1/8-stop two's-complement: +1/3 = 0x03, +2/3 = 0x05,
# +1.0 = 0x08, −1/3 = 0xFD, −1.0 = 0xF8, etc.
EXP_COMP_CODES: list[int] = [
    0x18,  # +3.0
    0x15,  # +2.7
    0x13,  # +2.3
    0x10,  # +2.0
    0x0D,  # +1.7
    0x0B,  # +1.3
    0x08,  # +1.0
    0x05,  # +0.7
    0x03,  # +0.3
    0x00,  # ±0
    0xFD,  # −0.3
    0xFB,  # −0.7
    0xF8,  # −1.0
    0xF5,  # −1.3
    0xF3,  # −1.7
    0xF0,  # −2.0
    0xED,  # −2.3
    0xEB,  # −2.7
    0xE8,  # −3.0
]


def exposure_mode_label(v: int) -> str:
    """DG2.ExposureMode → dial letter (Phase 3.22c).

    sigma-ptpy enum: 1 = Program, 2 = Aperture priority, 3 = Shutter
    priority, 4 = Manual. 0 / unknown render as an em-dash so the UI
    can show "not yet read" without inventing a mode.
    """
    return {1: "P", 2: "A", 3: "S", 4: "M"}.get(v, "—")


# DG2.ExposureMode codes for the in-app mode selector, in dial order.
# The fp L firmware replays the PC-mode template (and our settings
# cache) on every USB connect, so a mode set on the body while the
# app is down gets clobbered — the tether-native way to pick a mode
# is from the app, which also keeps the cache in sync.
EXPOSURE_MODE_CODES: tuple[tuple[str, int], ...] = (
    ("P — Program", 1),
    ("A — Aperture priority", 2),
    ("S — Shutter priority", 3),
    ("M — Manual", 4),
)


def battery_label(raw: int) -> str:
    """Render DG1.BatteryState as a 3-segment gauge (Phase 3.22b).

    The public scale is undocumented, but the fp L body's own battery
    indicator is a 3-segment icon, and the DG1 byte most plausibly
    mirrors it (observed live: raw=1). Small values render as x/3
    segments (4-5 clamp to full — harmless under either a 3- or
    5-level hypothesis); 6-100 renders as a percentage in case some
    firmware reports one; anything else renders empty rather than
    lying. Field-calibration ongoing — see battery_describe.
    """
    if raw == 0:
        return "▯▯▯"
    if 1 <= raw <= 5:
        seg = min(raw, 3)
        return "▮" * seg + "▯" * (3 - seg)
    if 6 <= raw <= 100:
        return f"{raw}%"
    return ""


def battery_level_class(raw: int) -> str:
    """Coarse severity for UI colouring: ok / low / critical / unknown.

    0 (empty gauge) and single-digit percentages are critical; the
    bottom segment / ≤25% is low; anything healthy is ok. Unknown
    values stay unknown so the UI can hide rather than guess.
    """
    if raw == 0:
        return "critical"
    if raw == 1:
        return "low"
    if 2 <= raw <= 5:
        return "ok"
    if 6 <= raw <= 100:
        if raw <= 10:
            return "critical"
        if raw <= 25:
            return "low"
        return "ok"
    return "unknown"


def battery_describe(raw: int) -> str:
    """Human-readable battery tooltip, raw value included.

    The raw byte stays visible (in parentheses) because the scale is
    still being field-calibrated against the body indicator — but the
    headline is now a plain reading, not a bare number.
    """
    if 0 <= raw <= 5:
        return f"Battery level {min(raw, 3)}/3 (camera raw: {raw})"
    if 6 <= raw <= 100:
        return f"Battery {raw}% (camera raw: {raw})"
    return f"Battery unknown (camera raw: {raw})"


def read_exposure(bridge: "USBBridge") -> ExposureSettings:
    """Pull DG1 + DG2 from the camera and decode the displayable fields.

    Cheap: each Get is a small read (≤22 bytes). Safe to call after every
    shot to refresh the floating panel.
    """
    dg1 = parse_datagroup1(bridge.sigma_get_datagroup(1))
    dg2 = parse_datagroup2(bridge.sigma_get_datagroup(2))

    iso_raw = dg1.get("ISOSpeed", 0)
    iso_auto = dg1.get("ISOAuto", 0)
    ss_raw = dg1.get("ShutterSpeed", 0)
    av_raw = dg1.get("Aperture", 0)
    wb_raw = dg2.get("WhiteBalance", 0)
    quality_raw = dg2.get("ImageQuality", 0)
    resolution_raw = dg2.get("Resolution", 0)
    exp_comp_raw = dg1.get("ExpComp", 0)
    battery_raw = dg1.get("BatteryState", -1)
    exposure_mode_raw = dg2.get("ExposureMode", 0)

    return ExposureSettings(
        iso=("Auto" if iso_auto else apex_to_iso(iso_raw)),
        shutter=apex_to_shutter(ss_raw),
        aperture=apex_to_aperture(av_raw),
        wb=wb_label(wb_raw),
        iso_raw=iso_raw,
        shutter_raw=ss_raw,
        aperture_raw=av_raw,
        wb_raw=wb_raw,
        iso_auto_raw=iso_auto,
        file_format=image_quality_label(quality_raw),
        file_format_raw=quality_raw,
        image_size=resolution_label(resolution_raw),
        image_size_raw=resolution_raw,
        exp_comp=expcomp_label(exp_comp_raw),
        exp_comp_raw=exp_comp_raw,
        battery_raw=battery_raw,
        exposure_mode_raw=exposure_mode_raw,
    )

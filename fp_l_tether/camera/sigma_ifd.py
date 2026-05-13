"""Sigma DataGroup IFD (Image File Directory) parser.

Several of Sigma's PTP responses (the various ``GetCamDataGroup*`` opcodes)
encode their payload as a TIFF-style Image File Directory: a list of
``(tag, type, count, value_or_offset)`` entries followed by an out-of-band
data area for values that don't fit inline.

This is the same format EXIF uses internally, just with Sigma-specific tag
numbers (whose meaning is NOT documented and must be reverse-engineered
empirically — that's the entire point of the ``fp-l-tether inspect``
command that this module powers).

Wire layout::

    bytes 0..3   : uint32 LE — total payload length (excluding final checksum)
    bytes 4..7   : uint32 LE — number of IFD entries
    bytes 8..    : N × 12-byte entries:
                     bytes 0..1 : uint16 LE  tag
                     bytes 2..3 : uint16 LE  type (TIFF types 1..12)
                     bytes 4..7 : uint32 LE  count
                     bytes 8..11: uint32 LE  value (inline) OR offset into payload
    bytes ...    : trailing data area (referenced by offset)
    last byte    : sum checksum of the bytes above (mod 256)

When ``count * type_size <= 4`` the value is stored inline in the entry's
last 4 bytes. Otherwise those 4 bytes are an *offset from the start of the
payload* into the trailing data area where the actual value bytes live.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TIFF type constants
# ---------------------------------------------------------------------------

# (size_per_element_bytes, friendly_name)
TIFF_TYPES: dict[int, tuple[int, str]] = {
    0x0001: (1, "BYTE"),        # uint8
    0x0002: (1, "ASCII"),       # NUL-terminated string
    0x0003: (2, "SHORT"),       # uint16
    0x0004: (4, "LONG"),        # uint32
    0x0005: (8, "RATIONAL"),    # 2 × uint32 (numerator, denominator)
    0x0006: (1, "SBYTE"),       # int8
    0x0007: (1, "UNDEFINED"),   # raw bytes
    0x0008: (2, "SSHORT"),      # int16
    0x0009: (4, "SLONG"),       # int32
    0x000A: (8, "SRATIONAL"),   # 2 × int32
    0x000B: (4, "FLOAT"),       # IEEE-754 single
    0x000C: (8, "DOUBLE"),      # IEEE-754 double
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class IFDEntry:
    """One parsed IFD entry."""

    tag: int
    type_id: int
    count: int
    value: Any                # decoded value (int, list, bytes, …)
    raw_value_bytes: bytes    # raw inline-or-offset 4 bytes from the entry
    type_name: str = ""

    def __post_init__(self) -> None:
        if not self.type_name:
            self.type_name = TIFF_TYPES.get(self.type_id, (0, f"unknown({self.type_id})"))[1]

    def format(self) -> str:
        """One-line human-readable representation."""
        if isinstance(self.value, bytes):
            v = self.value.hex(" ")
            if len(v) > 60:
                v = v[:60] + "…"
        elif isinstance(self.value, list):
            v = ", ".join(str(x) for x in self.value[:8])
            if len(self.value) > 8:
                v += f", … (+{len(self.value)-8})"
        else:
            v = str(self.value)
        return (
            f"tag=0x{self.tag:04X}  "
            f"type={self.type_name:9s} count={self.count:>3d}  "
            f"value={v}"
        )


@dataclass
class IFDParseResult:
    """Result of parsing a full DataGroup IFD blob."""

    declared_length: int
    entry_count: int
    entries: list[IFDEntry] = field(default_factory=list)
    trailer_bytes: bytes = b""    # any unparsed data after entries
    checksum: int | None = None
    checksum_ok: bool | None = None

    def by_tag(self, tag: int) -> IFDEntry | None:
        for e in self.entries:
            if e.tag == tag:
                return e
        return None

    def format(self, *, indent: str = "    ") -> str:
        """Multi-line human-readable dump."""
        lines = [
            f"IFD: declared_length={self.declared_length}, entries={self.entry_count}",
        ]
        for e in self.entries:
            lines.append(indent + e.format())
        if self.checksum is not None:
            ok = "✓" if self.checksum_ok else "✗"
            lines.append(f"{indent}checksum=0x{self.checksum:02X} {ok}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def parse_ifd(data: bytes) -> IFDParseResult:
    """Parse a Sigma DataGroup IFD blob.

    Tolerant: never raises on malformed entries — just logs and skips.
    """
    if len(data) < 8:
        raise ValueError(f"IFD payload too short: {len(data)} bytes")

    declared_length = int.from_bytes(data[0:4], "little")
    entry_count = int.from_bytes(data[4:8], "little")

    result = IFDParseResult(
        declared_length=declared_length,
        entry_count=entry_count,
    )

    # The IFD entries start at offset 8
    entries_end = 8 + entry_count * 12
    if entries_end > len(data):
        logger.warning(
            "IFD claims %d entries but only %d bytes available — truncated",
            entry_count, len(data),
        )
        entries_end = len(data)

    for i in range(entry_count):
        off = 8 + i * 12
        if off + 12 > len(data):
            break
        tag = int.from_bytes(data[off : off + 2], "little")
        type_id = int.from_bytes(data[off + 2 : off + 4], "little")
        count = int.from_bytes(data[off + 4 : off + 8], "little")
        raw_value_bytes = data[off + 8 : off + 12]

        # Decode the value
        value: Any
        try:
            value = _decode_entry_value(data, type_id, count, raw_value_bytes)
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "IFD entry %d (tag 0x%04X) decode failed: %s — using raw bytes",
                i, tag, e,
            )
            value = raw_value_bytes

        result.entries.append(
            IFDEntry(
                tag=tag,
                type_id=type_id,
                count=count,
                value=value,
                raw_value_bytes=raw_value_bytes,
            )
        )

    # Trailer
    if entries_end < len(data):
        # Last byte may be the Sigma external checksum
        result.trailer_bytes = data[entries_end:-1]
        result.checksum = data[-1]
        expected = sum(data[:-1]) & 0xFF
        result.checksum_ok = (result.checksum == expected)

    return result


def _decode_entry_value(
    full_buf: bytes,
    type_id: int,
    count: int,
    raw_value_bytes: bytes,
) -> Any:
    """Decode an IFD value, following the offset if it doesn't fit inline."""
    size_info = TIFF_TYPES.get(type_id)
    if size_info is None:
        # Unknown type — return raw 4 bytes
        return raw_value_bytes

    elem_size, _ = size_info
    total = count * elem_size

    if total <= 4:
        value_bytes = raw_value_bytes[:total]
    else:
        offset = int.from_bytes(raw_value_bytes, "little")
        if offset + total > len(full_buf):
            return raw_value_bytes  # would overflow — fall back to raw
        value_bytes = full_buf[offset : offset + total]

    return _bytes_to_value(type_id, count, value_bytes)


def _bytes_to_value(type_id: int, count: int, b: bytes) -> Any:
    """Convert raw bytes for a TIFF type into a Python value."""
    if type_id == 0x0001:  # BYTE
        return list(b) if count > 1 else b[0]
    if type_id == 0x0002:  # ASCII
        return b.split(b"\x00", 1)[0].decode("ascii", errors="replace")
    if type_id == 0x0003:  # SHORT (uint16)
        vals = [int.from_bytes(b[i : i + 2], "little") for i in range(0, count * 2, 2)]
        return vals[0] if count == 1 else vals
    if type_id == 0x0004:  # LONG (uint32)
        vals = [int.from_bytes(b[i : i + 4], "little") for i in range(0, count * 4, 4)]
        return vals[0] if count == 1 else vals
    if type_id == 0x0005:  # RATIONAL (uint32/uint32)
        out = []
        for i in range(count):
            num = int.from_bytes(b[i * 8 : i * 8 + 4], "little")
            den = int.from_bytes(b[i * 8 + 4 : i * 8 + 8], "little")
            out.append((num, den))
        return out[0] if count == 1 else out
    if type_id == 0x0006:  # SBYTE
        vals = [int.from_bytes(b[i : i + 1], "little", signed=True) for i in range(count)]
        return vals[0] if count == 1 else vals
    if type_id == 0x0007:  # UNDEFINED — keep raw
        return bytes(b)
    if type_id == 0x0008:  # SSHORT (int16)
        vals = [int.from_bytes(b[i : i + 2], "little", signed=True) for i in range(0, count * 2, 2)]
        return vals[0] if count == 1 else vals
    if type_id == 0x0009:  # SLONG (int32)
        vals = [int.from_bytes(b[i : i + 4], "little", signed=True) for i in range(0, count * 4, 4)]
        return vals[0] if count == 1 else vals
    if type_id == 0x000A:  # SRATIONAL (int32/int32)
        out = []
        for i in range(count):
            num = int.from_bytes(b[i * 8 : i * 8 + 4], "little", signed=True)
            den = int.from_bytes(b[i * 8 + 4 : i * 8 + 8], "little", signed=True)
            out.append((num, den))
        return out[0] if count == 1 else out
    if type_id == 0x000B:  # FLOAT
        import struct
        vals = list(struct.unpack(f"<{count}f", b[: count * 4]))
        return vals[0] if count == 1 else vals
    if type_id == 0x000C:  # DOUBLE
        import struct
        vals = list(struct.unpack(f"<{count}d", b[: count * 8]))
        return vals[0] if count == 1 else vals
    return bytes(b)

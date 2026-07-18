"""PTP opcodes, data structures, and constants for Sigma fp / fp L.

PTP opcode numbers and data-structure shapes documented here are protocol
facts observable in any USB capture of a Sigma fp / fp L. They were derived
from a combination of (a) direct observation of USB traffic produced by
our own fp L during interactive testing, (b) cross-reference against the
LGPL-licensed libgphoto2 PTP reference (``camlibs/ptp2/ptp.h``), and
(c) cross-reference against the open-source ``sigma-ptpy`` Python project
by makanikai.

NO PROPRIETARY SIGMA SDK SOURCE CODE OR HEADER FILES ARE EMBEDDED OR
REQUIRED. This module is part of a clean-room reimplementation of the
Sigma vendor PTP protocol; see ``NOTICE`` in the project root for full
attribution.

References:
  - libgphoto2 PTP reference (LGPL):
        https://github.com/gphoto/libgphoto2/blob/master/camlibs/ptp2/ptp.h
  - libgphoto2 Issue #882 (fp L behavior):
        https://github.com/gphoto/libgphoto2/issues/882
  - sigma-ptpy (independent Python implementation):
        https://github.com/makanikai/sigma-ptpy
  - Apple ImageCaptureCore (used only for camera enumeration):
        https://developer.apple.com/documentation/imagecapturecore
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import IntEnum

_logger = logging.getLogger(__name__)

# Sanity ceiling for sigma_download — anything larger than this returned
# by GetPictFileInfo2 is almost certainly a parser misalignment, not a
# real file. The largest fp L raw is ~80 MB (DNG), so 200 MB gives
# plenty of headroom while still catching the "1.9 GB garbage" failure
# mode seen in the DNG+JPG dual-file test (2026-05-13).
_MAX_REASONABLE_FILESIZE = 200 * 1024 * 1024

# Known fp / fp L file extensions returned by GetPictFileInfo2.
_KNOWN_FILE_EXTS = {"JPG", "JPEG", "DNG", "TIF", "TIFF", "MOV", "MP4"}

# ---------------------------------------------------------------------------
# USB identifiers
# ---------------------------------------------------------------------------

SIGMA_VENDOR_ID = 0x1003
SIGMA_FP_PRODUCT_ID = 0xC432  # Sigma fp
SIGMA_FP_L_PRODUCT_ID = 0xC442  # Sigma fp L (verified in libgphoto2 #882 logs)

# Phase 3.18 (B3): the fp L's internal ImageDB is a 29-slot ring
# (slot ids 0x00-0x1C). Observed on hardware 2026-07-18: after a
# DNG+JPG shutter landed on slot 0x1C, db_tail wrapped into the low
# ids instead of advancing to 0x1D — and the old ``& 0xFF`` clear
# arithmetic aimed at nonexistent "slot 29", leaving a stale entry
# whose data was later served for the NEXT shot's file (the
# long-mythologised "clone return" bug). All slot arithmetic must be
# modulo this ring size.
SIGMA_IMAGE_DB_SLOTS = 0x1D  # 29


# ---------------------------------------------------------------------------
# Standard PTP operation codes (ISO 15740)
# ---------------------------------------------------------------------------


class PTPOperationCode(IntEnum):
    GET_DEVICE_INFO = 0x1001
    OPEN_SESSION = 0x1002
    CLOSE_SESSION = 0x1003
    GET_STORAGE_IDS = 0x1004
    GET_STORAGE_INFO = 0x1005
    GET_NUM_OBJECTS = 0x1006
    GET_OBJECT_HANDLES = 0x1007
    GET_OBJECT_INFO = 0x1008
    GET_OBJECT = 0x1009
    GET_THUMB = 0x100A
    DELETE_OBJECT = 0x100B
    SEND_OBJECT_INFO = 0x100C
    SEND_OBJECT = 0x100D
    INITIATE_CAPTURE = 0x100E
    FORMAT_STORE = 0x100F
    RESET_DEVICE = 0x1010
    SELF_TEST = 0x1011
    SET_OBJECT_PROTECTION = 0x1012
    POWER_DOWN = 0x1013
    GET_DEVICE_PROP_DESC = 0x1014
    GET_DEVICE_PROP_VALUE = 0x1015
    SET_DEVICE_PROP_VALUE = 0x1016
    RESET_DEVICE_PROP_VALUE = 0x1017
    TERMINATE_OPEN_CAPTURE = 0x1018
    MOVE_OBJECT = 0x1019
    COPY_OBJECT = 0x101A
    GET_PARTIAL_OBJECT = 0x101B
    INITIATE_OPEN_CAPTURE = 0x101C


# ---------------------------------------------------------------------------
# Sigma vendor-specific operation codes (0x9000-0x90FF range)
# ---------------------------------------------------------------------------


class SigmaOperationCode(IntEnum):
    """Sigma-specific PTP opcodes for fp / fp L.

    Values are protocol facts present in the LGPL libgphoto2 reference
    (``camlibs/ptp2/ptp.h``) and have been observed in live USB traffic
    from a Sigma fp L; the names mirror common community usage (e.g.
    sigma-ptpy, libgphoto2 PTP_OC_SIGMA_* style).
    """

    GET_NUM_DOWNLOADABLE_OBJECTS = 0x9001  # Standard-ish ext
    GET_ALL_OBJECT_INFO = 0x9002
    GET_USER_ASSIGNED_DEVICE_NAME = 0x9003

    GET_CAM_CONFIG = 0x9010
    GET_CAM_STATUS = 0x9011  # Deprecated, use GET_CAM_STATUS_2

    # Camera data groups (settings: ISO, SS, aperture, WB, etc.)
    GET_CAM_DATA_GROUP_1 = 0x9012
    GET_CAM_DATA_GROUP_2 = 0x9013
    GET_CAM_DATA_GROUP_3 = 0x9014
    GET_CAM_CAPT_STATUS = 0x9015  # *** Critical: shutter detection polling ***
    SET_CAM_DATA_GROUP_1 = 0x9016
    SET_CAM_DATA_GROUP_2 = 0x9017
    SET_CAM_DATA_GROUP_3 = 0x9018
    SET_CAM_CLOCK_ADJ = 0x9019
    GET_CAM_CAN_SET_INFO = 0x901A

    SNAP_COMMAND = 0x901B  # *** Critical: Mac → camera shutter trigger ***
    CLEAR_IMAGE_DB_SINGLE = 0x901C  # Cleanup after download
    CLEAR_IMAGE_DB_ALL = 0x901D

    # File transfer
    GET_PICT_FILE_INFO = 0x9020  # v1.0
    GET_PARTIAL_PICT_FILE = 0x9021
    GET_BIG_PARTIAL_PICT_FILE = 0x9022  # *** Critical: DNG download ***

    GET_CAM_DATA_GROUP_4 = 0x9023  # v1.1
    SET_CAM_DATA_GROUP_4 = 0x9024
    GET_CAM_CAN_SET_INFO_2 = 0x9025
    GET_CAM_CAN_SET_INFO_3 = 0x9026
    GET_CAM_DATA_GROUP_5 = 0x9027  # v1.2
    SET_CAM_DATA_GROUP_5 = 0x9028
    GET_CAM_DATA_GROUP_6 = 0x9029  # v1.2
    SET_CAM_DATA_GROUP_6 = 0x902A

    GET_CAM_VIEW_FRAME = 0x902B  # v2.1 (live view)
    GET_CAM_STATUS_2 = 0x902C
    GET_PICT_FILE_INFO_2 = 0x902D  # *** Critical: file metadata for download ***
    GET_CAM_CAN_SET_INFO_4 = 0x902E
    CLOSE_APPLICATION = 0x902F

    GET_CAM_CAN_SET_INFO_5 = 0x9030  # v5
    GET_CAM_DATA_GROUP_FOCUS = 0x9031
    SET_CAM_DATA_GROUP_FOCUS = 0x9032
    GET_CAM_DATA_GROUP_MOVIE = 0x9033
    SET_CAM_DATA_GROUP_MOVIE = 0x9034
    CONFIG_API = 0x9035  # *** Critical: API version negotiation at startup ***
    GET_MOVIE_FILE_INFO = 0x9036
    GET_PARTIAL_MOVIE_FILE = 0x9037
    GET_CAM_OP_PERMISSION = 0x9039  # PC control mode confirmation


# ---------------------------------------------------------------------------
# PTP response codes (subset)
# ---------------------------------------------------------------------------


class PTPResponseCode(IntEnum):
    UNDEFINED = 0x2000
    OK = 0x2001
    GENERAL_ERROR = 0x2002
    SESSION_NOT_OPEN = 0x2003
    INVALID_TRANSACTION_ID = 0x2004
    OPERATION_NOT_SUPPORTED = 0x2005
    PARAMETER_NOT_SUPPORTED = 0x2006
    INCOMPLETE_TRANSFER = 0x2007
    INVALID_STORAGE_ID = 0x2008


# ---------------------------------------------------------------------------
# Sigma SnapCommand parameters
# ---------------------------------------------------------------------------


class SnapCaptureMode(IntEnum):
    """First byte of SgmSnapState. Values verified by experimentation
    against a Sigma fp L and cross-referenced with libgphoto2 USB capture
    annotations."""

    GENERAL_CAPTURE = 0x01  # may not exist; placeholder
    NON_AF_CAPTURE = 0x02  # used in libgphoto2 #882 Windows log (data=0x02,0x02,0x01)
    AF_DRIVE_ONLY = 0x03
    START_AF = 0x04
    STOP_AF = 0x05
    START_CAPTURE = 0x06
    STOP_CAPTURE = 0x07
    START_CAPTURE_LIVEVIEW = 0x08


# ---------------------------------------------------------------------------
# SgmSnapState structure (input to SnapCommand)
# ---------------------------------------------------------------------------


@dataclass
class SgmSnapState:
    """Input payload for SnapCommand (0x901B).

    Wire format (observed in USB traffic, cross-checked with libgphoto2):
        CaptureMode  : UInt8   (1 byte)
        CaptureAmount: UInt8   (1 byte)  -- number of frames
        CheckSum     : UInt8   (1 byte)  -- sum of preceding bytes & 0xFF

    The Sigma protocol wraps payloads in a length-prefixed container; see
    ``ptp_packets.py`` for the wire encoding helper.
    """

    capture_mode: int = SnapCaptureMode.NON_AF_CAPTURE
    capture_amount: int = 1  # 1 shot

    def to_bytes(self) -> bytes:
        """Raw 3-byte struct: CaptureMode, CaptureAmount, CheckSum."""
        payload = bytes([self.capture_mode & 0xFF, self.capture_amount & 0xFF])
        checksum = sigma_checksum(payload)
        return payload + bytes([checksum])

    def to_wire_outdata(self) -> bytes:
        """Pack with Sigma protocol length prefix for ImageCaptureCore outData.

        Wire format (deduced from libgphoto2 #882 USB capture annotations):
            4 bytes: uint32 LE length of payload that follows
            N bytes: payload (struct + built-in checksum byte)

        For 1-shot still: returns ``03 00 00 00 02 01 03``.
        """
        raw = self.to_bytes()
        return len(raw).to_bytes(4, "little") + raw


# ---------------------------------------------------------------------------
# SgmCaptStatus structure (output of GetCamCaptStatus)
# ---------------------------------------------------------------------------


class CaptStatusCode(IntEnum):
    """16-bit capture status code returned in SgmCaptStatus.

    These values are based on observation against a Sigma fp L and the
    libgphoto2 #882 capture-status annotations. The exact enumeration
    may need refinement as more states are observed.
    """

    CLEAR = 0x0000  # No capture in progress
    SHOOTING_IN_PROGRESS = 0x0001
    SHOOT_SUCCESS = 0x0002
    SHOOT_FAILURE = 0x0003
    IMAGE_GENERATING = 0x0004  # Camera is generating the DNG/JPEG
    IMAGE_DB_NOT_EMPTY = 0x0005  # ImageDB has unread shots
    IMAGE_DATA_STORAGE_COMPLETE = 0x0006  # Ready to GetPictFileInfo2
    CAMERA_DURING_MIRROR_UP = 0x0007  # Mirror up state
    AF_SUCCESS = 0x8001
    AF_FAILURE = 0x8002
    OK = 0x8003
    UNKNOWN_ERROR = 0x6001


@dataclass
class SgmCaptStatus:
    """Output payload of GetCamCaptStatus (0x9015).

    Wire format (CONFIRMED from libgphoto2 master ptp.c lines 1158-1189):
        data[0] = 0x06         (length byte indicating 6 bytes of struct follow)
        data[1] = ImageID
        data[2] = ImageDBHead
        data[3] = ImageDBTail
        data[4..5] = CaptStatus (uint16 LE)
        data[5] = DestinationToSave (overlaps high byte of status — libgphoto2 quirk)
        data[6] = checksum (sum of data[0..5] & 0xFF)

    Total: 7 bytes on wire.
    """

    image_id: int
    image_db_head: int
    image_db_tail: int
    capt_status: int
    destination_to_save: int
    checksum: int

    @classmethod
    def from_bytes(cls, data: bytes) -> "SgmCaptStatus":
        """DEPRECATED — treats data[0] as imageid (off-by-one bug).

        Kept for backward compatibility with Phase 0-B tests.
        New code should use ``from_libgphoto2_wire``.
        """
        if len(data) < 7:
            raise ValueError(f"SgmCaptStatus needs >=7 bytes, got {len(data)}")
        return cls(
            image_id=data[0],
            image_db_head=data[1],
            image_db_tail=data[2],
            capt_status=int.from_bytes(data[3:5], "little"),
            destination_to_save=data[5],
            checksum=data[6],
        )

    @classmethod
    def from_wire(cls, data: bytes) -> "SgmCaptStatus":
        """Use the 8-byte fp trace format (more accurate than libgphoto2's)."""
        return cls.from_fp_trace_wire(data)

    @classmethod
    def from_fp_trace_wire(cls, data: bytes) -> "SgmCaptStatus":
        """Parse the 8-byte wire format from libgphoto2's REVERSE-ENGINEERED
        ``cameras/sigma-fp.txt`` trace (more accurate than libgphoto2's
        own parser, which has an off-by-one between status hi byte and dest).

        Layout (8 bytes total)::

            byte 0 = 0x06            length marker
            byte 1 = imageid
            byte 2 = imagedbhead
            byte 3 = imagedbtail
            byte 4 = captstatus_lo   (uint16 LE with byte 5)
            byte 5 = captstatus_hi
            byte 6 = destination_to_save
            byte 7 = checksum (sum of bytes 0..6 & 0xFF)

        Example from sigma-fp.txt line ~205:
            ``06 00 00 01 05 00 02 0e``
            length=6, id=0, db_head=0, db_tail=1, status=0x0005,
            dest=0x02, chk=0x0e = 6+0+0+1+5+0+2 ✓
        """
        if len(data) < 8:
            # fallback for shorter responses (some firmware variants)
            return cls.from_libgphoto2_wire(data)
        return cls(
            image_id=data[1],
            image_db_head=data[2],
            image_db_tail=data[3],
            capt_status=int.from_bytes(data[4:6], "little"),
            destination_to_save=data[6],
            checksum=data[7],
        )

    @classmethod
    def from_libgphoto2_wire(cls, data: bytes) -> "SgmCaptStatus":
        """Parse the 7-byte wire format documented in libgphoto2 ptp.c.

        Verified against libgphoto2 master branch
        (camlibs/ptp2/ptp.c ``ptp_sigma_fp_getcapturestatus``).

        Sigma's response layout:
            byte 0: 0x06            (length indicator, expected constant)
            byte 1: imageid
            byte 2: imagedbhead
            byte 3: imagedbtail
            byte 4-5: status (uint16 LE) — failure codes have high nibble 0x6
            byte 5: destination (libgphoto2 overlaps with status hi byte)
            byte 6: checksum
        """
        if len(data) < 7:
            raise ValueError(f"wire SgmCaptStatus needs >=7 bytes, got {len(data)}")
        if data[0] != 0x06:
            # log via warnings would be cleaner but parser shouldn't import logging
            pass
        return cls(
            image_id=data[1],
            image_db_head=data[2],
            image_db_tail=data[3],
            capt_status=int.from_bytes(data[4:6], "little"),
            destination_to_save=data[5],
            checksum=data[6],
        )

    @property
    def has_new_image(self) -> bool:
        """True if a new image is ready for download."""
        return (
            self.image_id != 0
            and self.capt_status
            in (
                CaptStatusCode.IMAGE_DATA_STORAGE_COMPLETE,
                CaptStatusCode.OK,
            )
        )

    @property
    def is_capturing(self) -> bool:
        """True if the camera is mid-capture (poll faster)."""
        return self.capt_status in (
            CaptStatusCode.SHOOTING_IN_PROGRESS,
            CaptStatusCode.IMAGE_GENERATING,
            CaptStatusCode.CAMERA_DURING_MIRROR_UP,
        )

    def __repr__(self) -> str:
        return (
            f"SgmCaptStatus(image_id=0x{self.image_id:02X}, "
            f"db_head=0x{self.image_db_head:02X}, db_tail=0x{self.image_db_tail:02X}, "
            f"capt_status=0x{self.capt_status:04X}, "
            f"dest=0x{self.destination_to_save:02X})"
        )


# ---------------------------------------------------------------------------
# SgmPictureFileInfoData structure (output of GetPictFileInfo2)
# ---------------------------------------------------------------------------


class FileKind(IntEnum):
    """File format codes seen in SgmPictureFileInfoData."""

    NONE = 0x0000
    JPEG = 0x0001
    DNG = 0x0002
    # Additional kinds (HEIF, etc.) may exist; refine during testing


@dataclass
class SgmPictureFileInfo:
    """One of the two files returned by GetPictFileInfo2 (RAW+JPEG dual case)."""

    file_kind: int  # FileKind enum value
    size_x: int  # image width in pixels
    size_y: int  # image height in pixels
    file_name: str  # e.g. "SDIM0001.DNG"
    file_size: int  # in bytes
    data_ptr: int  # camera-side data pointer (used by GetBigPartialPictFile)


@dataclass
class SgmPictureFileInfoData:
    """Output payload of GetPictFileInfo2 (0x902D)."""

    file_count: int  # 1 for DNG-only, 2 for DNG+JPEG
    files: list[SgmPictureFileInfo]

    @classmethod
    def from_bytes(cls, data: bytes) -> "SgmPictureFileInfoData":
        # Wire encoding deduced from live USB observation of GetPictFileInfo2
        # responses against a Sigma fp L:
        #   FileCount (C)
        #   for each file:
        #     FileKind (S = uint16 LE)
        #     SizeX (S)
        #     SizeY (S)
        #     FileName (length-prefixed ASCII)
        #     FileSize (I = uint32 LE)
        #     DataPtr (I = uint32 LE)
        raise NotImplementedError(
            "Parser must be confirmed with a live trace. "
            "Run scripts/phase0_snap_test.py to capture sample bytes."
        )


# ---------------------------------------------------------------------------
# SIGMAFP_PictFileInfo2Ex — the libgphoto2 view of GetPictFileInfo2 (0x902D)
# ---------------------------------------------------------------------------


@dataclass
class SigmaFpPictFileInfo2Ex:
    """File metadata returned by GetPictFileInfo2 (0x902D).

    Two on-the-wire layouts have been observed on fp L (2026-05-13):

    **Single-file** (declared_length = 56, total ~60+ bytes) — the
    layout documented by libgphoto2 master ptp.c lines 1200-1261, used
    for JPG-only and DNG-only captures:

        [0..3]   uint32 LE  declared_length = 56
        [4..11]  (8 bytes, ignored)
        [12..15] uint32 LE  fileaddress
        [16..19] uint32 LE  filesize
        [20..23] uint32 LE  path_off (absolute)
        [24..27] uint32 LE  name_off (absolute)
        [28..31] char[4]    fileext  (e.g. "JPG\\0", "DNG\\0")
        [32..33] uint16 LE  width
        [34..35] uint16 LE  height
        [path_off..]        null-terminated path string ("100SIGMA")
        [name_off..]        null-terminated name string ("SDIM0001.JPG")

    **Multi-file** (declared_length > 56, observed = 108 for DNG+JPG) —
    fp L DNG+JPG mode emits two file entries in one response:

        [0..3]   uint32 LE  declared_length (108 observed)
        [4..7]   uint32 LE  entry_count (2 observed)
        [8..11]  uint32 LE  first_entry_offset (16 observed)
        [12..15] (4 bytes, ignored — 0x40 observed)
        [first_entry_offset..]  Entry 1 (24-byte header + strings)
        [next 4-byte aligned..] Entry 2 (24-byte header + strings)

    Each entry's 24-byte header has the same layout as the single-file
    case starting at offset 12 in that case (addr / size / path_off /
    name_off / fileext / width / height). path_off and name_off remain
    absolute offsets from the start of the response buffer.

    Reverse-engineered from a live DNG+JPG snap on fp L V90 by
    capturing the raw response with the Step-1 sanity-guard hex dump.
    """

    fileaddress: int  # camera-internal pointer for GetBigPartialPictFile
    filesize: int
    width: int
    height: int
    fileext: str  # e.g. "JPG", "DNG"
    path: str  # e.g. "100SIGMA"
    name: str  # e.g. "SDIM0001.JPG"

    # ---- parsing helpers --------------------------------------------------

    @classmethod
    def _parse_entry_at(
        cls, data: bytes, off: int
    ) -> "SigmaFpPictFileInfo2Ex":
        """Parse a single 24-byte entry header (+ its null-terminated
        path/name strings) starting at ``off``."""
        if off + 24 > len(data):
            raise ValueError(
                f"SigmaFpPictFileInfo2Ex entry header overflows buffer "
                f"(off={off}, total_len={len(data)})"
            )
        fileaddress = int.from_bytes(data[off:off + 4], "little")
        filesize = int.from_bytes(data[off + 4:off + 8], "little")
        path_off = int.from_bytes(data[off + 8:off + 12], "little")
        name_off = int.from_bytes(data[off + 12:off + 16], "little")
        fileext_raw = data[off + 16:off + 20]
        fileext = fileext_raw.split(b"\x00", 1)[0].decode(
            "ascii", errors="replace"
        )
        width = int.from_bytes(data[off + 20:off + 22], "little")
        height = int.from_bytes(data[off + 22:off + 24], "little")

        path = ""
        name = ""
        if 0 < path_off < len(data):
            path = data[path_off:].split(b"\x00", 1)[0].decode(
                "ascii", errors="replace"
            )
        if 0 < name_off < len(data):
            name = data[name_off:].split(b"\x00", 1)[0].decode(
                "ascii", errors="replace"
            )

        return cls(
            fileaddress=fileaddress,
            filesize=filesize,
            width=width,
            height=height,
            fileext=fileext,
            path=path,
            name=name,
        )

    # ---- public API --------------------------------------------------

    @classmethod
    def from_wire(cls, data: bytes) -> "SigmaFpPictFileInfo2Ex":
        """Return the *first* entry. Kept for callers (e.g. one-shot
        scripts) that don't need to handle DNG+JPG dual files.

        Equivalent to ``from_wire_list(data)[0]`` but with a more
        constrained type. Use ``from_wire_list`` if you might receive
        multiple files (DNG+JPG mode)."""
        entries = cls.from_wire_list(data)
        return entries[0]

    @classmethod
    def from_wire_list(
        cls, data: bytes
    ) -> list["SigmaFpPictFileInfo2Ex"]:
        """Parse one or many file entries from a GetPictFileInfo2 response.

        Auto-detects single-file vs multi-file layout by inspecting
        the ``declared_length`` field at bytes [0..3]: == 56 means
        legacy single-file, > 56 means multi-file with an extended
        header.
        """
        if len(data) < 60:
            raise ValueError(
                f"SigmaFpPictFileInfo2Ex needs >=60 bytes, got {len(data)}"
            )
        declared = int.from_bytes(data[0:4], "little")

        if declared == 56:
            # Legacy single-entry layout, entry header at offset 12.
            entries = [cls._parse_entry_at(data, 12)]
        else:
            # Multi-entry layout
            count = int.from_bytes(data[4:8], "little")
            first_off = int.from_bytes(data[8:12], "little")
            if count < 1 or count > 4:
                hex_head = data[: min(96, len(data))].hex(" ")
                _logger.error(
                    "SigmaFpPictFileInfo2Ex: implausible entry_count=%d "
                    "(declared=%d total_len=%d first_off=%d) raw_head=%s",
                    count, declared, len(data), first_off, hex_head,
                )
                raise ValueError(
                    f"SigmaFpPictFileInfo2Ex: implausible entry_count="
                    f"{count}; raw bytes logged."
                )
            if first_off < 12 or first_off >= len(data):
                hex_head = data[: min(96, len(data))].hex(" ")
                _logger.error(
                    "SigmaFpPictFileInfo2Ex: bad first_entry_offset=%d "
                    "(declared=%d count=%d total_len=%d) raw_head=%s",
                    first_off, declared, count, len(data), hex_head,
                )
                raise ValueError(
                    f"SigmaFpPictFileInfo2Ex: bad first_entry_offset="
                    f"{first_off}; raw bytes logged."
                )
            entries = []
            cursor = first_off
            for i in range(count):
                if cursor + 24 > len(data):
                    hex_head = data[: min(96, len(data))].hex(" ")
                    _logger.error(
                        "SigmaFpPictFileInfo2Ex: entry %d overflows at "
                        "cursor=%d (total_len=%d) raw_head=%s",
                        i, cursor, len(data), hex_head,
                    )
                    raise ValueError(
                        f"SigmaFpPictFileInfo2Ex: entry {i} overflows "
                        f"at cursor={cursor}; raw bytes logged."
                    )
                entry = cls._parse_entry_at(data, cursor)
                entries.append(entry)
                if i + 1 < count:
                    # Advance past this entry's strings to the next
                    # 4-byte-aligned offset.
                    boundary = cursor + 24
                    # The strings live at the absolute path_off / name_off
                    # offsets we just parsed. Both end with a null
                    # terminator; take the max end.
                    p_off = int.from_bytes(
                        data[cursor + 8:cursor + 12], "little"
                    )
                    n_off = int.from_bytes(
                        data[cursor + 12:cursor + 16], "little"
                    )
                    for s_off in (p_off, n_off):
                        if 0 < s_off < len(data):
                            null_pos = data.find(b"\x00", s_off)
                            if null_pos < 0:
                                # missing terminator → bail to declared_len
                                end = declared
                            else:
                                end = null_pos + 1
                            if end > boundary:
                                boundary = end
                    cursor = (boundary + 3) & ~0x3

        # Per-entry sanity guards — same logic as the Step 1 single-file
        # checks, now applied to every entry. Raises on bogus parses
        # before we hand the entries back to the bridge.
        for idx, e in enumerate(entries):
            suspicious = (
                e.filesize <= 0
                or e.filesize > _MAX_REASONABLE_FILESIZE
                or e.fileext.upper() not in _KNOWN_FILE_EXTS
            )
            if suspicious:
                hex_head = data[: min(128, len(data))].hex(" ")
                _logger.error(
                    "SigmaFpPictFileInfo2Ex: suspicious entry %d/%d "
                    "(declared_len=%d total_len=%d "
                    "addr=0x%X size=%d ext=%r path=%r name=%r "
                    "width=%d height=%d) raw_head=%s",
                    idx, len(entries), declared, len(data),
                    e.fileaddress, e.filesize, e.fileext,
                    e.path, e.name, e.width, e.height, hex_head,
                )
                raise ValueError(
                    f"SigmaFpPictFileInfo2Ex entry {idx} failed "
                    f"sanity check (size={e.filesize}, "
                    f"ext={e.fileext!r}); raw bytes logged."
                )

        if _logger.isEnabledFor(logging.DEBUG):
            _logger.debug(
                "SigmaFpPictFileInfo2Ex: parsed %d entr%s "
                "(declared=%d total=%d): %s",
                len(entries), "y" if len(entries) == 1 else "ies",
                declared, len(data),
                ", ".join(f"{e.fileext}/{e.filesize}B" for e in entries),
            )

        return entries

    @property
    def full_filename(self) -> str:
        """Like libgphoto2's path->name construction: name + ext."""
        if self.fileext and not self.name.lower().endswith(
            "." + self.fileext.lower()
        ):
            return f"{self.name}{self.fileext}"
        return self.name


# ---------------------------------------------------------------------------
# Sigma protocol checksum
# ---------------------------------------------------------------------------

DEFAULT_CHECKSUM_ALGORITHM = "sum_mod_256"


def sigma_checksum(data: bytes) -> int:
    """Compute the Sigma vendor-protocol checksum byte.

    Hypothesis (from libgphoto2 #882 Windows log):
        SnapCommand payload [0x02, 0x02, 0x01] → CheckSum 0x05
        → 0x02 + 0x02 + 0x01 = 0x05 ✅ (sum mod 256)

    Verify during Phase 0; switch to XOR if hypothesis fails.
    """
    return sum(data) & 0xFF


def verify_checksum(data: bytes, expected: int) -> bool:
    return sigma_checksum(data) == (expected & 0xFF)


# ---------------------------------------------------------------------------
# PTP capability flags (from ImageCaptureCore)
# ---------------------------------------------------------------------------

ICCAMERA_CAPABILITY_PTP_COMMANDS = "ICCameraDeviceCanAcceptPTPCommands"
ICCAMERA_CAPABILITY_TETHERED = "ICCameraDeviceCanTakePicture"


# ---------------------------------------------------------------------------
# Convenience: known sequences
# ---------------------------------------------------------------------------

# Standard SnapCommand outData for "1 still shot, no AF":
# CaptureMode=0x02 (NON_AF_CAPTURE), CaptureAmount=0x01
# Wrapped with the 4-byte Sigma protocol length prefix that ImageCaptureCore
# expects to forward as the PTP data phase.
SNAP_SINGLE_STILL_PAYLOAD = SgmSnapState(
    capture_mode=SnapCaptureMode.NON_AF_CAPTURE,
    capture_amount=1,
).to_wire_outdata()


def wrap_sigma_outdata(payload: bytes) -> bytes:
    """Wrap arbitrary payload bytes with the 4-byte Sigma length prefix.

    Use this when constructing outData for any Sigma-specific PTP write
    operation (SetCamDataGroupX, SnapCommand, etc.).
    """
    return len(payload).to_bytes(4, "little") + payload

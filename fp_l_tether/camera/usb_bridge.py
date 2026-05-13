"""USB-direct PTP transport for Sigma fp / fp L (libusb / pyusb backend).

Why this exists:
  Apple's ImageCaptureCore ``requestSendPTPCommand:outData:`` API does NOT
  expose the PTP data phase to the caller (see docs/PHASE0_LOG.md 2026-05-11
  CRITICAL FINDING). For Sigma's protocol that returns everything via data
  phase, we need raw USB Bulk access. This module provides that.

Architecture::

      ┌─────────────────────────────────────┐
      │            USBBridge                │
      │  open() → claim PTP interface 0     │
      │  send_command(opcode, params,       │
      │               out_data=...)         │
      │     → COMMAND container on BULK_OUT │
      │     → DATA container on BULK_OUT    │
      │     ← DATA container on BULK_IN     │
      │     ← RESPONSE container on BULK_IN │
      └─────────────────────────────────────┘

PTP USB container format (PTP/IP / USB ISO 15740 §3.2):

    Offset  Size  Field
    0       4     uint32 LE — total container length (includes this header)
    4       2     uint16 LE — container type (1=Command, 2=Data, 3=Response, 4=Event)
    6       2     uint16 LE — opcode (Command/Data) or response code (Response)
    8       4     uint32 LE — transaction ID
    12+     -     payload (params 4 bytes each for Command/Response, raw for Data)

Sigma's data phase additionally wraps the payload with:

    Offset  Size  Field
    0       4     uint32 LE — payload length (excluding this prefix, including checksum)
    4       N     bytes  — payload struct
    4+N     1     uint8  — checksum (sum of payload bytes & 0xFF)

This module handles the PTP layer. The Sigma wrapping is exposed via
``send_sigma_command()`` which takes the inner struct bytes and adds framing.

Requires::

    brew install libusb           # macOS
    pip install pyusb

On macOS the script must run with sudo (or the binary must be signed with
USB entitlements) to detach Apple's PTPCamera kernel driver. See PHASE0_LOG.
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Iterable

import usb.core  # type: ignore[import-not-found]
import usb.util  # type: ignore[import-not-found]

from .ptp_codes import (
    SIGMA_FP_L_PRODUCT_ID,
    SIGMA_FP_PRODUCT_ID,
    SIGMA_VENDOR_ID,
    PTPResponseCode,
    SgmCaptStatus,
    SigmaFpPictFileInfo2Ex,
    SigmaOperationCode,
    sigma_checksum,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PTP USB constants
# ---------------------------------------------------------------------------


class PTPContainerType(IntEnum):
    COMMAND = 1
    DATA = 2
    RESPONSE = 3
    EVENT = 4


PTP_HEADER_SIZE = 12  # length(4) + type(2) + code(2) + transaction_id(4)
PTP_USB_INTERFACE_CLASS = 0x06  # Still Image (PTP)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class USBBridgeError(RuntimeError):
    pass


class PTPError(USBBridgeError):
    def __init__(self, response_code: int, message: str = ""):
        self.response_code = response_code
        rc_name = _response_name(response_code)
        super().__init__(
            f"PTP error 0x{response_code:04X} ({rc_name}){': ' + message if message else ''}"
        )


def _response_name(code: int) -> str:
    try:
        return PTPResponseCode(code).name
    except ValueError:
        return f"unknown_0x{code:04X}"


# ---------------------------------------------------------------------------
# Container parsing/building
# ---------------------------------------------------------------------------


@dataclass
class PTPContainer:
    container_type: int
    code: int
    transaction_id: int
    payload: bytes

    @property
    def is_response(self) -> bool:
        return self.container_type == PTPContainerType.RESPONSE

    @property
    def is_data(self) -> bool:
        return self.container_type == PTPContainerType.DATA

    def __repr__(self) -> str:
        type_name = {1: "CMD", 2: "DATA", 3: "RESP", 4: "EVENT"}.get(
            self.container_type, str(self.container_type)
        )
        return (
            f"PTPContainer({type_name}, code=0x{self.code:04X}, "
            f"txid={self.transaction_id}, payload={len(self.payload)}B)"
        )


def build_container(
    container_type: int,
    code: int,
    transaction_id: int,
    payload: bytes = b"",
) -> bytes:
    """Pack a PTP USB container."""
    length = PTP_HEADER_SIZE + len(payload)
    return struct.pack("<IHHI", length, container_type, code, transaction_id) + payload


def parse_container_header(buf: bytes) -> tuple[int, int, int, int]:
    """Return (length, type, code, transaction_id) from the first 12 bytes."""
    if len(buf) < PTP_HEADER_SIZE:
        raise ValueError(f"Container header needs ≥12 bytes, got {len(buf)}")
    return struct.unpack("<IHHI", buf[:PTP_HEADER_SIZE])


def pack_command_params(params: Iterable[int]) -> bytes:
    """Pack 0..5 uint32 LE parameters for a Command container."""
    return b"".join(struct.pack("<I", p & 0xFFFFFFFF) for p in params)


# ---------------------------------------------------------------------------
# USBBridge — the actual transport
# ---------------------------------------------------------------------------


class USBBridge:
    """Direct USB Bulk transport for Sigma fp / fp L PTP.

    Usage::

        with USBBridge.find_sigma_fp_l() as bridge:
            bridge.open_session()
            resp = bridge.send_command(SigmaOperationCode.GET_CAM_CAPT_STATUS)
            print(resp.in_data.hex())
            bridge.close_session()

    Thread safety: a single bridge instance serializes commands with a
    threading.Lock. Don't share across processes.
    """

    DEFAULT_TIMEOUT_MS = 5000
    DOWNLOAD_TIMEOUT_MS = 30000

    def __init__(self, device: "usb.core.Device", interface_number: int = 0):
        self._dev = device
        self._intf_no = interface_number
        self._intf = None
        self._ep_out = None
        self._ep_in = None
        self._ep_int = None
        self._lock = threading.Lock()
        self._txid = 0
        self._session_id = 0
        self._session_open = False
        self._claimed = False
        self._detached_kernel = False

    # ----- discovery ---------------------------------------------------

    @classmethod
    def find_sigma_fp_l(cls) -> "USBBridge":
        """Find the first connected Sigma fp / fp L and return an unopened bridge."""
        dev = usb.core.find(idVendor=SIGMA_VENDOR_ID, idProduct=SIGMA_FP_L_PRODUCT_ID)
        if dev is None:
            dev = usb.core.find(idVendor=SIGMA_VENDOR_ID, idProduct=SIGMA_FP_PRODUCT_ID)
        if dev is None:
            raise USBBridgeError("No Sigma fp / fp L found by libusb")
        return cls(dev)

    # ----- lifecycle ---------------------------------------------------

    def __enter__(self) -> "USBBridge":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._session_open:
                try:
                    self.close_session()
                except Exception as e:  # noqa: BLE001
                    logger.warning("close_session on exit raised: %s", e)
        finally:
            self.close()

    def open(self) -> None:
        """Detach kernel driver, claim interface, find endpoints."""
        # Detach Apple PTPCamera kernel driver (requires sudo on macOS).
        try:
            if self._dev.is_kernel_driver_active(self._intf_no):
                logger.info("Kernel driver active — detaching")
                self._dev.detach_kernel_driver(self._intf_no)
                self._detached_kernel = True
        except NotImplementedError:
            pass
        except usb.core.USBError as e:
            raise USBBridgeError(
                f"Could not detach macOS PTPCamera kernel driver: {e}. "
                "On macOS, run this script with sudo (see docs/SETUP.md)."
            ) from e

        # Set the default configuration. set_configuration() must be called
        # before claim on some platforms.
        try:
            self._dev.set_configuration()
        except usb.core.USBError as e:
            # If already configured, this is fine
            logger.debug("set_configuration: %s (likely already configured)", e)

        # Find the PTP interface and its endpoints
        cfg = self._dev.get_active_configuration()
        intf = None
        for itf in cfg:
            if itf.bInterfaceClass == PTP_USB_INTERFACE_CLASS:
                intf = itf
                break
        if intf is None:
            raise USBBridgeError(
                "Camera does not expose a Still Image / PTP interface (class 0x06)"
            )
        self._intf = intf
        self._intf_no = intf.bInterfaceNumber

        usb.util.claim_interface(self._dev, self._intf_no)
        self._claimed = True
        logger.info(
            "Claimed PTP interface %d on Sigma fp/fp L (bus %d addr %d)",
            self._intf_no, self._dev.bus, self._dev.address,
        )

        # Locate endpoints
        for ep in intf:
            attr = ep.bmAttributes & 0x03
            dir_in = usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN
            if attr == 2 and not dir_in:  # BULK OUT
                self._ep_out = ep
            elif attr == 2 and dir_in:  # BULK IN
                self._ep_in = ep
            elif attr == 3 and dir_in:  # INTERRUPT IN
                self._ep_int = ep

        if self._ep_out is None or self._ep_in is None:
            raise USBBridgeError("Could not find both BULK OUT and BULK IN endpoints")

        logger.info(
            "Endpoints: BULK_OUT=0x%02X, BULK_IN=0x%02X, INT_IN=%s",
            self._ep_out.bEndpointAddress,
            self._ep_in.bEndpointAddress,
            f"0x{self._ep_int.bEndpointAddress:02X}" if self._ep_int else "none",
        )

    def close(self) -> None:
        """Release the interface and dispose resources."""
        try:
            if self._claimed and self._intf is not None:
                usb.util.release_interface(self._dev, self._intf_no)
        except Exception as e:  # noqa: BLE001
            logger.debug("release_interface raised: %s", e)
        try:
            usb.util.dispose_resources(self._dev)
        except Exception as e:  # noqa: BLE001
            logger.debug("dispose_resources raised: %s", e)
        self._claimed = False
        self._intf = None
        # NOTE: We deliberately don't re-attach the kernel driver. On macOS
        # the user can power-cycle the camera or unplug/replug to restore
        # normal Image Capture behavior.

    # ----- transaction IDs --------------------------------------------

    def _next_txid(self) -> int:
        self._txid += 1
        return self._txid

    # ----- low-level read/write ---------------------------------------

    def _write(self, data: bytes, timeout_ms: int | None = None) -> None:
        timeout = timeout_ms or self.DEFAULT_TIMEOUT_MS
        assert self._ep_out is not None
        written = self._ep_out.write(data, timeout=timeout)
        if written != len(data):
            raise USBBridgeError(f"Short USB write: {written} of {len(data)} bytes")

    def _read_container(self, timeout_ms: int | None = None) -> PTPContainer:
        """Read one full PTP container from BULK_IN."""
        timeout = timeout_ms or self.DEFAULT_TIMEOUT_MS
        assert self._ep_in is not None

        # Read up to 64KiB to get the header plus typical small payloads
        max_pkt = self._ep_in.wMaxPacketSize
        # Read enough to capture small containers in one shot. For large
        # data (e.g. file content), we'll loop below.
        first = bytes(self._ep_in.read(64 * 1024, timeout=timeout))
        if len(first) < PTP_HEADER_SIZE:
            raise USBBridgeError(f"PTP read too short: {len(first)} bytes")

        length, ctype, code, txid = parse_container_header(first)
        payload = first[PTP_HEADER_SIZE:]

        # Read additional chunks if needed
        while len(payload) + PTP_HEADER_SIZE < length:
            remaining = length - PTP_HEADER_SIZE - len(payload)
            chunk = bytes(self._ep_in.read(min(remaining, 64 * 1024), timeout=timeout))
            if not chunk:
                break
            payload += chunk

        if len(payload) + PTP_HEADER_SIZE != length:
            logger.warning(
                "Container length mismatch: declared %d, got %d (%d header + %d payload)",
                length, len(payload) + PTP_HEADER_SIZE, PTP_HEADER_SIZE, len(payload),
            )

        return PTPContainer(
            container_type=ctype,
            code=code,
            transaction_id=txid,
            payload=payload[: length - PTP_HEADER_SIZE],
        )

    # ----- high-level: session ----------------------------------------

    def open_session(self, session_id: int = 1) -> None:
        from .ptp_codes import PTPOperationCode

        if self._session_open:
            return
        self._session_id = session_id
        self.send_command_raw(
            PTPOperationCode.OPEN_SESSION,
            params=(session_id,),
            data_phase_out=False,
            data_phase_in=False,
        )
        self._session_open = True
        logger.info("PTP session %d opened", session_id)

    def close_session(self) -> None:
        from .ptp_codes import PTPOperationCode

        if not self._session_open:
            return
        try:
            self.send_command_raw(
                PTPOperationCode.CLOSE_SESSION,
                params=(),
                data_phase_out=False,
                data_phase_in=False,
            )
        finally:
            self._session_open = False
        logger.info("PTP session closed")

    # ----- high-level: commands ---------------------------------------

    @dataclass
    class Response:
        response_code: int
        response_params: tuple[int, ...]
        in_data: bytes

        @property
        def is_ok(self) -> bool:
            return self.response_code == PTPResponseCode.OK

        def raise_for_status(self) -> None:
            if not self.is_ok:
                raise PTPError(self.response_code)

    def send_command_raw(
        self,
        opcode: int,
        params: tuple[int, ...] = (),
        out_data: bytes | None = None,
        data_phase_out: bool = False,
        data_phase_in: bool = True,
        timeout_ms: int | None = None,
    ) -> "USBBridge.Response":
        """Send a PTP transaction. The general flow is:

        1. Send COMMAND container (BULK_OUT)
        2. If data_phase_out and out_data: send DATA container (BULK_OUT)
        3. If data_phase_in: read DATA container (BULK_IN); else skip
        4. Read RESPONSE container (BULK_IN)

        For most read ops: ``data_phase_in=True`` (default).
        For write ops with outgoing data: ``data_phase_out=True``, pass out_data.
        For ops with no data phase: ``data_phase_in=False``.

        The Sigma SDK ALWAYS wraps DATA payload with a 4-byte length prefix
        and a 1-byte checksum at the end. This method does NOT add that
        wrapping automatically — use ``send_sigma_command()`` for that.
        """
        with self._lock:
            txid = self._next_txid()

            # 1. Command container
            cmd_payload = pack_command_params(params)
            cmd_container = build_container(
                PTPContainerType.COMMAND, opcode, txid, cmd_payload
            )
            logger.debug("→ CMD 0x%04X txid=%d params=%s", opcode, txid, params)
            self._write(cmd_container, timeout_ms=timeout_ms)

            # 2. Outgoing data phase
            if data_phase_out and out_data is not None:
                data_container = build_container(
                    PTPContainerType.DATA, opcode, txid, out_data
                )
                logger.debug("→ DATA %d bytes", len(out_data))
                self._write(data_container, timeout_ms=timeout_ms)

            # 3. Incoming data phase + response
            in_data = b""
            response: PTPContainer | None = None

            if data_phase_in:
                first = self._read_container(timeout_ms=timeout_ms)
                if first.is_data:
                    in_data = first.payload
                    logger.debug("← DATA %d bytes", len(in_data))
                    response = self._read_container(timeout_ms=timeout_ms)
                elif first.is_response:
                    # Some ops with data_phase_in=True actually return no data
                    response = first
                else:
                    raise USBBridgeError(
                        f"Expected DATA or RESPONSE container, got {first}"
                    )
            else:
                response = self._read_container(timeout_ms=timeout_ms)

            assert response is not None
            if not response.is_response:
                raise USBBridgeError(f"Expected RESPONSE container, got {response}")

            # Parse response params (0-5 uint32 LE)
            nparams = len(response.payload) // 4
            params_out = struct.unpack(
                f"<{nparams}I", response.payload[: nparams * 4]
            )
            logger.debug(
                "← RESP 0x%04X txid=%d params=%s",
                response.code, response.transaction_id, params_out,
            )

            return USBBridge.Response(
                response_code=response.code,
                response_params=tuple(params_out),
                in_data=in_data,
            )

    # ----- Sigma high-level helpers (ported from libgphoto2 ptp.c) -----
    #
    # These mirror the C functions ``ptp_sigma_fp_*`` in
    # camlibs/ptp2/ptp.c. They form the complete capture+download
    # sequence that ``camera_sigma_fp_capture`` runs.
    #
    # The libgphoto2 implementation has a confirmed double-free bug in
    # ``ptp_sigma_fp_9035`` / ``camera_init``: the helper internally frees
    # the data buffer, then the caller frees it again. The Python port
    # avoids this entirely (Python has no manual free).

    def sigma_get_camera_info(self) -> bytes:
        """0x9035 ``GetCameraInfo`` (libgphoto2's name for ConfigApi).

        First call of the Sigma init sequence. Returns IFD-list-style
        data (we don't parse it — libgphoto2 doesn't use it either,
        it just logs and frees).
        """
        resp = self.send_command_raw(SigmaOperationCode.CONFIG_API)
        resp.raise_for_status()
        return resp.in_data

    def sigma_get_cam_can_set_info_5(self) -> bytes:
        """0x9030 ``GetCamCanSetInfo5``."""
        resp = self.send_command_raw(SigmaOperationCode.GET_CAM_CAN_SET_INFO_5)
        resp.raise_for_status()
        return resp.in_data

    def sigma_get_cam_config(self) -> bytes:
        """0x9010 ``GetCamConfig`` — global camera configuration blob."""
        resp = self.send_command_raw(SigmaOperationCode.GET_CAM_CONFIG)
        resp.raise_for_status()
        return resp.in_data

    def sigma_get_datagroup(self, group: int) -> bytes:
        """0x9012..0x9029 ``GetDataGroupN`` where N=1..6.

        Opcodes are NON-CONTIGUOUS (confirmed from libgphoto2 ptp.h):
          group 1 → 0x9012
          group 2 → 0x9013
          group 3 → 0x9014
          group 4 → 0x9023
          group 5 → 0x9027
          group 6 → 0x9029
        """
        opcode_by_group = {
            1: SigmaOperationCode.GET_CAM_DATA_GROUP_1,
            2: SigmaOperationCode.GET_CAM_DATA_GROUP_2,
            3: SigmaOperationCode.GET_CAM_DATA_GROUP_3,
            4: SigmaOperationCode.GET_CAM_DATA_GROUP_4,
            5: SigmaOperationCode.GET_CAM_DATA_GROUP_5,
            6: SigmaOperationCode.GET_CAM_DATA_GROUP_6,
        }
        if group not in opcode_by_group:
            raise ValueError(f"group must be 1..6, got {group}")
        resp = self.send_command_raw(opcode_by_group[group])
        resp.raise_for_status()
        return resp.in_data

    def sigma_get_cam_datagroup_focus(self) -> bytes:
        """0x9031 ``GetCamDataGroupFocus``."""
        resp = self.send_command_raw(SigmaOperationCode.GET_CAM_DATA_GROUP_FOCUS)
        resp.raise_for_status()
        return resp.in_data

    def sigma_get_cam_datagroup_movie(self) -> bytes:
        """0x9033 ``GetCamDataGroupMovie``."""
        resp = self.send_command_raw(SigmaOperationCode.GET_CAM_DATA_GROUP_MOVIE)
        resp.raise_for_status()
        return resp.in_data

    def sigma_get_cam_status_2(
        self, canset: int = 0, datagroup: int = 0, other: int = 0
    ) -> bytes:
        """0x902c ``GetCamStatus2`` — alternative status query.

        Per the libgphoto2 reverse-engineered trace
        (``cameras/sigma-fp.txt``), this is called multiple times in the
        capture sequence — both right after init and around each snap.
        Without it the camera doesn't fully transition to "PC capture mode".

        Returns raw response bytes (TLV-structured per the trace).
        """
        resp = self.send_command_raw(
            SigmaOperationCode.GET_CAM_STATUS_2,
            params=(canset, datagroup, other),
        )
        resp.raise_for_status()
        return resp.in_data

    def sigma_set_cam_datagroup_focus(self, x: int, y: int) -> None:
        """0x9032 ``SetCamDataGroupFocus`` — write AF point coordinates.

        Wire format is TIFF-IFD (matching the Get response). Confirmed from
        sigma-ptpy ``schema.py`` (CamDataGroupFocus / DMFPos / _encode) plus
        SDK Framework Mach-O type encoding
        ``i32@0:8^{_IFDArray=II^{_SgmDirectoryEntry}}16@24``.

        We send three tags together so the camera transitions into the
        free-form AF point mode and accepts arbitrary coordinates:

          tag 0x000A FocusArea          BYTE=2  (OnePointSelection)
          tag 0x000B OnePointSelection  BYTE=0  (Free — not the 49-point grid)
          tag 0x000D DMFPos             UNDEFINED×4  (Y_lo Y_hi X_lo X_hi)

        Sending only tag 0x000D leaves the camera in its previous AF area
        mode (often the 49-point grid) where DMFPos is ignored and the LCD
        renders the "[ ]" wide-area indicator instead of a single AF box.

        Critically, SetCamDataGroupFocus has **no trailing checksum byte**
        and no outer length prefix — just the bare IFD bytes whose first
        4 bytes are the declared length.

        Valid coordinate range (from GetCamCanSetInfo5 tag 0x0265 = (Y_min,
        Y_max, X_min, X_max) = (85, 597, 96, 928)):
            Y ∈ [85, 597], X ∈ [96, 928]
            center = (X=512, Y=340)  ←  default value 54 01 00 02

        UI parameters stay (x, y) — Y/X swap happens internally on the wire.

        NOTE: GetCamDataGroupFocus is a static cache and does NOT reflect the
        written value. Verify by visual check on the camera LCD.
        """
        if not (96 <= x <= 928 and 85 <= y <= 597):
            raise ValueError(
                f"AF point out of range: x={x} (must be 96..928), "
                f"y={y} (must be 85..597)"
            )

        # 3 entries × 12B + 8B header = 44 bytes. All values inline (≤4B), no
        # offset/trailing data area required. Entries sorted by tag ascending.
        declared_length = 44
        payload = (
            declared_length.to_bytes(4, "little")
            + (3).to_bytes(4, "little")        # entries_count
            # Entry 1: FocusArea = OnePointSelection (2)
            + (0x000A).to_bytes(2, "little")   # tag
            + (0x0001).to_bytes(2, "little")   # type BYTE
            + (1).to_bytes(4, "little")        # count
            + (2).to_bytes(1, "little") + b"\x00\x00\x00"  # value (padded)
            # Entry 2: OnePointSelection = Free (0)
            + (0x000B).to_bytes(2, "little")
            + (0x0001).to_bytes(2, "little")
            + (1).to_bytes(4, "little")
            + (0).to_bytes(1, "little") + b"\x00\x00\x00"
            # Entry 3: DMFPos (UNDEFINED ×4) = Y, X
            + (0x000D).to_bytes(2, "little")
            + (0x0007).to_bytes(2, "little")
            + (4).to_bytes(4, "little")
            + y.to_bytes(2, "little")
            + x.to_bytes(2, "little")
        )
        assert len(payload) == 44, f"payload size mismatch: {len(payload)}"

        resp = self.send_command_raw(
            SigmaOperationCode.SET_CAM_DATA_GROUP_FOCUS,
            out_data=payload,
            data_phase_out=True,
            data_phase_in=False,
        )
        resp.raise_for_status()

    def sigma_send_raw_setdatagroup(
        self,
        opcode: int,
        payload_no_checksum: bytes,
    ) -> None:
        """Low-level helper to send a SetDataGroup-style command with the
        exact byte payload (Sigma envelope: payload + 1-byte sum checksum).

        Used to replay the exact SET sequences captured in
        ``cameras/sigma-fp.txt`` without trying to interpret each field.
        """
        chk = sum(payload_no_checksum) & 0xFF
        out = payload_no_checksum + bytes([chk])
        resp = self.send_command_raw(
            opcode,
            out_data=out,
            data_phase_out=True,
            data_phase_in=False,
        )
        resp.raise_for_status()

    def sigma_set_datagroup_2_pc_mode(self) -> None:
        """0x9017 ``SetDataGroup2`` — exact bytes from sigma-fp.txt init trace.

        Wire (5 bytes): ``03 04 00 04 0b``
            FieldPresent=0x03, then 3 bytes of data ``04 00 04``, chk=0x0b.
        """
        self.sigma_send_raw_setdatagroup(
            SigmaOperationCode.SET_CAM_DATA_GROUP_2,
            bytes([0x03, 0x04, 0x00, 0x04]),
        )

    def sigma_set_datagroup_1(self, values: dict[str, int]) -> None:
        """0x9016 ``SetCamDataGroup1`` — write any subset of DG1 fields.

        ``values`` is keyed by sigma-ptpy schema field names (e.g.
        ``{"ShutterSpeed": 0x70, "ISOSpeed": 0x28}``). The FieldPresent
        bitmask is derived from the keys so the camera only updates
        what's actually supplied. Wire format mirrors the fp init traces:
        ``_Header(0x03) + FP_BE + fields + sum_checksum``.
        """
        from fp_l_tether.camera.sigma_datagroup import build_set_datagroup1

        payload = build_set_datagroup1(values)
        self.sigma_send_raw_setdatagroup(
            SigmaOperationCode.SET_CAM_DATA_GROUP_1,
            payload,
        )

    def sigma_set_datagroup_2(self, values: dict[str, int]) -> None:
        """0x9017 ``SetCamDataGroup2`` — write any subset of DG2 fields.

        See ``sigma_set_datagroup_1`` for wire format details.
        """
        from fp_l_tether.camera.sigma_datagroup import build_set_datagroup2

        payload = build_set_datagroup2(values)
        self.sigma_send_raw_setdatagroup(
            SigmaOperationCode.SET_CAM_DATA_GROUP_2,
            payload,
        )

    def sigma_set_datagroup_3_pc_capture(self) -> None:
        """0x9018 ``SetDataGroup3`` — **the critical PC-capture-mode switch**.

        Wire (22 bytes including checksum), from sigma-fp.txt trace::

            03 00 80 02 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 85

        Byte 0 = FieldPresent1 = 0x03 (bits 0,1: set DestinationToSave + ???)
        Byte 1 = FieldPresent2 = 0x00
        Byte 2 = DestinationToSave = 0x80 (= "send image to PC AND keep on card")
        Byte 3 = ??? = 0x02
        Bytes 4-20 = zero-padding for unset fields
        Byte 21 = checksum = 0x85

        The fp trace calls this TWICE: once during init AND right before
        each snap. The pre-snap call appears to be what unlocks the camera
        for the next shutter trigger.
        """
        payload = bytes([
            0x03,  # FieldPresent1
            0x00,  # FieldPresent2
            0x80,  # DestinationToSave = PC + card
            0x02,  # ??? (matches trace, unidentified field)
        ]) + bytes(17)  # 17 zero padding bytes
        self.sigma_send_raw_setdatagroup(
            SigmaOperationCode.SET_CAM_DATA_GROUP_3,
            payload,
        )

    def sigma_set_datagroup_4_pc_mode(self) -> None:
        """0x9024 ``SetDataGroup4`` — exact bytes from sigma-fp.txt init trace.

        Wire (22 bytes): ``03 01 00 02 00 00 ... 00 06``
        """
        payload = bytes([0x03, 0x01, 0x00, 0x02]) + bytes(17)
        self.sigma_send_raw_setdatagroup(
            SigmaOperationCode.SET_CAM_DATA_GROUP_4,
            payload,
        )

    def sigma_set_datagroup_movie_pc_mode(self) -> None:
        """0x9034 ``SetCamDataGroupMovie`` — exact bytes from init trace.

        Wire (25 bytes): ``15 00 00 00 01 00 00 00 00 00 01 00 01 00 00 00 01 00 00 00 00 00 00 00 1f``
        """
        payload = bytes([
            0x15, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00,
            0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0x00,
            0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        ])
        self.sigma_send_raw_setdatagroup(
            SigmaOperationCode.SET_CAM_DATA_GROUP_MOVIE,
            payload,
        )

    def sigma_init(self, pc_capture_mode: bool = True) -> dict[str, bytes]:
        """Run the Sigma fp/fp L init sequence.

        With ``pc_capture_mode=True`` (default), runs the FULL sequence
        from the libgphoto2 reverse-engineered fp trace
        (``cameras/sigma-fp.txt``). This includes the SET commands that
        switch the camera into "PC capture mode" — without these, only
        the first snap fires.

        With ``pc_capture_mode=False``, runs only the 10 GET calls that
        libgphoto2's own ``camera_init`` performs (which is insufficient
        for repeated capture but matches libgphoto2 for diagnostics).

        Returns a dict of step-name → raw response bytes (set commands
        return empty bytes since they have no data-in phase).
        """
        results: dict[str, bytes] = {}

        # Step 1: GetCameraInfo (0x9035) — Sigma's "ConfigApi" handshake
        results["camera_info"] = self.sigma_get_camera_info()
        logger.debug("sigma_init: camera_info %d bytes", len(results["camera_info"]))

        # NEW from fp trace: SetDataGroup2 with hard-coded init bytes
        if pc_capture_mode:
            self.sigma_set_datagroup_2_pc_mode()
            results["set_datagroup_2"] = b""
            logger.debug("sigma_init: SetDataGroup2 (PC mode bytes)")

        # Steps 2-4: DataGroup 1-3
        for g in (1, 2, 3):
            data = self.sigma_get_datagroup(g)
            results[f"datagroup_{g}"] = data
            logger.debug("sigma_init: datagroup_%d %d bytes", g, len(data))

        # NEW from fp trace: SetDataGroup3 (PC capture mode!), SetDataGroup4,
        # SetCamDataGroupMovie
        if pc_capture_mode:
            self.sigma_set_datagroup_3_pc_capture()
            results["set_datagroup_3"] = b""
            logger.debug("sigma_init: SetDataGroup3 (DestinationToSave=PC+card)")

            self.sigma_set_datagroup_4_pc_mode()
            results["set_datagroup_4"] = b""
            logger.debug("sigma_init: SetDataGroup4 (PC mode bytes)")

            try:
                self.sigma_set_datagroup_movie_pc_mode()
                results["set_datagroup_movie"] = b""
                logger.debug("sigma_init: SetCamDataGroupMovie (PC mode bytes)")
            except (PTPError, USBBridgeError) as e:
                # Some firmware variants may reject this; movie isn't critical
                # for still capture, so keep going.
                logger.warning(
                    "sigma_init: SetCamDataGroupMovie failed (continuing): %s", e
                )

        # Step 5: GetCamDataGroupMovie (0x9033)
        results["datagroup_movie"] = self.sigma_get_cam_datagroup_movie()
        logger.debug("sigma_init: datagroup_movie %d bytes",
                     len(results["datagroup_movie"]))

        # Step 6: GetCamCanSetInfo5 (0x9030)
        results["can_set_info_5"] = self.sigma_get_cam_can_set_info_5()
        logger.debug("sigma_init: can_set_info_5 %d bytes",
                     len(results["can_set_info_5"]))

        # NEW from fp trace: GetCamStatus2 after init
        if pc_capture_mode:
            try:
                results["cam_status_2"] = self.sigma_get_cam_status_2()
                logger.debug(
                    "sigma_init: cam_status_2 %d bytes",
                    len(results["cam_status_2"]),
                )
            except (PTPError, USBBridgeError) as e:
                logger.warning("sigma_init: GetCamStatus2 failed (continuing): %s", e)

        # Steps 7-9: DataGroup 4-6 (the fp trace re-queries these after init SETs)
        for g in (4, 5, 6):
            data = self.sigma_get_datagroup(g)
            results[f"datagroup_{g}"] = data
            logger.debug("sigma_init: datagroup_%d %d bytes", g, len(data))

        # Step 10: GetCamDataGroupFocus (0x9031)
        results["datagroup_focus"] = self.sigma_get_cam_datagroup_focus()
        logger.debug("sigma_init: datagroup_focus %d bytes",
                     len(results["datagroup_focus"]))

        return results

    def sigma_get_capture_status(self, p1: int = 0) -> SgmCaptStatus:
        """0x9015 ``GetCaptureStatus``.

        Returns the parsed 7-byte response struct. Pass ``p1=0`` for the
        "current" status (used by both pre-snap and polling).
        """
        resp = self.send_command_raw(
            SigmaOperationCode.GET_CAM_CAPT_STATUS, params=(p1,)
        )
        resp.raise_for_status()
        return SgmCaptStatus.from_libgphoto2_wire(resp.in_data)

    def sigma_snap(self, mode: int = 1, amount: int = 1) -> None:
        """0x901b ``Snap`` — fire the shutter.

        Wire format (CONFIRMED from libgphoto2 ptp.c lines 1272-1285):

            4 bytes outdata = [0x02, mode, amount, 0x02+mode+amount]

        The leading 0x02 is the "num data bytes following" byte (part of
        Sigma's envelope, NOT a PTP length prefix). The trailing byte is
        the running checksum.

        Defaults ``mode=1, amount=1`` match libgphoto2's call site.

        ⚠️ Earlier libusb implementation in this project sent 3 bytes
        ``[mode, amount, checksum]`` and the camera fired (audible click)
        but the image was not properly tracked in ImageDB. This 4-byte
        form is the wire-correct version.
        """
        chk = (0x02 + mode + amount) & 0xFF
        out = bytes([0x02, mode & 0xFF, amount & 0xFF, chk])
        logger.debug("sigma_snap: sending %s", out.hex())
        resp = self.send_command_raw(
            SigmaOperationCode.SNAP_COMMAND,
            out_data=out,
            data_phase_out=True,
            data_phase_in=False,
        )
        resp.raise_for_status()

    def sigma_get_pict_file_info_2(self) -> SigmaFpPictFileInfo2Ex:
        """0x902d ``GetPictFileInfo2`` — file metadata after a successful snap.

        Returns the parsed struct (fileaddress, filesize, name, ext, dims).
        """
        resp = self.send_command_raw(SigmaOperationCode.GET_PICT_FILE_INFO_2)
        resp.raise_for_status()
        return SigmaFpPictFileInfo2Ex.from_wire(resp.in_data)

    def sigma_get_big_partial_pict_file(
        self,
        fileaddress: int,
        offset: int,
        insize: int,
        timeout_ms: int | None = None,
    ) -> bytes:
        """0x9022 ``GetBigPartialPictFile`` — download file bytes.

        Params: (fileaddress, offset, insize) as 3 uint32 command params.
        The response is the requested file slice.

        ⚠️ The camera prepends a 4-byte length header that must be
        stripped. libgphoto2 does: ``gp_file_append(file, data+4, size-4)``.
        This method handles that — return value is the actual file bytes.
        """
        resp = self.send_command_raw(
            SigmaOperationCode.GET_BIG_PARTIAL_PICT_FILE,
            params=(fileaddress, offset, insize),
            timeout_ms=timeout_ms or self.DOWNLOAD_TIMEOUT_MS,
        )
        resp.raise_for_status()
        if len(resp.in_data) < 4:
            raise USBBridgeError(
                f"GetBigPartialPictFile response too short: {len(resp.in_data)} bytes"
            )
        return resp.in_data[4:]

    def sigma_clear_image_db_single(self, image_id: int) -> None:
        """0x901c ``ClearImageDBSingle`` — remove one image from camera DB.

        Wire format (CONFIRMED from libgphoto2 ptp.c lines 1287-1295):
          - Command param 1: image_id (uint32)
          - Out data: 8 zero bytes

        Called after each successful download so the camera knows the
        image has been collected.

        ⚠️ For fp L: pass ``image_db_head`` from the status struct, NOT
        ``image_id``. The latter is always 0 on fp L (firmware quirk) and
        clearing 0 is a no-op, leaving the camera "image pending" state
        that blocks subsequent snaps. libgphoto2 uses image_id and likely
        has the same multi-shot bug.
        """
        out = bytes(8)  # 8 zero bytes
        resp = self.send_command_raw(
            SigmaOperationCode.CLEAR_IMAGE_DB_SINGLE,
            params=(image_id,),
            out_data=out,
            data_phase_out=True,
            data_phase_in=False,
        )
        resp.raise_for_status()

    def sigma_clear_image_db_all(self) -> None:
        """0x901d ``ClearImageDBAll`` — wipe all images from camera DB.

        Use as a heavy reset between shots if ClearImageDBSingle
        doesn't fully unblock the camera. Wire format unconfirmed —
        assume same envelope as ClearImageDBSingle (no params, 8 zero bytes).
        """
        out = bytes(8)
        resp = self.send_command_raw(
            SigmaOperationCode.CLEAR_IMAGE_DB_ALL,
            out_data=out,
            data_phase_out=True,
            data_phase_in=False,
        )
        resp.raise_for_status()

    def sigma_wait_for_shot(
        self,
        slot: int,
        poll_interval_s: float = 0.1,
        timeout_s: float | None = None,
    ) -> SgmCaptStatus | None:
        """Poll GetCaptureStatus(p1=slot) until status reaches success.

        Used by the daemon to detect shots triggered by the **camera's
        physical shutter button** (no PC-side Snap command issued).

        Returns the post-shot SgmCaptStatus on success, or ``None`` on
        timeout (``timeout_s`` reached). Raises ``USBBridgeError`` on
        protocol error or a failure status code from the camera.

        ``slot`` should be the camera's current ``image_db_head`` value —
        that's where the next new image will land.

        Loop is short-sleep so a manual shutter press is detected within
        ~100 ms (default).
        """
        deadline = (time.monotonic() + timeout_s) if timeout_s else None
        while True:
            status = self.sigma_get_capture_status(slot)
            if (status.capt_status & 0xF000) == 0x6000:
                if status.capt_status == 0x6001:
                    raise USBBridgeError("Capture failed: no focus (0x6001)")
                raise USBBridgeError(
                    f"Capture failed with status 0x{status.capt_status:04X}"
                )
            if status.capt_status in (0x0002, 0x0005):
                return status
            if deadline and time.monotonic() >= deadline:
                return None
            time.sleep(poll_interval_s)

    def sigma_download_current(
        self,
        post_status: SgmCaptStatus,
        clear_strategy: str = "image_db_head",
    ) -> tuple[SigmaFpPictFileInfo2Ex, bytes]:
        """Download the image currently waiting in the camera, then clear it.

        Used after either ``sigma_wait_for_shot`` (camera-triggered) or
        an explicit ``sigma_snap`` (PC-triggered). Returns
        ``(file_info, file_bytes)``.

        ``post_status`` is the success-time SgmCaptStatus (used for
        clearing the right slot).
        """
        info = self.sigma_get_pict_file_info_2()
        logger.info(
            "sigma_download: file %s%s addr=0x%X size=%d (%dx%d)",
            info.name, info.fileext, info.fileaddress,
            info.filesize, info.width, info.height,
        )

        data = self.sigma_get_big_partial_pict_file(
            info.fileaddress, 0, info.filesize
        )
        if len(data) != info.filesize:
            logger.debug(
                "sigma_download: size mismatch got=%d expected=%d "
                "(USB ZLP padding, harmless for JPEG)",
                len(data), info.filesize,
            )

        # Clear from camera DB
        if clear_strategy == "none":
            logger.debug("sigma_download: skipping clear (clear_strategy=none)")
        elif clear_strategy == "all":
            self.sigma_clear_image_db_all()
            logger.debug("sigma_download: cleared ALL")
        else:
            clear_id_map = {
                "image_id": post_status.image_id,
                "image_db_head": post_status.image_db_head,
                "image_db_tail": post_status.image_db_tail,
            }
            clear_id = clear_id_map[clear_strategy]
            self.sigma_clear_image_db_single(clear_id)
            logger.debug(
                "sigma_download: cleared id=0x%02X (strategy=%s)",
                clear_id, clear_strategy,
            )

        return info, data

    def sigma_capture_one(
        self,
        mode: int = 2,
        amount: int = 1,
        max_poll_iterations: int = 50,
        poll_interval_s: float = 0.2,
        clear_strategy: str = "image_db_head",
        pre_snap_pc_setup: bool = True,
    ) -> tuple[SigmaFpPictFileInfo2Ex, bytes]:
        """Full single-shot capture + download cycle.

        Based on libgphoto2's reverse-engineered ``cameras/sigma-fp.txt``
        trace, with extensions for fp L (per empirical observation that
        a SET command is required before each snap to maintain PC capture
        state across multiple shots).

        Steps:

          0. *(NEW)* SetDataGroup3 with PC-capture-mode bytes (the magic
             that re-arms the camera for the next shot).
          1. GetCaptureStatus (pre-snap, for logging).
          2. Snap(mode, amount).  Default mode=2 (NON_AF_CAPTURE per Sigma
             SDK headers and matching the fp trace). libgphoto2 uses mode=1
             which works for the very first shot but breaks subsequent ones.
          3. Poll GetCaptureStatus up to ``max_poll_iterations × poll_interval``:
             - ``status & 0xf000 == 0x6000`` → failure (``0x6001`` = no focus)
             - ``status == 0x0002`` → success
             - ``status == 0x0005`` → image data ready
             - other → continue
          4. GetPictFileInfo2 → (fileaddress, filesize, name, ext, dims).
          5. GetBigPartialPictFile(fileaddress, 0, filesize) → file bytes.
          6. ClearImageDBSingle (id source chosen by ``clear_strategy``).

        Returns ``(file_info, file_bytes)``.

        Raises:
          USBBridgeError on protocol / transport errors.
          PTPError if any PTP step returns non-OK.
          TimeoutError if status never reaches success.
        """
        # NEW Step 0 — pre-snap PC capture mode re-arm
        if pre_snap_pc_setup:
            try:
                self.sigma_set_datagroup_3_pc_capture()
                logger.debug("sigma_capture: pre-snap SetDataGroup3 (PC mode)")
            except (PTPError, USBBridgeError) as e:
                logger.warning(
                    "sigma_capture: pre-snap SetDataGroup3 failed: %s", e
                )

        pre_status = self.sigma_get_capture_status(0)
        logger.info(
            "sigma_capture: pre-snap status=0x%04X image_id=0x%02X "
            "db_head=0x%02X db_tail=0x%02X dest=0x%02X",
            pre_status.capt_status, pre_status.image_id,
            pre_status.image_db_head, pre_status.image_db_tail,
            pre_status.destination_to_save,
        )

        self.sigma_snap(mode=mode, amount=amount)
        logger.info("sigma_capture: snap fired (mode=%d, amount=%d)", mode, amount)

        # ⚡ KEY INSIGHT — poll the slot the NEW image will land in, not slot 0.
        #
        # Per the fp trace ``cameras/sigma-fp.txt`` lines 138-167, the
        # GetCaptureStatus opcode takes a slot index as parameter 1, and
        # returns the status for THAT specific slot. The init sequence polls
        # all 29 slots (0x0..0x1c). After a snap, the new image lands in
        # the slot pointed to by the camera's internal db_head pointer —
        # which is the same as pre-snap db_head (slot 0 for the first shot,
        # 1 for the second, etc.).
        #
        # Polling slot 0 for every shot is what broke fp L multi-capture:
        # slot 0 was correctly drained, but the new image went into slot 1,
        # and slot 0's status stayed 0x0000 forever.
        target_slot = pre_status.image_db_head
        logger.debug(
            "sigma_capture: polling slot 0x%02X (= pre-snap db_head)",
            target_slot,
        )

        # Poll for capture completion
        post_status: SgmCaptStatus | None = None
        for i in range(max_poll_iterations):
            status = self.sigma_get_capture_status(target_slot)
            logger.debug(
                "sigma_capture: poll %d/%d slot=0x%02X status=0x%04X "
                "image_id=0x%02X db_head=0x%02X db_tail=0x%02X",
                i + 1, max_poll_iterations, target_slot,
                status.capt_status, status.image_id,
                status.image_db_head, status.image_db_tail,
            )
            if (status.capt_status & 0xF000) == 0x6000:
                if status.capt_status == 0x6001:
                    raise USBBridgeError("Capture failed: no focus (0x6001)")
                raise USBBridgeError(
                    f"Capture failed with status 0x{status.capt_status:04X}"
                )
            if status.capt_status in (0x0002, 0x0005):
                post_status = status
                logger.info(
                    "sigma_capture: success after %d polls "
                    "(slot=0x%02X status=0x%04X image_id=0x%02X "
                    "db_head=0x%02X db_tail=0x%02X)",
                    i + 1, target_slot, status.capt_status, status.image_id,
                    status.image_db_head, status.image_db_tail,
                )
                break
            time.sleep(poll_interval_s)
        else:
            raise TimeoutError(
                f"capture status never reached success on slot 0x{target_slot:02X} "
                f"after {max_poll_iterations * poll_interval_s}s "
                f"(shutter may have fired but image landed elsewhere)"
            )

        assert post_status is not None

        # File info + download
        info = self.sigma_get_pict_file_info_2()
        logger.info(
            "sigma_capture: file %s%s addr=0x%X size=%d (%dx%d)",
            info.name, info.fileext, info.fileaddress,
            info.filesize, info.width, info.height,
        )

        data = self.sigma_get_big_partial_pict_file(
            info.fileaddress, 0, info.filesize
        )
        if len(data) != info.filesize:
            logger.warning(
                "sigma_capture: download size mismatch: got %d, expected %d",
                len(data), info.filesize,
            )

        # Clear from camera DB so next snap can proceed.
        # The "id" passed to ClearImageDBSingle depends on firmware:
        #   - libgphoto2 passes image_id (always 0 on fp L → no-op bug)
        #   - For fp L, image_db_head appears to be the actual slot index.
        # The ``clear_strategy`` selects which value to use.
        if clear_strategy == "none":
            logger.info("sigma_capture: skipping clear (clear_strategy=none)")
        elif clear_strategy == "all":
            self.sigma_clear_image_db_all()
            logger.info("sigma_capture: cleared ALL images from camera DB")
        else:
            clear_id_map = {
                "image_id": post_status.image_id,
                "image_db_head": post_status.image_db_head,
                "image_db_tail": post_status.image_db_tail,
            }
            if clear_strategy not in clear_id_map:
                raise ValueError(
                    f"clear_strategy must be one of "
                    f"image_id|image_db_head|image_db_tail|all|none, "
                    f"got {clear_strategy!r}"
                )
            clear_id = clear_id_map[clear_strategy]
            self.sigma_clear_image_db_single(clear_id)
            logger.info(
                "sigma_capture: cleared id=0x%02X from camera DB "
                "(strategy=%s, full status: image_id=0x%02X db_head=0x%02X db_tail=0x%02X)",
                clear_id, clear_strategy,
                post_status.image_id, post_status.image_db_head,
                post_status.image_db_tail,
            )

        return info, data

    # ----- Sigma-specific wrapper -------------------------------------

    def send_sigma_command(
        self,
        opcode: int,
        params: tuple[int, ...] = (),
        sigma_payload: bytes | None = None,
        data_phase_in: bool = True,
        timeout_ms: int | None = None,
        wrap_with_length_prefix: bool = False,
    ) -> "USBBridge.Response":
        """Send a Sigma vendor-specific PTP command with proper framing.

        The OUT data phase format for Sigma fp L (verified 2026-05-11):

            sigma_payload + <uint8 external_checksum>

        where ``external_checksum = sum(sigma_payload) & 0xFF``.

        IMPORTANT: We do NOT prepend a 4-byte length prefix to the OUT data.
        The earlier hypothesis based on libgphoto2 #882 Windows SDK trace was
        wrong — the leading ``00 00 00 00 04 00 00 00`` bytes in that trace
        are ICAPTPPassThroughPB internal metadata, not actual on-wire bytes.

        For the rare op that DOES need a length-prefixed OUT data, pass
        ``wrap_with_length_prefix=True``.

        The returned ``in_data`` (data IN from camera) is op-specific:
            - Variable-length ops (ConfigApi, GetCamOpPermission, …):
              ``<uint32 length> <payload> <uint8 checksum>``
              → use ``unwrap_sigma_payload()``.
            - Fixed-length ops (GetCamCaptStatus, GetCamDataGroupN, …):
              ``<N struct bytes> <uint8 checksum>``
              → use ``unwrap_sigma_fixed_struct()`` or the struct's
              ``.from_wire()`` classmethod.
        """
        out_data = None
        data_phase_out = False
        if sigma_payload is not None:
            chk = sigma_checksum(sigma_payload)
            body = sigma_payload + bytes([chk])
            if wrap_with_length_prefix:
                out_data = struct.pack("<I", len(body)) + body
            else:
                out_data = body
            data_phase_out = True

        return self.send_command_raw(
            opcode,
            params=params,
            out_data=out_data,
            data_phase_out=data_phase_out,
            data_phase_in=data_phase_in,
            timeout_ms=timeout_ms,
        )


# ---------------------------------------------------------------------------
# Sigma payload helpers
# ---------------------------------------------------------------------------


def unwrap_sigma_payload(data: bytes) -> bytes:
    """Strip Sigma's variable-length wrapping: ``<uint32 length LE> <payload> <uint8 checksum>``.

    The checksum byte sits AFTER the ``length`` bytes of payload (not inside).
    Total wire size = 4 + length + 1 = length + 5.

    Verified against actual fp L responses (2026-05-11):
      - GetCamOpPermission 25-byte wire: length=20, payload=20B, checksum=1B
      - ConfigApi 81-byte wire: length=76, payload=76B, checksum=1B

    Used for VARIABLE-length Sigma responses (ConfigApi, GetCamOpPermission,
    GetCamCanSetInfoN). For FIXED-length structs like SgmCaptStatus, use the
    dedicated parser (``SgmCaptStatus.from_wire``) which expects
    ``<N struct bytes> <1 checksum byte>`` with no length prefix.

    Returns the payload bytes only. Logs a warning on checksum mismatch but
    still returns the payload.

    Raises ValueError if the wrapping is malformed (truncated, no room).
    """
    if len(data) < 5:
        raise ValueError(f"Sigma payload too short: {len(data)} bytes")
    declared = struct.unpack("<I", data[:4])[0]
    end_of_payload = 4 + declared
    end_with_chk = end_of_payload + 1
    if end_with_chk > len(data):
        raise ValueError(
            f"Sigma payload truncated: declared length {declared}, "
            f"need {end_with_chk} bytes, have {len(data)}"
        )
    payload = data[4:end_of_payload]
    chk = data[end_of_payload]
    # Sigma's wrapping checksum sums the length prefix + payload bytes
    expected = (sum(data[:end_of_payload])) & 0xFF
    if chk != expected:
        logger.warning(
            "Sigma checksum mismatch: got 0x%02X, expected 0x%02X (declared=%d, payload_hex=%s)",
            chk, expected, declared, payload.hex(),
        )
    return payload


def unwrap_sigma_fixed_struct(data: bytes, struct_size: int) -> bytes:
    """Strip the 1-byte trailing checksum from a fixed-size Sigma response.

    Wire format: ``<struct_size bytes of struct> <1-byte external checksum>``.
    Used for GetCamCaptStatus, GetCamDataGroupN, etc. — anything that has a
    fixed-size data phase with no outer length prefix.

    Returns the ``struct_size`` payload bytes. Verifies that
    ``sum(struct_bytes) & 0xFF == checksum``.
    """
    needed = struct_size + 1
    if len(data) < needed:
        raise ValueError(
            f"Fixed-size Sigma struct needs {needed} bytes, got {len(data)}"
        )
    struct_bytes = data[:struct_size]
    chk = data[struct_size]
    expected = sum(struct_bytes) & 0xFF
    if chk != expected:
        logger.warning(
            "Sigma checksum mismatch for fixed struct (size=%d): "
            "got 0x%02X, expected 0x%02X (struct_hex=%s)",
            struct_size, chk, expected, struct_bytes.hex(),
        )
    return struct_bytes


def utc_now_iso() -> str:
    """Helper for log timestamps."""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())

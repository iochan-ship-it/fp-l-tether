"""Unit tests for fp_l_tether.camera.ptp_codes.

These tests run on any platform — they don't touch ImageCaptureCore.
"""

from __future__ import annotations

import pytest

from fp_l_tether.camera.ptp_codes import (
    SIGMA_FP_L_PRODUCT_ID,
    SIGMA_VENDOR_ID,
    CaptStatusCode,
    PTPResponseCode,
    SgmCaptStatus,
    SgmSnapState,
    SigmaOperationCode,
    SnapCaptureMode,
    sigma_checksum,
    verify_checksum,
)


class TestUSBIdentifiers:
    def test_sigma_vendor_id(self):
        assert SIGMA_VENDOR_ID == 0x1003

    def test_sigma_fp_l_product_id(self):
        # Verified in libgphoto2 #882 lsusb output
        assert SIGMA_FP_L_PRODUCT_ID == 0xC442


class TestOpcodes:
    """Critical opcodes — values verified against SDK headers."""

    def test_snap_command_opcode(self):
        assert SigmaOperationCode.SNAP_COMMAND == 0x901B

    def test_get_cam_capt_status_opcode(self):
        assert SigmaOperationCode.GET_CAM_CAPT_STATUS == 0x9015

    def test_get_big_partial_pict_file_opcode(self):
        assert SigmaOperationCode.GET_BIG_PARTIAL_PICT_FILE == 0x9022

    def test_clear_image_db_single_opcode(self):
        assert SigmaOperationCode.CLEAR_IMAGE_DB_SINGLE == 0x901C

    def test_get_pict_file_info_2_opcode(self):
        assert SigmaOperationCode.GET_PICT_FILE_INFO_2 == 0x902D

    def test_config_api_opcode(self):
        assert SigmaOperationCode.CONFIG_API == 0x9035


class TestSigmaChecksum:
    """Verify the checksum algorithm against the libgphoto2 #882 trace."""

    def test_snap_payload_checksum(self):
        # From the Windows SDK log in libgphoto2 #882:
        #   payload [0x02, 0x02, 0x01] → CheckSum 0x05
        assert sigma_checksum(bytes([0x02, 0x02, 0x01])) == 0x05

    def test_empty_payload(self):
        assert sigma_checksum(b"") == 0x00

    def test_overflow_wraps(self):
        # 0xFF + 0x01 = 0x100 → 0x00 mod 256
        assert sigma_checksum(bytes([0xFF, 0x01])) == 0x00

    def test_verify_checksum_helper(self):
        assert verify_checksum(b"\x02\x02\x01", 0x05) is True
        assert verify_checksum(b"\x02\x02\x01", 0x06) is False


class TestSgmSnapState:
    def test_single_still_shot_payload(self):
        """Default payload should be 3 bytes for 1 still shot with checksum."""
        state = SgmSnapState(
            capture_mode=SnapCaptureMode.NON_AF_CAPTURE,
            capture_amount=1,
        )
        b = state.to_bytes()
        assert len(b) == 3
        assert b[0] == 0x02  # CaptureMode = NON_AF_CAPTURE
        assert b[1] == 0x01  # CaptureAmount = 1
        assert b[2] == 0x03  # checksum = 0x02 + 0x01

    def test_burst_payload(self):
        """5-shot burst."""
        state = SgmSnapState(capture_mode=0x02, capture_amount=5)
        b = state.to_bytes()
        assert b == bytes([0x02, 0x05, 0x07])

    def test_wire_outdata_has_length_prefix(self):
        """to_wire_outdata() prepends a 4-byte LE length to to_bytes()."""
        state = SgmSnapState(
            capture_mode=SnapCaptureMode.NON_AF_CAPTURE,
            capture_amount=1,
        )
        wire = state.to_wire_outdata()
        # Length prefix = 3 (raw payload is 3 bytes)
        assert wire[:4] == bytes([0x03, 0x00, 0x00, 0x00])
        assert wire[4:] == bytes([0x02, 0x01, 0x03])
        assert len(wire) == 7


class TestSgmCaptStatus:
    def test_parse_known_response(self):
        """Parse the response from libgphoto2 #882 Windows log."""
        # Bytes from log: 06 00 00 01 04 00 with checksum 0B (after status code)
        # Layout: image_id, db_head, db_tail, capt_status_lo, capt_status_hi, dest, checksum
        data = bytes([0x06, 0x00, 0x00, 0x01, 0x00, 0x04, 0x00])
        status = SgmCaptStatus.from_bytes(data)
        assert status.image_id == 0x06
        assert status.image_db_head == 0x00
        assert status.image_db_tail == 0x00
        assert status.capt_status == 0x0001
        assert status.destination_to_save == 0x04

    def test_too_short_raises(self):
        with pytest.raises(ValueError):
            SgmCaptStatus.from_bytes(bytes([0x06, 0x00]))

    def test_has_new_image_when_storage_complete(self):
        # ImageID nonzero + capt_status == IMAGE_DATA_STORAGE_COMPLETE → True
        data = bytes(
            [
                0x01,  # image_id
                0x00,  # db_head
                0x00,  # db_tail
                0x06,
                0x00,  # capt_status = IMAGE_DATA_STORAGE_COMPLETE (0x0006)
                0x00,
                0x00,
            ]
        )
        status = SgmCaptStatus.from_bytes(data)
        assert status.has_new_image is True

    def test_has_no_new_image_when_image_id_zero(self):
        data = bytes([0x00, 0x00, 0x00, 0x06, 0x00, 0x00, 0x00])
        status = SgmCaptStatus.from_bytes(data)
        assert status.has_new_image is False

    def test_is_capturing(self):
        data = bytes(
            [
                0x01,
                0x00,
                0x00,
                CaptStatusCode.SHOOTING_IN_PROGRESS & 0xFF,
                (CaptStatusCode.SHOOTING_IN_PROGRESS >> 8) & 0xFF,
                0x00,
                0x00,
            ]
        )
        status = SgmCaptStatus.from_bytes(data)
        assert status.is_capturing is True

    def test_from_wire_real_capture(self):
        """Parse the actual 8-byte wire form captured from fp L on 2026-05-11.

        Wire: 06 00 00 00 00 00 00 06

        Per the fp trace (libgphoto2 ``cameras/sigma-fp.txt``) the response
        is 8 bytes (not 7 as libgphoto2's own parser assumes):

            data[0] = 0x06       length marker
            data[1] = imageid    = 0
            data[2] = imagedbhead = 0
            data[3] = imagedbtail = 0
            data[4..5] = capt_status (uint16 LE) = 0x0000
            data[6] = dest_to_save = 0
            data[7] = checksum = 0x06 = sum(data[0..6]) & 0xFF
        """
        wire = bytes([0x06, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x06])
        status = SgmCaptStatus.from_wire(wire)
        assert status.image_id == 0x00
        assert status.image_db_head == 0x00
        assert status.image_db_tail == 0x00
        assert status.capt_status == 0x0000
        assert status.destination_to_save == 0x00
        assert status.checksum == 0x06  # byte 7

    def test_from_wire_libgphoto2_trace(self):
        """Parse the wire form seen in libgphoto2 #882 Windows SDK trace.

        Wire: 06 00 00 01 04 00 00 0B
          data[0] = 0x06 length
          data[1] = imageid = 0
          data[2] = imagedbhead = 0
          data[3] = imagedbtail = 1
          data[4..5] = status (uint16 LE) = 0x0004
          data[6] = destination = 0
          data[7] = checksum = 0x0B = 6+0+0+1+4+0+0 ✓
        """
        wire = bytes([0x06, 0x00, 0x00, 0x01, 0x04, 0x00, 0x00, 0x0B])
        status = SgmCaptStatus.from_wire(wire)
        assert status.image_id == 0x00
        assert status.image_db_tail == 0x01
        assert status.capt_status == 0x0004
        assert status.destination_to_save == 0x00
        assert status.checksum == 0x0B

    def test_from_fp_trace_wire_actual_capture(self):
        """Real wire from fp trace at moment of capture success.

        Wire (from cameras/sigma-fp.txt line ~205):
            06 00 00 01 05 00 02 0e
          → length=6, id=0, head=0, tail=1, status=0x0005,
            dest=0x02, chk=0x0e (= 6+0+0+1+5+0+2 ✓)
        """
        wire = bytes([0x06, 0x00, 0x00, 0x01, 0x05, 0x00, 0x02, 0x0E])
        status = SgmCaptStatus.from_fp_trace_wire(wire)
        assert status.image_id == 0x00
        assert status.image_db_head == 0x00
        assert status.image_db_tail == 0x01
        assert status.capt_status == 0x0005  # IMAGE_DB_NOT_EMPTY (ready to download)
        assert status.destination_to_save == 0x02
        assert status.checksum == 0x0E

    def test_from_libgphoto2_wire_explicit(self):
        """Direct call to the new wire parser with synthetic success-state data."""
        # Camera state: image 0x42 captured, status=0x0005 (image ready), dest=0
        wire = bytes([0x06, 0x42, 0x00, 0x00, 0x05, 0x00, 0xFF])
        status = SgmCaptStatus.from_libgphoto2_wire(wire)
        assert status.image_id == 0x42
        assert status.capt_status == 0x0005
        assert status.destination_to_save == 0x00


class TestResponseCodes:
    def test_ok(self):
        assert PTPResponseCode.OK == 0x2001

    def test_general_error(self):
        assert PTPResponseCode.GENERAL_ERROR == 0x2002

    def test_operation_not_supported(self):
        assert PTPResponseCode.OPERATION_NOT_SUPPORTED == 0x2005

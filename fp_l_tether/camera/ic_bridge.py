"""ImageCaptureCore bridge for Sigma fp / fp L.

Wraps PyObjC bindings to Apple's ImageCaptureCore framework. Provides:

  * ``CameraBrowser`` — enumerates connected cameras (Phase 0-A)
  * ``Camera`` — represents a single opened camera session (Phase 0-B onward)
  * ``send_ptp(opcode, data)`` — sends a raw PTP pass-through command

This module deliberately uses **synchronous, blocking** semantics on top of the
inherently asynchronous ICCameraDevice API. The delegate callbacks are bridged
to ``threading.Event`` + result queues so callers don't need to think in
NSRunLoop terms.

Why this design:
  * Phase 0/1.0 needs simplicity over throughput.
  * The shutter polling loop is at most ~10 Hz; no need for async.
  * Pure Python tests can mock ``Camera`` without an NSRunLoop.

References:
  * https://developer.apple.com/documentation/imagecapturecore
  * https://pyobjc.readthedocs.io/en/latest/apinotes/ImageCaptureCore.html
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

# These imports require macOS. On Linux / CI we expose stubs so the module
# can still be imported for type checking. The actual functions raise at use.
try:
    import objc  # type: ignore[import-not-found]
    from Foundation import (  # type: ignore[import-not-found]
        NSObject,
        NSRunLoop,
        NSDate,
        NSData,
    )
    import ImageCaptureCore as ICC  # type: ignore[import-not-found]

    _PYOBJC_AVAILABLE = True
except ImportError:  # pragma: no cover — non-macOS fallback for tooling
    _PYOBJC_AVAILABLE = False

    class NSObject:  # type: ignore[no-redef]
        pass


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class DiscoveredCamera:
    """A camera reported by ICDeviceBrowser."""

    name: str
    vendor_id: int | None
    product_id: int | None
    serial_number: str
    transport_type: str
    can_accept_ptp_commands: bool
    can_take_picture: bool
    ic_device_handle: Any = field(default=None, repr=False)
    """The underlying ICCameraDevice object (kept opaque)."""

    @property
    def is_sigma_fp_family(self) -> bool:
        from .ptp_codes import (
            SIGMA_VENDOR_ID,
            SIGMA_FP_L_PRODUCT_ID,
            SIGMA_FP_PRODUCT_ID,
        )

        return self.vendor_id == SIGMA_VENDOR_ID and self.product_id in (
            SIGMA_FP_PRODUCT_ID,
            SIGMA_FP_L_PRODUCT_ID,
        )

    @property
    def is_fp_l(self) -> bool:
        from .ptp_codes import SIGMA_VENDOR_ID, SIGMA_FP_L_PRODUCT_ID

        return self.vendor_id == SIGMA_VENDOR_ID and self.product_id == SIGMA_FP_L_PRODUCT_ID


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ICBridgeError(RuntimeError):
    pass


class MacOSOnlyError(ICBridgeError):
    """Raised when a macOS-only function is called on a non-Mac platform."""


def _require_pyobjc() -> None:
    if not _PYOBJC_AVAILABLE:
        raise MacOSOnlyError(
            "This function requires macOS with PyObjC ImageCaptureCore installed. "
            "Install with: pip install pyobjc-framework-ImageCaptureCore"
        )


# ---------------------------------------------------------------------------
# CameraBrowser — Phase 0-A
# ---------------------------------------------------------------------------


def _spin_runloop(seconds: float) -> None:
    """Run the current thread's NSRunLoop for ``seconds`` seconds.

    Required so ICDeviceBrowser delegate callbacks can fire.
    """
    _require_pyobjc()
    deadline = NSDate.dateWithTimeIntervalSinceNow_(seconds)
    NSRunLoop.currentRunLoop().runUntilDate_(deadline)


class _BrowserDelegate(NSObject if _PYOBJC_AVAILABLE else object):
    """ICDeviceBrowserDelegate that collects discovered devices."""

    def init(self):  # noqa: N802 — Obj-C naming
        if _PYOBJC_AVAILABLE:
            self = objc.super(_BrowserDelegate, self).init()
        if self is None:
            return None
        self._devices: list[Any] = []
        self._lock = threading.Lock()
        self._finished = threading.Event()
        return self

    # ICDeviceBrowserDelegate methods
    def deviceBrowser_didAddDevice_moreComing_(self, browser, device, moreComing):  # noqa: N802,E501
        logger.debug("deviceBrowser:didAddDevice: %s moreComing=%s", device, moreComing)
        with self._lock:
            self._devices.append(device)
        if not moreComing:
            self._finished.set()

    def deviceBrowser_didRemoveDevice_moreGoing_(self, browser, device, moreGoing):  # noqa: N802,E501
        logger.debug("deviceBrowser:didRemoveDevice: %s", device)
        with self._lock:
            try:
                self._devices.remove(device)
            except ValueError:
                pass

    def snapshot(self) -> list[Any]:
        with self._lock:
            return list(self._devices)


def list_cameras(timeout_seconds: float = 3.0) -> list[DiscoveredCamera]:
    """Enumerate all connected cameras via ICDeviceBrowser.

    Args:
        timeout_seconds: How long to wait for the browser to settle. 3s is
            usually enough; bump to 10s if the camera was just plugged in.

    Returns:
        A list of DiscoveredCamera. May be empty if no camera is connected.

    Raises:
        MacOSOnlyError: when called from non-macOS or PyObjC missing.
    """
    _require_pyobjc()

    browser = ICC.ICDeviceBrowser.alloc().init()
    delegate = _BrowserDelegate.alloc().init()
    browser.setDelegate_(delegate)

    # macOS 14 (Sonoma) and later require a combined mask:
    #   device type bits  + device location bits
    # Otherwise the browser returns nothing.
    #
    # Device type bits (low byte):
    #   ICDeviceTypeMaskCamera           = 0x00000001
    #   ICDeviceTypeMaskScanner          = 0x00000002
    # Device location bits (higher bytes):
    #   ICDeviceLocationTypeMaskLocal    = 0x00000100  (USB / FireWire / Thunderbolt)
    #   ICDeviceLocationTypeMaskShared   = 0x00000200
    #   ICDeviceLocationTypeMaskBonjour  = 0x00000400
    #   ICDeviceLocationTypeMaskBluetooth= 0x00000800
    #   ICDeviceLocationTypeMaskRemote   = 0x0000FE00
    mask = 0x00000001 | 0x00000100  # local USB cameras
    # Try the PyObjC-exposed constants first (newer SDK), fall back to literals
    try:
        mask = (
            ICC.ICDeviceTypeMaskCamera
            | ICC.ICDeviceLocationTypeMaskLocal
        )
    except AttributeError:
        pass  # older SDK exposes only the type masks
    logger.debug("Setting browser mask to 0x%08X", mask)
    browser.setBrowsedDeviceTypeMask_(mask)
    browser.start()

    try:
        # Drain the run loop in small slices until timeout or first batch settles
        elapsed = 0.0
        slice_seconds = 0.25
        while elapsed < timeout_seconds:
            _spin_runloop(slice_seconds)
            elapsed += slice_seconds
            if delegate._finished.is_set():
                # Even after first batch, wait a tiny bit for stragglers
                _spin_runloop(0.5)
                break

        # Also consult the browser's own devices list as a fallback.
        # On some macOS versions, the delegate callback fires before our
        # event loop wakes up, so we may have devices in `browser.devices()`
        # that weren't captured by the delegate snapshot.
        from_property = list(browser.devices() or [])
        from_delegate = delegate.snapshot()
        # Union by object identity
        seen_ids = {id(d) for d in from_delegate}
        for d in from_property:
            if id(d) not in seen_ids:
                from_delegate.append(d)
                seen_ids.add(id(d))
    finally:
        browser.stop()

    cameras: list[DiscoveredCamera] = []
    for dev in from_delegate:
        cameras.append(_describe(dev))
    return cameras


def _describe(ic_device: Any) -> DiscoveredCamera:
    """Convert an ICCameraDevice into a DiscoveredCamera dataclass."""
    _require_pyobjc()

    name = str(ic_device.name() or "<unknown>")
    serial = str(ic_device.serialNumberString() or "")
    transport = str(ic_device.transportType() or "")

    # Capabilities is an NSArray of strings; convert to list[str]
    caps_raw = ic_device.capabilities() or []
    caps = [str(c) for c in caps_raw]
    can_ptp = "ICCameraDeviceCanAcceptPTPCommands" in caps
    can_capture = "ICCameraDeviceCanTakePicture" in caps

    # Vendor/Product ID are deep in the device's USB info dict.
    # ICCameraDevice exposes ``usbVendorID`` and ``usbProductID`` on modern macOS.
    vid: int | None = None
    pid: int | None = None
    try:
        vid = int(ic_device.usbVendorID())
        pid = int(ic_device.usbProductID())
    except (AttributeError, Exception):  # pragma: no cover
        # Older macOS doesn't expose these; fall back to parsing persistent ID
        pass

    return DiscoveredCamera(
        name=name,
        vendor_id=vid,
        product_id=pid,
        serial_number=serial,
        transport_type=transport,
        can_accept_ptp_commands=can_ptp,
        can_take_picture=can_capture,
        ic_device_handle=ic_device,
    )


def find_first_sigma_fp_l(timeout_seconds: float = 3.0) -> DiscoveredCamera | None:
    """Convenience: return the first connected Sigma fp L, or None."""
    for cam in list_cameras(timeout_seconds=timeout_seconds):
        if cam.is_fp_l:
            return cam
    return None


# ---------------------------------------------------------------------------
# Camera — Phase 0-B and onward (PTP session + send_ptp)
# ---------------------------------------------------------------------------


class _CameraDelegate(NSObject if _PYOBJC_AVAILABLE else object):
    """ICCameraDeviceDelegate that signals open/close + PTP responses.

    PTP responses arrive via:
        - (void)didSendPTPCommand:(NSData *)command
                          inData:(NSData *)data
                        response:(NSData *)response
                           error:(NSError *)error
                     contextInfo:(void *)contextInfo
    """

    def init(self):  # noqa: N802
        if _PYOBJC_AVAILABLE:
            self = objc.super(_CameraDelegate, self).init()
        if self is None:
            return None
        self.session_open_event = threading.Event()
        self.session_open_error: Any = None
        self.ptp_event = threading.Event()
        self.ptp_response: tuple[bytes, bytes, Any] | None = None  # (in_data, response, err)
        return self

    # ICDeviceDelegate
    def device_didOpenSessionWithError_(self, device, error):  # noqa: N802
        logger.debug("didOpenSessionWithError: %s", error)
        self.session_open_error = error
        self.session_open_event.set()

    def device_didCloseSessionWithError_(self, device, error):  # noqa: N802
        logger.debug("didCloseSessionWithError: %s", error)

    def didRemoveDevice_(self, device):  # noqa: N802
        logger.warning("didRemoveDevice: %s", device)

    # PTP completion callback
    def didSendPTPCommand_inData_response_error_contextInfo_(  # noqa: N802
        self, command, in_data, response, error, contextInfo
    ):
        in_bytes = bytes(in_data) if in_data else b""
        resp_bytes = bytes(response) if response else b""
        self.ptp_response = (in_bytes, resp_bytes, error)
        self.ptp_event.set()


@dataclass
class PTPResponse:
    in_data: bytes  # data phase bytes from camera (the "interesting" payload)
    response: bytes  # PTP response container (code + parameters)
    response_code: int = 0  # parsed from response[6:8] little-endian

    @property
    def is_ok(self) -> bool:
        return self.response_code == 0x2001  # PTPResponseCode.OK


class Camera:
    """A single opened PTP session on a Sigma fp / fp L.

    Usage::

        cam = find_first_sigma_fp_l()
        if cam is None:
            raise RuntimeError("No Sigma fp L connected")
        with Camera(cam) as session:
            response = session.send_ptp(SigmaOperationCode.GET_CAM_CAPT_STATUS)
            status = SgmCaptStatus.from_bytes(response.in_data[some_offset:])
    """

    def __init__(self, discovered: DiscoveredCamera, ptp_timeout_seconds: float = 5.0):
        _require_pyobjc()
        if not discovered.can_accept_ptp_commands:
            raise ICBridgeError(
                f"Device {discovered.name} does not advertise PTP capability"
            )
        self._discovered = discovered
        self._device = discovered.ic_device_handle
        self._delegate = _CameraDelegate.alloc().init()
        self._device.setDelegate_(self._delegate)
        self._ptp_timeout = ptp_timeout_seconds
        self._opened = False

    def __enter__(self) -> "Camera":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def open(self) -> None:
        if self._opened:
            return
        logger.info("Opening PTP session with %s", self._discovered.name)
        self._delegate.session_open_event.clear()
        self._device.requestOpenSession()
        # Drain run loop until callback fires or 5s timeout
        deadline = 5.0
        slice_s = 0.1
        elapsed = 0.0
        while not self._delegate.session_open_event.is_set() and elapsed < deadline:
            _spin_runloop(slice_s)
            elapsed += slice_s
        if not self._delegate.session_open_event.is_set():
            raise ICBridgeError("Timed out waiting for session open callback")
        if self._delegate.session_open_error is not None:
            raise ICBridgeError(
                f"Session open failed: {self._delegate.session_open_error}"
            )
        self._opened = True
        logger.info("PTP session opened OK")

    def close(self) -> None:
        if not self._opened:
            return
        logger.info("Closing PTP session")
        try:
            self._device.requestCloseSession()
            _spin_runloop(0.5)
        finally:
            self._opened = False

    def send_ptp(
        self,
        opcode: int,
        out_data: bytes = b"",
        parameters: tuple[int, ...] = (),
    ) -> PTPResponse:
        """Send a single PTP pass-through command and wait for the response.

        Args:
            opcode: Operation code (use ``SigmaOperationCode.X`` enum values).
            out_data: Optional outgoing data-phase bytes (e.g. SnapCommand payload).
            parameters: Up to 5 PTP parameters (uint32 each).

        Returns:
            PTPResponse with in_data (camera's reply payload) and response.
        """
        if not self._opened:
            raise ICBridgeError("Session not open. Call open() or use context manager.")

        # Build the PTP command container.
        # Format (16+ bytes, little-endian):
        #   uint32 length
        #   uint16 type (0x0001 = command)
        #   uint16 opcode
        #   uint32 transaction_id (managed by ImageCaptureCore)
        #   uint32 parameter1..parameter5 (variable count)
        cmd_buf = bytearray()
        cmd_buf += int(opcode & 0xFFFF).to_bytes(2, "little")
        # ICCameraDevice.requestSendPTPCommand expects the raw command container
        # **without** the length / type / transaction-ID prefix on some macOS
        # versions; on others it expects the full container. We follow the
        # SigmaSDK pattern which sends the OpCode + parameters as a single
        # NSData and lets ImageCaptureCore prepend the rest.
        for p in parameters:
            cmd_buf += int(p & 0xFFFFFFFF).to_bytes(4, "little")
        cmd_nsdata = NSData.dataWithBytes_length_(bytes(cmd_buf), len(cmd_buf))

        out_nsdata = None
        if out_data:
            out_nsdata = NSData.dataWithBytes_length_(out_data, len(out_data))

        self._delegate.ptp_event.clear()
        self._delegate.ptp_response = None

        # didSendCommandSelector = didSendPTPCommand:inData:response:error:contextInfo:
        selector = b"didSendPTPCommand:inData:response:error:contextInfo:"
        self._device.requestSendPTPCommand_outData_sendCommandDelegate_didSendCommandSelector_contextInfo_(  # noqa: E501
            cmd_nsdata,
            out_nsdata,
            self._delegate,
            objc.selector(self._delegate.didSendPTPCommand_inData_response_error_contextInfo_, selector=selector),  # type: ignore[arg-type]
            None,
        )

        # Drain run loop until callback fires
        elapsed = 0.0
        slice_s = 0.05
        while not self._delegate.ptp_event.is_set() and elapsed < self._ptp_timeout:
            _spin_runloop(slice_s)
            elapsed += slice_s
        if not self._delegate.ptp_event.is_set():
            raise ICBridgeError(
                f"Timed out waiting for PTP response (opcode=0x{opcode:04X})"
            )

        in_bytes, resp_bytes, err = self._delegate.ptp_response  # type: ignore[misc]
        if err is not None:
            raise ICBridgeError(f"PTP error for opcode 0x{opcode:04X}: {err}")

        # Parse response code from the last 2 bytes of the response container
        # (PTP response container has the response code at bytes 6:8)
        rc = 0
        if len(resp_bytes) >= 8:
            rc = int.from_bytes(resp_bytes[6:8], "little")

        return PTPResponse(
            in_data=in_bytes,
            response=resp_bytes,
            response_code=rc,
        )

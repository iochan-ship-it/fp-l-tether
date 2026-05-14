"""USB-level recovery for a wedged Sigma fp / fp L.

When the camera's bulk endpoint stalls (Errno 60 / 0-byte read after
the firmware enters its undocumented "doze" state), no PTP-level retry
can recover it — the only known fixes are:

  1. Physical power-cycle of the camera body (current MVP behavior;
     painful UX).
  2. Physical unplug-replug of the USB-C cable (also painful, but
     proves the wedge is at the USB layer, not the camera firmware).
  3. **Force USB re-enumeration via IOKit** (this module).

Option 3 is functionally equivalent to option 2 — IOKit's
``IOUSBDeviceInterface::USBDeviceReEnumerate()`` tells the macOS
host controller to drop the device off the bus and re-discover it,
exactly as if the cable had been pulled. The camera firmware doesn't
participate; this is bus-level.

We use this because:

  - ``libusb_reset_device()`` is silently broken on macOS since
    10.11 (libusb issue #455). Calls return success but do nothing.
  - The IOKit path is independent of libusb and verified to actually
    cycle the device on Apple Silicon (2026-05-13).

Mechanics
---------
1. ``IOServiceMatching("IOUSBDevice")`` + idVendor/idProduct filter
2. ``IOCreatePlugInInterfaceForService(...)`` → IOCFPlugInInterface **
3. ``(*plugin)->QueryInterface(plugin, kIOUSBDeviceInterfaceID*, &dev)``
   — walks newest-first (ID320 → ID300 → ...) so we get the most
   modern vtable the kernel is willing to hand back.
4. ``(*dev)->USBDeviceOpen(dev)`` — required; ReEnumerate returns
   ``kIOReturnNotOpen`` (0xE00002CD) without this.
5. ``(*dev)->USBDeviceReEnumerate(dev, 0)`` — slot 37 in 187+ vtable.

Vtable slot map (stable across all macOS versions that ship ID187+):

  slot 0  : _reserved
  slot 1  : QueryInterface  (IUNKNOWN_C_GUTS)
  slot 2  : AddRef          (IUNKNOWN_C_GUTS)
  slot 3  : Release         (IUNKNOWN_C_GUTS)
  slot 8  : USBDeviceOpen
  slot 9  : USBDeviceClose
  slot 25 : ResetDevice
  slot 29 : USBDeviceOpenSeize (182+)
  slot 37 : USBDeviceReEnumerate (187+)

Verified working on macOS Apple Silicon with Sigma fp L (PID 0xC442)
on 2026-05-13. See scripts/phase3_usb_reenum_test.py for the
standalone verification harness.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import (
    CFUNCTYPE,
    POINTER,
    Structure,
    byref,
    c_char_p,
    c_int32,
    c_uint8,
    c_uint32,
    c_void_p,
)
from typing import Any

from fp_l_tether.telemetry import get_logger

_default_logger = get_logger("usb_recovery")

# ---------------------------------------------------------------------------
# ctypes bindings — IOKit + CoreFoundation
# ---------------------------------------------------------------------------

iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

kIOReturnSuccess = 0
kIOMainPortDefault = 0
kCFNumberSInt32Type = 3
kCFStringEncodingUTF8 = 0x08000100

# IOKit return codes we recognise
kIOReturnExclusiveAccess = 0xE00002C5
kIOReturnNotOpen         = 0xE00002CD

PTR_SIZE = ctypes.sizeof(c_void_p)


class CFUUIDBytes(Structure):
    """16-byte CFUUIDBytes struct used by IOKit interface IDs."""

    _fields_ = [(f"b{i}", c_uint8) for i in range(16)]


def _uuid(s: str) -> CFUUIDBytes:
    """Parse 'C244E858-109C-11D4-91D4-0050E4C6426F' into CFUUIDBytes."""
    hex_str = s.replace("-", "")
    if len(hex_str) != 32:
        raise ValueError(f"bad UUID string: {s!r}")
    out = CFUUIDBytes()
    for i in range(16):
        setattr(out, f"b{i}", int(hex_str[i * 2 : i * 2 + 2], 16))
    return out


# Plugin + interface UUIDs (verified against Apple's open-source IOUSBLib.h).
UUID_IOCFPlugInInterface         = _uuid("C244E858-109C-11D4-91D4-0050E4C6426F")
UUID_IOUSBDeviceUserClientTypeID = _uuid("9DC7B780-9EC0-11D4-A54F-000A27052861")

# Try newest → oldest; we only need a version ≥187 (which adds
# USBDeviceReEnumerate at slot 37). Empirically ID300 is what the kernel
# hands back on modern macOS / Apple Silicon for the fp L.
USB_DEVICE_INTERFACE_UUIDS: list[tuple[str, CFUUIDBytes]] = [
    ("ID320", _uuid("01A2D0E9-42F6-4A0E-A39F-CB408646FC05")),
    ("ID300", _uuid("396104F7-943D-4893-90F1-69BD6CF5C2EB")),
    ("ID245", _uuid("A33CF047-4B5B-48E2-B57D-0207FCEAE13B")),
    ("ID197", _uuid("FE2FD52F-3B5A-473B-978B-AD99001EB3ED")),
    ("ID187", _uuid("3C9EE1EB-2402-11B2-8E7E-000A27801E86")),
    ("ID182", _uuid("152FC496-4891-11D5-9D52-000A27801E86")),
    ("ID",    _uuid("5C8187D0-9EF3-11D4-8B45-000A27052861")),
]

# --- IOKit prototypes -----------------------------------------------------

iokit.IOServiceMatching.restype = c_void_p
iokit.IOServiceMatching.argtypes = [c_char_p]

iokit.IOServiceGetMatchingServices.restype = c_int32
iokit.IOServiceGetMatchingServices.argtypes = [c_uint32, c_void_p, POINTER(c_void_p)]

iokit.IOIteratorNext.restype = c_void_p
iokit.IOIteratorNext.argtypes = [c_void_p]

iokit.IOObjectRelease.restype = c_int32
iokit.IOObjectRelease.argtypes = [c_void_p]

iokit.IOCreatePlugInInterfaceForService.restype = c_int32
iokit.IOCreatePlugInInterfaceForService.argtypes = [
    c_void_p, c_void_p, c_void_p, POINTER(c_void_p), POINTER(c_int32)
]

# --- CoreFoundation prototypes -------------------------------------------

cf.CFRelease.argtypes = [c_void_p]
cf.CFRelease.restype = None
cf.CFUUIDCreateFromUUIDBytes.restype = c_void_p
cf.CFUUIDCreateFromUUIDBytes.argtypes = [c_void_p, CFUUIDBytes]
cf.CFStringCreateWithCString.restype = c_void_p
cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
cf.CFNumberCreate.restype = c_void_p
cf.CFNumberCreate.argtypes = [c_void_p, c_int32, c_void_p]
cf.CFDictionaryAddValue.argtypes = [c_void_p, c_void_p, c_void_p]
cf.CFDictionaryAddValue.restype = None

# --- COM-style function pointer signatures -------------------------------

QueryInterfaceFn       = CFUNCTYPE(c_int32, c_void_p, CFUUIDBytes, POINTER(c_void_p))
ReleaseFn              = CFUNCTYPE(c_uint32, c_void_p)
USBDeviceOpenFn        = CFUNCTYPE(c_int32, c_void_p)
USBDeviceCloseFn       = CFUNCTYPE(c_int32, c_void_p)
USBDeviceOpenSeizeFn   = CFUNCTYPE(c_int32, c_void_p)
USBDeviceReEnumerateFn = CFUNCTYPE(c_int32, c_void_p, c_uint32)
USBDeviceResetFn       = CFUNCTYPE(c_int32, c_void_p)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _cf_string(s: str) -> c_void_p:
    return cf.CFStringCreateWithCString(None, s.encode("utf-8"), kCFStringEncodingUTF8)


def _cf_number_sint32(value: int) -> c_void_p:
    n = c_int32(value)
    return cf.CFNumberCreate(None, kCFNumberSInt32Type, byref(n))


def _vtable_slot(double_ptr: c_void_p, slot: int) -> int:
    """Read function pointer at ``slot`` of the COM-style vtable that ``double_ptr`` points to."""
    if not double_ptr.value:
        raise RuntimeError("double pointer is NULL")
    vtable_ptr = c_void_p.from_address(double_ptr.value).value
    if not vtable_ptr:
        raise RuntimeError("vtable pointer is NULL")
    addr = c_void_p.from_address(vtable_ptr + slot * PTR_SIZE).value
    if not addr:
        raise RuntimeError(f"vtable slot {slot} is NULL")
    return addr


def _release_interface(double_ptr: c_void_p) -> None:
    """Best-effort Release() on a COM interface pointer."""
    try:
        release_addr = _vtable_slot(double_ptr, 3)
        ReleaseFn(release_addr)(double_ptr)
    except Exception:  # noqa: BLE001
        pass


def _find_usb_service(vid: int, pid: int) -> int:
    """Return io_service_t for first matching IOUSBDevice (or 0). Caller must release."""
    matching = iokit.IOServiceMatching(b"IOUSBDevice")
    if not matching:
        raise RuntimeError("IOServiceMatching returned NULL")

    vid_key = _cf_string("idVendor")
    pid_key = _cf_string("idProduct")
    vid_num = _cf_number_sint32(vid)
    pid_num = _cf_number_sint32(pid)
    try:
        cf.CFDictionaryAddValue(matching, vid_key, vid_num)
        cf.CFDictionaryAddValue(matching, pid_key, pid_num)
    finally:
        cf.CFRelease(vid_key)
        cf.CFRelease(pid_key)
        cf.CFRelease(vid_num)
        cf.CFRelease(pid_num)

    iterator = c_void_p()
    kr = iokit.IOServiceGetMatchingServices(kIOMainPortDefault, matching, byref(iterator))
    if kr != kIOReturnSuccess:
        raise RuntimeError(f"IOServiceGetMatchingServices failed: 0x{kr:08x}")
    try:
        service = iokit.IOIteratorNext(iterator)
        while True:
            extra = iokit.IOIteratorNext(iterator)
            if not extra:
                break
            iokit.IOObjectRelease(extra)
    finally:
        iokit.IOObjectRelease(iterator)
    return service or 0


def _open_usb_device_interface(service: int) -> tuple[c_void_p, str]:
    """Walk UUID list newest-first; return (device, uuid_name) on success."""
    plugin = c_void_p()
    score = c_int32(0)

    plugin_type_uuid = cf.CFUUIDCreateFromUUIDBytes(None, UUID_IOUSBDeviceUserClientTypeID)
    plugin_iface_uuid = cf.CFUUIDCreateFromUUIDBytes(None, UUID_IOCFPlugInInterface)
    try:
        kr = iokit.IOCreatePlugInInterfaceForService(
            service, plugin_type_uuid, plugin_iface_uuid, byref(plugin), byref(score),
        )
    finally:
        cf.CFRelease(plugin_type_uuid)
        cf.CFRelease(plugin_iface_uuid)
    if kr != kIOReturnSuccess or not plugin.value:
        raise RuntimeError(f"IOCreatePlugInInterfaceForService failed: kr=0x{kr:08x}")

    qi_addr = _vtable_slot(plugin, 1)
    QueryInterface = QueryInterfaceFn(qi_addr)

    last_hr = 0
    last_name = "(none tried)"
    for name, uuid_bytes in USB_DEVICE_INTERFACE_UUIDS:
        device = c_void_p()
        hr = QueryInterface(plugin, uuid_bytes, byref(device))
        if hr == 0 and device.value:
            _release_interface(plugin)
            return device, name
        last_hr = hr & 0xFFFFFFFF
        last_name = name

    _release_interface(plugin)
    raise RuntimeError(
        f"QueryInterface failed for all IOUSBDeviceInterface UUIDs; "
        f"last={last_name} hr=0x{last_hr:08x}"
    )


def _device_open(device: c_void_p) -> str:
    """Call USBDeviceOpen, fall back to USBDeviceOpenSeize. Returns 'open' or 'seize'."""
    open_addr = _vtable_slot(device, 8)
    kr = USBDeviceOpenFn(open_addr)(device) & 0xFFFFFFFF
    if kr == 0:
        return "open"

    # Some other client (ptpcamerad, a stale libusb handle) owns the device.
    # Seize it.
    try:
        seize_addr = _vtable_slot(device, 29)
    except RuntimeError as e:
        raise RuntimeError(
            f"USBDeviceOpen failed (kr=0x{kr:08x}) and seize not available: {e}"
        ) from None
    kr2 = USBDeviceOpenSeizeFn(seize_addr)(device) & 0xFFFFFFFF
    if kr2 != 0:
        raise RuntimeError(
            f"Both USBDeviceOpen (kr=0x{kr:08x}) and "
            f"USBDeviceOpenSeize (kr=0x{kr2:08x}) failed"
        )
    return "seize"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _wait_for_presence(vid: int, pid: int, timeout_s: float, *, want_present: bool) -> bool:
    """Poll IOKit until presence matches ``want_present``. Returns True on match."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        svc = _find_usb_service(vid, pid)
        present = bool(svc)
        if svc:
            iokit.IOObjectRelease(svc)
        if present == want_present:
            return True
        time.sleep(0.1)
    return False


def force_reenumerate(vid: int, pid: int, options: int = 0) -> str:
    """Open the IOUSBDeviceInterface and call USBDeviceReEnumerate.

    Returns the UUID name that QueryInterface succeeded on (e.g. "ID300"),
    for diagnostic logging. Raises RuntimeError on any failure.
    """
    service = _find_usb_service(vid, pid)
    if not service:
        raise RuntimeError(f"no USB device matching {vid:04x}:{pid:04x}")
    try:
        device, uuid_name = _open_usb_device_interface(service)
    finally:
        iokit.IOObjectRelease(service)
    try:
        _device_open(device)
        reenum_addr = _vtable_slot(device, 37)
        kr = USBDeviceReEnumerateFn(reenum_addr)(device, options) & 0xFFFFFFFF
        if kr != 0:
            raise RuntimeError(
                f"USBDeviceReEnumerate failed: kr=0x{kr:08x} (via {uuid_name})"
            )
        return uuid_name
    finally:
        _release_interface(device)


def force_reset_device(vid: int, pid: int) -> str:
    """Lighter alternative to ReEnumerate — calls IOUSBDeviceInterface::ResetDevice.

    The device stays on the bus but its USB state machine is reset.
    Sometimes sufficient for a stalled bulk endpoint; doesn't help if
    the camera firmware itself is dozing.
    """
    service = _find_usb_service(vid, pid)
    if not service:
        raise RuntimeError(f"no USB device matching {vid:04x}:{pid:04x}")
    try:
        device, uuid_name = _open_usb_device_interface(service)
    finally:
        iokit.IOObjectRelease(service)
    try:
        _device_open(device)
        reset_addr = _vtable_slot(device, 25)
        kr = USBDeviceResetFn(reset_addr)(device) & 0xFFFFFFFF
        if kr != 0:
            raise RuntimeError(
                f"USBDeviceResetDevice failed: kr=0x{kr:08x} (via {uuid_name})"
            )
        # ResetDevice leaves the device on the bus; close cleanly.
        try:
            close_addr = _vtable_slot(device, 9)
            USBDeviceCloseFn(close_addr)(device)
        except Exception:  # noqa: BLE001
            pass
        return uuid_name
    finally:
        _release_interface(device)


def recover_camera(
    vid: int,
    pid: int,
    *,
    log: Any = None,
    reappear_timeout_s: float = 12.0,
    settle_s: float = 1.5,
) -> bool:
    """End-to-end recovery: force re-enumerate, wait for the bus, settle.

    Parameters
    ----------
    vid, pid:
        USB Vendor / Product IDs of the camera to recover.
    log:
        Optional structlog-style logger. Falls back to the project default.
    reappear_timeout_s:
        How long to wait for the device to come back after re-enumerate.
        12 s is generous; empirically the fp L re-enumerates in <1 s.
    settle_s:
        Post-reappear sleep to let ``ptpcamerad`` (re-)attach and
        the kernel driver settle before our caller opens libusb again.

    Returns
    -------
    bool
        True if the device disappeared and reappeared. False on any
        failure (bad IOKit call, device didn't come back). Does NOT
        verify PTP works — caller is responsible for re-establishing
        the session and noticing if it fails.
    """
    lg = log if log is not None else _default_logger
    if not _find_usb_service(vid, pid):
        lg.warning("usb_recovery_no_device_to_recover", vid=vid, pid=pid)
        return False

    try:
        uuid_name = force_reenumerate(vid, pid)
        lg.info("usb_reenum_call_ok", uuid=uuid_name)
    except Exception as e:  # noqa: BLE001
        lg.warning("usb_reenum_call_failed", error=str(e))
        return False

    # Disappear is optional — some macOS versions hide the brief detach
    # phase entirely. Don't fail if we missed it.
    disappeared = _wait_for_presence(vid, pid, timeout_s=3.0, want_present=False)
    lg.info("usb_reenum_disappear", disappeared=disappeared)

    if not _wait_for_presence(vid, pid, timeout_s=reappear_timeout_s, want_present=True):
        lg.error("usb_reenum_no_reappear", timeout_s=reappear_timeout_s)
        return False

    # Let the kernel driver and any userland watchers settle.
    time.sleep(settle_s)
    lg.info("usb_recovery_success", settle_s=settle_s)
    return True

#!/usr/bin/env python3
"""Phase 3.7 — USB re-enumerate recovery test for a wedged fp L.

Goal
====
Prove (or disprove) that we can recover a wedged-bulk-endpoint fp L
without physical power cycle, by calling IOKit's
``IOUSBDeviceInterface187::USBDeviceReEnumerate()`` directly through
ctypes. If this works, the daemon can auto-recover from doze-induced
wedges instead of forcing the user to toggle the camera switch.

Why this route
==============
``libusb_reset_device()`` has been silently broken on macOS since 10.11
(libusb issue #455 — returns success but does nothing). The IOKit path
is independent of libusb and is documented to actually force USB
re-enumeration — equivalent to physically unplugging and replugging
the device, but driven from software.

Mechanics
=========
We talk to IOKit's CFPlugIn machinery through ctypes:

  1. ``IOServiceMatching("IOUSBDevice")`` + idVendor/idProduct filter
     → ``IOServiceGetMatchingServices`` → io_service_t
  2. ``IOCreatePlugInInterfaceForService(service,
        kIOUSBDeviceUserClientTypeID, kIOCFPlugInInterfaceID, ...)``
     → IOCFPlugInInterface ** (a COM-style double pointer to vtable)
  3. ``(*plugin)->QueryInterface(plugin, kIOUSBDeviceInterfaceID187,
        &device)``
     → IOUSBDeviceInterface187 ** (the actual USB device interface)
  4. ``(*device)->USBDeviceReEnumerate(device, options)``
     → kernel kicks the device off the bus and re-enumerates it.

The vtable layout for IOUSBDeviceInterface187 is fixed:

  slot 0  : _reserved
  slot 1  : QueryInterface  (IUNKNOWN_C_GUTS)
  slot 2  : AddRef          (IUNKNOWN_C_GUTS)
  slot 3  : Release         (IUNKNOWN_C_GUTS)
  slots 4-28 : base IOUSBDeviceInterface methods
              (slot 25 = ResetDevice)
  slots 29-36: 182 additions
  slot 37 : USBDeviceReEnumerate   (187 addition)

We walk the vtable by raw offset rather than declaring the full struct
because we only need two slots and the struct order is stable across
all macOS versions that ship the 187 UUID.

Usage
=====

  # Sanity check on a healthy camera (verify the IOKit dance itself works)::

      sudo killall ptpcamerad 2>/dev/null
      cd "/path/to/fp-l-tether"
      sudo venv/bin/python scripts/phase3_usb_reenum_test.py

  # Real test: let the camera wedge naturally (doze for 2+ min), then::

      sudo venv/bin/python scripts/phase3_usb_reenum_test.py --skip-pre-check

Exit codes
==========
  0 : success — device re-enumerated and PTP works afterwards
  1 : camera not found via IOKit
  2 : IOKit call to USBDeviceReEnumerate failed
  3 : device did not re-appear on the bus within the timeout
  4 : device re-appeared but PTP session could not be re-established
"""

from __future__ import annotations

import argparse
import ctypes
import sys
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
from pathlib import Path

# Allow running directly from the repo without `pip install -e .`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fp_l_tether.camera.ptp_codes import (  # noqa: E402
    SIGMA_FP_L_PRODUCT_ID,
    SIGMA_FP_PRODUCT_ID,
    SIGMA_VENDOR_ID,
)

# ---------------------------------------------------------------------------
# ctypes bindings — IOKit + CoreFoundation
# ---------------------------------------------------------------------------

iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

kIOReturnSuccess = 0
kIOMainPortDefault = 0  # NULL mach_port_t -> default port
kCFAllocatorDefault = None
kCFNumberSInt32Type = 3
kCFStringEncodingUTF8 = 0x08000100

PTR_SIZE = ctypes.sizeof(c_void_p)  # 8 on arm64/x86_64


class CFUUIDBytes(Structure):
    """The 16-byte CFUUIDBytes struct that IOKit uses for interface IDs."""

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


# Plugin & interface UUIDs (from <IOKit/IOCFPlugIn.h> and <IOKit/usb/IOUSBLib.h>).
#
# Each IOUSBDeviceInterface version adds new vtable slots on top of the previous
# one — they're all subclasses with stable slot indices. We try the highest
# version the kernel is willing to hand us first (so we get the most modern
# vtable) and fall back to older UUIDs if the kernel returns E_NOINTERFACE.
# USBDeviceReEnumerate is at slot 37 (introduced in 187), so any version from
# 187 upward will work for our purpose.
UUID_IOCFPlugInInterface         = _uuid("C244E858-109C-11D4-91D4-0050E4C6426F")
UUID_IOUSBDeviceUserClientTypeID = _uuid("9DC7B780-9EC0-11D4-A54F-000A27052861")

# Ordered newest → oldest, but only including UUIDs we're confident about.
# We walk this list until QueryInterface succeeds. Bytes verified against
# Apple's open-source IOUSBFamily/IOUSBLib/Headers/IOUSBLib.h.
USB_DEVICE_INTERFACE_UUIDS: list[tuple[str, CFUUIDBytes]] = [
    ("ID320", _uuid("01A2D0E9-42F6-4A0E-A39F-CB408646FC05")),
    ("ID300", _uuid("396104F7-943D-4893-90F1-69BD6CF5C2EB")),
    ("ID245", _uuid("A33CF047-4B5B-48E2-B57D-0207FCEAE13B")),
    ("ID197", _uuid("FE2FD52F-3B5A-473B-978B-AD99001EB3ED")),
    ("ID187", _uuid("3C9EE1EB-2402-11B2-8E7E-000A27801E86")),  # adds USBDeviceReEnumerate
    ("ID182", _uuid("152FC496-4891-11D5-9D52-000A27801E86")),
    ("ID",    _uuid("5C8187D0-9EF3-11D4-8B45-000A27052861")),  # base; has ResetDevice (slot 25)
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
    c_void_p,           # io_service_t
    c_void_p,           # CFUUIDRef pluginType
    c_void_p,           # CFUUIDRef interfaceType
    POINTER(c_void_p),  # IOCFPlugInInterface ***
    POINTER(c_int32),   # SInt32 *
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

# --- COM-style function pointer types ------------------------------------

# (*self)->QueryInterface(self, CFUUIDBytes iid, void **ppv)
QueryInterfaceFn = CFUNCTYPE(c_int32, c_void_p, CFUUIDBytes, POINTER(c_void_p))
# (*self)->Release(self)
ReleaseFn = CFUNCTYPE(c_uint32, c_void_p)
# (*self)->USBDeviceOpen(self)         — slot 8
# (*self)->USBDeviceClose(self)        — slot 9
# (*self)->USBDeviceOpenSeize(self)    — slot 29 (added in 182+)
USBDeviceOpenFn = CFUNCTYPE(c_int32, c_void_p)
USBDeviceCloseFn = CFUNCTYPE(c_int32, c_void_p)
USBDeviceOpenSeizeFn = CFUNCTYPE(c_int32, c_void_p)
# (*self)->USBDeviceReEnumerate(self, UInt32 options) — slot 37 (added in 187+)
USBDeviceReEnumerateFn = CFUNCTYPE(c_int32, c_void_p, c_uint32)
# (*self)->ResetDevice(self) — slot 25 (base)
USBDeviceResetFn = CFUNCTYPE(c_int32, c_void_p)

# IOKit error codes we want to recognize:
kIOReturnExclusiveAccess = 0xE00002C5
kIOReturnNotOpen         = 0xE00002CD


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cf_string(s: str) -> c_void_p:
    return cf.CFStringCreateWithCString(None, s.encode("utf-8"), kCFStringEncodingUTF8)


def _cf_number_sint32(value: int) -> c_void_p:
    n = c_int32(value)
    return cf.CFNumberCreate(None, kCFNumberSInt32Type, byref(n))


def _vtable_slot(double_ptr: c_void_p, slot: int) -> int:
    """Read function pointer at ``slot`` of the vtable that ``double_ptr`` points to.

    ``double_ptr`` is a COM-style ``T **`` — its value is the address of a
    vtable pointer; ``*double_ptr`` gives us the vtable address.
    """
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
    """Call (*double_ptr)->Release(double_ptr) — vtable slot 3."""
    try:
        release_addr = _vtable_slot(double_ptr, 3)
        ReleaseFn(release_addr)(double_ptr)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# IOKit operations
# ---------------------------------------------------------------------------


def _find_usb_service(vid: int, pid: int) -> int:
    """Return the io_service_t of the first matching IOUSBDevice, or 0.

    Caller must release the returned service with ``IOObjectRelease``.
    """
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

    # IOServiceGetMatchingServices CONSUMES one reference on `matching` —
    # do not release it ourselves.
    iterator = c_void_p()
    kr = iokit.IOServiceGetMatchingServices(kIOMainPortDefault, matching, byref(iterator))
    if kr != kIOReturnSuccess:
        raise RuntimeError(f"IOServiceGetMatchingServices failed: 0x{kr:08x}")

    try:
        service = iokit.IOIteratorNext(iterator)
        # Drain & release any further matches so we don't leak them.
        while True:
            extra = iokit.IOIteratorNext(iterator)
            if not extra:
                break
            iokit.IOObjectRelease(extra)
    finally:
        iokit.IOObjectRelease(iterator)
    return service or 0


def _which_sigma_pid() -> int | None:
    """Return whichever Sigma fp / fp L PID is present right now."""
    for pid in (SIGMA_FP_L_PRODUCT_ID, SIGMA_FP_PRODUCT_ID):
        svc = _find_usb_service(SIGMA_VENDOR_ID, pid)
        if svc:
            iokit.IOObjectRelease(svc)
            return pid
    return None


def _open_usb_device_interface(service: int, verbose: bool = True) -> tuple[c_void_p, str]:
    """Return (device_double_ptr, uuid_name) for the given io_service_t.

    Walks ``USB_DEVICE_INTERFACE_UUIDS`` newest-first and returns the first
    interface version the kernel hands back. Caller must call
    ``_release_interface`` on the result when done.
    """
    plugin = c_void_p()
    score = c_int32(0)

    plugin_type_uuid = cf.CFUUIDCreateFromUUIDBytes(None, UUID_IOUSBDeviceUserClientTypeID)
    plugin_iface_uuid = cf.CFUUIDCreateFromUUIDBytes(None, UUID_IOCFPlugInInterface)
    try:
        kr = iokit.IOCreatePlugInInterfaceForService(
            service,
            plugin_type_uuid,
            plugin_iface_uuid,
            byref(plugin),
            byref(score),
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
        # ctypes returns the c_int32 result as a Python int; normalize sign.
        hr_u = hr & 0xFFFFFFFF
        if hr == 0 and device.value:
            if verbose:
                print(f"  ✓ QueryInterface({name}) succeeded")
            _release_interface(plugin)
            return device, name
        if verbose:
            print(f"  · QueryInterface({name}) hr=0x{hr_u:08x} — trying next")
        last_hr = hr_u
        last_name = name

    _release_interface(plugin)
    raise RuntimeError(
        f"QueryInterface failed for all IOUSBDeviceInterface UUIDs; "
        f"last={last_name} hr=0x{last_hr:08x}"
    )


def _device_open(device: c_void_p, *, verbose: bool = True) -> None:
    """Call USBDeviceOpen (slot 8), falling back to USBDeviceOpenSeize (slot 29).

    ReEnumerate and ResetDevice both require the caller to be the open owner
    of the device interface; otherwise they return kIOReturnNotOpen (0xe00002cd).
    """
    open_addr = _vtable_slot(device, 8)
    USBDeviceOpen = USBDeviceOpenFn(open_addr)
    kr = USBDeviceOpen(device) & 0xFFFFFFFF
    if kr == 0:
        if verbose:
            print("  ✓ USBDeviceOpen succeeded")
        return

    if verbose:
        print(f"  · USBDeviceOpen kr=0x{kr:08x} — trying USBDeviceOpenSeize")

    # Fall back to Seize, which steals the device away from any other client
    # (e.g. ptpcamerad, if it raced back in).
    try:
        seize_addr = _vtable_slot(device, 29)
    except RuntimeError as e:
        raise RuntimeError(
            f"USBDeviceOpen failed (kr=0x{kr:08x}) and USBDeviceOpenSeize not available: {e}"
        ) from None
    USBDeviceOpenSeize = USBDeviceOpenSeizeFn(seize_addr)
    kr2 = USBDeviceOpenSeize(device) & 0xFFFFFFFF
    if kr2 != 0:
        raise RuntimeError(
            f"Both USBDeviceOpen (kr=0x{kr:08x}) and "
            f"USBDeviceOpenSeize (kr=0x{kr2:08x}) failed"
        )
    if verbose:
        print("  ✓ USBDeviceOpenSeize succeeded")


def _device_close_safely(device: c_void_p) -> None:
    """Best-effort Close. After ReEnumerate the device is gone, so this no-ops."""
    try:
        close_addr = _vtable_slot(device, 9)
        USBDeviceClose = USBDeviceCloseFn(close_addr)
        USBDeviceClose(device)
    except Exception:  # noqa: BLE001
        pass


def call_reenumerate(vid: int, pid: int, options: int = 0) -> None:
    """Force USB re-enumeration of the Sigma device. Raises on failure."""
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
        ReEnumerate = USBDeviceReEnumerateFn(reenum_addr)
        kr = ReEnumerate(device, options)
        kr_u = kr & 0xFFFFFFFF
        if kr != 0:
            raise RuntimeError(
                f"USBDeviceReEnumerate failed: kr=0x{kr_u:08x} (via {uuid_name})"
            )
        # NOTE: do NOT call USBDeviceClose here — the device is being kicked
        # off the bus, so the handle is invalid.
    finally:
        _release_interface(device)


def call_reset_device(vid: int, pid: int) -> None:
    """Lighter alternative — IOUSBDeviceInterface::ResetDevice (slot 25)."""
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
        ResetDevice = USBDeviceResetFn(reset_addr)
        kr = ResetDevice(device)
        kr_u = kr & 0xFFFFFFFF
        if kr != 0:
            raise RuntimeError(
                f"USBDeviceResetDevice failed: kr=0x{kr_u:08x} (via {uuid_name})"
            )
        # After ResetDevice, the device is still on the bus, so try to close.
        _device_close_safely(device)
    finally:
        _release_interface(device)


def wait_for_device(vid: int, pid: int, timeout_s: float, *, want_present: bool) -> bool:
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _ptp_roundtrip() -> tuple[bool, str]:
    """Try a single sigma_get_camera_info round trip. (ok, message)"""
    try:
        from fp_l_tether.camera.usb_bridge import USBBridge  # noqa: WPS433
    except Exception as e:  # noqa: BLE001
        return False, f"USBBridge import failed: {e}"

    try:
        bridge = USBBridge.find_sigma_fp_l()
    except Exception as e:  # noqa: BLE001
        return False, f"find_sigma_fp_l: {e}"

    try:
        bridge.open()
    except Exception as e:  # noqa: BLE001
        return False, f"bridge.open: {e}"
    try:
        try:
            bridge.open_session()
        except Exception as e:  # noqa: BLE001
            return False, f"open_session: {e}"
        try:
            info = bridge.sigma_get_camera_info()
        except Exception as e:  # noqa: BLE001
            return False, f"sigma_get_camera_info: {e}"
        try:
            bridge.close_session()
        except Exception:  # noqa: BLE001
            pass
        return True, f"sigma_get_camera_info returned {len(info)} bytes"
    finally:
        try:
            bridge.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="USB re-enumerate recovery test")
    ap.add_argument(
        "--skip-pre-check",
        action="store_true",
        help="Skip the pre-recovery PTP roundtrip (use when camera is wedged).",
    )
    ap.add_argument(
        "--mode",
        choices=("reenum", "reset"),
        default="reenum",
        help="Recovery mode: re-enumerate (default, hard) or just ResetDevice (soft).",
    )
    ap.add_argument(
        "--options",
        type=lambda x: int(x, 0),
        default=0,
        help="UInt32 options for USBDeviceReEnumerate (default 0). Bit 0 = release "
             "interface, bit 1 = wait for re-enumerate. 0 is fine for our use.",
    )
    ap.add_argument(
        "--timeout-s",
        type=float,
        default=12.0,
        help="How long to wait for device re-appearance after re-enumerate.",
    )
    args = ap.parse_args()

    print("=" * 64)
    print(f"Phase 3.7 — USB {args.mode} test")
    print("=" * 64)

    # ---------------- Step 1: locate the camera ----------------
    pid = _which_sigma_pid()
    if pid is None:
        print("✗ no Sigma fp / fp L found on the USB bus (IOKit lookup).")
        print("  Is it connected, powered on, and in Camera Control mode?")
        return 1
    print(f"✓ Sigma device present (VID=0x{SIGMA_VENDOR_ID:04X}, PID=0x{pid:04X})")

    # ---------------- Step 2: optional pre-check ----------------
    if not args.skip_pre_check:
        print("\n[step 2] pre-check: verify PTP works before recovery...")
        ok, msg = _ptp_roundtrip()
        if ok:
            print(f"  ✓ {msg}")
        else:
            print(f"  ✗ pre-check failed: {msg}")
            print("    (continuing — camera may already be wedged, which is the case we care about)")
    else:
        print("\n[step 2] pre-check skipped (--skip-pre-check)")

    # Small settle so libusb releases handles cleanly before re-enumerate.
    time.sleep(0.3)

    # ---------------- Step 3: trigger recovery ----------------
    print(f"\n[step 3] calling IOKit {args.mode}...")
    try:
        if args.mode == "reset":
            call_reset_device(SIGMA_VENDOR_ID, pid)
        else:
            call_reenumerate(SIGMA_VENDOR_ID, pid, options=args.options)
    except Exception as e:  # noqa: BLE001
        print(f"✗ {args.mode} call failed: {e}")
        return 2
    print(f"  ✓ {args.mode} call returned success")

    # ---------------- Step 4: observe bus cycle ----------------
    if args.mode == "reenum":
        print("\n[step 4a] watching for device to drop off the bus (≤3s)...")
        gone = wait_for_device(SIGMA_VENDOR_ID, pid, timeout_s=3.0, want_present=False)
        if gone:
            print("  ✓ device disappeared")
        else:
            print("  … device still present after 3s — re-enumerate may have skipped detach phase")

    print("\n[step 4b] waiting for device to come back...")
    back = wait_for_device(SIGMA_VENDOR_ID, pid, timeout_s=args.timeout_s, want_present=True)
    if not back:
        print(f"✗ device did not reappear within {args.timeout_s}s")
        print("  This is the failure mode that would force a physical power cycle.")
        return 3
    print("  ✓ device is back on the bus")

    # ---------------- Step 5: settle, then PTP ----------------
    settle_s = 1.5
    print(f"\n[step 5] sleeping {settle_s}s for ptpcamerad to (re-)attach and settle...")
    time.sleep(settle_s)

    print("\n[step 6] verify PTP after recovery...")
    ok, msg = _ptp_roundtrip()
    if not ok:
        print(f"✗ post-recovery PTP failed: {msg}")
        print("  Camera re-enumerated, but the PTP session could not be re-established.")
        print("  Try: sudo killall ptpcamerad  &&  rerun this script with --skip-pre-check")
        return 4
    print(f"  ✓ {msg}")

    print()
    print("=" * 64)
    print(f"✓ SUCCESS — fp L recovered via {args.mode} without physical power cycle")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())

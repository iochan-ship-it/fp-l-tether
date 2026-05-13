#!/usr/bin/env python3
"""Step 5 — call sgm_ConfigAPI THEN sgm_SnapCommand.

Previous test returned 0xA081 (NOT_INITIALIZED) because we skipped
sgm_ConfigAPI which initializes the SDK's internal state for this camera
session. This script does the proper sequence:

  1. ICCameraDevice session open
  2. sgm_ConfigAPI:AdjustmentMode:cameraHandle: (initialize SDK)
  3. sgm_SnapCommand:cameraHandle: (fire shutter)
  4. (next iteration: GetCamCaptStatus, GetPictFileInfo2, GetBigPartialPictFile)

Type encodings (from runtime introspection):
  sgm_ConfigAPI:AdjustmentMode:cameraHandle:
    i36@0:8^{_IFDArray=II^{_SgmDirectoryEntry}}16I24@28
    → returns int, args: (IFDArray*, uint32 AdjustmentMode, ICCameraDevice*)

  sgm_SnapCommand:cameraHandle:
    i32@0:8^{_SgmSnapState=CCC}16@24
    → returns int, args: (SgmSnapState*, ICCameraDevice*)

Run with the camera connected, NO sudo::

    arch -x86_64 venv-x86/bin/python scripts/sigma_sdk_snap2.py
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SDK_FRAMEWORKS = PROJECT_ROOT / "sdk" / "Frameworks"

SIGMA_VENDOR_ID = 0x1003
SIGMA_FP_L_PID = 0xC442
SIGMA_FP_PID = 0xC432


# Forward declaration for the pointer type inside IFDArray
class SgmDirectoryEntry(ctypes.Structure):
    # We don't know the layout yet; treat as opaque
    _fields_ = []


class IFDArray(ctypes.Structure):
    """Sigma's "Internal File/Field Descriptor Array" — output struct for ConfigAPI.

    Layout per Obj-C type encoding ``{_IFDArray=II^{_SgmDirectoryEntry}}``:
      uint32  count;
      uint32  ???;  (capacity? unused?)
      SgmDirectoryEntry *entries;
    """
    _fields_ = [
        ("count", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("entries", ctypes.POINTER(SgmDirectoryEntry)),
    ]


class SgmSnapState(ctypes.Structure):
    _fields_ = [
        ("CaptureMode", ctypes.c_uint8),
        ("CaptureAmount", ctypes.c_uint8),
        ("CheckSum", ctypes.c_uint8),
    ]


def err_name(code: int) -> str:
    return {
        0: "OK",
        1: "SgmErrorCodeParameter",
        2: "SgmErrorCodeUsbError",
        0xA080: "PTP_RC_CHECKSUM_ERROR",
        0xA081: "PTP_RC_NOTINITIALIZED_ERROR",
    }.get(code, f"unknown 0x{code:04X} ({code})")


def main() -> int:
    print("=" * 70)
    print("Sigma SDK — ConfigAPI + SnapCommand live test")
    print("=" * 70)

    import objc
    from Foundation import NSBundle, NSDate, NSObject, NSRunLoop
    try:
        import ImageCaptureCore as ICC
    except ImportError as e:
        print(f"\n✗ ImageCaptureCore not importable: {e}")
        return 2

    # ------------------------------------------------------------------
    # Load frameworks
    # ------------------------------------------------------------------
    print("\n[1] Loading frameworks ...")
    fw_list = sorted(SDK_FRAMEWORKS.glob("*.framework"))
    sharedptp = SDK_FRAMEWORKS / "SharedPTP.framework"
    fw_list = [sharedptp] + [fw for fw in fw_list if fw != sharedptp]
    loaded = 0
    for fw_path in fw_list:
        b = NSBundle.bundleWithPath_(str(fw_path))
        if b and b.load():
            loaded += 1
    print(f"    Loaded {loaded}/{len(fw_list)}")

    # ------------------------------------------------------------------
    # Find camera
    # ------------------------------------------------------------------
    print("\n[2] Enumerating cameras ...")

    def spin(seconds: float) -> None:
        NSRunLoop.currentRunLoop().runUntilDate_(
            NSDate.dateWithTimeIntervalSinceNow_(seconds)
        )

    class BrowserDelegate(NSObject):
        def init(self):
            self = objc.super(BrowserDelegate, self).init()
            if self is None:
                return None
            self._devices = []
            self._finished = threading.Event()
            return self

        def deviceBrowser_didAddDevice_moreComing_(self, br, dev, more):  # noqa: N802
            self._devices.append(dev)
            if not more:
                self._finished.set()

        def deviceBrowser_didRemoveDevice_moreGoing_(self, br, dev, more):  # noqa: N802
            pass

    browser = ICC.ICDeviceBrowser.alloc().init()
    delegate = BrowserDelegate.alloc().init()
    browser.setDelegate_(delegate)
    browser.setBrowsedDeviceTypeMask_(0x0101)
    browser.start()
    elapsed = 0.0
    while not delegate._finished.is_set() and elapsed < 5.0:
        spin(0.25)
        elapsed += 0.25
    spin(0.5)
    browser.stop()

    sigma_fp_l = None
    for dev in delegate._devices:
        try:
            vid = int(dev.usbVendorID())
            pid = int(dev.usbProductID())
        except Exception:  # noqa: BLE001
            continue
        if vid == SIGMA_VENDOR_ID and pid in (SIGMA_FP_L_PID, SIGMA_FP_PID):
            sigma_fp_l = dev
            print(f"    ✓ {dev.name()}")
            break
    if sigma_fp_l is None:
        print("    ✗ Not found")
        return 1

    # ------------------------------------------------------------------
    # Open session
    # ------------------------------------------------------------------
    print("\n[3] Opening PTP session ...")

    class CamDelegate(NSObject):
        def init(self):
            self = objc.super(CamDelegate, self).init()
            if self is None:
                return None
            self.open_event = threading.Event()
            self.open_error = None
            self.close_event = threading.Event()
            return self

        def device_didOpenSessionWithError_(self, dev, err):  # noqa: N802
            self.open_error = err
            self.open_event.set()

        def device_didCloseSessionWithError_(self, dev, err):  # noqa: N802
            self.close_event.set()

        def didRemoveDevice_(self, dev):  # noqa: N802
            pass

    cdel = CamDelegate.alloc().init()
    sigma_fp_l.setDelegate_(cdel)
    sigma_fp_l.requestOpenSession()
    elapsed = 0.0
    while not cdel.open_event.is_set() and elapsed < 10.0:
        spin(0.25)
        elapsed += 0.25
    if not cdel.open_event.is_set() or cdel.open_error is not None:
        print(f"    ✗ open failed (err={cdel.open_error})")
        return 1
    print("    ✓ Session opened")

    try:
        # --------------------------------------------------------------
        # Set up ctypes / libobjc
        # --------------------------------------------------------------
        libobjc = ctypes.CDLL(ctypes.util.find_library("objc"))
        libobjc.objc_getClass.restype = ctypes.c_void_p
        libobjc.objc_getClass.argtypes = [ctypes.c_char_p]
        libobjc.sel_registerName.restype = ctypes.c_void_p
        libobjc.sel_registerName.argtypes = [ctypes.c_char_p]

        # Get the camera's raw Obj-C id pointer.
        # objc.pyobjc_id() returns the Python wrapper's id (wrong for us).
        # __c_void_p__() returns the actual Obj-C id (right).
        cam_void_p = sigma_fp_l.__c_void_p__()
        cam_ptr_val = int(cam_void_p.value) if cam_void_p.value else 0
        cam_ptr_pyobjc_id = objc.pyobjc_id(sigma_fp_l)
        print(f"\n[4] Camera pointers:")
        print(f"    __c_void_p__()   : 0x{cam_ptr_val:x}  ← real Obj-C id (use this)")
        print(f"    pyobjc_id()      : 0x{cam_ptr_pyobjc_id:x}  ← was being used (wrong)")
        cam_ptr = cam_void_p  # use the correct one as ctypes c_void_p

        # --------------------------------------------------------------
        # Step A: sgm_ConfigAPI:AdjustmentMode:cameraHandle:
        # --------------------------------------------------------------
        print("\n[5] Calling sgm_ConfigAPI ...")

        configapi_cls = libobjc.objc_getClass(b"sgm_ConfigAPI")
        configapi_sel = libobjc.sel_registerName(
            b"sgm_ConfigAPI:AdjustmentMode:cameraHandle:"
        )

        # Set up msgSend with this specific signature:
        # int (Class, SEL, IFDArray*, uint32, id)
        msg_send = libobjc.objc_msgSend
        msg_send.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(IFDArray),
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        msg_send.restype = ctypes.c_int

        ifd = IFDArray(count=0, reserved=0, entries=None)
        adjustment_mode = ctypes.c_uint32(0)  # Try 0 first

        result = msg_send(
            configapi_cls, configapi_sel,
            ctypes.byref(ifd), adjustment_mode, cam_ptr,
        )
        print(f"    ConfigAPI returned: {result} ({err_name(result)})")
        print(f"    IFDArray after call: count={ifd.count}, "
              f"reserved={ifd.reserved}, entries=0x{(ctypes.cast(ifd.entries, ctypes.c_void_p).value or 0):x}")

        if result != 0:
            print(f"    ⚠ ConfigAPI failed; SnapCommand will probably fail too")
            # Continue anyway so we can see what happens

        # --------------------------------------------------------------
        # Step B: sgm_SnapCommand:cameraHandle:
        # --------------------------------------------------------------
        print("\n[6] Calling sgm_SnapCommand ...")

        snap_cls = libobjc.objc_getClass(b"sgm_SnapCommand")
        snap_sel = libobjc.sel_registerName(b"sgm_SnapCommand:cameraHandle:")

        # Re-set msgSend with snap signature: int (Class, SEL, SgmSnapState*, id)
        # We need a fresh function pointer since argtypes is mutable global state
        msg_send2 = libobjc["objc_msgSend"]
        msg_send2.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(SgmSnapState),
            ctypes.c_void_p,
        ]
        msg_send2.restype = ctypes.c_int

        snap = SgmSnapState(CaptureMode=0x02, CaptureAmount=0x01, CheckSum=0x03)
        print(f"    SgmSnapState = {{mode=0x{snap.CaptureMode:02X}, "
              f"amount=0x{snap.CaptureAmount:02X}, cksum=0x{snap.CheckSum:02X}}}")
        print("    ★ Listen for the camera shutter ★")
        time.sleep(0.5)

        result = msg_send2(snap_cls, snap_sel, ctypes.byref(snap), cam_ptr)
        print(f"\n    SnapCommand returned: {result} ({err_name(result)})")

        if result == 0:
            print(f"    🎯🎯🎯 SUCCESS — official SDK fired the shutter from Python")

        time.sleep(1.5)  # Let the camera finish processing
        return 0 if result == 0 else 1

    finally:
        print("\n[7] Closing PTP session ...")
        sigma_fp_l.requestCloseSession()
        elapsed = 0.0
        while not cdel.close_event.is_set() and elapsed < 5.0:
            spin(0.25)
            elapsed += 0.25
        print("    ✓ Done")


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Step 4 — call sgm_SnapCommand against the connected Sigma fp L.

We use ctypes + objc_msgSend to bypass PyObjC's class-method discovery
(which doesn't expose Sigma's class methods automatically because there's
no BridgeSupport metadata).

The method signature (from Obj-C runtime introspection):
    + (int)sgm_SnapCommand:(SgmSnapState *)inSnapState
              cameraHandle:(ICCameraDevice *)inCameraHandle;
    Type encoding: i32@0:8^{_SgmSnapState=CCC}16@24

Run with the camera connected, NO sudo needed::

    arch -x86_64 venv-x86/bin/python scripts/sigma_sdk_snap.py
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SDK_FRAMEWORKS = PROJECT_ROOT / "sdk" / "Frameworks"

SIGMA_VENDOR_ID = 0x1003
SIGMA_FP_L_PID = 0xC442
SIGMA_FP_PID = 0xC432


class SgmSnapState(ctypes.Structure):
    """The 3-byte input struct for sgm_SnapCommand."""
    _fields_ = [
        ("CaptureMode", ctypes.c_uint8),
        ("CaptureAmount", ctypes.c_uint8),
        ("CheckSum", ctypes.c_uint8),
    ]


def main() -> int:
    print("=" * 70)
    print("Sigma SDK — sgm_SnapCommand live camera test (ctypes + objc_msgSend)")
    print("=" * 70)

    import objc
    from Foundation import NSBundle, NSDate, NSObject, NSRunLoop
    try:
        import ImageCaptureCore as ICC
    except ImportError as e:
        print(f"\n✗ ImageCaptureCore not importable: {e}")
        return 2

    # ------------------------------------------------------------------
    # Load Sigma frameworks
    # ------------------------------------------------------------------
    print("\n[1] Loading Sigma frameworks ...")
    fw_list = sorted(SDK_FRAMEWORKS.glob("*.framework"))
    sharedptp = SDK_FRAMEWORKS / "SharedPTP.framework"
    fw_list = [sharedptp] + [fw for fw in fw_list if fw != sharedptp]
    loaded = 0
    for fw_path in fw_list:
        b = NSBundle.bundleWithPath_(str(fw_path))
        if b and b.load():
            loaded += 1
    print(f"    Loaded {loaded}/{len(fw_list)} frameworks")

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
    # Open PTP session via ImageCaptureCore
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
        # Set up ctypes + libobjc
        # --------------------------------------------------------------
        print("\n[4] Setting up ctypes / libobjc ...")
        libobjc = ctypes.CDLL(ctypes.util.find_library("objc"))
        libobjc.objc_getClass.restype = ctypes.c_void_p
        libobjc.objc_getClass.argtypes = [ctypes.c_char_p]
        libobjc.sel_registerName.restype = ctypes.c_void_p
        libobjc.sel_registerName.argtypes = [ctypes.c_char_p]

        # objc_msgSend with our specific signature:
        # int (Class, SEL, SgmSnapState*, ICCameraDevice*)
        # We'll redefine its prototype right before calling.
        msg_send = libobjc.objc_msgSend
        msg_send.argtypes = [
            ctypes.c_void_p,  # receiver (Class ptr)
            ctypes.c_void_p,  # selector
            ctypes.POINTER(SgmSnapState),  # struct pointer
            ctypes.c_void_p,  # camera handle (id)
        ]
        msg_send.restype = ctypes.c_int

        cls_ptr = libobjc.objc_getClass(b"sgm_SnapCommand")
        sel = libobjc.sel_registerName(b"sgm_SnapCommand:cameraHandle:")
        print(f"    sgm_SnapCommand class ptr: 0x{cls_ptr:x}")
        print(f"    selector ptr:              0x{sel:x}")

        # Get the camera's raw void* from PyObjC
        cam_ptr = objc.pyobjc_id(sigma_fp_l)
        print(f"    camera id ptr:             0x{cam_ptr:x}")

        # --------------------------------------------------------------
        # Build SgmSnapState: 1 still shot, non-AF
        # --------------------------------------------------------------
        snap_state = SgmSnapState(
            CaptureMode=0x02,    # NON_AF_CAPTURE
            CaptureAmount=0x01,  # 1 shot
            CheckSum=0x03,       # 0x02 + 0x01 (best guess)
        )
        print(f"    SgmSnapState = {{mode=0x{snap_state.CaptureMode:02X}, "
              f"amount=0x{snap_state.CaptureAmount:02X}, "
              f"cksum=0x{snap_state.CheckSum:02X}}}")

        # --------------------------------------------------------------
        # Call sgm_SnapCommand
        # --------------------------------------------------------------
        print("\n[5] Calling sgm_SnapCommand:cameraHandle: ...")
        print("    ★ Listen for the camera shutter ★")
        result = msg_send(cls_ptr, sel, ctypes.byref(snap_state), cam_ptr)
        print(f"\n    Return value: {result}")

        # Interpret return codes (from SGMErrorCode.h):
        # 0   = success
        # 1   = SgmErrorCodeParameter
        # 2   = SgmErrorCodeUsbError
        if result == 0:
            print(f"    🎯🎯🎯 SUCCESS — Sigma SDK returned 0 (OK)")
            print(f"    If you heard a shutter click, the official SDK works.")
        elif result == 1:
            print(f"    ⚠ ErrorParameter — payload format issue")
        elif result == 2:
            print(f"    ⚠ ErrorUsbError — USB communication problem")
        else:
            print(f"    ⚠ Unknown error code {result}")

        # Brief pause so any beeps/clicks are observable
        import time
        time.sleep(1.0)

        return 0 if result == 0 else 1

    finally:
        print("\n[6] Closing PTP session ...")
        sigma_fp_l.requestCloseSession()
        elapsed = 0.0
        while not cdel.close_event.is_set() and elapsed < 5.0:
            spin(0.25)
            elapsed += 0.25
        print("    ✓ Done")


if __name__ == "__main__":
    raise SystemExit(main())

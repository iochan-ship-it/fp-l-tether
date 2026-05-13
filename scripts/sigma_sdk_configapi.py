#!/usr/bin/env python3
"""Step 3 — call sgm_ConfigAPI against the connected Sigma fp L.

This is the FIRST end-to-end test of the Sigma SDK from Python. If it
returns a success status, we've confirmed:

  1. The SDK frameworks load and dispatch correctly
  2. PyObjC can pass an ICCameraDevice handle to Sigma class methods
  3. The Sigma SDK can talk to the actual camera over ImageCaptureCore

ConfigAPI is chosen because:
  - It has no input struct (just the camera handle)
  - It's the official "handshake" call recommended at startup
  - It returns a single int — easy to interpret

Workflow:
  1. Load all 28 SDK frameworks
  2. Enumerate cameras via ICDeviceBrowser, pick Sigma fp L
  3. Open ICCameraDevice session
  4. Look up sgm_ConfigAPI class
  5. Probe for the actual method name (sgm_ConfigAPI_cameraHandle_, etc.)
  6. Call it, print the return value
  7. Close session, exit

Run::

    arch -x86_64 venv-x86/bin/python scripts/sigma_sdk_configapi.py

No sudo needed — Sigma SDK uses ImageCaptureCore which doesn't need kernel
driver detach.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SDK_FRAMEWORKS = PROJECT_ROOT / "sdk" / "Frameworks"

SIGMA_VENDOR_ID = 0x1003
SIGMA_FP_L_PID = 0xC442
SIGMA_FP_PID = 0xC432


def main() -> int:
    print("=" * 70)
    print("Sigma SDK — sgm_ConfigAPI live camera test")
    print("=" * 70)

    import objc
    from Foundation import NSBundle, NSDate, NSObject, NSRunLoop

    try:
        import ImageCaptureCore as ICC
    except ImportError as e:
        print(f"\n✗ ImageCaptureCore not importable: {e}")
        print("  Make sure you ran setup_x86_venv.sh and are running with arch -x86_64")
        return 2

    # --------------------------------------------------------------
    # 1) Load all Sigma frameworks
    # --------------------------------------------------------------
    print("\n[1] Loading Sigma frameworks ...")
    fw_list = sorted(SDK_FRAMEWORKS.glob("*.framework"))
    sharedptp = SDK_FRAMEWORKS / "SharedPTP.framework"
    fw_list = [sharedptp] + [fw for fw in fw_list if fw != sharedptp]
    loaded = 0
    for fw_path in fw_list:
        bundle = NSBundle.bundleWithPath_(str(fw_path))
        if bundle and bundle.load():
            loaded += 1
    print(f"    Loaded {loaded}/{len(fw_list)} frameworks")
    if loaded == 0:
        return 1

    # --------------------------------------------------------------
    # 2) Find the camera via ICDeviceBrowser
    # --------------------------------------------------------------
    print("\n[2] Enumerating cameras via ICDeviceBrowser ...")

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

        def deviceBrowser_didAddDevice_moreComing_(self, browser, device, moreComing):  # noqa: N802
            self._devices.append(device)
            if not moreComing:
                self._finished.set()

        def deviceBrowser_didRemoveDevice_moreGoing_(self, browser, device, moreGoing):  # noqa: N802
            pass

    browser = ICC.ICDeviceBrowser.alloc().init()
    delegate = BrowserDelegate.alloc().init()
    browser.setDelegate_(delegate)
    # camera (0x01) + local (0x100)
    browser.setBrowsedDeviceTypeMask_(0x0101)
    browser.start()

    elapsed = 0.0
    while not delegate._finished.is_set() and elapsed < 5.0:
        spin(0.25)
        elapsed += 0.25
    spin(0.5)  # extra
    browser.stop()

    sigma_fp_l = None
    for dev in delegate._devices:
        try:
            vid = int(dev.usbVendorID())
            pid = int(dev.usbProductID())
        except Exception:  # noqa: BLE001
            vid = pid = None
        name = str(dev.name() or "")
        vid_str = f"0x{vid:04X}" if vid else "(unknown)"
        pid_str = f"0x{pid:04X}" if pid else "(unknown)"
        print(f"    Found: {name}  VID={vid_str}  PID={pid_str}")
        if vid == SIGMA_VENDOR_ID and pid in (SIGMA_FP_L_PID, SIGMA_FP_PID):
            sigma_fp_l = dev

    if sigma_fp_l is None:
        print("    ✗ No Sigma fp/fp L found")
        return 1
    print(f"    ✓ Selected: {sigma_fp_l.name()}")

    # --------------------------------------------------------------
    # 3) Open PTP session via ImageCaptureCore
    # --------------------------------------------------------------
    print("\n[3] Opening PTP session via ICCameraDevice ...")

    class CameraDelegate(NSObject):
        def init(self):
            self = objc.super(CameraDelegate, self).init()
            if self is None:
                return None
            self.open_event = threading.Event()
            self.open_error = None
            self.close_event = threading.Event()
            return self

        def device_didOpenSessionWithError_(self, device, error):  # noqa: N802
            self.open_error = error
            self.open_event.set()

        def device_didCloseSessionWithError_(self, device, error):  # noqa: N802
            self.close_event.set()

        def didRemoveDevice_(self, device):  # noqa: N802
            pass

    cam_delegate = CameraDelegate.alloc().init()
    sigma_fp_l.setDelegate_(cam_delegate)
    sigma_fp_l.requestOpenSession()

    elapsed = 0.0
    while not cam_delegate.open_event.is_set() and elapsed < 10.0:
        spin(0.25)
        elapsed += 0.25
    if not cam_delegate.open_event.is_set():
        print("    ✗ Timeout waiting for session open")
        return 1
    if cam_delegate.open_error is not None:
        print(f"    ✗ Session open error: {cam_delegate.open_error}")
        return 1
    print("    ✓ Session opened")

    try:
        # --------------------------------------------------------------
        # 4) Find sgm_ConfigAPI and call it
        # --------------------------------------------------------------
        print("\n[4] Looking up sgm_ConfigAPI class ...")
        try:
            cls = objc.lookUpClass("sgm_ConfigAPI")
        except objc.error as e:
            print(f"    ✗ Class lookup failed: {e}")
            return 1
        print(f"    ✓ Class: {cls}")

        # Class methods (+ in Obj-C) live in cls.pyobjc_classMethods
        # in PyObjC, NOT directly on cls. This is the canonical way to
        # access them when BridgeSupport metadata isn't provided.
        try:
            cm = cls.pyobjc_classMethods
            print(f"    ✓ Got pyobjc_classMethods namespace")
        except AttributeError:
            cm = cls  # fallback
            print(f"    ⚠ pyobjc_classMethods not available, using cls directly")

        # Dump all class method names for diagnostics
        all_cm = sorted(
            a for a in dir(cm)
            if not a.startswith("_") and not a.startswith("pyobjc")
        )
        print(f"    Class-method-namespace attrs ({len(all_cm)} total):")
        for a in all_cm[:30]:
            print(f"      {a}")
        if len(all_cm) > 30:
            print(f"      ... +{len(all_cm) - 30} more")

        # Probe likely method names
        candidates = [
            "sgm_ConfigAPI_cameraHandle_",
            "sgm_ConfigApi_cameraHandle_",
            "configAPI_cameraHandle_",
            "ConfigAPI_cameraHandle_",
        ]
        method = None
        method_name = None
        for name in candidates:
            m = getattr(cm, name, None)
            if m is not None:
                method = m
                method_name = name
                break
        if method is None:
            print(f"    ✗ None of {candidates} found")
            return 1
        print(f"    ✓ Method: {method_name}")

        # Some Sigma calls take just (cameraHandle), others take (struct*, cameraHandle).
        # ConfigAPI from header was: + (int)sgm_ConfigApi:(...) cameraHandle:(ICCameraDevice*)
        # Try with cameraHandle only first.
        print(f"\n[5] Calling {method_name}(cameraHandle) ...")
        try:
            # Most Sigma class methods take 2 args (first arg + cameraHandle).
            # For ConfigAPI which has no struct, the first arg is typically nil.
            result = method(None, sigma_fp_l)
        except Exception as e:
            # Maybe the signature is different — try with just the camera
            print(f"    Two-arg call raised: {e!r}")
            print(f"    Trying single-arg call (just cameraHandle)...")
            try:
                result = method(sigma_fp_l)
            except Exception as e2:
                print(f"    Single-arg call raised: {e2!r}")
                return 1

        print(f"    ★ Return value: {result!r}")

        # Sigma SDK return codes:
        #   0 = success
        #   non-zero = error code (see SGMErrorCode.h)
        if isinstance(result, int):
            if result == 0:
                print(f"    🎯 SUCCESS — ConfigAPI returned 0 (OK)")
            else:
                print(f"    ⚠ Returned error code {result} "
                      f"(see SGMErrorCode.h; common: 1=Parameter, 2=USB)")

        return 0 if isinstance(result, int) and result == 0 else 1

    finally:
        # --------------------------------------------------------------
        # 6) Close session
        # --------------------------------------------------------------
        print("\n[6] Closing PTP session ...")
        sigma_fp_l.requestCloseSession()
        elapsed = 0.0
        while not cam_delegate.close_event.is_set() and elapsed < 5.0:
            spin(0.25)
            elapsed += 0.25
        print("    ✓ Done")


if __name__ == "__main__":
    raise SystemExit(main())

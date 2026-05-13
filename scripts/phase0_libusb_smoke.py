#!/usr/bin/env python3
"""Phase 0 pivot — libusb-level smoke test (robust version).

Tries multiple ways to load libusb on macOS:
  1. libusb_package.find() — bundled libusb via libusb-package
  2. libusb_package.find_library() + explicit backend
  3. Default pyusb backend (uses system-installed libusb if any)
  4. Common macOS paths (/usr/local/lib, /opt/homebrew/lib)

Reports which path worked, then tests device enumeration and claim.
"""

from __future__ import annotations

import ctypes.util
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fp_l_tether.camera.ptp_codes import (  # noqa: E402
    SIGMA_FP_L_PRODUCT_ID,
    SIGMA_FP_PRODUCT_ID,
    SIGMA_VENDOR_ID,
)


def header(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def try_libusb_package_find_library():
    """Return (path, error) tuple. path is None on failure."""
    try:
        import libusb_package  # type: ignore[import-not-found]
    except ImportError as e:
        return None, f"libusb_package not installed: {e}"

    try:
        path = libusb_package.find_library("usb-1.0")
        if path:
            return path, None
        return None, "find_library returned empty/None"
    except Exception as e:  # noqa: BLE001
        return None, f"find_library raised: {e!r}"


def try_libusb_package_get_backend():
    try:
        import libusb_package  # type: ignore[import-not-found]
        backend = libusb_package.get_libusb1_backend()
        return backend, None
    except Exception as e:  # noqa: BLE001
        return None, f"raised: {e!r}"


def try_default_backend():
    try:
        import usb.backend.libusb1  # type: ignore[import-not-found]
        backend = usb.backend.libusb1.get_backend()
        return backend, None
    except Exception as e:  # noqa: BLE001
        return None, f"raised: {e!r}"


def try_explicit_path(path: str):
    """Try loading libusb from a specific path."""
    try:
        import usb.backend.libusb1  # type: ignore[import-not-found]
        backend = usb.backend.libusb1.get_backend(find_library=lambda x: path)
        if backend is None:
            return None, "backend is None"
        return backend, None
    except Exception as e:  # noqa: BLE001
        return None, f"raised: {e!r}"


def main() -> int:
    header("Phase 0 libusb smoke — robust backend resolver")

    # Show what we have installed
    print("\nChecking installed packages:")
    try:
        import libusb_package
        print(f"  libusb_package : {libusb_package.__file__}")
    except ImportError as e:
        print(f"  libusb_package : NOT INSTALLED ({e})")

    try:
        import usb
        print(f"  pyusb (usb)    : {usb.__file__} (version {usb.__version__})")
    except ImportError as e:
        print(f"  pyusb (usb)    : NOT INSTALLED ({e})")
        return 2

    import usb.core
    import usb.util

    # ------------------------------------------------------------------
    # Try several ways to get a libusb backend
    # ------------------------------------------------------------------
    header("Trying libusb backend candidates")

    candidates: list[tuple[str, object, str | None]] = []

    # 1. libusb_package.find_library
    path, err = try_libusb_package_find_library()
    print(f"\n[1] libusb_package.find_library('usb-1.0'):")
    if path:
        print(f"    ✓ Found: {path}")
        if os.path.exists(path):
            print(f"    File exists: yes ({os.path.getsize(path)} bytes)")
        else:
            print(f"    File exists: NO — path is stale")
        backend, e2 = try_explicit_path(path)
        candidates.append(("libusb_package.find_library + explicit", backend, e2))
    else:
        print(f"    ✗ {err}")

    # 2. libusb_package.get_libusb1_backend
    backend, err = try_libusb_package_get_backend()
    print(f"\n[2] libusb_package.get_libusb1_backend():")
    if backend is not None:
        print(f"    ✓ Backend: {backend}")
        candidates.append(("libusb_package.get_libusb1_backend", backend, None))
    else:
        print(f"    ✗ Returned None ({err})")

    # 3. Default pyusb backend (system libusb)
    backend, err = try_default_backend()
    print(f"\n[3] usb.backend.libusb1.get_backend() (system):")
    if backend is not None:
        print(f"    ✓ Backend: {backend}")
        candidates.append(("system libusb", backend, None))
    else:
        print(f"    ✗ {err or 'returned None'}")

    # 4. Common macOS paths
    common_paths = [
        "/usr/local/lib/libusb-1.0.dylib",
        "/opt/homebrew/lib/libusb-1.0.dylib",
        "/usr/lib/libusb-1.0.dylib",
    ]
    for p in common_paths:
        print(f"\n[?] Trying {p}:")
        if not os.path.exists(p):
            print(f"    Not present")
            continue
        backend, err = try_explicit_path(p)
        if backend is not None:
            print(f"    ✓ Backend: {backend}")
            candidates.append((f"explicit {p}", backend, None))
        else:
            print(f"    ✗ {err}")

    # 5. ctypes.util.find_library
    auto = ctypes.util.find_library("usb-1.0")
    print(f"\n[?] ctypes.util.find_library('usb-1.0'): {auto}")
    if auto:
        backend, err = try_explicit_path(auto)
        if backend is not None:
            candidates.append((f"ctypes find_library: {auto}", backend, None))

    if not candidates:
        header("✗ No working libusb backend found")
        print("\nNext steps to try (pick the easiest):")
        print()
        print("Option A — Install Homebrew + libusb (most reliable):")
        print('  /bin/bash -c "$(curl -fsSL '
              'https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"')
        print("  brew install libusb")
        print()
        print("Option B — Force-install libusb-package as binary wheel:")
        print("  pip install --upgrade --force-reinstall --no-cache-dir "
              "--prefer-binary libusb-package")
        print()
        print("Option C — Try pyusb's libusb0 backend (legacy):")
        print("  pip install pyusb libusb1")
        print()
        print("After installing, re-run this script.")
        return 1

    # ------------------------------------------------------------------
    # Use the first working backend to enumerate
    # ------------------------------------------------------------------
    header("Working backends — trying device enumeration")
    for name, backend, _ in candidates:
        print(f"\nUsing backend: {name}")
        print(f"  Searching for VID 0x{SIGMA_VENDOR_ID:04X}…")

        found_any = False
        for pid in (SIGMA_FP_L_PRODUCT_ID, SIGMA_FP_PRODUCT_ID):
            try:
                for dev in usb.core.find(
                    idVendor=SIGMA_VENDOR_ID,
                    idProduct=pid,
                    find_all=True,
                    backend=backend,
                ):
                    found_any = True
                    describe(dev)
                    # try claim
                    test_claim(dev)
            except Exception as e:  # noqa: BLE001
                print(f"    find() raised: {e!r}")

        if found_any:
            print("\n" + "=" * 70)
            print(f"✓ libusb smoke test PASSED via backend: {name}")
            print("=" * 70)
            return 0
        else:
            print("    (no Sigma device via this backend)")

    print("\n✗ No backend could see the Sigma fp L.")
    print("  Camera is on, USB connected, USB mode = Camera Control?")
    return 1


def describe(dev) -> None:
    import usb.util
    print(f"  --- Device found ---")
    try:
        print(f"  Bus.Address     : {dev.bus}.{dev.address}")
        print(f"  Vendor ID       : 0x{dev.idVendor:04X}")
        print(f"  Product ID      : 0x{dev.idProduct:04X}")
        print(f"  USB version     : 0x{dev.bcdUSB:04X}")
        print(f"  Device version  : 0x{dev.bcdDevice:04X}")
    except Exception as e:  # noqa: BLE001
        print(f"  Descriptor read failed: {e!r}")
        return

    for name, idx in (("Manufacturer", dev.iManufacturer),
                      ("Product     ", dev.iProduct),
                      ("Serial      ", dev.iSerialNumber)):
        try:
            print(f"  {name}    : {usb.util.get_string(dev, idx)}")
        except Exception as e:  # noqa: BLE001
            print(f"  {name}    : (failed: {e!r})")

    try:
        for cfg in dev:
            print(f"  Config {cfg.bConfigurationValue}:")
            for intf in cfg:
                print(f"    Interface {intf.bInterfaceNumber}: "
                      f"class=0x{intf.bInterfaceClass:02X} "
                      f"(0x06 = Still Image / PTP)")
                for ep in intf:
                    dir_str = "IN" if usb.util.endpoint_direction(ep.bEndpointAddress) else "OUT"
                    tt = ep.bmAttributes & 0x03
                    type_str = {0: "CTRL", 1: "ISO", 2: "BULK", 3: "INT"}.get(tt, "?")
                    print(f"      EP 0x{ep.bEndpointAddress:02X} {type_str}/{dir_str} "
                          f"max_pkt={ep.wMaxPacketSize}")
    except Exception as e:  # noqa: BLE001
        print(f"  Config enumeration failed: {e!r}")


def test_claim(dev) -> None:
    import usb.core
    import usb.util
    print(f"\n  Testing interface claim…")
    try:
        try:
            if dev.is_kernel_driver_active(0):
                print(f"    Kernel driver IS active — attempting detach")
                dev.detach_kernel_driver(0)
                print(f"    ✓ detached")
            else:
                print(f"    Kernel driver not active")
        except NotImplementedError:
            print(f"    (is_kernel_driver_active not implemented on macOS)")
        except usb.core.USBError as e:
            print(f"    detach failed: {e!r}")

        usb.util.claim_interface(dev, 0)
        print(f"    ✓ Successfully claimed interface 0")
        usb.util.release_interface(dev, 0)
        print(f"    ✓ Released")
    except usb.core.USBError as e:
        print(f"    ✗ claim failed: {e!r}")


if __name__ == "__main__":
    raise SystemExit(main())

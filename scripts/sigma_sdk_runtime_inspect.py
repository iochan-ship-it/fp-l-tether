#!/usr/bin/env python3
"""Use the Obj-C runtime directly (via ctypes) to enumerate ALL methods on
the Sigma SDK classes — both instance methods AND class methods on the
metaclass — bypassing PyObjC's filtering.

Also dumps the contents of each framework's Header file so we see the
canonical Obj-C selector names.

Run::

    arch -x86_64 venv-x86/bin/python scripts/sigma_sdk_runtime_inspect.py
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SDK_FRAMEWORKS = PROJECT_ROOT / "sdk" / "Frameworks"


def dump_headers() -> None:
    """Print the Header file contents for the most-needed frameworks."""
    print("=" * 70)
    print("Framework Header files (the canonical Obj-C declarations)")
    print("=" * 70)
    for name in [
        "ConfigAPI",
        "SnapCommand",
        "GetCamCaptStatus",
        "GetPictFileInfo2",
        "GetBigPartialPictFile",
        "ClearImageDBSingle",
    ]:
        fw = SDK_FRAMEWORKS / f"{name}.framework"
        # The header could be in either Headers/ or Versions/A/Headers/
        for candidate in [
            fw / "Headers" / f"{name}.h",
            fw / "Versions" / "A" / "Headers" / f"{name}.h",
        ]:
            if candidate.exists():
                print(f"\n--- {candidate.relative_to(PROJECT_ROOT)} ---")
                try:
                    text = candidate.read_text(errors="replace")
                    # Show first 60 lines
                    for line in text.splitlines()[:60]:
                        print(f"  {line}")
                except Exception as e:  # noqa: BLE001
                    print(f"  (error reading: {e!r})")
                break
        else:
            print(f"\n--- {name}: header not found ---")


def list_runtime_methods(class_name: str) -> None:
    """Use ctypes + libobjc to enumerate all methods on a class AND its
    metaclass."""
    import objc

    # Load all frameworks first
    from Foundation import NSBundle
    fw_list = sorted(SDK_FRAMEWORKS.glob("*.framework"))
    sharedptp = SDK_FRAMEWORKS / "SharedPTP.framework"
    fw_list = [sharedptp] + [fw for fw in fw_list if fw != sharedptp]
    for fw_path in fw_list:
        bundle = NSBundle.bundleWithPath_(str(fw_path))
        if bundle:
            bundle.load()

    libobjc_path = ctypes.util.find_library("objc")
    libobjc = ctypes.CDLL(libobjc_path)

    libobjc.objc_getClass.restype = ctypes.c_void_p
    libobjc.objc_getClass.argtypes = [ctypes.c_char_p]
    libobjc.object_getClass.restype = ctypes.c_void_p
    libobjc.object_getClass.argtypes = [ctypes.c_void_p]
    libobjc.class_copyMethodList.restype = ctypes.POINTER(ctypes.c_void_p)
    libobjc.class_copyMethodList.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    libobjc.method_getName.restype = ctypes.c_void_p
    libobjc.method_getName.argtypes = [ctypes.c_void_p]
    libobjc.method_getTypeEncoding.restype = ctypes.c_char_p
    libobjc.method_getTypeEncoding.argtypes = [ctypes.c_void_p]
    libobjc.sel_getName.restype = ctypes.c_char_p
    libobjc.sel_getName.argtypes = [ctypes.c_void_p]
    libobjc.free.restype = None
    libobjc.free.argtypes = [ctypes.c_void_p]

    def list_methods(class_ptr: int, label: str) -> None:
        count = ctypes.c_uint(0)
        methods_ptr = libobjc.class_copyMethodList(class_ptr, ctypes.byref(count))
        print(f"\n  {label}: {count.value} methods")
        names: list[tuple[str, str]] = []
        for i in range(count.value):
            method = methods_ptr[i]
            sel = libobjc.method_getName(method)
            name = libobjc.sel_getName(sel)
            type_enc = libobjc.method_getTypeEncoding(method)
            names.append((
                name.decode("ascii", errors="replace") if name else "?",
                type_enc.decode("ascii", errors="replace") if type_enc else "?",
            ))
        names.sort()
        for n, te in names:
            marker = "  ★" if n.startswith("sgm_") or "sgm" in n else "   "
            print(f"  {marker} {n:50s}  {te}")
        # free the array (each Method itself doesn't need freeing)
        libobjc.free(ctypes.cast(methods_ptr, ctypes.c_void_p))

    # Get the class
    class_ptr = libobjc.objc_getClass(class_name.encode("ascii"))
    if class_ptr == 0:
        print(f"\n  Class {class_name} not found")
        return
    print(f"\n=== Class: {class_name} (ptr 0x{class_ptr:x}) ===")
    list_methods(class_ptr, "Instance methods")
    metaclass_ptr = libobjc.object_getClass(class_ptr)
    list_methods(metaclass_ptr, "Class methods (on metaclass)")


def main() -> int:
    print("=" * 70)
    print("Sigma SDK runtime introspection (ctypes + libobjc)")
    print("=" * 70)

    # 1. Dump Header files for definitive method declarations
    dump_headers()

    # 2. Enumerate runtime methods on key classes
    print()
    print("=" * 70)
    print("Obj-C runtime method enumeration")
    print("=" * 70)
    for cls_name in [
        "sgm_APIBase",
        "sgm_ConfigAPI",
        "sgm_SnapCommand",
        "sgm_GetCamCaptStatus",
    ]:
        list_runtime_methods(cls_name)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

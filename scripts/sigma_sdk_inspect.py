#!/usr/bin/env python3
"""Step 2.5 — list all sgm_* classes registered after loading the SDK,
plus dump their method signatures (class methods AND instance methods).

This tells us the EXACT class names and selectors to call from Python.

Run::

    arch -x86_64 venv-x86/bin/python scripts/sigma_sdk_inspect.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SDK_FRAMEWORKS = PROJECT_ROOT / "sdk" / "Frameworks"


def main() -> int:
    print("=" * 70)
    print("Sigma SDK class & method introspection")
    print("=" * 70)

    import objc
    from Foundation import NSBundle

    # Load all frameworks (SharedPTP first)
    fw_list = sorted(SDK_FRAMEWORKS.glob("*.framework"))
    sharedptp = SDK_FRAMEWORKS / "SharedPTP.framework"
    fw_list = [sharedptp] + [fw for fw in fw_list if fw != sharedptp]
    for fw_path in fw_list:
        bundle = NSBundle.bundleWithPath_(str(fw_path))
        if bundle:
            bundle.load()

    # Enumerate ALL classes whose name starts with "sgm_"
    print("\n--- All sgm_* classes registered in the Obj-C runtime ---")
    sgm_classes: list = []
    for cls in objc.getClassList():
        name = cls.__name__
        if name.startswith("sgm_"):
            sgm_classes.append(cls)
    sgm_classes.sort(key=lambda c: c.__name__)
    print(f"Found {len(sgm_classes)} sgm_* classes:\n")
    for cls in sgm_classes:
        print(f"  {cls.__name__}")

    # For each, dump class methods (+ in Obj-C) with their signatures
    print("\n" + "=" * 70)
    print("Method signatures")
    print("=" * 70)

    for cls in sgm_classes:
        print(f"\n## {cls.__name__}")
        # The metaclass holds class methods
        try:
            method_list = cls.pyobjc_classMethods.__dir__()
        except AttributeError:
            method_list = []

        # Filter for sgm_ named selectors
        if not method_list:
            # Fallback: scan all attributes
            method_list = [n for n in dir(cls) if n.startswith("sgm_")]

        for m_name in sorted(method_list):
            if not m_name.startswith("sgm_"):
                continue
            try:
                m = getattr(cls, m_name)
                # PyObjC selectors have .selector and .signature attributes
                selector = getattr(m, "selector", None)
                sig = getattr(m, "signature", None)
                if isinstance(selector, bytes):
                    selector = selector.decode("ascii", errors="replace")
                if isinstance(sig, bytes):
                    sig = sig.decode("ascii", errors="replace")
                print(f"   + {m_name}")
                if selector:
                    print(f"      Obj-C selector: {selector}")
                if sig:
                    print(f"      type encoding : {sig}")
            except Exception as e:  # noqa: BLE001
                print(f"   + {m_name}  (error: {e!r})")

    print("\n" + "=" * 70)
    print("Done.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

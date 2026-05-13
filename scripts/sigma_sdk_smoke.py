#!/usr/bin/env python3
"""Step 2 — load Sigma SDK frameworks via PyObjC and introspect them.

Reads ``sdk/Frameworks/*.framework`` and:
  1. Loads SharedPTP first (everything else depends on it)
  2. Loads each per-operation framework (ConfigAPI, SnapCommand, etc.)
  3. Looks up each ``sgm_*`` class via PyObjC's class registry
  4. Prints the available methods so we know exact signatures

We do NOT yet call any Sigma functions — that's the next step. This script
just confirms the frameworks load cleanly and PyObjC can see the classes.

Run::

    python scripts/sigma_sdk_smoke.py

No sudo needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SDK_FRAMEWORKS = PROJECT_ROOT / "sdk" / "Frameworks"


def main() -> int:
    print("=" * 70)
    print("Sigma SDK PyObjC bridge — load test")
    print("=" * 70)

    if not SDK_FRAMEWORKS.is_dir():
        print(f"\n✗ {SDK_FRAMEWORKS} not found. Run setup_sigma_sdk.py first.")
        return 1

    try:
        import objc
        from Foundation import NSBundle
    except ImportError as e:
        print(f"\n✗ PyObjC not available: {e}")
        return 2

    # Load order: SharedPTP first, then any others
    # We're going to load all 28 to be safe.
    print(f"\n--- Loading frameworks from {SDK_FRAMEWORKS} ---")

    # SharedPTP MUST be first
    fw_list = sorted(SDK_FRAMEWORKS.glob("*.framework"))
    sharedptp = SDK_FRAMEWORKS / "SharedPTP.framework"
    if sharedptp not in fw_list:
        print(f"  ✗ SharedPTP.framework not found at {sharedptp}")
        return 1
    # Reorder so SharedPTP is first
    fw_list = [sharedptp] + [fw for fw in fw_list if fw != sharedptp]

    loaded_bundles: dict[str, NSBundle] = {}
    for fw_path in fw_list:
        name = fw_path.stem  # e.g. "SnapCommand"
        bundle = NSBundle.bundleWithPath_(str(fw_path))
        if bundle is None:
            print(f"  ✗ {name}: NSBundle.bundleWithPath_ returned None")
            continue
        ok = bundle.load()
        if not ok:
            print(f"  ✗ {name}: bundle.load() returned False — "
                  f"likely Gatekeeper / signature issue")
            # Print why
            err = bundle.preflightAndReturnError_(None)
            if err:
                print(f"     preflight error: {err}")
            continue
        loaded_bundles[name] = bundle
        print(f"  ✓ {name}: loaded")

    if not loaded_bundles:
        print("\n✗ No frameworks loaded. Cannot proceed.")
        return 1

    # ------------------------------------------------------------------
    # Look up the sgm_* classes and dump their methods
    # ------------------------------------------------------------------
    print(f"\n--- Sigma classes registered in the Objective-C runtime ---")

    interesting_classes = [
        "sgm_ConfigApi",
        "sgm_GetCamCaptStatus",
        "sgm_GetCamOpPermission",
        "sgm_SnapCommand",
        "sgm_GetPictFileInfo2",
        "sgm_GetBigPartialPictFile",
        "sgm_ClearImageDBSingle",
        "sgm_CloseApplication",
        "sgm_GetCamDataGroup1",
        "sgm_GetCamDataGroup3",
        "sgm_SetCamDataGroup3",
    ]

    for cls_name in interesting_classes:
        try:
            cls = objc.lookUpClass(cls_name)
        except objc.error as e:
            print(f"\n  ✗ {cls_name}: lookup failed ({e})")
            continue

        print(f"\n  ✓ {cls_name}: found")
        # List class methods (the + methods in Obj-C)
        meta = cls.class__()  # the metaclass holds class methods
        class_methods = meta.instanceMethods() if hasattr(meta, "instanceMethods") else []
        # PyObjC: use dir() and filter for callable selectors
        names = dir(cls)
        sgm_methods = [
            n for n in names
            if n.startswith("sgm_") or "cameraHandle" in n.lower()
        ]
        for m in sgm_methods[:10]:
            try:
                method = getattr(cls, m)
                sig = getattr(method, "encoding", lambda: b"?")()
                if isinstance(sig, bytes):
                    sig = sig.decode("ascii", errors="replace")
                print(f"      {m}    encoding={sig}")
            except Exception as e:  # noqa: BLE001
                print(f"      {m}    (error reading signature: {e!r})")

    print()
    print("=" * 70)
    print("✓ Sigma SDK frameworks load and classes are accessible from Python.")
    print("=" * 70)
    print()
    print("Next: call sgm_ConfigApi against the connected camera.")
    print("      → see scripts/sigma_sdk_configapi.py (next step)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Mount the SIGMA Camera Control SDK DMG and copy Frameworks/Headers to the
project so we can load them via PyObjC.

Run::

    python scripts/setup_sigma_sdk.py

No sudo required — we copy to ``<project>/sdk/`` which is user-writable.

This script is idempotent: it skips work that's already done.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DMG_PATH = PROJECT_ROOT / "CameraControlSDK_for_Mac (1).dmg"
SDK_DEST = PROJECT_ROOT / "sdk"


def run(cmd: list[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, capture_output=capture, text=True, check=check)


def find_mount_point() -> Path | None:
    """If the DMG is already mounted, return its mount point. Otherwise None."""
    try:
        out = run(["hdiutil", "info"], check=False)
    except FileNotFoundError:
        return None
    # Parse output, look for our DMG file
    current_image: str | None = None
    for line in out.stdout.splitlines():
        if line.startswith("image-path"):
            current_image = line.split(":", 1)[1].strip()
        if "/Volumes/" in line and current_image and str(DMG_PATH) in current_image:
            for tok in line.split():
                if tok.startswith("/Volumes/"):
                    return Path(tok)
    return None


def attach_dmg() -> Path:
    """Mount the DMG and return the volume path."""
    existing = find_mount_point()
    if existing:
        print(f"  DMG is already mounted at {existing}")
        return existing

    if not DMG_PATH.exists():
        raise FileNotFoundError(f"DMG not found: {DMG_PATH}")

    print(f"  Mounting {DMG_PATH.name} ...")
    result = run(["hdiutil", "attach", "-nobrowse", "-readonly", str(DMG_PATH)])
    # Output has lines like:
    #   /dev/disk4s1   Apple_HFS    /Volumes/CameraControlSDK
    for line in result.stdout.splitlines():
        if "/Volumes/" in line:
            for tok in line.split("\t"):
                tok = tok.strip()
                if tok.startswith("/Volumes/"):
                    return Path(tok)
    # Fallback parse
    for line in result.stdout.splitlines():
        parts = line.split()
        for p in parts:
            if p.startswith("/Volumes/"):
                return Path(p)
    raise RuntimeError(
        f"Could not parse mount point from hdiutil output:\n{result.stdout}"
    )


def detach_dmg(mount_point: Path) -> None:
    print(f"  Detaching {mount_point} ...")
    run(["hdiutil", "detach", str(mount_point), "-force"], check=False)


def explore(mount_point: Path) -> None:
    """Print the top-level structure of the SDK."""
    print(f"\n  Contents of {mount_point}:")
    for entry in sorted(mount_point.iterdir()):
        if entry.is_dir():
            print(f"    [DIR]  {entry.name}/")
            # Show one level deeper
            try:
                for sub in sorted(entry.iterdir())[:5]:
                    suffix = "/" if sub.is_dir() else ""
                    print(f"           {sub.name}{suffix}")
                if len(list(entry.iterdir())) > 5:
                    print(f"           ... ({len(list(entry.iterdir()))} total)")
            except PermissionError:
                pass
        else:
            print(f"           {entry.name}")


def find_frameworks_dir(mount_point: Path) -> Path | None:
    """Locate the Frameworks directory in the mounted SDK."""
    candidates = [
        mount_point / "Frameworks",
        mount_point / "SDK" / "Frameworks",
        mount_point / "CameraControlSDK" / "Frameworks",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    # Recursive search up to 3 levels deep
    for p in mount_point.rglob("*.framework"):
        return p.parent
    return None


def copy_with_attrs(src: Path, dst: Path) -> None:
    """Copy a framework directory preserving symlinks and signatures."""
    if dst.exists():
        print(f"    skip (already exists): {dst.name}")
        return
    # Use cp -R to preserve macOS metadata, symlinks, and code signature
    run(["cp", "-RpP", str(src), str(dst)], capture=False)
    print(f"    copied: {src.name} → {dst}")


def main() -> int:
    print("=" * 70)
    print("SIGMA Camera Control SDK setup")
    print("=" * 70)

    if not DMG_PATH.exists():
        print(f"\n✗ DMG not found at: {DMG_PATH}")
        print(f"  Please put 'CameraControlSDK_for_Mac (1).dmg' at that path,")
        print(f"  or update DMG_PATH in this script.")
        return 1

    SDK_DEST.mkdir(parents=True, exist_ok=True)

    mount_point = None
    try:
        mount_point = attach_dmg()
        explore(mount_point)

        fw_dir = find_frameworks_dir(mount_point)
        if fw_dir is None:
            print("\n✗ Could not find a Frameworks/ directory in the SDK.")
            print(f"  Please inspect {mount_point} manually and tell me the structure.")
            return 1

        print(f"\n  Frameworks source: {fw_dir}")
        frameworks = sorted(p for p in fw_dir.iterdir() if p.suffix == ".framework")
        print(f"  Found {len(frameworks)} framework(s):")
        for fw in frameworks:
            print(f"    {fw.name}")

        # Copy Frameworks
        dest_fw = SDK_DEST / "Frameworks"
        dest_fw.mkdir(parents=True, exist_ok=True)
        print(f"\n  Copying frameworks to {dest_fw} ...")
        for fw in frameworks:
            copy_with_attrs(fw, dest_fw / fw.name)

        # Copy Headers if present
        headers_src = None
        for cand in (
            mount_point / "Headers",
            fw_dir.parent / "Headers",
            mount_point / "SDK" / "Headers",
        ):
            if cand.is_dir():
                headers_src = cand
                break
        if headers_src:
            dest_h = SDK_DEST / "Headers"
            dest_h.mkdir(parents=True, exist_ok=True)
            print(f"\n  Copying headers from {headers_src} to {dest_h} ...")
            for h in headers_src.iterdir():
                target = dest_h / h.name
                if target.exists():
                    continue
                shutil.copy2(h, target)
                print(f"    copied: {h.name}")

        # Copy sample app for reference (read-only)
        sample_src = None
        for cand in (
            mount_point / "SampleAPP",
            mount_point / "Sample",
            mount_point / "SDK" / "SampleAPP",
        ):
            if cand.is_dir():
                sample_src = cand
                break
        if sample_src:
            dest_s = SDK_DEST / "SampleAPP"
            if not dest_s.exists():
                print(f"\n  Copying sample app for reference ...")
                run(["cp", "-RpP", str(sample_src), str(dest_s)], capture=False)
                print(f"    copied SampleAPP → {dest_s}")

        # Inspect one framework's binary to confirm code signature & deps
        if frameworks:
            primary = SDK_DEST / "Frameworks" / "SnapCommand.framework"
            primary_bin = primary / "Versions" / "A" / "SnapCommand"
            if primary_bin.exists():
                print(f"\n  Verifying SnapCommand framework binary:")
                print(f"    Path: {primary_bin}")
                # Check signature
                res = run(["codesign", "-dv", str(primary)], check=False)
                if res.returncode == 0:
                    # codesign writes to stderr usually
                    sig_info = res.stderr or res.stdout
                    for line in sig_info.splitlines()[:6]:
                        print(f"    {line}")
                # Show its dynamic library deps
                res = run(["otool", "-L", str(primary_bin)], check=False)
                if res.returncode == 0:
                    print(f"\n    Dependencies:")
                    for line in res.stdout.splitlines()[1:6]:
                        print(f"     {line}")

        print()
        print("=" * 70)
        print(f"✓ SDK extracted to {SDK_DEST}")
        print("=" * 70)
        print()
        print("Next steps:")
        print("  1. Inspect sdk/Headers/ to see the API")
        print("  2. Run the PyObjC bridge test:")
        print("     python scripts/sigma_sdk_smoke.py")
        return 0

    except subprocess.CalledProcessError as e:
        print(f"\n✗ Command failed: {e}")
        print(f"  stdout: {e.stdout}")
        print(f"  stderr: {e.stderr}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\n✗ {e!r}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        # Detach only if we mounted it ourselves and it's still mounted
        # (leave it mounted if user had it open)
        pass  # Keep mounted for now; user can manually eject


if __name__ == "__main__":
    raise SystemExit(main())

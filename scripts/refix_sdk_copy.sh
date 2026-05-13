#!/bin/bash
# Re-copy the Sigma SDK frameworks using ditto (Apple's bundle-aware tool)
# instead of cp, which incorrectly turned symlinks into text files.
#
# Usage:
#   chmod +x scripts/refix_sdk_copy.sh
#   ./scripts/refix_sdk_copy.sh

set -e

cd "$(dirname "$0")/.."  # project root

DMG="CameraControlSDK_for_Mac (1).dmg"

echo "================================================================"
echo "Re-copying Sigma SDK frameworks with ditto"
echo "================================================================"

# 1. Ensure DMG is mounted
echo ""
echo "1) Mounting DMG (if not already mounted)..."
MOUNT_PATH=""
for vol in /Volumes/CameraControlSDK_for_Mac*; do
    if [ -d "$vol" ]; then
        MOUNT_PATH="$vol"
        echo "   DMG already mounted at: $MOUNT_PATH"
        break
    fi
done
if [ -z "$MOUNT_PATH" ]; then
    echo "   Attaching $DMG ..."
    hdiutil attach -nobrowse -readonly "$DMG"
    for vol in /Volumes/CameraControlSDK_for_Mac*; do
        if [ -d "$vol" ]; then
            MOUNT_PATH="$vol"
            break
        fi
    done
fi
echo "   Mount point: $MOUNT_PATH"

# 2. Locate the Frameworks source inside the DMG
SRC=""
for candidate in "$MOUNT_PATH/CameraControlSDK_for_Mac/SDK" "$MOUNT_PATH/SDK" "$MOUNT_PATH/Frameworks"; do
    if [ -d "$candidate" ] && ls "$candidate"/*.framework >/dev/null 2>&1; then
        SRC="$candidate"
        break
    fi
done
if [ -z "$SRC" ]; then
    echo "   ✗ Could not find Frameworks source inside DMG"
    exit 1
fi
echo "   Source: $SRC"

# 3. Remove broken copy
DEST="sdk/Frameworks"
echo ""
echo "2) Removing broken copy at $DEST ..."
rm -rf "$DEST"
mkdir -p "$DEST"

# 4. Re-copy using ditto
echo ""
echo "3) Copying with ditto (this preserves macOS bundle symlinks) ..."
for fw in "$SRC"/*.framework; do
    name=$(basename "$fw")
    echo "   ditto: $name"
    ditto "$fw" "$DEST/$name"
done

# 5. Verify
echo ""
echo "4) Verifying symlink preservation ..."
TOP="$DEST/SnapCommand.framework/SnapCommand"
if [ -L "$TOP" ]; then
    echo "   ✓ Top-level binary is a symlink (correct): $(readlink "$TOP")"
else
    echo "   ✗ Top-level binary is NOT a symlink at $TOP"
    file "$TOP"
    exit 1
fi

CUR="$DEST/SnapCommand.framework/Versions/Current"
if [ -L "$CUR" ]; then
    echo "   ✓ Versions/Current is a symlink (correct): $(readlink "$CUR")"
else
    echo "   ✗ Versions/Current is NOT a symlink"
    exit 1
fi

REAL="$DEST/SnapCommand.framework/Versions/A/SnapCommand"
echo "   $(file "$REAL")"

echo ""
echo "================================================================"
echo "✓ SDK frameworks re-copied correctly"
echo "================================================================"
echo ""
echo "Now re-run the smoke test:"
echo ""
echo "  arch -x86_64 venv-x86/bin/python scripts/sigma_sdk_smoke.py"

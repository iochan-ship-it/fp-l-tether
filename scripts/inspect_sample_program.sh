#!/bin/bash
# Inspect the Sigma SDK SampleProgram to find the canonical
# initialization sequence we're missing.

set -e
cd "$(dirname "$0")/.."

echo "================================================================"
echo "Inspecting SampleProgram from SIGMA Camera Control SDK"
echo "================================================================"

# Find DMG mount
MOUNT=""
for v in /Volumes/CameraControlSDK_for_Mac*; do
    [ -d "$v" ] && MOUNT="$v" && break
done
if [ -z "$MOUNT" ]; then
    echo "✗ DMG not mounted. Mount it first:"
    echo "  hdiutil attach 'CameraControlSDK_for_Mac (1).dmg'"
    exit 1
fi

SAMPLE_DIR=""
for cand in "$MOUNT/CameraControlSDK_for_Mac/SampleProgram" \
            "$MOUNT/SampleProgram" \
            "$MOUNT/CameraControlSDK_for_Mac/Sample" \
            "$MOUNT/Sample" \
            "$MOUNT/CameraControlSDK_for_Mac/SampleAPP" \
            "$MOUNT/SampleAPP"; do
    if [ -d "$cand" ]; then
        SAMPLE_DIR="$cand"
        break
    fi
done

if [ -z "$SAMPLE_DIR" ]; then
    echo "✗ SampleProgram not found. Listing $MOUNT contents:"
    ls -la "$MOUNT/"
    if [ -d "$MOUNT/CameraControlSDK_for_Mac" ]; then
        echo ""
        echo "$MOUNT/CameraControlSDK_for_Mac contents:"
        ls -la "$MOUNT/CameraControlSDK_for_Mac/"
    fi
    exit 1
fi

echo ""
echo "Sample program location: $SAMPLE_DIR"
echo ""
echo "--- Directory contents ---"
ls -la "$SAMPLE_DIR/"

# Look for source files
echo ""
echo "--- Source files (.h .m .mm .swift .cpp) ---"
find "$SAMPLE_DIR" -type f \( -name "*.h" -o -name "*.m" -o -name "*.mm" \
                              -o -name "*.swift" -o -name "*.cpp" \) 2>/dev/null | head -30

# Look for .app bundles
echo ""
echo "--- App bundles (.app, .xcodeproj) ---"
find "$SAMPLE_DIR" -type d \( -name "*.app" -o -name "*.xcodeproj" \) 2>/dev/null | head -10

# Look for ANY file that might have initialization code
echo ""
echo "--- All non-binary files ---"
find "$SAMPLE_DIR" -type f ! -name "*.png" ! -name "*.jpg" ! -name "*.icns" \
     ! -name "*.tiff" 2>/dev/null | head -40

# If we find source, search for key SDK function calls
echo ""
echo "--- Searching source for SDK call patterns ---"
SEARCH_PATTERN="sgm_ConfigAPI|sgm_SnapCommand|requestOpenSession|requestSendPTPCommand|sgm_APIBase"
for f in $(find "$SAMPLE_DIR" -type f \( -name "*.h" -o -name "*.m" -o -name "*.mm" -o -name "*.swift" \) 2>/dev/null); do
    matches=$(grep -nE "$SEARCH_PATTERN" "$f" 2>/dev/null || true)
    if [ -n "$matches" ]; then
        echo ""
        echo "  $f:"
        echo "$matches" | head -30 | sed 's/^/    /'
    fi
done

# If there's an app binary, dump useful strings from it
echo ""
echo "--- Strings from app binary (if any) ---"
for app in $(find "$SAMPLE_DIR" -name "*.app" -type d 2>/dev/null); do
    bin_dir="$app/Contents/MacOS"
    if [ -d "$bin_dir" ]; then
        for bin in "$bin_dir"/*; do
            if file "$bin" 2>/dev/null | grep -q "Mach-O"; then
                echo ""
                echo "  Binary: $bin"
                echo "  Sigma-related strings:"
                strings "$bin" 2>/dev/null | grep -E "sgm_|sigma|SIGMA|cameraHandle|requestOpenSession|requestSendPTPCommand" | head -50 | sed 's/^/    /'
            fi
        done
    fi
done

echo ""
echo "================================================================"
echo "Done. Tell me which files look like initialization examples."
echo "================================================================"

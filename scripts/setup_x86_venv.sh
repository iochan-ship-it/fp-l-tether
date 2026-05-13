#!/bin/bash
# Set up an x86_64 venv for using the Sigma SDK (Intel-only frameworks).
#
# The Sigma SDK was compiled in 2020 for x86_64 only. Apple Silicon Macs
# (M1/M2/M3/M4) cannot load these frameworks natively, but Rosetta 2 lets
# us run x86_64 Python which CAN load them.
#
# Usage:
#   chmod +x scripts/setup_x86_venv.sh
#   ./scripts/setup_x86_venv.sh
#
# Or run line by line in your terminal.

set -e

cd "$(dirname "$0")/.."  # project root

echo "================================================================"
echo "Setting up x86_64 Python venv for Sigma SDK"
echo "================================================================"

# Verify Python is universal2
PY_BIN="$(which python3)"
echo "Found Python: $PY_BIN"
if ! file "$PY_BIN" | grep -q "x86_64"; then
    echo "✗ Your Python does NOT include x86_64 support."
    echo "  Please install Python 3.x universal2 from https://www.python.org/downloads/"
    exit 1
fi
echo "✓ Python is universal2 (supports both arm64 and x86_64)"

# Verify Rosetta 2 is available
echo ""
echo "Checking Rosetta 2..."
if arch -x86_64 /usr/bin/true 2>/dev/null; then
    echo "✓ Rosetta 2 is installed and working"
else
    echo "✗ Rosetta 2 not available. Install with:"
    echo "  softwareupdate --install-rosetta"
    exit 1
fi

# Create venv-x86
if [ -d "venv-x86" ]; then
    echo ""
    echo "venv-x86 already exists. Skipping creation."
else
    echo ""
    echo "Creating x86_64 venv at ./venv-x86 ..."
    arch -x86_64 python3 -m venv venv-x86
    echo "✓ venv-x86 created"
fi

# Activate and verify
echo ""
echo "Activating venv-x86 and verifying architecture..."
# We can't easily activate inside a script, so just run python with the
# venv's interpreter and arch -x86_64
ARCH=$(arch -x86_64 venv-x86/bin/python -c "import platform; print(platform.machine())")
if [ "$ARCH" != "x86_64" ]; then
    echo "✗ venv-x86 python is reporting arch '$ARCH', expected 'x86_64'"
    exit 1
fi
echo "✓ venv-x86 Python is running as x86_64"

# Install dependencies in x86_64 mode
echo ""
echo "Installing PyObjC dependencies under x86_64 ..."
arch -x86_64 venv-x86/bin/pip install --upgrade pip
arch -x86_64 venv-x86/bin/pip install \
    pyobjc-core \
    pyobjc-framework-Cocoa \
    pyobjc-framework-ImageCaptureCore

echo ""
echo "================================================================"
echo "✓ x86_64 environment ready"
echo "================================================================"
echo ""
echo "From now on, to use the Sigma SDK, run scripts via:"
echo ""
echo "  arch -x86_64 venv-x86/bin/python scripts/sigma_sdk_smoke.py"
echo ""
echo "Or activate the venv:"
echo "  source venv-x86/bin/activate"
echo "  arch -x86_64 python scripts/sigma_sdk_smoke.py"
echo ""
echo "Note: keep the original 'venv' (arm64) for libusb-based scripts."

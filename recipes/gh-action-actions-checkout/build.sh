#!/bin/bash
set -euo pipefail

# Install action source files
mkdir -p "$PREFIX/share/gh-actions/actions/checkout"
cp -r "$SRC_DIR/." "$PREFIX/share/gh-actions/actions/checkout/"

# Install bash entry point
mkdir -p "$PREFIX/bin"
install -m 755 "$RECIPE_DIR/checkout.sh" "$PREFIX/bin/checkout"

# Sanity-check the main entry point exists
if [[ ! -f "$PREFIX/share/gh-actions/actions/checkout/dist/index.js" ]]; then
  echo "WARNING: $PREFIX/share/gh-actions/actions/checkout/dist/index.js not found" >&2
fi

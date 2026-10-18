#!/bin/sh
# Render the 128x128 Marketplace icon (which must be a PNG) from its SVG source.
# Requires rsvg-convert (librsvg). Commit both files after a change.
set -eu
cd "$(dirname "$0")"
rsvg-convert --width 128 --height 128 --output glitnir-128.png glitnir-icon.svg

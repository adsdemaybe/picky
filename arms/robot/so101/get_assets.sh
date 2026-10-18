#!/usr/bin/env bash
# Fetch the official SO-101 URDF + STL meshes (TheRobotStudio/SO-ARM100, Apache-2.0) into ./assets/.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
git clone --depth 1 --filter=blob:none --sparse https://github.com/TheRobotStudio/SO-ARM100 "$TMP/so-arm100"
git -C "$TMP/so-arm100" sparse-checkout set Simulation/SO101
mkdir -p "$HERE/assets"
cp -r "$TMP/so-arm100/Simulation/SO101/." "$HERE/assets/"
rm -rf "$TMP"
ls "$HERE/assets"

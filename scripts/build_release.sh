#!/bin/bash
#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause
#
# Build the release bitstreams in dist/ (standard, virtual cartridge, BIOS demo) with Gowin
# V1.9.12.04 (GOWIN_PATH, default: ~/tools/gowin_1.9.12.04/IDE). Builds run sequentially (shared
# build/ directory) and are retried on Gowin license server failures.

set -u
cd "$(dirname "$0")/.."
GOWIN_PATH=${GOWIN_PATH:-~/tools/gowin_1.9.12.04/IDE}
mkdir -p dist

for variant in "standard:" "vcart:--with-debug-bridge" "bios:--with-bios"; do
    name=${variant%%:*}
    args=${variant#*:}
    for try in 1 2 3; do
        rm -rf build
        ./chromatix.py --gowin-path "$GOWIN_PATH" --build $args > dist/build_$name.log 2>&1
        [ -f build/gateware/chromatic.fs ] && break
        grep -q "License verification failed" dist/build_$name.log || break
        echo "$name: Gowin license failure, retry $try."
    done
    if [ ! -f build/gateware/chromatic.fs ]; then
        echo "$name: build failed (see dist/build_$name.log)."
        exit 1
    fi
    cp build/gateware/chromatic.fs dist/chromatix-$name.fs
    echo "$name: dist/chromatix-$name.fs"
done

cp scripts/chromatix_flash.py dist/
(cd dist && sha256sum chromatix-*.fs chromatix_flash.py > SHA256SUMS)

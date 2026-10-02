#!/usr/bin/env bash
#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause
#
# Chromatic ESP-SDR build: ESP-SDR (GPL-3.0, pinned) + the Chromatic QSPI transport, for the
# Chromatic ESP32. Needs the ESP-IDF pinned by ESP-SDR (firmware-targets.json), sourced (export.sh).
#
#   ./build.sh [work_dir]    (default: ./build), images in <work_dir>/esp-sdr/build-esp32/.

set -e

ESP_SDR_URL=https://github.com/ESPARGOS/esp-sdr.git
ESP_SDR_REF=550fadea4d00a9e26ce921c5832167becb3dc20c

HERE=$(cd "$(dirname "$0")" && pwd)
WORK=$(mkdir -p "${1:-$HERE/build}" && cd "${1:-$HERE/build}" && pwd)
SRC=$WORK/esp-sdr

command -v idf.py > /dev/null || { echo "idf.py not found: source the ESP-IDF export.sh."; exit 1; }

# ESP-SDR (pinned) + Chromatic patch/sources.
if [ ! -d "$SRC" ]; then
    git init -q "$SRC"
    git -C "$SRC" remote add origin $ESP_SDR_URL
    git -C "$SRC" fetch -q --depth 1 origin $ESP_SDR_REF
    git -C "$SRC" checkout -q FETCH_HEAD
    git -C "$SRC" submodule update -q --init --depth 1
    git -C "$SRC" apply "$HERE/esp-sdr-chromatic.patch"
fi
mkdir -p "$SRC/main/chromatic"
cp "$HERE"/chromatic_*.[ch] "$SRC/main/chromatic/"

# Build.
cd "$SRC"
idf.py -B build-esp32 -DIDF_TARGET=esp32 -DSDKCONFIG=sdkconfig.esp32 \
    -DSDKCONFIG_DEFAULTS=sdkconfig.defaults.esp32 build
echo "Flash (through the Chromatic USB bridge, standard bitstream):"
echo "  cd $SRC/build-esp32 && python -m esptool --chip esp32 -p /dev/ttyACM0 -b 460800 write-flash @flash_args"

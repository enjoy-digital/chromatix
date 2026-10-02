// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// ESP-SDR Chromatic tuning (original ESP32): RF PLL software calibration on the requested
// frequency (2386-2504MHz, 1kHz steps, measured/corrected) instead of ESP-SDR's 2412MHz
// calibration + direct PLL offset.
//
// Commands: "FREQK <kHz>" (FREQ <MHz> also uses it), "RANGEK?", "TUNEMODE <0|1>" (0: ESP-SDR
// tuning, 1: Chromatic tuning, default), "TUNEMODE?", "TUNESW <index> <offset>" (experiments: raw
// calibration call, index = MHz - 2400, offset in 1/1024 MHz).

#ifndef CHROMATIC_TUNE_H
#define CHROMATIC_TUNE_H

#include <stdbool.h>

extern int chromatic_tune_mode;

/* Tune (FREQ/FREQK): returns false if not handled (ESP-SDR tuning). */
bool chromatic_tune(unsigned mhz);
bool chromatic_tune_khz(unsigned khz);
/* Tuning commands (replies on the burst serial), prepare: RX path setup after a tuning. */
bool chromatic_tune_command(const char *line, void (*prepare)(void));

#endif

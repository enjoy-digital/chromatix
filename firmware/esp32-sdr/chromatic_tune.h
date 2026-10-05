// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Part of the Chromatic port of ESP-SDR (https://espargos.net/espsdr/, ESPARGOS: Florian Euchner).
//
// ESP-SDR Chromatic tuning (original ESP32): RF PLL frequency table entry loaded with the requested
// divider + VCO capacitor code then PHY software calibration: 2150-2880MHz, and 5/6 LO mode below
// (eSpDR/ESP-SDR finding): 1792-2150MHz, 1kHz steps (instead of ESP-SDR's 2412MHz calibration +
// direct PLL divider: the LO doesn't move off the Wi-Fi channels).
//
// Commands: "FREQK <kHz>" (FREQ <MHz> also uses it), "RANGEK?", "TUNEMODE <0|1|2>" (0: ESP-SDR
// tuning, 1: table tuning, 2150-2880MHz, default, 2: calibration + offset, 2386-2504MHz),
// "TUNEMODE?". Experiments: "TUNESW <index> <offset>" (raw calibration call, index = MHz - 2400,
// offset in 1/1024 MHz), "I2CR/I2CW/I2CD" (analog registers), "REGR/REGW" (registers), "FTAB
// <index>" (PLL frequency table entry).

#ifndef CHROMATIC_TUNE_H
#define CHROMATIC_TUNE_H

#include <stdbool.h>

extern int chromatic_tune_mode;

/* Tune (FREQ/FREQK): returns false if not handled (ESP-SDR tuning). */
bool chromatic_tune(unsigned mhz);
bool chromatic_tune_khz(unsigned khz);
/* Receive LO selector of the last tuning (5/6 mode), to apply after the RX setup. */
void chromatic_tune_apply_lo(void);
/* Tuning commands (replies on the burst serial), prepare: RX path setup after a tuning. */
bool chromatic_tune_command(const char *line, void (*prepare)(void));

#endif

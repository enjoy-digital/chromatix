// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Part of the Chromatic port of ESP-SDR (https://espargos.net/espsdr/, ESPARGOS: Florian Euchner).
//
// ESP-SDR Chromatic capture timing: commands are handled on the FreeRTOS tick (1ms), so the
// captures start on a 1ms grid of the ESP32 clock: against a periodic signal (LTE: PSS every 5ms,
// PBCH every 10ms) a burst always sees the same part of the period, drifting by the clocks ppm
// only. A random delay before each capture spreads them over the period.
//
// Commands: "CAPDLY <max_us>" (random delay 0..max_us before each capture, 0: none, <= 10000),
// "CAPDLY?".

#ifndef CHROMATIC_CAPTURE_H
#define CHROMATIC_CAPTURE_H

#include <stdbool.h>

/* Delay before a capture (random, 0..max). */
void chromatic_capture_delay(void);
/* Capture timing commands (replies on the burst serial): returns true if the line was handled. */
bool chromatic_capture_command(const char *line);

#endif

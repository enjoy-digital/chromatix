// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// ESP-SDR Chromatic capture timing, see chromatic_capture.h.

#include <stdio.h>
#include <string.h>

#include "esp_random.h"
#include "esp_rom_sys.h"

#include "burst_serial.h"
#include "chromatic_capture.h"

#define MAX_DELAY_US 10000

static unsigned delay_max_us;

void chromatic_capture_delay(void)
{
    if (delay_max_us)
        esp_rom_delay_us(esp_random() % (delay_max_us + 1));
}

static void reply(const char *text)
{
    burst_serial_send(text, strlen(text));
}

bool chromatic_capture_command(const char *line)
{
    char text[32];
    unsigned us;
    if (!strcmp(line, "CAPDLY?")) {
        snprintf(text, sizeof(text), "CAPDLY %u\n", delay_max_us);
        reply(text);
        return true;
    }
    if (sscanf(line, "CAPDLY %u", &us) == 1) {
        if (us > MAX_DELAY_US) {
            reply("ERR args\n");
            return true;
        }
        delay_max_us = us;
        reply("OK\n");
        return true;
    }
    return false;
}

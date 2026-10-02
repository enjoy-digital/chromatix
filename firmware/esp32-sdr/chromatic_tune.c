// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// ESP-SDR Chromatic tuning (original ESP32), see chromatic_tune.h.
//
// ESP-SDR tunes out of channel frequencies by calibrating the RF PLL on 2412MHz then writing the
// PLL divider directly (rom_set_rf_freq_offset): the VCO capacitor bank stays calibrated for
// 2412MHz and the LO only follows a few MHz around it (measured on the Chromatic). The PHY software
// channel calibration (set_chan_freq_sw_start, used by set_channel_rfpll_freq for the Wi-Fi
// channels and by the Bluetooth PHY for its 1MHz channels) programs and calibrates the PLL (VCO
// capacitor bank, offset) for a frequency index relative to 2400MHz (0-84 here: no lock above) and
// an offset (1/1024 MHz).
//
// Measured on the Chromatic (console 24MHz crystal harmonics as references, interpolated peaks,
// ~1kHz, the 2400MHz line excluded: another source ~16kHz above it): the LO moves by 1.0546*c MHz
// for an offset of c MHz (1024*c units), with a -0.243MHz step past a 15MHz move above the 2484MHz
// calibration. PLL lock from 2400 - 14.7MHz to 2484 + 22MHz: 2386-2504MHz used, corrected below.
// Residual: the crystals difference (console/ESP32: ~-1.2kHz at 2.4GHz).

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "burst_serial.h"
#include "chromatic_tune.h"

/* libphy (ESP32). */
extern void    set_chan_freq_sw_start(uint8_t index, int16_t offset, uint8_t ctrl);
extern void    set_chanfreq(unsigned mhz, unsigned mode);
extern uint8_t chip7_phy_init_ctrl[];
extern int16_t phy_freq_offset;

int chromatic_tune_mode = 1;

#define TUNE_MIN_KHZ 2386000
#define TUNE_MAX_KHZ 2504000
#define OFFSET_SCALE 1.0546 /* LO move (MHz) per offset MHz. */

static void reply(const char *text)
{
    burst_serial_send(text, strlen(text));
}

static unsigned nearest_channel(unsigned mhz)
{
    if (mhz >= 2478)
        return 2484;
    if (mhz <= 2412)
        return 2412;
    unsigned ch = 2412 + (mhz - 2412 + 2)/5*5;
    return (ch > 2472) ? 2472 : ch;
}

static void tune_sw(unsigned index, int offset)
{
    set_chan_freq_sw_start(index, offset, chip7_phy_init_ctrl[1]);
}

static int offset_units(double mhz)
{
    return (int)(mhz*1024 + ((mhz >= 0) ? 0.5 : -0.5));
}

bool chromatic_tune_khz(unsigned khz)
{
    if (chromatic_tune_mode != 1 || khz < TUNE_MIN_KHZ || khz > TUNE_MAX_KHZ)
        return false;
    unsigned index;
    double   c; /* Offset (MHz). */
    if (khz < 2400000) {
        /* Below the 2400MHz calibration. */
        double d = (2400000 - khz)/1000.0;
        index = 0;
        c     = -d/OFFSET_SCALE;
    } else if (khz < 2485000) {
        /* Calibration on the MHz, offset < 1MHz. */
        index = (khz - 2400000)/1000;
        c     = (khz - 2400000 - index*1000)/1000.0/OFFSET_SCALE;
    } else {
        /* Above the 2484MHz calibration (step past 15MHz: overlapping, switch at 14.9MHz). */
        double d = (khz - 2484000)/1000.0;
        index = 84;
        c     = ((d <= 14.9) ? d : d + 0.243)/OFFSET_SCALE;
    }
    /* Wi-Fi channel configuration (baseband, filters) of the nearest channel, then the PLL
       software calibration. */
    set_chanfreq(nearest_channel(khz/1000), 0);
    tune_sw(index, phy_freq_offset + offset_units(c));
    return true;
}

bool chromatic_tune(unsigned mhz)
{
    return chromatic_tune_khz(mhz*1000);
}

bool chromatic_tune_command(const char *line, void (*prepare)(void))
{
    char     text[48];
    unsigned mode, index, khz;
    int      offset;
    char     extra;
    if (!strcmp(line, "TUNEMODE?")) {
        snprintf(text, sizeof(text), "TUNEMODE %d\n", chromatic_tune_mode);
        reply(text);
        return true;
    }
    if (sscanf(line, "FREQK %u %c", &khz, &extra) == 1) {
        if (!chromatic_tune_khz(khz)) {
            reply("ERR freqk_range\n");
            return true;
        }
        prepare();
        reply("OK\n");
        return true;
    }
    if (!strcmp(line, "RANGEK?")) {
        snprintf(text, sizeof(text), "RANGEK %u %u\n", TUNE_MIN_KHZ, TUNE_MAX_KHZ);
        reply(text);
        return true;
    }
    if (sscanf(line, "TUNEMODE %u %c", &mode, &extra) == 1) {
        if (mode > 1) {
            reply("ERR tunemode_args\n");
            return true;
        }
        chromatic_tune_mode = mode;
        reply("OK\n");
        return true;
    }
    /* Experiments: raw PLL software calibration (index: MHz - 2400, offset: PHY units). */
    if (sscanf(line, "TUNESW %u %d %c", &index, &offset, &extra) == 2) {
        if (index > 255 || offset < -32768 || offset > 32767) {
            reply("ERR tunesw_args\n");
            return true;
        }
        tune_sw(index, offset);
        prepare();
        reply("OK\n");
        return true;
    }
    return false;
}

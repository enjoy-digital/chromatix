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
// an offset (1/1024 MHz) (the calibration itself stops at index 84: 85-entry table).
//
// Table tuning (mode 1, default): the 85-entry frequency table read by the calibration holds the
// PLL divider and the VCO capacitor code: loading a borrowed entry with the requested divider and
// a capacitor code close to the result makes the calibration lock from 2130 to 2890MHz (2-4 comb
// lines verified at 80MS/s): the ends of the VCO capacitor bank.
//
// 5/6 LO mode (mode 1 below 2150MHz): the CKGEN selector (analog block 0x65, host 4, register 0,
// bit 4: libphy patched ram_chip_i2c_* functions) makes the receive LO 5/6 of the PLL frequency
// (found by h0m3us3r's eSpDR, https://github.com/h0m3us3r/eSpDR, and qualified on the ESP32 by
// ESP-SDR): the PLL is tuned to 6/5 of the requested frequency (calibration in normal mode), the
// selector set after the RX setup: 1775-2150MHz (PLL 2130-2580MHz).
//
// VCO edges, measured with forced capacitor codes (RF PLL block 0x62 reg 1, coarse/fine nibbles):
// the VCO locks from 2130 (code 255) to 2890MHz (code 0), the calibration started from the fitted
// code finds a locking code up to these edges.
//
// Offset tuning (mode 2), measured on the Chromatic (console 24MHz crystal harmonics as
// references, interpolated peaks, ~1kHz, the 2400MHz line excluded: another source ~16kHz above
// it): the LO moves by 1.0546*c MHz for an offset of c MHz (1024*c units), with a -0.243MHz step
// past a 15MHz move above the 2484MHz calibration. PLL lock from 2400 - 14.7MHz to 2484 + 22MHz:
// 2386-2504MHz used, corrected below. Residual: the crystals difference (console/ESP32: ~-1.2kHz
// at 2.4GHz).

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "soc/dport_access.h"
#include "esp_rom_sys.h"

#include "burst_serial.h"
#include "chromatic_tune.h"

/* libphy (ESP32). */
extern void    set_chan_freq_sw_start(uint8_t index, int16_t offset, uint8_t ctrl);
extern void    set_chanfreq(unsigned mhz, unsigned mode);
extern uint8_t chip7_phy_init_ctrl[];
extern int16_t phy_freq_offset;
/* libphy patched analog (regi2c) register functions (all blocks/hosts, ex: CKGEN host 4). */
extern unsigned ram_chip_i2c_readReg(unsigned block, unsigned host, unsigned reg);
extern void     ram_chip_i2c_writeReg(unsigned block, unsigned host, unsigned reg, unsigned data);

/* RF PLL frequency table: 85 entries (2400-2484MHz) of 3 words (write_wifi_chan_data,
   bt_opt_write_mem): word 0: VCO capacitor bank code (bits 7:0, analog block 0x62 reg 1), word 1:
   divider (LO = 480MHz*(2 + word/2^20)), word 2: front-end tuning. */
#define FTAB_ENTRIES 85
#define FTAB_SEL     0x3ff4e0c4 /* Word address (bits 7:0), write strobe (bit 9). */
#define FTAB_DATA    0x3ff4e0c0 /* Read data. */
#define FTAB_DATA_W  0x3ff4e148 /* Write data. */
#define FTAB_WRITE   (1 << 9)

/* CKGEN receive LO selector: 5/6 of the PLL frequency when set. */
#define LO56_BLOCK 0x65
#define LO56_HOST  4
#define LO56_REG   0
#define LO56_MASK  0x10

/* Tuning ranges (kHz). */
#define TABLE_MIN_KHZ 2130000 /* Mode 1: table tuning (PLL range: VCO capacitor bank ends). */
#define TABLE_MAX_KHZ 2890000
#define LO56_MIN_KHZ  1775000 /* Mode 1: 5/6 LO below 2150MHz (PLL >= 2130MHz). */
#define LO56_MAX_KHZ  2150000
#define TUNE_MIN_KHZ  2386000 /* Mode 2: calibration + offset. */
#define TUNE_MAX_KHZ  2504000
#define OFFSET_SCALE  1.0546  /* Mode 2: LO move (MHz) per offset MHz. */

int chromatic_tune_mode = 1;

static bool lo56; /* 5/6 LO selected by the last tuning (applied by chromatic_tune_apply_lo). */

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

static uint32_t ftab_read(unsigned addr)
{
    DPORT_REG_WRITE(FTAB_SEL, (DPORT_REG_READ(FTAB_SEL) & ~0x2ffu) | addr);
    return DPORT_REG_READ(FTAB_DATA);
}

static void ftab_write(unsigned addr, uint32_t value)
{
    uint32_t sel = (DPORT_REG_READ(FTAB_SEL) & ~0x2ffu) | addr;
    DPORT_REG_WRITE(FTAB_SEL, sel);
    DPORT_REG_WRITE(FTAB_DATA_W, value);
    DPORT_REG_WRITE(FTAB_SEL, sel | FTAB_WRITE);
    DPORT_REG_WRITE(FTAB_SEL, sel);
}

/* VCO capacitor code (calibration starting point) measured from the calibrations results over
   2150-2880MHz (quadratic fit, +-1.6, used from 2130 to 2890MHz). */
static unsigned vco_dcap(double mhz)
{
    double x = mhz - 2500;
    double d = 76.93 - 0.25398*x + 1.4673e-4*x*x;
    return (d < 0) ? 0 : (d > 255) ? 255 : (unsigned)(d + 0.5);
}

static bool tune_table(double mhz)
{
    /* Borrowed entry (nearest MHz), loaded with the divider of the requested frequency (and the
       capacitor code: calibrated one around its MHz, else from the fit), calibrated, restored. */
    int      index = (int)(mhz + 0.5) - 2400;
    index = (index < 0) ? 0 : (index > FTAB_ENTRIES - 1) ? FTAB_ENTRIES - 1 : index;
    uint32_t w0 = ftab_read(3*index + 0);
    uint32_t w1 = ftab_read(3*index + 1);
    uint32_t dcap = (mhz - (2400 + index) < 1.0 && (2400 + index) - mhz < 1.0) ? (w0 & 0xff) :
        vco_dcap(mhz);
    ftab_write(3*index + 0, (w0 & ~0xffu) | dcap);
    ftab_write(3*index + 1, (uint32_t)((mhz/480.0 - 2.0)*1048576.0 + 0.5));
    set_chanfreq(nearest_channel((unsigned)mhz), 0);
    tune_sw(index, phy_freq_offset);
    ftab_write(3*index + 0, w0);
    ftab_write(3*index + 1, w1);
    return true;
}

static int offset_units(double mhz)
{
    return (int)(mhz*1024 + ((mhz >= 0) ? 0.5 : -0.5));
}

void chromatic_tune_apply_lo(void)
{
    /* Receive LO selector (after the RX setup, calibration done in normal mode). */
    unsigned old   = ram_chip_i2c_readReg(LO56_BLOCK, LO56_HOST, LO56_REG);
    unsigned value = (old & ~LO56_MASK) | (lo56 ? LO56_MASK : 0);
    if (value != old) {
        ram_chip_i2c_writeReg(LO56_BLOCK, LO56_HOST, LO56_REG, value);
        esp_rom_delay_us(3000);
    }
}

bool chromatic_tune_khz(unsigned khz)
{
    lo56 = false;
    chromatic_tune_apply_lo(); /* Calibrations in normal mode. */
    if (chromatic_tune_mode == 1) {
        if (khz >= LO56_MAX_KHZ && khz <= TABLE_MAX_KHZ)
            return tune_table(khz/1000.0);
        if (khz >= LO56_MIN_KHZ && khz < LO56_MAX_KHZ) {
            lo56 = true;
            return tune_table(khz/1000.0*6/5);
        }
        return false;
    }
    if (chromatic_tune_mode != 2 || khz < TUNE_MIN_KHZ || khz > TUNE_MAX_KHZ)
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
        if (chromatic_tune_mode == 1)
            snprintf(text, sizeof(text), "RANGEK %u %u\n", LO56_MIN_KHZ, TABLE_MAX_KHZ);
        else
            snprintf(text, sizeof(text), "RANGEK %u %u\n", TUNE_MIN_KHZ, TUNE_MAX_KHZ);
        reply(text);
        return true;
    }
    /* Experiments: analog registers (regi2c) and memory mapped registers. */
    unsigned block, host, reg, value, n, addr;
    char     dump[400];
    if (sscanf(line, "I2CR %u %u %u %c", &block, &host, &reg, &extra) == 3) {
        snprintf(text, sizeof(text), "I2C %u\n", ram_chip_i2c_readReg(block, host, reg));
        reply(text);
        return true;
    }
    if (sscanf(line, "I2CW %u %u %u %u %c", &block, &host, &reg, &value, &extra) == 4) {
        ram_chip_i2c_writeReg(block, host, reg, value);
        reply("OK\n");
        return true;
    }
    if (sscanf(line, "I2CD %u %u %u %c", &block, &host, &n, &extra) == 3 && n <= 64) {
        int len = snprintf(dump, sizeof(dump), "I2CD");
        for (unsigned r = 0; r < n; r++)
            len += snprintf(dump + len, sizeof(dump) - len, " %02x",
                ram_chip_i2c_readReg(block, host, r));
        snprintf(dump + len, sizeof(dump) - len, "\n");
        reply(dump);
        return true;
    }
    if (sscanf(line, "REGR %x %c", &addr, &extra) == 1 && addr >= 0x3ff00000 && addr < 0x60040000) {
        snprintf(text, sizeof(text), "REG %08x\n", (unsigned)DPORT_REG_READ(addr));
        reply(text);
        return true;
    }
    if (sscanf(line, "REGW %x %x %c", &addr, &value, &extra) == 2 && addr >= 0x3ff00000 &&
        addr < 0x60040000) {
        DPORT_REG_WRITE(addr, value);
        reply("OK\n");
        return true;
    }
    if (sscanf(line, "FTAB %u %c", &index, &extra) == 1 && index < FTAB_ENTRIES) {
        snprintf(text, sizeof(text), "FTAB %08x %08x %08x\n", (unsigned)ftab_read(3*index + 0),
            (unsigned)ftab_read(3*index + 1), (unsigned)ftab_read(3*index + 2));
        reply(text);
        return true;
    }
    if (sscanf(line, "TUNEMODE %u %c", &mode, &extra) == 1) {
        if (mode > 2) {
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

// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// DECT scanner tool: the DECT carriers (EU/US) captured in pairs (16MS/s, 1ms), carriers activity,
// bursts decoded (dect.c, A-field only): base stations (RFPI) and handsets transmissions (calls).

#include <stdio.h>
#include <string.h>

#include "app.h"
#include "dsp.h"
#include "dect.h"

#define MAX_CARRIERS 10
#define MAX_BASES    8
#define MAX_BURSTS   8

struct carrier {
	int      level_db4;              /* Activity (peak above the floor, decaying). */
	int      rfp, pp;                /* Bursts decoded. */
	uint32_t last_ms;
};

struct base {
	uint64_t rfpi;
	int      carrier;
	int      level_db4;
	int      count;
	uint32_t last_ms;
};

static int               region;
static int               pair;
static struct carrier    carriers[MAX_CARRIERS];
static struct base       bases[MAX_BASES];
static int               nbases;
static uint32_t          pp_ms;           /* Last handset burst. */
static int               pp_carrier;
static uint32_t          sweeps;
static uint64_t          power[FFT_SIZE];
static struct dect_burst bursts[MAX_BURSTS];

static const char *const help[] = {
	"DECT SCANNER: CARRIERS",
	"ACTIVITY, BASE STATIONS (RFPI",
	"FROM THEIR BEACONS), HANDSET",
	"TRANSMISSIONS (CALLS). ONLY THE",
	"A-FIELD (NO VOICE) IS DECODED.",
	"",
	"SELECT      REGION EU/US",
	"B           CLEAR",
	NULL,
};

static void clear(void)
{
	memset(carriers, 0, sizeof(carriers));
	nbases = 0;
	pp_ms  = 0;
	sweeps = 0;
}

static void enter(void)
{
	app_filter(0);
	app_gain(-1);
	clear();
}

static void add_burst(int c, const struct dect_burst *d)
{
	uint32_t t = app_ms();
	carriers[c].last_ms = t;
	if (!d->rfp) {
		carriers[c].pp++;
		pp_ms      = t;
		pp_carrier = c;
		return;
	}
	carriers[c].rfp++;
	if (!d->rfpi)
		return;
	struct base *b = NULL;
	for (int i = 0; i < nbases; i++)
		if (bases[i].rfpi == d->rfpi)
			b = &bases[i];
	if (!b && nbases < MAX_BASES) {
		b = &bases[nbases++];
		memset(b, 0, sizeof(*b));
		b->rfpi = d->rfpi;
	}
	if (!b)
		return;
	b->carrier   = c;
	b->level_db4 = d->level_db4;
	b->count++;
	b->last_ms   = t;
}

static int band_median(const int *db, int k0, int n)
{
	static int v[64];
	for (int i = 0; i < n; i++) {
		int j = i;
		for (; j > 0 && v[j - 1] > db[k0 + i]; j--)
			v[j] = v[j - 1];
		v[j] = db[k0 + i];
	}
	return v[n/2];
}

static int update(void)
{
	/* Carriers pair c, c + 1: tuned between them (+-864kHz). */
	int n  = dect_carriers(region);
	int c0 = 2*pair, c1 = (c0 + 1 < n) ? c0 + 1 : -1;
	int tuned = (c1 >= 0) ? (dect_carrier_khz(region, c0) + dect_carrier_khz(region, c1))/2 :
		dect_carrier_khz(region, c0) - 864;
	app_tune(tuned);
	int r = app_capture(APP_SAMPLES, ESP32SDR_RATE_16MSPS, app_iq);
	if (r == 0) {
		/* Activity: carrier band (+-500kHz) median over the capture mean (31.25kHz bins, spurs
		   ignored). */
		dsp_power_spectrum(app_iq, APP_SAMPLES, power);
		static int db[FFT_SIZE];
		for (int k = 0; k < FFT_SIZE; k++)
			db[k] = dsp_db4(power[k]);
		int floor = 0;
		for (int k = FFT_SIZE/4; k < 3*FFT_SIZE/4; k++)
			floor += db[k];
		floor /= FFT_SIZE/2;
		for (int c = c0; c <= c1 || (c1 < 0 && c == c0); c++) {
			int off = 1000*(dect_carrier_khz(region, c) - tuned);
			int peak = band_median(db, FFT_SIZE/2 + (off - 500000)/31250, 32) - floor;
			carriers[c].level_db4 = (peak > carriers[c].level_db4) ? peak :
				carriers[c].level_db4 - (carriers[c].level_db4 + 7)/8;
			int nb = dect_decode(app_iq, APP_SAMPLES, off, bursts, MAX_BURSTS);
			for (int i = 0; i < nb; i++)
				add_burst(c, &bursts[i]);
		}
		pair = (2*(pair + 1) < n) ? pair + 1 : 0;
		sweeps += (pair == 0);
	}

	/* Display. */
	char line[48];
	lcd_rect(0, 0, LCD_WIDTH, LCD_HEIGHT, COLOR_BLACK);
	snprintf(line, sizeof(line), "%s %d-%d MHZ", region ? "US" : "EU",
		dect_carrier_khz(region, n - 1)/1000, (dect_carrier_khz(region, 0) + 999)/1000);
	app_header("DECT", line);
	/* Carriers: activity bars, decoded bursts. */
	int bw = LCD_WIDTH/MAX_CARRIERS, y0 = APP_HEADER_H + 4, h = 30;
	for (int c = 0; c < n; c++) {
		int x = c*bw, v = carriers[c].level_db4*h/(40*4);
		v = (v < 1) ? 1 : (v > h) ? h : v;
		uint8_t color = (carriers[c].rfp || carriers[c].pp) ? COLOR_GREEN :
			(carriers[c].level_db4 > 10*4) ? COLOR_PEAK : COLOR_FILL;
		lcd_rect(x + 2, y0 + h - v, bw - 4, v, color);
		if (c == c0 || c == c1)
			lcd_rect(x + 2, y0 + h + 1, bw - 4, 1, COLOR_CURSOR);
		snprintf(line, sizeof(line), "%d", c);
		lcd_text(x + bw/2 - 1, y0 + h + 3, COLOR_DIM, line);
	}
	int y = y0 + h + 12;
	lcd_text(1, y, COLOR_DIM, "BASE RFPI    CARRIER  DB  BEACONS");
	y += 8;
	for (int i = 0; i < nbases && y < LCD_HEIGHT - 20; i++, y += 8) {
		const struct base *b = &bases[i];
		snprintf(line, sizeof(line), "%02X%08lX   %d %4d.%03d %3d %5d", (int)(b->rfpi >> 32) & 0xff,
			(unsigned long)(b->rfpi & 0xffffffff), b->carrier, dect_carrier_khz(region, b->carrier)/1000,
			dect_carrier_khz(region, b->carrier) % 1000, b->level_db4/4, b->count);
		lcd_text(1, y, (app_ms() - b->last_ms > 30000) ? COLOR_DIM : COLOR_WHITE, line);
	}
	if (!nbases)
		lcd_text(1, y, COLOR_DIM, sweeps ? "NO BASE STATION DECODED" : "SCANNING...");
	if (pp_ms && app_ms() - pp_ms < 5000) {
		snprintf(line, sizeof(line), "HANDSET TRANSMITTING (CARRIER %d)", pp_carrier);
		lcd_rect(0, LCD_HEIGHT - 10, LCD_WIDTH, 10, COLOR_SELECT);
		lcd_text(2, LCD_HEIGHT - 8, COLOR_WHITE, line);
	} else {
		snprintf(line, sizeof(line), "%lu SWEEPS", (unsigned long)sweeps);
		lcd_text(1, LCD_HEIGHT - 7, COLOR_DIM, line);
	}
	app_present();
	return r;
}

static void buttons(uint32_t p)
{
	if (p & (1 << BTN_SEL)) {
		region = !region;
		pair   = 0;
		clear();
		app_toast("REGION %s", region ? "US (DECT 6.0)" : "EU");
	}
	if (p & (1 << BTN_B)) {
		clear();
		app_toast("CLEARED");
	}
}

const struct tool tool_dect = {
	.name    = "DECT SCANNER",
	.enter   = enter,
	.update  = update,
	.buttons = buttons,
	.help    = help,
};

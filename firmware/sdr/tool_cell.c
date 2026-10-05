// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Cell scanner tool: LTE carriers of a band found on its spectrum (sweep of wide captures, clusters
// tiled with the LTE bandwidths), then decoded (lte.c, 16MS/s captures until the PSS/SSS/PBCH are
// found): physical cell ID, duplex, PSS correlation, MIB (bandwidth, transmit ports, frame number),
// frequency offset (receiver LO error).

#include <stdio.h>
#include <string.h>

#include "app.h"
#include "dsp.h"
#include "lte.h"

/* Bands (3GPP TS 36.101 downlink, within the tuning range). */
static const struct {
	const char *name;
	int         lo, hi;   /* kHz. */
	int         earfcn;   /* N_Offs-DL. */
} bands[] = {
	{"B1 2100",   2110000, 2170000,     0},
	{"B3 1800",   1805000, 1880000,  1200},
	{"B7 2600",   2620000, 2690000,  2750},
	{"B38 TDD",   2570000, 2620000, 37750},
	{"B40 TDD",   2300000, 2400000, 38650},
	{"B41 TDD",   2496000, 2690000, 39650},
	{"B2 1900",   1930000, 1990000,   600},
	{"B66 AWS",   2110000, 2200000, 66436},
};
#define BANDS (int)(sizeof(bands)/sizeof(bands[0]))

#define BIN_HZ      156250               /* Sweep resolution (512-point FFT at 80MS/s). */
#define STEP_KHZ    50000                /* Sweep step (+-25MHz of the wide captures: flat). */
#define MAX_BINS    1300                 /* 200MHz. */
#define OFFSET_KHZ  2000                 /* Carrier offset from the tuned frequency (DC away). */
#define MAX_CARRIERS 16
#define MAX_CAPTURES 40                  /* Per carrier (PSS: ~1 capture out of 5). */
#define MIB_CAPTURES 60                  /* Per found cell (MIB: ~1 capture out of 20). */
#define MIN_HITS     3
#define SWEEP_GAIN   50                  /* Sweep: manual gain (comparable steps). */

enum {
	STATE_IDLE = 0,
	STATE_SWEEP,
	STATE_DECODE,
	STATE_DONE,
};

struct carrier {
	int khz;           /* Center (100kHz raster). */
	int bw_khz;        /* Occupied bandwidth. */
	int level_db4;
	int captures;
	int raster;        /* Raster offset tried (index: 0, -100, 100, ... +-300kHz). */
	int hits;
	int pci;           /* -1: not found. */
	int tdd;
	int pss;           /* PSS correlation (%). */
	int cfo_hz;
	int rbs;           /* MIB: bandwidth (resource blocks), 0: not decoded. */
	int ports;
	int sfn;
};

static int            band;
static int            state;
static int16_t        spec[MAX_BINS];  /* Band spectrum (dB4). */
static int16_t        smooth[MAX_BINS];
static int            bins;
static int            sweep_step;
static struct carrier carriers[MAX_CARRIERS];
static int            ncarriers;
static int            current;
static int            scroll;
static uint64_t       power[FFT_SIZE];
static int            search_ms;
static int            lo_ppb;          /* Receiver LO error (cells' frequency offsets). */
static int            lo_known;
static int            lo_hits;

static int lo_error_hz(int khz)
{
	return (int)((int64_t)lo_ppb*khz/1000000);
}

static const char *const help[] = {
	"LTE CELL SCANNER: CARRIERS OF",
	"A BAND FOUND ON ITS SPECTRUM,",
	"THEN DECODED (PSS/SSS/PBCH):",
	"PCI, FDD/TDD, PSS CORRELATION,",
	"EARFCN, MIB (BANDWIDTH, TX",
	"PORTS, SFN), RECEIVER LO ERROR.",
	"",
	"LEFT/RIGHT  BAND",
	"A           SCAN (B: STOP)",
	"UP/DOWN     SCROLL",
	"",
	"OUTDOORS/NEAR A WINDOW HELPS.",
	NULL,
};

static int band_bins(void)
{
	return (int)((int64_t)(bands[band].hi - bands[band].lo)*1000/BIN_HZ) + 1;
}

static void enter(void)
{
	lte_init();
	state = STATE_IDLE;
	app_filter(0);
}

static void start(void)
{
	state      = STATE_SWEEP;
	sweep_step = 0;
	bins       = band_bins();
	bins       = (bins > MAX_BINS) ? MAX_BINS : bins;
	ncarriers  = 0;
	scroll     = 0;
	memset(spec, 0, sizeof(spec));
	app_gain(SWEEP_GAIN);
	app_filter(1);
}

/* Carriers ------------------------------------------------------------------------------------- */

static int percentile(const int16_t *v, int n, int pct)
{
	/* Value at the pct percentile (histogram of the dB4 values). */
	static uint16_t hist[1024];
	memset(hist, 0, sizeof(hist));
	for (int i = 0; i < n; i++)
		hist[(v[i] < 0) ? 0 : (v[i] > 1023) ? 1023 : v[i]]++;
	int target = n*pct/100, sum = 0;
	for (int i = 0; i < 1024; i++) {
		sum += hist[i];
		if (sum > target)
			return i;
	}
	return 1023;
}

static void add_carrier(int khz, int bw_khz, int level)
{
	/* Candidate (100kHz raster), skipped if already tried (+-300kHz). */
	khz = (khz + 50)/100*100;
	for (int i = 0; i < ncarriers; i++)
		if (carriers[i].khz - khz < 300 && khz - carriers[i].khz < 300)
			return;
	if (ncarriers >= MAX_CARRIERS)
		return;
	struct carrier *c = &carriers[ncarriers++];
	memset(c, 0, sizeof(*c));
	c->khz       = khz;
	c->bw_khz    = bw_khz;
	c->level_db4 = level;
	c->pci       = -1;
}

static int bin_khz(int b)
{
	return bands[band].lo + (int)((int64_t)b*BIN_HZ/1000);
}

static void find_carriers(void)
{
	/* Smoothed spectrum (~470kHz), bins above the floor + 4dB, clusters (gaps < 2MHz bridged:
	   lightly loaded carriers are not flat, only their reference signals fill the unused resource
	   blocks), >= 1MHz and half filled (not a group of narrowband lines), tiled with the LTE
	   bandwidths from the low edge (occupied: 90%): candidate centers. */
	static const int widths[] = {20000, 15000, 10000, 5000, 3000};
	static int16_t block[MAX_BINS];
	for (int i = 0; i < bins; i++) {
		int s = 0, n = 0;
		for (int k = i - 1; k <= i + 1; k++)
			if (k >= 0 && k < bins) {
				s += spec[k];
				n++;
			}
		smooth[i] = s/n;
	}
	int threshold = percentile(smooth, bins, 20) + 4*4;
	int gap_max   = 2000000/BIN_HZ;
	for (int i = 0; i < bins; ) {
		if (smooth[i] < threshold) {
			i++;
			continue;
		}
		int b0 = i, b1 = i, gap = 0, above = 0;
		for (; i < bins && gap <= gap_max; i++) {
			if (smooth[i] >= threshold) {
				b1  = i;
				gap = 0;
				above++;
			} else
				gap++;
		}
		int n = b1 - b0 + 1;
		if (n*BIN_HZ < 1000000 || above*2 < n)
			continue;
		memcpy(block, &smooth[b0], n*sizeof(int16_t));
		int level = percentile(block, n, 50);
		int lo = bin_khz(b0), hi = bin_khz(b1 + 1);
		/* Single carrier: centered. */
		for (int k = 0; k < 5; k++)
			if (hi - lo <= widths[k] + 1000 && hi - lo >= widths[k]*8/10) {
				add_carrier((lo + hi)/2, hi - lo, level);
				lo = hi;
				break;
			}
		while (hi - lo >= 1000) {
			int w = widths[4];
			for (int k = 0; k < 5; k++)
				if (widths[k]*9/10 <= hi - lo + 1000) {
					w = widths[k];
					break;
				}
			int occupied = (w*9/10 < hi - lo) ? w*9/10 : hi - lo;
			add_carrier(lo + occupied/2, occupied, level);
			lo += w;
		}
	}
}

/* Steps ---------------------------------------------------------------------------------------- */

static int sweep(void)
{
	/* One 50MHz step: 4 wide captures (80MS/s, RX filter opened) averaged, +-25MHz kept. */
	int center = bands[band].lo + STEP_KHZ/2 + sweep_step*STEP_KHZ;
	app_tune(center);
	for (int k = 0; k < 4; k++) {
		int r = app_capture(APP_SAMPLES, ESP32SDR_RATE_80MSPS, app_iq);
		if (r != 0)
			return r;
		dsp_power_spectrum(app_iq, APP_SAMPLES, power);
		for (int i = 0; i < FFT_SIZE; i++) {
			int64_t f = (int64_t)center*1000 - 40000000 + (int64_t)i*BIN_HZ;
			int     b = (int)((f - (int64_t)bands[band].lo*1000)/BIN_HZ);
			if (i < FFT_SIZE/2 - 160 || i >= FFT_SIZE/2 + 160 || b < 0 || b >= bins)
				continue;
			int db4 = dsp_db4(power[i]);
			spec[b] = k ? (k*spec[b] + db4)/(k + 1) : db4;
		}
	}
	sweep_step++;
	if (bands[band].lo + sweep_step*STEP_KHZ >= bands[band].hi) {
		find_carriers();
		state   = STATE_DECODE;
		current = 0;
		app_gain(-1);
		app_filter(0);
		if (ncarriers == 0)
			state = STATE_DONE;
	}
	return 0;
}

static int decode(void)
{
	/* One capture of the current carrier: raster offsets tried in turn until the cell is found. */
	static const int rasters[7] = {0, -100, 100, -200, 200, -300, 300};
	struct carrier *c = &carriers[current];
	int raster = rasters[c->raster];
	app_tune(c->khz + raster + OFFSET_KHZ);
	int r = app_capture(APP_SAMPLES, ESP32SDR_RATE_16MSPS, app_iq);
	if (r != 0)
		return r;
	c->captures++;
	struct lte_cell cell;
	uint32_t t0 = app_ms();
	int found = lte_search(app_iq, APP_SAMPLES, -OFFSET_KHZ*1000 + lo_error_hz(c->khz), !lo_known,
		&cell);
	search_ms = app_ms() - t0;
	if (found) {
		if (c->pci < 0 || c->pci == cell.pci) {
			/* Frequency offset: expected (LO error) + measured. */
			int cfo = lo_error_hz(c->khz) + cell.cfo_hz;
			c->khz += raster;
			c->raster = 0;
			c->pci    = cell.pci;
			c->tdd    = cell.tdd;
			c->pss    = (cell.pss > c->pss) ? cell.pss : c->pss;
			c->cfo_hz = (c->hits*c->cfo_hz + cfo)/(c->hits + 1);
			c->hits++;
			/* MIB (PBCH after the PSS of subframe 0, in ~2/3 of these captures). */
			struct lte_mib mib;
			if (lte_mib(&cell, &mib)) {
				c->rbs   = mib.rbs;
				c->ports = mib.ports;
				c->sfn   = mib.sfn;
			}
			/* Receiver LO error: following searches around it (+-4kHz). */
			int ppb  = (int)((int64_t)cfo*1000000/c->khz);
			lo_ppb   = (lo_hits*lo_ppb + ppb)/(lo_hits + 1);
			lo_hits  = (lo_hits < 16) ? lo_hits + 1 : lo_hits;
			lo_known = 1;
		}
	} else if (c->pci < 0 && c->captures % 4 == 0)
		c->raster = (c->raster + 1) % 7;
	/* Done: cell and MIB found, or no cell (MAX_CAPTURES), or no MIB (MIB_CAPTURES). */
	if ((c->hits >= MIN_HITS && c->rbs) ||
		c->captures >= ((c->pci >= 0) ? MIB_CAPTURES : MAX_CAPTURES)) {
		current++;
		if (current >= ncarriers)
			state = STATE_DONE;
	}
	return 0;
}

/* Display -------------------------------------------------------------------------------------- */

#define PLOT_Y 16
#define PLOT_H 32
#define LIST_Y (PLOT_Y + PLOT_H + 10)

static void draw(void)
{
	static const char *states[] = {"A: SCAN", "SWEEP", "DECODE", "DONE"};
	char line[48];
	snprintf(line, sizeof(line), "%s", bands[band].name);
	lcd_rect(0, 0, LCD_WIDTH, LCD_HEIGHT, COLOR_BLACK);
	app_header("CELL", "");
	lcd_text(46, 1, COLOR_WHITE, line);
	snprintf(line, sizeof(line), "%d-%d MHZ %s", bands[band].lo/1000, bands[band].hi/1000,
		states[state]);
	lcd_text(46, 7, (state == STATE_IDLE || state == STATE_DONE) ? COLOR_DIM : COLOR_PEAK, line);

	/* Band spectrum (columns: max), carriers marked (decoded: green). */
	if (state != STATE_IDLE) {
		int ref = 0;
		for (int i = 0; i < bins; i++)
			ref = (spec[i] > ref) ? spec[i] : ref;
		for (int x = 0; x < LCD_WIDTH; x++) {
			int v = 0;
			for (int i = x*bins/LCD_WIDTH; i < (x + 1)*bins/LCD_WIDTH; i++)
				v = (spec[i] > v) ? spec[i] : v;
			if (!v)
				continue;
			int h = (v - (ref - 30*4))*PLOT_H/(30*4);
			h = (h < 1) ? 1 : (h > PLOT_H) ? PLOT_H : h;
			lcd_rect(x, PLOT_Y + PLOT_H - h, 1, h, COLOR_FILL);
			lcd_rect(x, PLOT_Y + PLOT_H - h, 1, 1, COLOR_TRACE);
		}
		for (int i = 0; i < ncarriers; i++) {
			struct carrier *c = &carriers[i];
			int span = bands[band].hi - bands[band].lo;
			int x0 = (int)((int64_t)(c->khz - c->bw_khz/2 - bands[band].lo)*LCD_WIDTH/span);
			int x1 = (int)((int64_t)(c->khz + c->bw_khz/2 - bands[band].lo)*LCD_WIDTH/span);
			uint8_t color = (c->pci >= 0) ? COLOR_GREEN : (i == current && state == STATE_DECODE) ?
				COLOR_CURSOR : COLOR_DIM;
			lcd_rect(x0, PLOT_Y + PLOT_H + 2, (x1 - x0 > 0) ? x1 - x0 : 1, 2, color);
		}
		if (state == STATE_SWEEP) {
			int steps = (bands[band].hi - bands[band].lo + STEP_KHZ - 1)/STEP_KHZ;
			lcd_rect(0, APP_HEADER_H - 1, sweep_step*LCD_WIDTH/steps, 1, COLOR_CURSOR);
		}
	}

	/* Carriers. */
	lcd_text(1, LIST_Y, COLOR_DIM, "MHZ      BW  PCI  MODE PSS EARFCN");
	int rows = (LCD_HEIGHT - LIST_Y - 16)/8;
	for (int r = 0; r < rows && scroll + r < ncarriers; r++) {
		struct carrier *c = &carriers[scroll + r];
		char f[16], pci[8], pss[8];
		app_format_khz(f, sizeof(f), c->khz, 1);
		int earfcn = bands[band].earfcn + (c->khz - bands[band].lo)/100;
		snprintf(pci, sizeof(pci), (c->pci >= 0) ? "%d" : "-", c->pci);
		snprintf(pss, sizeof(pss), (c->pci >= 0) ? "%d%%" : "", c->pss);
		static const struct { int rbs; const char *mhz; } bws[6] = {
			{6, "1.4"}, {15, "3"}, {25, "5"}, {50, "10"}, {75, "15"}, {100, "20"},
		};
		char bw[8];
		snprintf(bw, sizeof(bw), "%d", (c->bw_khz + 500)/1000);
		for (int i = 0; i < 6; i++)
			if (c->rbs == bws[i].rbs)
				snprintf(bw, sizeof(bw), "%s", bws[i].mhz);
		snprintf(line, sizeof(line), "%-8s %3s %4s %4s %3s %d", f, bw, pci,
			(c->pci < 0) ? ((scroll + r == current && state == STATE_DECODE) ? "...." :
			(c->captures ? "NONE" : "")) : c->tdd ? "TDD" : "FDD", pss, earfcn);
		lcd_text(1, LIST_Y + 8 + 8*r, (c->pci >= 0) ? COLOR_WHITE : COLOR_DIM, line);
	}
	if (state == STATE_DONE && ncarriers == 0)
		lcd_text(1, LIST_Y + 8, COLOR_DIM, "NO CARRIER FOUND");

	/* MIB of the first decoded cell from the scroll position. */
	for (int i = scroll; i < ncarriers; i++)
		if (carriers[i].rbs) {
			snprintf(line, sizeof(line), "MIB PCI %d: %d RB, %d TX, SFN %d", carriers[i].pci,
				carriers[i].rbs, carriers[i].ports, carriers[i].sfn);
			lcd_text(1, LCD_HEIGHT - 15, COLOR_GREEN, line);
			break;
		}
	/* Receiver LO error (mean frequency offset of the cells). */
	int n = 0, ppb = 0;
	for (int i = 0; i < ncarriers; i++)
		if (carriers[i].pci >= 0) {
			ppb += (int)((int64_t)carriers[i].cfo_hz*1000000/carriers[i].khz);
			n++;
		}
	if (!n && search_ms) {
		snprintf(line, sizeof(line), "SEARCH %d MS", search_ms);
		lcd_text(1, LCD_HEIGHT - 7, COLOR_DIM, line);
	}
	if (n) {
		ppb /= n;
		snprintf(line, sizeof(line), "%d CELLS  LO ERROR %+d.%d PPM", n, ppb/1000,
			((ppb < 0) ? -ppb : ppb)/100 % 10);
		lcd_text(1, LCD_HEIGHT - 7, COLOR_PEAK, line);
	}
}

static int update(void)
{
	int r = 0;
	if (state == STATE_SWEEP)
		r = sweep();
	else if (state == STATE_DECODE)
		r = decode();
	draw();
	app_present();
	return r;
}

static void buttons(uint32_t p)
{
	int running = (state == STATE_SWEEP || state == STATE_DECODE);
	if (!running && (p & ((1 << BTN_LEFT) | (1 << BTN_RIGHT)))) {
		band  = (band + BANDS + ((p & (1 << BTN_RIGHT)) ? 1 : -1)) % BANDS;
		state = STATE_IDLE;
		ncarriers = 0;
	}
	if (p & (1 << BTN_A))
		start();
	if (running && (p & (1 << BTN_B))) {
		state = STATE_DONE;
		app_gain(-1);
		app_filter(0);
	}
	if (p & (1 << BTN_UP))
		scroll = (scroll > 0) ? scroll - 1 : 0;
	if (p & (1 << BTN_DOWN))
		scroll = (scroll < ncarriers - 1) ? scroll + 1 : scroll;
}

const struct tool tool_cell = {
	.name    = "CELL SCANNER",
	.enter   = enter,
	.update  = update,
	.buttons = buttons,
	.help    = help,
};

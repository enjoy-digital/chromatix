// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// IEEE 802.15.4 sniffer tool (Zigbee, Thread, Matter): channels 11-26 captured in pairs (16MS/s,
// 1ms), channels activity, frames decoded (zigbee.c: short frames, FCS checked): networks (PAN IDs)
// and the last frames.

#include <stdio.h>
#include <string.h>

#include "app.h"
#include "dsp.h"
#include "zigbee.h"

#define CHANNELS   16
#define MAX_PANS   8
#define MAX_LAST   6
#define MAX_FRAMES 4

struct channel {
	int level_db4;                   /* Activity (peak above the floor, decaying). */
	int frames;
};

struct pan {
	int      id;
	int      channel;
	int      count;
	char     net[8];
	uint32_t last_ms;
};

struct last {
	int             channel;
	struct zb_frame f;
};

static int             pair;
static struct channel  channels[CHANNELS];
static struct pan      pans[MAX_PANS];
static int             npans;
static struct last     last[MAX_LAST];
static int             nlast;
static uint32_t        total;
static uint64_t        power[FFT_SIZE];
static struct zb_frame frames[MAX_FRAMES];

static const char *const help[] = {
	"802.15.4 SNIFFER (ZIGBEE,",
	"THREAD, MATTER): CHANNELS",
	"11-26 ACTIVITY, NETWORKS (PAN",
	"IDS), LAST FRAMES (SHORT",
	"FRAMES: 1MS CAPTURES).",
	"",
	"B           CLEAR",
	NULL,
};

static void clear(void)
{
	memset(channels, 0, sizeof(channels));
	npans = 0;
	nlast = 0;
	total = 0;
}

static void enter(void)
{
	app_filter(0);
	app_gain(-1);
	clear();
}

static void add_frame(int ch, const struct zb_frame *f)
{
	channels[ch - 11].frames++;
	total++;
	memmove(&last[1], &last[0], (MAX_LAST - 1)*sizeof(last[0]));
	last[0].channel = ch;
	last[0].f       = *f;
	nlast = (nlast < MAX_LAST) ? nlast + 1 : nlast;
	if (f->pan < 0)
		return;
	struct pan *p = NULL;
	for (int i = 0; i < npans; i++)
		if (pans[i].id == f->pan && pans[i].channel == ch)
			p = &pans[i];
	if (!p && npans < MAX_PANS) {
		p = &pans[npans++];
		memset(p, 0, sizeof(*p));
		p->id      = f->pan;
		p->channel = ch;
	}
	if (!p)
		return;
	p->count++;
	p->last_ms = app_ms();
	if (zb_network(f)[0])
		snprintf(p->net, sizeof(p->net), "%s", zb_network(f));
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
	/* Channels pair (+-2.5MHz from the tuned frequency). */
	int ch0 = 11 + 2*pair, tuned = zb_channel_khz(ch0) + 2500;
	app_tune(tuned);
	int r = app_capture(APP_SAMPLES, ESP32SDR_RATE_16MSPS, app_iq);
	if (r == 0) {
		/* Activity: channel band (+-1MHz) median over the capture mean (spurs ignored). */
		dsp_power_spectrum(app_iq, APP_SAMPLES, power);
		static int db[FFT_SIZE];
		int floor = 0;
		for (int k = 0; k < FFT_SIZE; k++)
			db[k] = dsp_db4(power[k]);
		for (int k = FFT_SIZE/4; k < 3*FFT_SIZE/4; k++)
			floor += db[k];
		floor /= FFT_SIZE/2;
		for (int ch = ch0; ch <= ch0 + 1; ch++) {
			int off  = 1000*(zb_channel_khz(ch) - tuned);
			int peak = band_median(db, FFT_SIZE/2 + (off - 1000000)/31250, 64) - floor;
			struct channel *c = &channels[ch - 11];
			c->level_db4 = (peak > c->level_db4) ? peak : c->level_db4 - (c->level_db4 + 7)/8;
			int n = zb_decode(app_iq, APP_SAMPLES, off, frames, MAX_FRAMES);
			for (int i = 0; i < n; i++)
				add_frame(ch, &frames[i]);
		}
		pair = (pair + 1) % (CHANNELS/2);
	}

	/* Display. */
	char line[48];
	lcd_rect(0, 0, LCD_WIDTH, LCD_HEIGHT, COLOR_BLACK);
	snprintf(line, sizeof(line), "CH 11-26 %lu FRAMES", (unsigned long)total);
	app_header("802.15.4", line);
	int bw = LCD_WIDTH/CHANNELS, y0 = APP_HEADER_H + 4, h = 24;
	for (int i = 0; i < CHANNELS; i++) {
		int x = i*bw, v = channels[i].level_db4*h/(40*4);
		v = (v < 1) ? 1 : (v > h) ? h : v;
		lcd_rect(x + 1, y0 + h - v, bw - 2, v, channels[i].frames ? COLOR_GREEN :
			(channels[i].level_db4 > 10*4) ? COLOR_PEAK : COLOR_FILL);
		if (i/2 == pair)
			lcd_rect(x + 1, y0 + h + 1, bw - 2, 1, COLOR_CURSOR);
		if (i % 5 == 0) {
			snprintf(line, sizeof(line), "%d", 11 + i);
			lcd_text(x, y0 + h + 3, COLOR_DIM, line);
		}
	}
	int y = y0 + h + 11;
	lcd_text(1, y, COLOR_DIM, "PAN   CH NETWORK   FRAMES");
	y += 8;
	for (int i = 0; i < npans && i < 3; i++, y += 8) {
		snprintf(line, sizeof(line), "%04X  %2d %-9s %5d", pans[i].id, pans[i].channel, pans[i].net,
			pans[i].count);
		lcd_text(1, y, COLOR_WHITE, line);
	}
	if (!npans)
		y += 8;
	y += 2;
	lcd_text(1, y, COLOR_DIM, "CH TYPE   SRC>DST          DB");
	y += 8;
	for (int i = 0; i < nlast && y < LCD_HEIGHT - 6; i++, y += 7) {
		const struct zb_frame *f = &last[i].f;
		char src[12] = "-", dst[12] = "-";
		if (f->src_mode)
			snprintf(src, sizeof(src), (f->src_mode == 3) ? "%06lX" : "%04lX",
				(unsigned long)(f->src & ((f->src_mode == 3) ? 0xffffff : 0xffff)));
		if (f->dst_mode)
			snprintf(dst, sizeof(dst), (f->dst_mode == 3) ? "%06lX" : "%04lX",
				(unsigned long)(f->dst & ((f->dst_mode == 3) ? 0xffffff : 0xffff)));
		snprintf(line, sizeof(line), "%2d %-6s %6s>%-6s %4s %3d", last[i].channel,
			zb_type_name(f->type), src, dst, zb_network(f), f->level_db4/4);
		lcd_text(1, y, COLOR_WHITE, line);
	}
	if (!nlast)
		lcd_text(1, y, COLOR_DIM, "LISTENING...");
	app_present();
	return r;
}

static void buttons(uint32_t p)
{
	if (p & (1 << BTN_B)) {
		clear();
		app_toast("CLEARED");
	}
}

const struct tool tool_zigbee = {
	.name    = "802.15.4 SNIFFER",
	.enter   = enter,
	.update  = update,
	.buttons = buttons,
	.help    = help,
};

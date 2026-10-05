// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Signal identification tool: the 2.4GHz ISM band (2398-2485MHz) captured in two halves (80MS/s
// wide captures), bursts classified (classify.c): waterfall with the bursts marked (class colors),
// classes seen (count, last frequency/bandwidth/duration, age), drone alert (10MHz OFDM links, DJI
// DroneID, continuous wideband video) with an optional alarm.

#include <stdio.h>
#include <string.h>

#include "app.h"
#include "classify.h"

#define BAND_LO   2398000
#define BAND_HI   2485000
#define MAX_SIGS  32
#define WF_Y      (APP_HEADER_H + 4)
#define WF_H      48
#define AXIS_Y    (WF_Y + WF_H)
#define LIST_Y    (AXIS_Y + 9)

static const int centers[2] = {2420000, 2463000}; /* +-22MHz each. */

struct seen {
	int      count;
	int      khz;
	int      bw_khz;
	int      us;
	int      level_db4;
	uint32_t last_ms;
};

static struct seen seen[SIG_CLASSES];
static struct sig  sigs[MAX_SIGS];
static int16_t     columns[SIG_BINS];
static int         line_db4[LCD_WIDTH];
static uint8_t     marks[LCD_WIDTH];       /* Bursts of the current sweep (class + 1). */
static int         half;
static int         floor_db4 = -1;
static int         alarm;
static int         paused;
static uint32_t    alert_ms;

static const uint8_t colors[SIG_CLASSES] = {
	COLOR_WIFI, COLOR_WIFI, COLOR_RED, COLOR_RED, COLOR_RED, COLOR_WHITE, COLOR_GREEN, COLOR_BLE,
	COLOR_PEAK, COLOR_DIM, COLOR_DIM, COLOR_DIM,
};

static const char *const help[] = {
	"SIGNAL ID: 2.4GHZ BURSTS",
	"CLASSIFIED (BANDWIDTH, LENGTH,",
	"CHANNEL): WIFI, BLE, 802.15.4,",
	"NARROWBAND (BT/RC HOPPING),",
	"DRONE LINKS (10MHZ OFDM, DJI",
	"DRONEID, WIDEBAND VIDEO).",
	"",
	"START       DRONE ALARM ON/OFF",
	"A           PAUSE",
	"B           CLEAR",
	NULL,
};

static void enter(void)
{
	sig_init();
	app_filter(1);
	app_gain(-1);
	memset(line_db4, 0, sizeof(line_db4));
	lcd_rect(0, 0, LCD_WIDTH, LCD_HEIGHT, COLOR_BLACK);
}

static int khz_to_x(int khz)
{
	return (int)((int64_t)(khz - BAND_LO)*LCD_WIDTH/(BAND_HI - BAND_LO));
}

static void process(int n)
{
	/* Columns of this half (spectrum max) to the waterfall line, bursts to the stats/marks. */
	for (int x = 0; x < LCD_WIDTH; x++) {
		int khz = BAND_LO + (int)((int64_t)(2*x + 1)*(BAND_HI - BAND_LO)/(2*LCD_WIDTH));
		int d   = khz - centers[half];
		if (d < -SIG_USABLE_KHZ || d >= SIG_USABLE_KHZ || (half == 0 && khz >= centers[1] -
			SIG_USABLE_KHZ))
			continue;
		line_db4[x] = columns[SIG_BINS/2 + d/625];
	}
	for (int i = 0; i < n; i++) {
		struct sig  *s = &sigs[i];
		struct seen *e = &seen[s->cls];
		e->count++;
		e->khz       = s->khz;
		e->bw_khz    = s->bw_khz;
		e->us        = s->us;
		e->level_db4 = s->level_db4;
		e->last_ms   = app_ms();
		int x0 = khz_to_x(s->khz - s->bw_khz/2), x1 = khz_to_x(s->khz + s->bw_khz/2);
		for (int x = (x0 < 0) ? 0 : x0; x <= x1 && x < LCD_WIDTH; x++)
			if (!marks[x] || sig_drone(s->cls))
				marks[x] = s->cls + 1;
		if (sig_drone(s->cls)) {
			alert_ms = app_ms();
			if (alarm)
				app_beep(1500, 120);
		}
	}
}

static void draw_line(void)
{
	/* New waterfall line (scrolled image), noise floor tracked (median of the line). */
	static int sorted[LCD_WIDTH];
	memcpy(sorted, line_db4, sizeof(sorted));
	for (int i = 1; i < LCD_WIDTH; i++)
		for (int j = i; j > 0 && sorted[j - 1] > sorted[j]; j--) {
			int t = sorted[j]; sorted[j] = sorted[j - 1]; sorted[j - 1] = t;
		}
	int median = sorted[LCD_WIDTH/2];
	floor_db4 = (floor_db4 < 0) ? median : floor_db4 + (median - floor_db4)/4;
	memmove(lcd_screen[WF_Y + 1], lcd_screen[WF_Y], (WF_H - 1)*LCD_WIDTH);
	app_heat_line(WF_Y, line_db4, floor_db4 - 3*4, 30);
	/* Bursts marks (top row). */
	for (int x = 0; x < LCD_WIDTH; x++)
		lcd_screen[WF_Y - 3][x] = marks[x] ? colors[marks[x] - 1] : COLOR_BLACK;
	memcpy(lcd_screen[WF_Y - 2], lcd_screen[WF_Y - 3], LCD_WIDTH);
	memset(marks, 0, sizeof(marks));
}

static void draw(void)
{
	char line[48];
	/* Header, axis (the waterfall image is kept). */
	snprintf(line, sizeof(line), "%d-%d MHZ%s%s", BAND_LO/1000, BAND_HI/1000, paused ? " PAUSED" : "",
		alarm ? " ALARM" : "");
	app_header("SIGNALS", line);
	lcd_rect(0, APP_HEADER_H, LCD_WIDTH, 1, COLOR_BLACK);
	lcd_rect(0, AXIS_Y, LCD_WIDTH, LCD_HEIGHT - AXIS_Y, COLOR_BLACK);
	for (int f = 2400; f <= 2480; f += 20) {
		int x = khz_to_x(1000*f);
		snprintf(line, sizeof(line), "%d", f);
		lcd_text((x < 8) ? 0 : x - 7, AXIS_Y + 2, COLOR_DIM, line);
		lcd_rect(x, AXIS_Y, 1, 2, COLOR_DIM);
	}
	/* Classes seen (most recent first). */
	int order[SIG_CLASSES], n = 0;
	for (int c = 0; c < SIG_CLASSES; c++)
		if (seen[c].count && c != SIG_BROADBAND)
			order[n++] = c;
	for (int i = 1; i < n; i++)
		for (int j = i; j > 0 && (int32_t)(seen[order[j]].last_ms - seen[order[j - 1]].last_ms) > 0;
			j--) {
			int t = order[j]; order[j] = order[j - 1]; order[j - 1] = t;
		}
	lcd_text(1, LIST_Y, COLOR_DIM, "CLASS           MHZ   BW  US  AGE");
	int rows = (LCD_HEIGHT - LIST_Y - 18)/7;
	for (int r = 0; r < n && r < rows; r++) {
		const struct seen *e = &seen[order[r]];
		int age = (app_ms() - e->last_ms)/1000;
		snprintf(line, sizeof(line), "%-15.15s %4d.%d %2d %3d %3d", sig_name(order[r]), e->khz/1000,
			(e->khz % 1000)/100, (e->bw_khz + 500)/1000, (e->us > 999) ? 999 : e->us,
			(age > 999) ? 999 : age);
		lcd_rect(1, LIST_Y + 9 + 7*r, 2, 5, colors[order[r]]);
		lcd_text(5, LIST_Y + 8 + 7*r, (age > 10) ? COLOR_DIM : COLOR_WHITE, line);
	}
	if (!n)
		lcd_text(1, LIST_Y + 8, COLOR_DIM, "LISTENING...");
	/* Drone alert (10s). */
	if (alert_ms && app_ms() - alert_ms < 10000) {
		int c = SIG_OFDM10;
		for (int k = 0; k < SIG_CLASSES; k++)
			if (sig_drone(k) && seen[k].count && seen[k].last_ms == alert_ms)
				c = k;
		snprintf(line, sizeof(line), "DRONE LINK? %s %d MHZ", sig_name(c), seen[c].khz/1000);
		lcd_rect(0, LCD_HEIGHT - 10, LCD_WIDTH, 10, COLOR_RED);
		lcd_text(2, LCD_HEIGHT - 8, COLOR_WHITE, line);
	}
}

static int update(void)
{
	int r = 0;
	if (!paused) {
		app_tune(centers[half]);
		r = app_capture(APP_SAMPLES, ESP32SDR_RATE_80MSPS, app_iq);
		if (r == 0) {
			int n = sig_detect(app_iq, APP_SAMPLES, centers[half], half, sigs, MAX_SIGS, columns);
			process(n);
			if (half == 1)
				draw_line();
			half ^= 1;
		}
	}
	draw();
	app_present();
	return r;
}

static void buttons(uint32_t p)
{
	if (p & (1 << BTN_A)) {
		paused = !paused;
		app_toast(paused ? "PAUSED" : "RUNNING");
	}
	if (p & (1 << BTN_B)) {
		memset(seen, 0, sizeof(seen));
		alert_ms = 0;
		app_toast("CLEARED");
	}
	if (p & (1 << BTN_START)) {
		alarm = !alarm;
		app_toast("DRONE ALARM %s", alarm ? "ON" : "OFF");
	}
}

const struct tool tool_signals = {
	.name    = "SIGNAL ID",
	.enter   = enter,
	.update  = update,
	.buttons = buttons,
	.help    = help,
};

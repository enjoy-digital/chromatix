// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Hunt tool (interference hunting, transmitter direction finding): power of a channel (frequency,
// bandwidth) at each capture, gain corrected (manual gain auto-ranged: the AGC would hide the
// level changes): large readout, bar with peak hold, level history, tone (pitch following the
// level), max/average.

#include <stdio.h>
#include <string.h>

#include "app.h"
#include "dsp.h"

#define HISTORY  LCD_WIDTH
#define GAIN_MIN 0
#define GAIN_MAX 66

static const struct {
	int         khz;
	const char *name;
} widths[] = {
	{ 100, "100K"},
	{ 500, "500K"},
	{2000, "2M"},
	{5000, "5M"},
	{10000, "10M"},
};
#define WIDTHS (int)(sizeof(widths)/sizeof(widths[0]))

static const int steps[] = {100, 1000, 5000};

static int      freq_khz = 2441000;
static int      width    = 2;
static int      step     = 1;
static int      gain     = 40;
static int      level_db4;            /* Gain corrected (1/4 dB, arbitrary reference). */
static int      peak_db4;
static int      history[HISTORY];
static int      count;
static int      tone;
static uint64_t power[FFT_SIZE];

static const char *const help[] = {
	"HUNT: CHANNEL POWER (GAIN",
	"CORRECTED) TO FIND A",
	"TRANSMITTER/INTERFERENCE:",
	"POINT/MOVE, WATCH THE LEVEL.",
	"",
	"LEFT/RIGHT  TUNE",
	"UP/DOWN     STEP",
	"A           BANDWIDTH",
	"START       TONE",
	"B           RESET PEAK/HISTORY",
	NULL,
};

static int offset_khz(void)
{
	/* Tuned 2MHz above for the narrow bandwidths (DC away), else centered (DC bin interpolated). */
	return (widths[width].khz <= 2000) ? 2000 : 0;
}

static void reset(void)
{
	peak_db4 = 0;
	count    = 0;
	memset(history, 0, sizeof(history));
}

static void enter(void)
{
	app_filter(0);
	app_gain(gain);
	app_tune(freq_khz + offset_khz());
	reset();
}

static int measure(void)
{
	/* 16MS/s capture (+-5MHz used: RX filter), band power. */
	int r = app_capture(APP_SAMPLES, ESP32SDR_RATE_16MSPS, app_iq);
	if (r != 0)
		return r;
	/* Gain auto-ranging: clipping -> -6dB, weak -> +6dB. */
	int top = 0;
	for (int i = 0; i < 2*APP_SAMPLES; i += 7) {
		int v = (app_iq[i] < 0) ? -app_iq[i] : app_iq[i];
		top = (v > top) ? v : top;
	}
	int new_gain = gain;
	if (top >= 120 && gain > GAIN_MIN)
		new_gain = gain - 6;
	else if (top < 24 && gain < GAIN_MAX)
		new_gain = gain + 6;
	dsp_power_spectrum(app_iq, APP_SAMPLES, power);
	uint64_t sum = 0;
	int half = widths[width].khz/2;
	int k0 = FFT_SIZE/2 + (-offset_khz() - half)*FFT_SIZE/16000;
	int k1 = FFT_SIZE/2 + (-offset_khz() + half)*FFT_SIZE/16000;
	k0 = (k0 < 0) ? 0 : k0;
	k1 = (k1 > FFT_SIZE) ? FFT_SIZE : (k1 > k0) ? k1 : k0 + 1;
	for (int k = k0; k < k1; k++)
		sum += power[k];
	level_db4 = dsp_db4(sum + 1) - 4*gain;
	if (new_gain != gain) {
		gain = new_gain;
		app_gain(gain);
	}
	/* History, peak, tone. */
	memmove(&history[0], &history[1], (HISTORY - 1)*sizeof(int));
	history[HISTORY - 1] = level_db4;
	count++;
	peak_db4 = (count == 1 || level_db4 > peak_db4) ? level_db4 : peak_db4;
	if (tone)
		app_beep(200 + 15*(level_db4 - (peak_db4 - 40*4))/4, 400);
	return 0;
}

static int update(void)
{
	int r = measure();
	/* Display. */
	char line[48], f[16];
	lcd_rect(0, 0, LCD_WIDTH, LCD_HEIGHT, COLOR_BLACK);
	app_format_khz(f, sizeof(f), freq_khz, 3);
	snprintf(line, sizeof(line), "%s MHZ BW %s", f, widths[width].name);
	app_header("HUNT", line);
	snprintf(line, sizeof(line), "STEP %dK GAIN %d%s", steps[step], gain, tone ? " TONE" : "");
	lcd_text(LCD_WIDTH - 4*(int)strlen(line), 7, COLOR_DIM, line);
	/* Level (relative dB), bar (60dB scale below the peak + 10dB), peak hold. */
	snprintf(line, sizeof(line), "%d", level_db4/4);
	lcd_text_scaled(4, APP_HEADER_H + 6, COLOR_WHITE, line, 4);
	lcd_text(4 + 4*4*(int)strlen(line) + 2, APP_HEADER_H + 22, COLOR_DIM, "DB");
	snprintf(line, sizeof(line), "PEAK %d", peak_db4/4);
	lcd_text(110, APP_HEADER_H + 8, COLOR_PEAK, line);
	int avg = 0, n = (count < HISTORY) ? count : HISTORY;
	for (int i = HISTORY - n; i < HISTORY; i++)
		avg += history[i];
	snprintf(line, sizeof(line), "AVG  %d", n ? avg/n/4 : 0);
	lcd_text(110, APP_HEADER_H + 16, COLOR_DIM, line);
	int top = peak_db4 + 10*4, y = APP_HEADER_H + 36, w = LCD_WIDTH - 8;
	int x = (level_db4 - (top - 60*4))*w/(60*4);
	x = (x < 0) ? 0 : (x > w) ? w : x;
	lcd_rect(4, y, w, 10, COLOR_GRID);
	lcd_rect(4, y, x, 10, (level_db4 > peak_db4 - 3*4) ? COLOR_RED : COLOR_TRACE);
	int px = (peak_db4 - (top - 60*4))*w/(60*4);
	lcd_rect(4 + px, y - 2, 1, 14, COLOR_PEAK);
	/* History (60dB scale). */
	int hy = y + 18, hh = LCD_HEIGHT - hy - 2;
	lcd_rect(0, hy - 1, LCD_WIDTH, 1, COLOR_GRID);
	for (int i = HISTORY - n; i < HISTORY; i++) {
		int v = (history[i] - (top - 60*4))*hh/(60*4);
		v = (v < 1) ? 1 : (v > hh) ? hh : v;
		lcd_rect(i, hy + hh - v, 1, v, COLOR_FILL);
		lcd_rect(i, hy + hh - v, 1, 1, COLOR_TRACE);
	}
	app_present();
	return r;
}

static void buttons(uint32_t p)
{
	int dir = (p & (1 << BTN_RIGHT)) ? 1 : (p & (1 << BTN_LEFT)) ? -1 : 0;
	if (dir) {
		freq_khz += dir*steps[step];
		freq_khz  = (freq_khz < app_freq_min) ? app_freq_min : (freq_khz > app_freq_max - 2000) ?
			app_freq_max - 2000 : freq_khz;
		app_tune(freq_khz + offset_khz());
		reset();
	}
	if (p & (1 << BTN_UP))
		step = (step < 2) ? step + 1 : step;
	if (p & (1 << BTN_DOWN))
		step = (step > 0) ? step - 1 : step;
	if (p & (1 << BTN_A)) {
		width = (width + 1) % WIDTHS;
		app_tune(freq_khz + offset_khz());
		reset();
		app_toast("BANDWIDTH %s", widths[width].name);
	}
	if (p & (1 << BTN_START)) {
		tone = !tone;
		app_toast("TONE %s", tone ? "ON" : "OFF");
	}
	if (p & (1 << BTN_B)) {
		reset();
		app_toast("PEAK/HISTORY RESET");
	}
}

const struct tool tool_hunt = {
	.name    = "HUNT",
	.enter   = enter,
	.update  = update,
	.buttons = buttons,
	.help    = help,
};

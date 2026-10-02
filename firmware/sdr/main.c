// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// ChromatiX SDR (--with-app build, ESP-SDR on the ESP32): 2.4GHz spectrum analyzer/waterfall on the
// LCD. I/Q bursts captured by the ESP32 (CAP16 over the ESP32 UART), FFT on the CPU.
//
// Controls: Left/Right: tune (5MHz), Up/Down: reference level (5dB), A: span (16/40MHz),
// B: gain (AGC/manual), Start: peak hold, Select: auto reference level. Waterfall scaled from the
// noise floor.

#include <stdio.h>
#include <stdint.h>
#include <stdarg.h>
#include <string.h>

#include <irq.h>
#include <system.h>
#include <libbase/uart.h>
#include <generated/csr.h>
#include <generated/mem.h>
#include <generated/soc.h>

#include "layout.h"
#include "esp32sdr.h"
#include "lcd.h"
#include "dsp.h"

/* Log (host readable) -------------------------------------------------------------------------- */

#define LOG_OFFSET 0x800
#define LOG_SIZE   0x800

static char *log_buf = (char *)(MAIN_RAM_BASE + LAYOUT_HOST_OFFSET + LOG_OFFSET);

static void log_status(const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	vsnprintf(log_buf, LOG_SIZE, fmt, ap);
	va_end(ap);
	flush_cpu_dcache(); /* Write-back caches: make the log visible to the host. */
	flush_l2_cache();
}

/* Layout/Colors -------------------------------------------------------------------------------- */

#define HEADER_Y   0
#define SPEC_Y     14
#define SPEC_H     56
#define MARKER_Y   (SPEC_Y + SPEC_H)
#define MARKER_H   8
#define WATER_Y    (MARKER_Y + MARKER_H)
#define WATER_H    (LCD_HEIGHT - WATER_Y)
#define RANGE_DB4  (60*4) /* Spectrum: displayed range below the reference level (1/4 dB). */
#define WATER_DB4  (36*4) /* Waterfall: displayed range above the noise floor (1/4 dB). */

enum {
	COLOR_BLACK = 0,
	COLOR_WHITE,
	COLOR_GRID,
	COLOR_TRACE,
	COLOR_FILL,
	COLOR_PEAK,
	COLOR_WIFI,
	COLOR_BLE,
	COLOR_RED,
	COLOR_HEAT  = 64, /* 64-255: waterfall heat map. */
	HEAT_COLORS = 192,
};

static void palette_init(void)
{
	static const uint8_t colors[][3] = {
		{  0,   0,   0}, /* Black.     */
		{255, 255, 255}, /* White.     */
		{ 48,  48,  64}, /* Grid.      */
		{ 64, 255,  64}, /* Trace.     */
		{  0,  80,  24}, /* Fill.      */
		{255, 224,   0}, /* Peak hold. */
		{  0, 200, 255}, /* Wi-Fi.     */
		{255,  64, 255}, /* BLE.       */
		{255,  48,  48}, /* Red.       */
	};
	for (int i = 0; i < (int)(sizeof(colors)/sizeof(colors[0])); i++)
		lcd_palette(i, colors[i][0], colors[i][1], colors[i][2]);
	/* Heat map: black -> blue -> cyan -> yellow -> red -> white. */
	static const uint8_t steps[][3] = {
		{0, 0, 0}, {0, 0, 192}, {0, 192, 255}, {255, 255, 0}, {255, 32, 0}, {255, 255, 255},
	};
	for (int i = 0; i < HEAT_COLORS; i++) {
		int s = i*5*256/HEAT_COLORS;
		int k = s >> 8, f = s & 0xff;
		uint8_t rgb[3];
		for (int c = 0; c < 3; c++)
			rgb[c] = (steps[k][c]*(256 - f) + steps[k + 1][c]*f) >> 8;
		lcd_palette(COLOR_HEAT + i, rgb[0], rgb[1], rgb[2]);
	}
}

/* Settings ------------------------------------------------------------------------------------- */

static const struct {
	int index;
	int mhz;
} spans[] = {
	{ESP32SDR_RATE_16MSPS, 16},
	{ESP32SDR_RATE_40MSPS, 40},
	/* 80MS/s: no wider view (ESP32 RX filter: ~40MHz). */
};
#define SPANS (int)(sizeof(spans)/sizeof(spans[0]))

static const int gains[] = {-1, 20, 30, 40, 50, 60, 70}; /* -1: AGC (hardware gain). */
#define GAINS (int)(sizeof(gains)/sizeof(gains[0]))

static int freq_mhz  = 2437;
static int span      = 1;
static int gain      = 0;
static int ref_db4   = -1; /* < 0: auto. */
static int peak_hold = 0;

static void set_freq(void)
{
	char cmd[32], reply[32];
	snprintf(cmd, sizeof(cmd), "FREQ %d", freq_mhz);
	esp32sdr_cmd(cmd, reply, sizeof(reply), 200);
}

static void set_gain(void)
{
	char cmd[32];
	if (gains[gain] < 0)
		snprintf(cmd, sizeof(cmd), "GAIN HARDWARE");
	else
		snprintf(cmd, sizeof(cmd), "GAIN MANUAL %d", gains[gain]);
	esp32sdr_send(cmd);
}

/* Display -------------------------------------------------------------------------------------- */

static int col_db4[LCD_WIDTH];
static int peak_db4[LCD_WIDTH];
static int floor_db4 = -1; /* Noise floor (median of the center half columns, smoothed). */

static void update_floor(void)
{
	/* Center half: in the ESP32 RX filter passband for all spans (~40MHz). */
	static int sorted[LCD_WIDTH/2];
	memcpy(sorted, &col_db4[LCD_WIDTH/4], sizeof(sorted));
	for (int i = 1; i < LCD_WIDTH/2; i++)
		for (int j = i; j > 0 && sorted[j - 1] > sorted[j]; j--) {
			int t = sorted[j]; sorted[j] = sorted[j - 1]; sorted[j - 1] = t;
		}
	int median = sorted[LCD_WIDTH/4];
	floor_db4 = (floor_db4 < 0) ? median : floor_db4 + (median - floor_db4)/4;
}

static int freq_to_x(int mhz_x10)
{
	/* Column of a frequency (MHz*10), -1 outside of the span. */
	int lo = 10*freq_mhz - 5*spans[span].mhz;
	int x  = (mhz_x10 - lo)*LCD_WIDTH/(10*spans[span].mhz);
	return (mhz_x10 < lo || x >= LCD_WIDTH) ? -1 : x;
}

static int db4_to_y(int db4)
{
	int y = (ref_db4 - db4)*SPEC_H/RANGE_DB4;
	return (y < 0) ? 0 : (y > SPEC_H - 1) ? SPEC_H - 1 : y;
}

static void draw_header(int fps10, int peak_x)
{
	char line[48];
	lcd_rect(0, HEADER_Y, LCD_WIDTH, SPEC_Y, COLOR_BLACK);
	if (gains[gain] < 0)
		snprintf(line, sizeof(line), "%4d MHZ SPAN %d GAIN AGC", freq_mhz, spans[span].mhz);
	else
		snprintf(line, sizeof(line), "%4d MHZ SPAN %d GAIN %d", freq_mhz, spans[span].mhz, gains[gain]);
	lcd_text(1, HEADER_Y + 1, COLOR_WHITE, line);
	int peak_khz = 1000*freq_mhz - 500*spans[span].mhz + (2*peak_x + 1)*500*spans[span].mhz/LCD_WIDTH;
	snprintf(line, sizeof(line), "REF %d PK %d.%d %d %s%d.%dFPS", ref_db4/4, peak_khz/1000,
		(peak_khz % 1000)/100, col_db4[peak_x]/4, peak_hold ? "HOLD " : "", fps10/10, fps10 % 10);
	lcd_text(1, HEADER_Y + 8, COLOR_WHITE, line);
}

static void draw_spectrum(void)
{
	lcd_rect(0, SPEC_Y, LCD_WIDTH, SPEC_H, COLOR_BLACK);
	/* Grid: 10dB lines, 10MHz ticks. */
	for (int db = 1; db < 6; db++)
		for (int x = 0; x < LCD_WIDTH; x += 2)
			lcd_screen[SPEC_Y + db*SPEC_H/6][x] = COLOR_GRID;
	for (int f = (freq_mhz - spans[span].mhz/2 + 9)/10*10; f <= freq_mhz + spans[span].mhz/2; f += 10) {
		int x = freq_to_x(10*f);
		if (x >= 0)
			for (int y = SPEC_Y; y < SPEC_Y + SPEC_H; y += 2)
				lcd_screen[y][x] = COLOR_GRID;
	}
	/* Trace (filled), peak hold. */
	int prev = db4_to_y(col_db4[0]);
	for (int x = 0; x < LCD_WIDTH; x++) {
		int y = db4_to_y(col_db4[x]);
		for (int j = y + 1; j < SPEC_H; j++)
			lcd_screen[SPEC_Y + j][x] = COLOR_FILL;
		int y0 = (prev < y) ? prev : y, y1 = (prev < y) ? y : prev;
		for (int j = y0; j <= y1; j++)
			lcd_screen[SPEC_Y + j][x] = COLOR_TRACE;
		prev = y;
		if (peak_hold)
			lcd_screen[SPEC_Y + db4_to_y(peak_db4[x])][x] = COLOR_PEAK;
	}
}

static void draw_markers(void)
{
	char label[4];
	lcd_rect(0, MARKER_Y, LCD_WIDTH, MARKER_H, COLOR_BLACK);
	/* Wi-Fi channels 1-14: numbers, 20MHz band of the non-overlapping ones on the last row. */
	int w = 20*LCD_WIDTH/spans[span].mhz;
	for (int ch = 1; ch <= 14; ch++) {
		int f    = (ch == 14) ? 2484 : 2407 + 5*ch;
		int x    = freq_to_x(10*f);
		int main = (ch == 1 || ch == 6 || ch == 11 || ch == 14);
		if (x < 0)
			continue;
		if (main)
			lcd_rect(x - w/2 + 1, MARKER_Y + MARKER_H - 1, w - 2, 1, COLOR_WIFI);
		if (main || spans[span].mhz <= 40) {
			snprintf(label, sizeof(label), "%d", ch);
			lcd_text(x - 2*(int)strlen(label) + 1, MARKER_Y, COLOR_WIFI, label);
		}
	}
	/* BLE advertising channels 37/38/39. */
	static const int ble[] = {2402, 2426, 2480};
	for (int i = 0; i < 3; i++) {
		int x = freq_to_x(10*ble[i]);
		if (x >= 0)
			lcd_rect(x, MARKER_Y, 1, MARKER_H - 2, COLOR_BLE);
	}
}

static void draw_waterfall(void)
{
	memmove(lcd_screen[WATER_Y + 1], lcd_screen[WATER_Y], (WATER_H - 1)*LCD_WIDTH);
	for (int x = 0; x < LCD_WIDTH; x++) {
		int v = (col_db4[x] - (floor_db4 - 4*4))*HEAT_COLORS/WATER_DB4;
		v = (v < 0) ? 0 : (v > HEAT_COLORS - 1) ? HEAT_COLORS - 1 : v;
		lcd_screen[WATER_Y][x] = COLOR_HEAT + v;
	}
}

static void draw_message(const char *msg)
{
	lcd_rect(0, SPEC_Y + SPEC_H/2 - 4, LCD_WIDTH, 9, COLOR_BLACK);
	lcd_text((LCD_WIDTH - 4*(int)strlen(msg))/2, SPEC_Y + SPEC_H/2 - 3, COLOR_RED, msg);
}

/* Buttons -------------------------------------------------------------------------------------- */

enum {
	BTN_A = 0, BTN_B, BTN_DOWN, BTN_LEFT, BTN_RIGHT, BTN_UP, BTN_SEL, BTN_START, BTN_MENU,
};

static uint32_t buttons_last;
static int      buttons_held;

static uint32_t buttons_pressed(void)
{
	/* Pressed edges, auto repeat for the directions (held over 6 updates). */
	uint32_t buttons = demo_buttons_status_read();
	uint32_t pressed = buttons & ~buttons_last;
	uint32_t dirs    = (1 << BTN_LEFT) | (1 << BTN_RIGHT) | (1 << BTN_UP) | (1 << BTN_DOWN);
	if (buttons & dirs) {
		if (++buttons_held > 6)
			pressed |= buttons & dirs;
	} else
		buttons_held = 0;
	buttons_last = buttons;
	return pressed;
}

/* Main ----------------------------------------------------------------------------------------- */

#define SAMPLES 4096

static int8_t   iq[2*SAMPLES];
static uint64_t power[FFT_SIZE];

int main(void)
{
	char reply[64];

#ifdef CONFIG_CPU_HAS_INTERRUPT
	irq_setmask(0);
	irq_setie(1);
#endif
	uart_init();
	esp32sdr_init();
	palette_init();
	lcd_init();
	lcd_text(1, 1, COLOR_WHITE, "CHROMATIX SDR: WAITING FOR ESP-SDR...");
	lcd_present();

	esp32sdr_cmd("SYNC 1", reply, sizeof(reply), 200);
	set_freq();
	set_gain();

	uint32_t errors[6] = {0};
	uint32_t frames = 0, fps_frames = 0, fps_t0 = esp32sdr_ms();
	int      fps10 = 0;
	for (;;) {
		/* Controls. */
		uint32_t pressed = buttons_pressed();
		if (pressed & ((1 << BTN_LEFT) | (1 << BTN_RIGHT))) {
			freq_mhz += (pressed & (1 << BTN_RIGHT)) ? 5 : -5;
			freq_mhz  = (freq_mhz < 2300) ? 2300 : (freq_mhz > 2600) ? 2600 : freq_mhz;
			set_freq();
		}
		if (pressed & (1 << BTN_UP))
			ref_db4 += 5*4;
		if (pressed & (1 << BTN_DOWN))
			ref_db4 -= 5*4;
		if (pressed & (1 << BTN_A)) {
			span      = (span + 1) % SPANS;
			ref_db4   = -1;
			floor_db4 = -1;
		}
		if (pressed & (1 << BTN_B)) {
			gain      = (gain + 1) % GAINS;
			ref_db4   = -1;
			floor_db4 = -1;
			set_gain();
		}
		if (pressed & (1 << BTN_START))
			peak_hold = !peak_hold;
		if (pressed & (1 << BTN_SEL))
			ref_db4 = -1;
		if (pressed & ((1 << BTN_START) | (1 << BTN_A) | (1 << BTN_LEFT) | (1 << BTN_RIGHT)))
			memset(peak_db4, 0, sizeof(peak_db4));

		/* Capture. */
		uint32_t t_capture = esp32sdr_ms();
		int r = esp32sdr_capture(SAMPLES, spans[span].index, iq, NULL);
		if (r != 0) {
			errors[-r]++;
			draw_message("ESP-SDR: NO CAPTURE");
			lcd_present();
			log_status("frames %lu errors %lu/%lu/%lu/%lu/%lu\n", (unsigned long)frames,
				(unsigned long)errors[1], (unsigned long)errors[2], (unsigned long)errors[3],
				(unsigned long)errors[4], (unsigned long)errors[5]);
			esp32sdr_flush();
			continue;
		}

		/* Spectrum: FFT bins -> columns (max). */
		uint32_t t_dsp = esp32sdr_ms();
		dsp_power_spectrum(iq, SAMPLES, power);
		int peak_x = 0;
		for (int x = 0; x < LCD_WIDTH; x++) {
			uint64_t p = 0;
			for (int k = x*FFT_SIZE/LCD_WIDTH; k < (x + 1)*FFT_SIZE/LCD_WIDTH; k++)
				p = (power[k] > p) ? power[k] : p;
			col_db4[x] = dsp_db4(p);
			if (col_db4[x] > peak_db4[x])
				peak_db4[x] = col_db4[x];
			if (col_db4[x] > col_db4[peak_x])
				peak_x = x;
		}
		update_floor();
		if (ref_db4 < 0)
			ref_db4 = (col_db4[peak_x] + 5*4 + 19)/20*20; /* Auto: 5dB above the peak, 5dB steps. */

		/* Display. */
		uint32_t t_draw = esp32sdr_ms();
		draw_spectrum();
		draw_markers();
		draw_waterfall();
		draw_header(fps10, peak_x);
		uint32_t t_present = esp32sdr_ms();
		lcd_present();
		uint32_t t_end = esp32sdr_ms();

		/* Statistics. */
		frames++;
		fps_frames++;
		uint32_t t = esp32sdr_ms();
		if (t - fps_t0 >= 2000) {
			fps10      = fps_frames*10000/(t - fps_t0);
			fps_frames = 0;
			fps_t0     = t;
			log_status("frames %lu errors %lu fps %d.%d freq %d span %d gain %d ref %d peak %d@%d\n"
				"ms: capture %lu dsp %lu draw %lu present %lu\n",
				(unsigned long)frames, (unsigned long)(errors[1] + errors[2] + errors[3] + errors[4] + errors[5]), fps10/10, fps10 % 10, freq_mhz,
				spans[span].mhz, gains[gain], ref_db4/4, col_db4[peak_x]/4, peak_x,
				(unsigned long)(t_dsp - t_capture), (unsigned long)(t_draw - t_dsp),
				(unsigned long)(t_present - t_draw), (unsigned long)(t_end - t_present));
		}
	}
	return 0;
}

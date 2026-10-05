// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// ChromatiX SDR (--with-app build, ESP-SDR on the ESP32): spectrum analyzer/waterfall on the LCD.
// ESP-SDR (https://espargos.net/espsdr/) by ESPARGOS (Florian Euchner): ESP32 raw I/Q captures.
// I/Q bursts captured by the ESP32 (CAP16, over QSPI with the Chromatic ESP-SDR fork, else over the
// ESP32 UART), FFT on the CPU (dsp.c).
//
// Views (Select): spectrum + waterfall, waterfall, band scan (sweep of a frequency range in 64MHz
// steps with the 80MS/s wide captures).
// Controls: Left/Right: tune (held: accelerated) or move the cursor, Up/Down: tuning step, A: span
// (cursor: tune to the cursor), B: cursor on/off, Start: peak hold, Menu: menu (tool, band presets,
// gain, reference level, waterfall, RSSI tone, scan range, help, info).
//
// Tools (menu, app.h): cell scanner (tool_cell.c).
//
// USB relay (USB link gateware): the host talks the ESP-SDR protocol over the USB CDC port
// (commands forwarded to the ESP32, capture payloads sent by DMA from the QSPI buffer at the USB
// rate); the relayed captures are displayed. Local captures resume 2s after the last host command.

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
#include "app.h"
#include "esp32sdr.h"
#include "lcd.h"
#include "dsp.h"
#include "usblink.h"

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

#define HEADER_H   APP_HEADER_H
#define SPEC_Y     HEADER_H
#define SPEC_H     56
#define AXIS_H     8
#define RANGE_DB4  (60*4) /* Spectrum: displayed range below the reference level (1/4 dB). */

static void palette_init(void)
{
	static const uint8_t colors[][3] = {
		{  0,   0,   0}, /* Black.          */
		{255, 255, 255}, /* White.          */
		{ 48,  48,  64}, /* Grid.           */
		{ 64, 255,  64}, /* Trace.          */
		{  0,  80,  24}, /* Fill.           */
		{255, 224,   0}, /* Peak hold.      */
		{  0, 200, 255}, /* Wi-Fi.          */
		{255,  64, 255}, /* BLE.            */
		{255,  48,  48}, /* Red.            */
		{255, 160,   0}, /* Cursor.         */
		{140, 140, 160}, /* Dim text.       */
		{255, 120,  60}, /* Cellular bands. */
		{120, 120, 255}, /* ISM band.       */
		{ 16,  24,  64}, /* Menu.           */
		{ 40,  90, 200}, /* Menu selection. */
		{ 64, 255,  64}, /* Green.          */
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
	{ESP32SDR_RATE_80MSPS, 80}, /* With the RX filter opened (LPF 0): +-38MHz usable. */
};
#define SPANS (freqk ? 3 : 2) /* Local spans (80MHz: Chromatic ESP-SDR fork). */

static const int gains[] = {-1, 20, 30, 40, 50, 60, 70}; /* -1: AGC (hardware gain). */
#define GAINS (int)(sizeof(gains)/sizeof(gains[0]))

static const struct {
	int         khz;
	const char *name;
} steps[] = {
	{   10, "10K"},
	{  100, "100K"},
	{ 1000, "1M"},
	{ 5000, "5M"},
	{20000, "20M"},
};
#define STEPS (int)(sizeof(steps)/sizeof(steps[0]))

/* Band presets (menu): center frequency and span. */
static const struct {
	const char *name;
	int         khz;
	int         span;
} presets[] = {
	{"WIFI 2.4GHZ",   2442000, 2},
	{"WIFI CH 1",     2412000, 1},
	{"WIFI CH 6",     2437000, 1},
	{"WIFI CH 11",    2462000, 1},
	{"BLUETOOTH/BLE", 2441000, 2},
	{"LTE B3 DL",     1842500, 2},
	{"DECT",          1890000, 1},
	{"LTE B1 DL",     2140000, 2},
	{"LTE B40",       2350000, 2},
	{"LTE B7 UL",     2535000, 2},
	{"LTE B38",       2595000, 1},
	{"LTE B7 DL",     2655000, 2},
};
#define PRESETS (int)(sizeof(presets)/sizeof(presets[0]))

/* Band scan ranges (kHz, clipped to the tuning range). */
static const struct {
	const char *name;
	int         lo;
	int         hi;
} scans[] = {
	{"FULL",      1000000, 3000000},
	{"2.4G ISM",  2400000, 2500000},
	{"1.8G",      1792000, 2150000},
	{"B3/DECT",   1800000, 1910000},
	{"LTE B1",    2100000, 2180000},
	{"LTE B40",   2300000, 2400000},
	{"LTE B7",    2500000, 2690000},
	{"LOW",       1792000, 2400000},
	{"HIGH",      2400000, 2880000},
};
#define SCANS (int)(sizeof(scans)/sizeof(scans[0]))

/* Known bands (overlays). */
static const struct {
	int         lo, hi; /* kHz. */
	uint8_t     color;
	const char *name;
} bands[] = {
	{1805000, 1880000, COLOR_CELL, "B3 DL"},
	{1880000, 1900000, COLOR_BLE,  "DECT"},
	{2110000, 2170000, COLOR_CELL, "B1 DL"},
	{2300000, 2400000, COLOR_CELL, "B40"},
	{2400000, 2483500, COLOR_ISM,  "ISM"},
	{2500000, 2570000, COLOR_CELL, "B7 UL"},
	{2570000, 2620000, COLOR_CELL, "B38"},
	{2620000, 2690000, COLOR_CELL, "B7 DL"},
};
#define BANDS (int)(sizeof(bands)/sizeof(bands[0]))

enum {
	MODE_SPECTRUM = 0,
	MODE_WATERFALL,
	MODE_SCAN,
};

static int freq_khz  = 2437000;
static int freqk     = 0; /* ESP32 kHz tuning (FREQK, Chromatic ESP-SDR fork). */
static int span      = 1;
static int gain      = 0;
static int step      = 3;
static int mode      = MODE_SPECTRUM;
static int preset    = 0;
static int scan      = 0;
static int ref_db4   = -1; /* < 0: to set. */
static int ref_auto  = 1;  /* Reference level following the peak level (else manual). */
static int peak_hold = 0;
static int sound     = 0; /* RSSI tone. */
static int cursor    = 0; /* Cursor on. */
static int cursor_x  = 80;
static int wf_speed  = 1;  /* Waterfall: one line every wf_speed updates. */
static int wf_range  = 36; /* Waterfall: dB above the noise floor. */
static int qspi      = 0; /* Captures over QSPI (else UART). */
static int host      = 0; /* USB host relay active. */
static int gain_db   = -1; /* Displayed gain (-1: AGC). */
static int fps10;
static char esp_info[48];

int app_freq_min = 2386000; /* FREQK range (kHz). */
int app_freq_max = 2504000;

/* Tools (menu): 0: spectrum (views above). */
static const struct tool *const tools[] = {
	NULL,
	&tool_cell,
};
#define TOOLS (int)(sizeof(tools)/sizeof(tools[0]))

static int tool     = 0;
static int tool_sel = 0; /* Menu selection. */

/* Stock ESP-SDR: the ESP32 only tunes reliably on the Wi-Fi channel frequencies (2412-2472MHz/5MHz,
   2484MHz): its out of channel frequencies don't move the LO (measured on the console crystal
   harmonics). The Chromatic ESP-SDR fork tunes 1775-2890MHz in kHz steps (FREQK). */
static int next_channel(int mhz, int dir)
{
	static const int channels[] = {
		2412, 2417, 2422, 2427, 2432, 2437, 2442, 2447, 2452, 2457, 2462, 2467, 2472, 2484,
	};
	const int n = sizeof(channels)/sizeof(channels[0]);
	int i = 0;
	while (i < n - 1 && channels[i] < mhz)
		i++;
	if (channels[i] == mhz)
		i += dir;
	else if (dir < 0)
		i -= 1;
	i = (i < 0) ? 0 : (i > n - 1) ? n - 1 : i;
	return channels[i];
}

static void esp_cmd(const char *cmd)
{
	char reply[32];
	esp32sdr_cmd(cmd, reply, sizeof(reply), 200);
}

static void set_freq_khz(int khz)
{
	char cmd[32];
	if (freqk)
		snprintf(cmd, sizeof(cmd), "FREQK %d", khz);
	else
		snprintf(cmd, sizeof(cmd), "FREQ %d", khz/1000);
	esp_cmd(cmd);
}

static void set_filter(int wide)
{
	/* ESP32 RX filter: opened for the 80MHz span/scan (default ~+-17MHz passband). */
	esp_cmd(wide ? "LPF 0" : "LPF AUTO");
}

static void set_gain(void)
{
	char cmd[32];
	gain_db = gains[gain];
	if (gain_db < 0)
		snprintf(cmd, sizeof(cmd), "GAIN HARDWARE");
	else
		snprintf(cmd, sizeof(cmd), "GAIN MANUAL %d", gain_db);
	esp_cmd(cmd);
}

/* App Services (tools) ------------------------------------------------------------------------- */

int8_t app_iq[2*APP_SAMPLES + 1024] __attribute__((aligned(4))); /* + QSPI padding. */

void app_tune(int khz)
{
	khz = (khz < app_freq_min) ? app_freq_min : (khz > app_freq_max) ? app_freq_max : khz;
	set_freq_khz(khz);
}

void app_filter(int wide)
{
	set_filter(wide);
}

void app_gain(int db)
{
	char cmd[32];
	if (db < 0)
		snprintf(cmd, sizeof(cmd), "GAIN HARDWARE");
	else
		snprintf(cmd, sizeof(cmd), "GAIN MANUAL %d", db);
	esp_cmd(cmd);
}

int app_capture(int samples, int rate, int8_t *iq)
{
	int r = esp32sdr_capture(samples, rate, iq, NULL);
	if (r != 0)
		esp32sdr_flush();
	return r;
}

void app_heat_line(int y, const int *db4, int floor_db4, int range_db)
{
	for (int x = 0; x < LCD_WIDTH; x++) {
		int v = (db4[x] - floor_db4)*HEAT_COLORS/(range_db*4);
		v = (v < 0) ? 0 : (v > HEAT_COLORS - 1) ? HEAT_COLORS - 1 : v;
		lcd_screen[y][x] = COLOR_HEAT + v;
	}
}

/* Display State -------------------------------------------------------------------------------- */

static int col_db4[LCD_WIDTH];
static int peak_db4[LCD_WIDTH];
static int floor_db4 = -1; /* Noise floor (median of the center half columns, smoothed). */
static int view_lo, view_hi; /* Displayed range (kHz). */

static void reset_levels(void)
{
	floor_db4 = -1;
	if (ref_auto)
		ref_db4 = -1;
	memset(peak_db4, 0, sizeof(peak_db4));
}

static void tune(int khz)
{
	if (freqk)
		khz = (khz < app_freq_min) ? app_freq_min : (khz > app_freq_max) ? app_freq_max : khz;
	freq_khz = khz;
	set_freq_khz(freq_khz);
	reset_levels();
}

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

static void update_ref(int peak_x)
{
	/* Auto reference: 5dB above the peak (5dB steps), followed when off by 10dB or more. */
	int ref_target = (col_db4[peak_x] + 5*4 + 19)/20*20;
	if (ref_db4 < 0 || (ref_auto && (ref_target - ref_db4 >= 10*4 || ref_db4 - ref_target >= 10*4)))
		ref_db4 = ref_target;
}

static int x_to_khz(int x)
{
	return view_lo + (int)((int64_t)(2*x + 1)*(view_hi - view_lo)/(2*LCD_WIDTH));
}

static int khz_to_x(int khz)
{
	/* Column of a frequency, -1 outside of the view. */
	if (khz < view_lo || khz >= view_hi)
		return -1;
	return (int)((int64_t)(khz - view_lo)*LCD_WIDTH/(view_hi - view_lo));
}

static int db4_to_y(int db4)
{
	int y = (ref_db4 - db4)*SPEC_H/RANGE_DB4;
	return (y < 0) ? 0 : (y > SPEC_H - 1) ? SPEC_H - 1 : y;
}

void app_format_khz(char *s, int len, int khz, int decimals)
{
	/* MHz with 0-3 decimals. */
	static const int div[] = {1000, 100, 10, 1};
	if (decimals == 0)
		snprintf(s, len, "%d", khz/1000);
	else
		snprintf(s, len, "%d.%0*d", khz/1000, decimals, (khz % 1000)/div[decimals]);
}

/* Band scan state (see below). */
static int scan_step;
static int scan_steps(void);

/* Toast ---------------------------------------------------------------------------------------- */

static char     toast_text[40];
static uint32_t toast_until;

void app_toast(const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	vsnprintf(toast_text, sizeof(toast_text), fmt, ap);
	va_end(ap);
	toast_until = esp32sdr_ms() + 1500;
}

/* Drawing -------------------------------------------------------------------------------------- */

static void draw_header(int peak_x)
{
	char line[48], f[16];
	lcd_rect(0, 0, LCD_WIDTH, HEADER_H, COLOR_BLACK);
	if (mode == MODE_SCAN) {
		lcd_text_scaled(1, 1, COLOR_WHITE, "SCAN", 2);
		snprintf(line, sizeof(line), "%s %d-%d", scans[scan].name, view_lo/1000, view_hi/1000);
		lcd_text(36, 1, COLOR_WHITE, line);
		/* Sweep progress. */
		lcd_rect(0, HEADER_H - 1, (scan_step + 1)*LCD_WIDTH/scan_steps(), 1, COLOR_CURSOR);
	} else {
		app_format_khz(f, sizeof(f), freq_khz, 3);
		lcd_text_scaled(1, 1, COLOR_WHITE, f, 2);
		lcd_text(67, 7, COLOR_DIM, "MHZ");
		snprintf(line, sizeof(line), "SPAN%d STEP%s", spans[span].mhz, steps[step].name);
		lcd_text(81, 1, COLOR_WHITE, line);
	}
	/* Cursor or peak readout. */
	int x = cursor ? cursor_x : peak_x;
	app_format_khz(f, sizeof(f), x_to_khz(x), (view_hi - view_lo) > 100000 ? 1 : 2);
	snprintf(line, sizeof(line), "%s %s %d", cursor ? "CUR" : "PK", f, col_db4[x]/4);
	lcd_text((mode == MODE_SCAN) ? 36 : 81, 8, cursor ? COLOR_CURSOR : COLOR_PEAK, line);
}

static void draw_axis(int y)
{
	/* Frequency labels: spacing giving >= 24 pixels per label. */
	static const int spacings[] = {1000, 2000, 5000, 10000, 20000, 50000, 100000, 200000};
	char label[8];
	int  width = view_hi - view_lo, spacing = spacings[7];
	for (int i = 0; i < 8; i++)
		if ((int64_t)spacings[i]*LCD_WIDTH/width >= 24) {
			spacing = spacings[i];
			break;
		}
	lcd_rect(0, y, LCD_WIDTH, AXIS_H, COLOR_BLACK);
	for (int f = (view_lo + spacing - 1)/spacing*spacing; f < view_hi; f += spacing) {
		int x = khz_to_x(f);
		snprintf(label, sizeof(label), "%d", f/1000);
		int lx = x - 2*(int)strlen(label) + 1;
		if (lx >= 0 && lx + 4*(int)strlen(label) <= LCD_WIDTH)
			lcd_text(lx, y + 1, COLOR_DIM, label);
		lcd_rect(x, y, 1, 1, COLOR_DIM);
	}
}

static void draw_bands(void)
{
	/* Known bands (top of the spectrum), Wi-Fi channels/BLE advertising channels. */
	for (int i = 0; i < BANDS; i++) {
		int lo = (bands[i].lo > view_lo) ? bands[i].lo : view_lo;
		int hi = (bands[i].hi < view_hi) ? bands[i].hi : view_hi - 1;
		if (lo >= hi)
			continue;
		int x0 = khz_to_x(lo), x1 = khz_to_x(hi);
		lcd_rect(x0, SPEC_Y, x1 - x0 + 1, 1, bands[i].color);
		if (x1 - x0 > 4*(int)strlen(bands[i].name) + 2)
			lcd_text(x0 + 1, SPEC_Y + 2, bands[i].color, bands[i].name);
	}
	if (view_hi - view_lo <= 100000) {
		char label[4];
		for (int ch = 1; ch <= 14; ch++) {
			int x = khz_to_x(1000*((ch == 14) ? 2484 : 2407 + 5*ch));
			if (x < 0 || (view_hi - view_lo > 40000 && ch != 1 && ch != 6 && ch != 11 && ch != 14))
				continue;
			snprintf(label, sizeof(label), "%d", ch);
			lcd_text(x - 2*(int)strlen(label) + 1, SPEC_Y + 9, COLOR_WIFI, label);
		}
		static const int ble[] = {2402000, 2426000, 2480000};
		for (int i = 0; i < 3; i++) {
			int x = khz_to_x(ble[i]);
			if (x >= 0)
				lcd_rect(x, SPEC_Y + 1, 1, 6, COLOR_BLE);
		}
	}
}

static void draw_spectrum(void)
{
	lcd_rect(0, SPEC_Y, LCD_WIDTH, SPEC_H, COLOR_BLACK);
	/* Grid: 10dB lines. */
	for (int db = 1; db < 6; db++)
		for (int x = 0; x < LCD_WIDTH; x += 2)
			lcd_screen[SPEC_Y + db*SPEC_H/6][x] = COLOR_GRID;
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
	draw_bands();
	if (cursor)
		for (int y = SPEC_Y; y < SPEC_Y + SPEC_H; y += 2)
			lcd_screen[y][cursor_x] = COLOR_CURSOR;
	/* Status flags (top right). */
	char flags[32];
	snprintf(flags, sizeof(flags), "%s%s%s%d.%d", host ? "USB " : "", peak_hold ? "HOLD " : "",
		sound ? "TONE " : "", fps10/10, fps10 % 10);
	lcd_text(LCD_WIDTH - 4*(int)strlen(flags), SPEC_Y + SPEC_H - 7, COLOR_DIM, flags);
}

static void draw_waterfall(int y0, int h, int new_line)
{
	/* Scrolled image (overlays/cursor never drawn into it), new line on top. */
	if (!new_line)
		return;
	memmove(lcd_screen[y0 + 1], lcd_screen[y0], (h - 1)*LCD_WIDTH);
	for (int x = 0; x < LCD_WIDTH; x++) {
		int v = (col_db4[x] - (floor_db4 - 4*4))*HEAT_COLORS/(wf_range*4);
		v = (v < 0) ? 0 : (v > HEAT_COLORS - 1) ? HEAT_COLORS - 1 : v;
		lcd_screen[y0][x] = COLOR_HEAT + v;
	}
}

static void draw_message(const char *msg, uint8_t color)
{
	int w = 4*(int)strlen(msg) + 6;
	int x = (LCD_WIDTH - w)/2, y = SPEC_Y + SPEC_H + AXIS_H + 6;
	lcd_rect(x, y, w, 10, COLOR_MENU);
	lcd_rect(x, y, w, 1, color);
	lcd_text(x + 3, y + 3, color, msg);
}

/* Menu/Pages ----------------------------------------------------------------------------------- */

enum {
	MENU_TOOL = 0,
	MENU_BAND,
	MENU_GAIN,
	MENU_REF,
	MENU_WF_SPEED,
	MENU_WF_RANGE,
	MENU_TONE,
	MENU_SCAN,
	MENU_HELP,
	MENU_INFO,
	MENU_ITEMS,
};

static int menu_open = 0;
static int menu_item = 0;
static int page      = 0; /* 0: none, 1: help, 2: info. */

static void menu_value(int item, char *s, int len)
{
	switch (item) {
	case MENU_TOOL:     snprintf(s, len, "%s", tool_sel ? tools[tool_sel]->name : "SPECTRUM"); break;
	case MENU_BAND:     snprintf(s, len, "%s", presets[preset].name); break;
	case MENU_GAIN:
		if (gains[gain] < 0)
			snprintf(s, len, "AGC");
		else
			snprintf(s, len, "%d", gains[gain]);
		break;
	case MENU_REF:
		if (ref_auto)
			snprintf(s, len, "AUTO");
		else
			snprintf(s, len, "%d DB", ref_db4/4);
		break;
	case MENU_WF_SPEED: snprintf(s, len, "1/%d", wf_speed); break;
	case MENU_WF_RANGE: snprintf(s, len, "%d DB", wf_range); break;
	case MENU_TONE:     snprintf(s, len, "%s", sound ? "ON" : "OFF"); break;
	case MENU_SCAN:     snprintf(s, len, "%s", scans[scan].name); break;
	default:            s[0] = 0; break;
	}
}

static void draw_menu(void)
{
	static const char *names[MENU_ITEMS] = {
		"TOOL", "BAND", "GAIN", "REF LEVEL", "WATERFALL", "WF RANGE", "RSSI TONE", "SCAN RANGE", "HELP",
		"INFO",
	};
	int x = 12, y = 14, w = LCD_WIDTH - 24, h = 14 + 9*MENU_ITEMS;
	lcd_rect(x, y, w, h, COLOR_MENU);
	lcd_rect(x, y, w, 1, COLOR_SELECT);
	lcd_text(x + 4, y + 3, COLOR_WHITE, "MENU");
	lcd_text(x + w - 4*12 - 2, y + 3, COLOR_DIM, "A/<>:SET B:X");
	for (int i = 0; i < MENU_ITEMS; i++) {
		char value[24];
		int  iy = y + 12 + 9*i;
		if (i == menu_item)
			lcd_rect(x + 2, iy - 1, w - 4, 8, COLOR_SELECT);
		lcd_text(x + 4, iy, COLOR_WHITE, names[i]);
		menu_value(i, value, sizeof(value));
		lcd_text(x + w - 4 - 4*(int)strlen(value), iy, COLOR_PEAK, value);
	}
}

static void draw_page(void)
{
	static const char *help[] = {
		"LEFT/RIGHT  TUNE (HOLD: FAST)",
		"UP/DOWN     TUNING STEP",
		"A           SPAN 16/40/80MHZ",
		"B           CURSOR (A: TUNE TO)",
		"START       PEAK HOLD",
		"SELECT      VIEW: SPECTRUM,",
		"            WATERFALL, SCAN",
		"MENU        TOOLS, BANDS, GAIN,",
		"            REF, WATERFALL...",
		"SCAN: LEFT/RIGHT CURSOR,",
		"      A: OPEN AT CURSOR",
	};
	char info[9][40];
	const char **lines = help;
	int n = sizeof(help)/sizeof(help[0]);
	if (page == 1 && tool) {
		lines = (const char **)tools[tool]->help;
		for (n = 0; lines[n]; n++);
	}
	if (page == 2) {
		snprintf(info[0], 40, "ESP32: %s", esp_info);
		snprintf(info[1], 40, "CAPTURES: %s", qspi ? "QSPI (FAST)" : "UART");
		snprintf(info[2], 40, "TUNING: %d-%d MHZ", app_freq_min/1000, app_freq_max/1000);
		snprintf(info[3], 40, "%s", freqk ? "(CHROMATIX ESP-SDR FORK)" : "(STOCK ESP-SDR)");
		snprintf(info[4], 40, "USB HOST: %s", host ? "ACTIVE" : "IDLE");
		snprintf(info[5], 40, "UPDATE RATE: %d.%d/S", fps10/10, fps10 % 10);
		snprintf(info[6], 40, "FFT: 512 PTS, CPU (VEXRISCV)");
		snprintf(info[7], 40, "ESP-SDR: ESPARGOS.NET/ESPSDR");
		snprintf(info[8], 40, "<2.15GHZ: 5/6 LO (H0M3US3R ESPDR)");
		static const char *ptr[9];
		for (int i = 0; i < 9; i++)
			ptr[i] = info[i];
		lines = ptr;
		n = 9;
	}
	int x = 4, y = 16, w = LCD_WIDTH - 8, h = 14 + 8*n;
	lcd_rect(x, y, w, h, COLOR_MENU);
	lcd_rect(x, y, w, 1, COLOR_SELECT);
	lcd_text(x + 3, y + 3, COLOR_WHITE, (page == 2) ? "INFO" : tool ? tools[tool]->name : "HELP");
	for (int i = 0; i < n; i++)
		lcd_text(x + 3, y + 12 + 8*i, COLOR_WHITE, lines[i]);
}

static uint8_t overlay_saved[LCD_HEIGHT][LCD_WIDTH] __attribute__((aligned(4)));

static int draw_overlays(void)
{
	/* Overlays (menu, pages, toast) on top of the screen, the screen saved first (restored after
	   the LCD update: the waterfall scrolls its image). Returns 1 if saved. */
	int toast_on = (int32_t)(toast_until - esp32sdr_ms()) > 0;
	if (!menu_open && !page && !toast_on)
		return 0;
	memcpy(overlay_saved, lcd_screen, sizeof(overlay_saved));
	if (menu_open)
		draw_menu();
	if (page)
		draw_page();
	if (!menu_open && !page && toast_on)
		draw_message(toast_text, COLOR_WHITE);
	return 1;
}

static void restore_overlays(int saved)
{
	if (saved)
		memcpy(lcd_screen, overlay_saved, sizeof(overlay_saved));
}

void app_present(void)
{
	int saved = draw_overlays();
	lcd_present();
	restore_overlays(saved);
}

void app_header(const char *title, const char *status)
{
	lcd_rect(0, 0, LCD_WIDTH, HEADER_H, COLOR_BLACK);
	lcd_text_scaled(1, 1, COLOR_WHITE, title, 2);
	lcd_text(LCD_WIDTH - 4*(int)strlen(status), 1, COLOR_DIM, status);
	lcd_rect(0, HEADER_H - 1, LCD_WIDTH, 1, COLOR_GRID);
}

/* RSSI Tone ------------------------------------------------------------------------------------ */

#define PCM_RATE  11025
#define PCM_DEPTH 512

static volatile uint32_t tone_freq;
static uint32_t          tone_phase;

static void tone_fill(void)
{
	/* Square wave (or silence), FIFO filled (refilled from the PCM IRQ when half empty). */
	while (pcm_level_read() < PCM_DEPTH - 8) {
		int16_t sample = 0;
		if (tone_freq) {
			tone_phase += tone_freq;
			sample = ((tone_phase/(PCM_RATE/2)) & 1) ? 3000 : -3000;
		}
		pcm_data_write(((uint32_t)(uint16_t)sample << 16) | (uint16_t)sample);
	}
}

static void tone_isr(void)
{
	tone_fill();
}

static void tone_init(void)
{
	irq_attach(PCM_INTERRUPT, tone_isr);
	pcm_ev_enable_write(1);
	irq_setmask(irq_getmask() | (1 << PCM_INTERRUPT));
}

static void tone_update(void)
{
	/* Pitch: 200Hz + 40Hz/dB of the peak above the noise floor around the cursor (or the center
	   quarter of the span). */
	if (!sound || mode == MODE_SCAN) {
		tone_freq = 0;
		return;
	}
	int peak = 0;
	int x0 = cursor ? cursor_x - 4 : 3*LCD_WIDTH/8, x1 = cursor ? cursor_x + 4 : 5*LCD_WIDTH/8;
	for (int x = (x0 < 0) ? 0 : x0; x < x1 && x < LCD_WIDTH; x++)
		peak = (col_db4[x] > peak) ? col_db4[x] : peak;
	int db = (peak - floor_db4)/4;
	db = (db < 0) ? 0 : (db > 50) ? 50 : db;
	tone_freq = 200 + 40*db;
}

/* Spectrum ------------------------------------------------------------------------------------- */

#define SAMPLES      4096
#define SCAN_SAMPLES 2048
#define SCAN_STEP    64000 /* kHz: center +-32MHz of the 80MS/s wide captures. */

static int8_t   iq[2*SAMPLES];
static uint64_t power[FFT_SIZE];
static uint32_t updates;

static int find_peak(void)
{
	int peak_x = 0;
	for (int x = 0; x < LCD_WIDTH; x++)
		if (col_db4[x] > col_db4[peak_x])
			peak_x = x;
	return peak_x;
}

static void present(int peak_x, int new_line)
{
	/* Spectrum/axis/waterfall layout of the current view, overlays, LCD. */
	draw_header(peak_x);
	int axis_y = (mode == MODE_WATERFALL) ? HEADER_H : SPEC_Y + SPEC_H;
	if (mode != MODE_WATERFALL)
		draw_spectrum();
	draw_axis(axis_y);
	if (cursor) /* Cursor mark in the axis row (the waterfall image scrolls). */
		lcd_rect(cursor_x - 1, axis_y + AXIS_H - 2, 3, 2, COLOR_CURSOR);
	draw_waterfall(axis_y + AXIS_H, LCD_HEIGHT - axis_y - AXIS_H, new_line);
	int saved = draw_overlays();
	lcd_present();
	restore_overlays(saved);
}

static int show_spectrum(const int8_t *data, int samples)
{
	/* Spectrum: FFT bins -> columns (max). */
	view_lo = freq_khz - 500*spans[span].mhz;
	view_hi = freq_khz + 500*spans[span].mhz;
	dsp_power_spectrum(data, samples, power);
	for (int x = 0; x < LCD_WIDTH; x++) {
		uint64_t p = 0;
		for (int k = x*FFT_SIZE/LCD_WIDTH; k < (x + 1)*FFT_SIZE/LCD_WIDTH; k++)
			p = (power[k] > p) ? power[k] : p;
		col_db4[x] = dsp_db4(p);
		if (col_db4[x] > peak_db4[x])
			peak_db4[x] = col_db4[x];
	}
	int peak_x = find_peak();
	update_floor();
	update_ref(peak_x);
	tone_update();
	updates++;
	present(peak_x, updates % wf_speed == 0);
	return peak_x;
}

/* Band Scan ------------------------------------------------------------------------------------ */

static int scan_new[LCD_WIDTH]; /* Columns of the current sweep. */

static int scan_lo(void)
{
	return (scans[scan].lo > app_freq_min) ? scans[scan].lo : app_freq_min;
}

static int scan_hi(void)
{
	return (scans[scan].hi < app_freq_max) ? scans[scan].hi : app_freq_max;
}

static int scan_steps(void)
{
	return (scan_hi() - scan_lo() + SCAN_STEP - 1)/SCAN_STEP;
}

static void scan_start(void)
{
	scan_step = 0;
	view_lo   = scan_lo();
	view_hi   = scan_hi();
	for (int x = 0; x < LCD_WIDTH; x++)
		scan_new[x] = 0;
	reset_levels();
}

static int scan_update(void)
{
	/* One sweep step: tune, 80MS/s capture, bins within +-32MHz -> columns (max). */
	int center = view_lo + SCAN_STEP/2 + scan_step*SCAN_STEP;
	center = (center > app_freq_max) ? app_freq_max : center; /* Last step: within the tuning range. */
	set_freq_khz(center);
	if (esp32sdr_capture(SCAN_SAMPLES, ESP32SDR_RATE_80MSPS, iq, NULL) != 0) {
		esp32sdr_flush();
		return -1;
	}
	dsp_power_spectrum(iq, SCAN_SAMPLES, power);
	for (int k = 0; k < FFT_SIZE; k++) {
		int f = center + (k - FFT_SIZE/2)*80000/FFT_SIZE;
		if (k < FFT_SIZE/2 - 205 || k > FFT_SIZE/2 + 205) /* +-32MHz. */
			continue;
		int x = khz_to_x(f);
		if (x < 0)
			continue;
		int db4 = dsp_db4(power[k]);
		if (db4 > scan_new[x])
			scan_new[x] = db4;
	}
	/* Columns of this step shown as they come, the waterfall line once per sweep. */
	for (int x = 0; x < LCD_WIDTH; x++) {
		int f = x_to_khz(x);
		if (f >= center - SCAN_STEP/2 && f < center + SCAN_STEP/2) {
			col_db4[x] = scan_new[x];
			scan_new[x] = 0;
			if (col_db4[x] > peak_db4[x])
				peak_db4[x] = col_db4[x];
		}
	}
	int peak_x = find_peak();
	int done   = 0;
	if (ref_db4 < 0)
		update_ref(peak_x);
	if (scan_step + 1 >= scan_steps()) {
		update_floor();
		update_ref(peak_x);
		updates++;
		done = 1;
	}
	/* Waterfall line once per sweep. */
	present(peak_x, done && updates % wf_speed == 0);
	scan_step = done ? 0 : scan_step + 1;
	return 0;
}

/* Buttons -------------------------------------------------------------------------------------- */

#define REPEAT_DELAY_MS  400
#define REPEAT_PERIOD_MS 80

static uint32_t buttons_last;
static uint32_t buttons_repeat; /* Held directions repeated (after 400ms, every 80ms). */
static uint32_t buttons_t0;     /* Directions pressed time. */
static uint32_t buttons_t1;     /* Last repeat time. */
static uint32_t buttons_held;   /* Directions held time (ms). */

static uint32_t buttons_pressed(void)
{
	/* Pressed edges, Left/Right auto repeat (buttons_repeat: all the directions). */
	uint32_t buttons = demo_buttons_status_read();
	uint32_t pressed = buttons & ~buttons_last;
	uint32_t dirs    = (1 << BTN_LEFT) | (1 << BTN_RIGHT) | (1 << BTN_UP) | (1 << BTN_DOWN);
	uint32_t t       = esp32sdr_ms();
	buttons_repeat = 0;
	if (buttons & dirs) {
		if (pressed & dirs)
			buttons_t0 = t;
		buttons_held = t - buttons_t0;
		if (buttons_held > REPEAT_DELAY_MS && t - buttons_t1 >= REPEAT_PERIOD_MS) {
			buttons_repeat = buttons & dirs;
			buttons_t1     = t;
		}
	} else
		buttons_held = 0;
	buttons_last = buttons;
	return pressed | (buttons_repeat & ((1 << BTN_LEFT) | (1 << BTN_RIGHT)));
}

static void set_mode(int m)
{
	int wide_before = (mode == MODE_SCAN) || (spans[span].mhz == 80);
	mode = m;
	int wide = (mode == MODE_SCAN) || (spans[span].mhz == 80);
	if (wide != wide_before)
		set_filter(wide);
	lcd_rect(0, 0, LCD_WIDTH, LCD_HEIGHT, COLOR_BLACK);
	if (mode == MODE_SCAN) {
		scan_start();
		app_toast("SCAN %s: A OPENS CURSOR", scans[scan].name);
	} else {
		tune(freq_khz);
		app_toast((mode == MODE_SPECTRUM) ? "SPECTRUM + WATERFALL" : "WATERFALL");
	}
}

static void set_tool(int t)
{
	tool      = t;
	tool_sel  = t;
	menu_open = 0;
	lcd_rect(0, 0, LCD_WIDTH, LCD_HEIGHT, COLOR_BLACK);
	if (tool)
		tools[tool]->enter();
	else {
		/* Spectrum: its radio settings back. */
		set_filter((mode == MODE_SCAN) || (spans[span].mhz == 80));
		set_gain();
		if (mode == MODE_SCAN)
			scan_start();
		else
			tune(freq_khz);
	}
	app_toast("%s: MENU > HELP", tool ? tools[tool]->name : "SPECTRUM");
}

static void menu_change(int dir)
{
	switch (menu_item) {
	case MENU_TOOL:
		tool_sel = (tool_sel + TOOLS + dir) % TOOLS;
		break;
	case MENU_BAND:
		preset = (preset + PRESETS + dir) % PRESETS;
		break;
	case MENU_GAIN:
		gain = (gain + GAINS + dir) % GAINS;
		set_gain();
		reset_levels();
		break;
	case MENU_REF:
		if (ref_auto) {
			ref_auto = 0;
			if (ref_db4 < 0)
				ref_db4 = 0;
		}
		ref_db4 += dir*5*4;
		break;
	case MENU_WF_SPEED:
		wf_speed = (dir > 0) ? ((wf_speed < 8) ? 2*wf_speed : 8) : ((wf_speed > 1) ? wf_speed/2 : 1);
		break;
	case MENU_WF_RANGE:
		wf_range += dir*6;
		wf_range  = (wf_range < 12) ? 12 : (wf_range > 60) ? 60 : wf_range;
		break;
	case MENU_TONE:
		sound = !sound;
		break;
	case MENU_SCAN:
		scan = (scan + SCANS + dir) % SCANS;
		if (mode == MODE_SCAN)
			scan_start();
		break;
	}
}

static void menu_select(void)
{
	switch (menu_item) {
	case MENU_TOOL:
		set_tool(tool_sel);
		break;
	case MENU_BAND:
		/* Apply the preset: spectrum view. */
		if (tool)
			set_tool(0);
		span = presets[preset].span;
		menu_open = 0;
		set_mode(MODE_SPECTRUM);
		set_filter(spans[span].mhz == 80);
		tune(presets[preset].khz);
		app_toast("%s", presets[preset].name);
		break;
	case MENU_REF:
		ref_auto = 1;
		ref_db4  = -1;
		break;
	case MENU_HELP:
		page = 1;
		menu_open = 0;
		break;
	case MENU_INFO:
		page = 2;
		menu_open = 0;
		break;
	default:
		menu_change(1);
		break;
	}
}

static void handle_buttons(void)
{
	uint32_t p = buttons_pressed();
	if (menu_open || tool)
		p |= buttons_repeat & ((1 << BTN_UP) | (1 << BTN_DOWN));
	if (!p)
		return;
	/* Pages: any button closes. */
	if (page) {
		page = 0;
		return;
	}
	/* Menu (Up/Down repeated too). */
	if (menu_open) {
		if (p & ((1 << BTN_MENU) | (1 << BTN_B)))
			menu_open = 0;
		if (p & (1 << BTN_UP))
			menu_item = (menu_item + MENU_ITEMS - 1) % MENU_ITEMS;
		if (p & (1 << BTN_DOWN))
			menu_item = (menu_item + 1) % MENU_ITEMS;
		if (p & (1 << BTN_LEFT))
			menu_change(-1);
		if (p & (1 << BTN_RIGHT))
			menu_change(1);
		if (p & (1 << BTN_A))
			menu_select();
		return;
	}
	if (p & (1 << BTN_MENU)) {
		menu_open = 1;
		tool_sel  = tool;
		return;
	}
	if (tool) {
		tools[tool]->buttons(p);
		return;
	}
	if (p & (1 << BTN_SEL)) {
		int modes = freqk ? 3 : 2; /* Scan: Chromatic ESP-SDR fork (kHz tuning, wide captures). */
		set_mode((mode + 1) % modes);
		return;
	}
	if (p & (1 << BTN_START)) {
		peak_hold = !peak_hold;
		memset(peak_db4, 0, sizeof(peak_db4));
		app_toast("PEAK HOLD %s", peak_hold ? "ON" : "OFF");
	}
	if (p & (1 << BTN_B)) {
		cursor = !cursor;
		app_toast(cursor ? "CURSOR: <> MOVE, A: TUNE" : "CURSOR OFF");
	}
	int dir = (p & (1 << BTN_RIGHT)) ? 1 : (p & (1 << BTN_LEFT)) ? -1 : 0;
	if (mode == MODE_SCAN || cursor) {
		/* Cursor: 1 column (held: 4). */
		if (dir) {
			cursor   = 1;
			cursor_x += dir*((buttons_held > 2000) ? 4 : 1);
			cursor_x  = (cursor_x < 0) ? 0 : (cursor_x > LCD_WIDTH - 1) ? LCD_WIDTH - 1 : cursor_x;
		}
		if (p & (1 << BTN_A)) {
			int khz = x_to_khz(cursor_x);
			if (mode == MODE_SCAN) {
				span = freqk ? 1 : span;
				set_mode(MODE_SPECTRUM);
			}
			tune(freqk ? khz/10*10 : 1000*next_channel(khz/1000, 0));
			cursor_x = LCD_WIDTH/2;
			char f[16];
			app_format_khz(f, sizeof(f), freq_khz, 3);
			app_toast("TUNED %s MHZ", f);
		}
	} else {
		if (dir) {
			/* Tuning step (held: x5 after ~1s). */
			if (freqk) {
				int s = steps[step].khz*((buttons_held > 2000) ? 5 : 1);
				tune(freq_khz + dir*s);
			} else
				tune(1000*next_channel(freq_khz/1000, dir));
		}
		if (p & (1 << BTN_A)) {
			int wide_before = (spans[span].mhz == 80);
			span = (span + 1) % SPANS;
			if ((spans[span].mhz == 80) != wide_before)
				set_filter(spans[span].mhz == 80);
			reset_levels();
			app_toast("SPAN %d MHZ%s", spans[span].mhz, (spans[span].mhz == 80) ? " (WIDE)" : "");
		}
	}
	if (mode != MODE_SCAN && (p & ((1 << BTN_UP) | (1 << BTN_DOWN)))) {
		step += (p & (1 << BTN_UP)) ? 1 : -1;
		step  = (step < 0) ? 0 : (step > STEPS - 1) ? STEPS - 1 : step;
		app_toast("STEP %sHZ", steps[step].name);
	}
	if (mode == MODE_SCAN && (p & ((1 << BTN_UP) | (1 << BTN_DOWN)))) {
		scan = (scan + SCANS + ((p & (1 << BTN_UP)) ? 1 : -1)) % SCANS;
		scan_start();
		app_toast("SCAN %s %d-%d MHZ", scans[scan].name, scan_lo()/1000, scan_hi()/1000);
	}
}

/* USB Host Relay ------------------------------------------------------------------------------- */

#define HOST_TIMEOUT_MS 2000

static char     host_line[160];
static int      host_len;
static uint32_t host_last_ms;
static int      relay_bits;     /* Pending capture(s) payload format (8/10/32 bits), 0: none. */
static int      relay_captures; /* Pending captures. */
static char     esp_line[128];
static int      esp_len;
static uint32_t relay_frames;
static uint32_t relay_bytes;
static uint32_t relay_display_ms;

static void host_reply(const char *text)
{
	usblink_write(text, strlen(text));
}

static void host_command(char *line)
{
	unsigned n, rate, repeats, format;

	/* Transport commands: the ESP32 UART stays at 2Mbaud, the QSPI transport is the relay's. */
	if (!strcmp(line, "BAUD?")) {
		host_reply("BAUD 2000000\n");
		return;
	}
	if (!strncmp(line, "BAUD ", 5)) {
		char text[32];
		snprintf(text, sizeof(text), "OK BAUD %s\n", line + 5);
		host_reply(text);
		return;
	}
	if (!strncmp(line, "QSPI", 4)) {
		host_reply("ERR command\n");
		return;
	}
	/* Displayed settings. */
	if (sscanf(line, "FREQ %u", &n) == 1 || sscanf(line, "FREQK %u", &n) == 1) {
		freq_khz = (line[4] == 'K') ? (int)n : (int)n*1000;
		reset_levels();
	} else if (!strcmp(line, "GAIN HARDWARE")) {
		gain_db = -1;
	} else if (sscanf(line, "GAIN MANUAL %u", &n) == 1) {
		gain_db = n;
	}
	/* Captures: payloads over QSPI, sent by DMA after their DATA header. */
	relay_bits     = 0;
	relay_captures = 0;
	rate           = 0xff;
	if (sscanf(line, "CAP16 %u %u", &n, &rate) == 2) {
		relay_bits     = 8;
		relay_captures = 1;
	} else if (sscanf(line, "CAP20 %u %u", &n, &rate) == 2) {
		relay_bits     = 10;
		relay_captures = 1;
	} else if (sscanf(line, "CAP %u %u", &n, &rate) == 2) {
		relay_bits     = 32;
		relay_captures = 1;
	} else if (sscanf(line, "RXRUN %u %u %u %u", &n, &rate, &repeats, &format) == 4) {
		relay_bits     = (format == 20) ? 10 : 8;
		relay_captures = repeats;
	}
	for (int i = 0; i < 3; i++)
		if (spans[i].index == (int)rate && span != i) {
			span = i;
			reset_levels();
		}
	if (!qspi)
		relay_captures = 0; /* Stock ESP-SDR: payloads relayed from the UART. */
	esp32sdr_send(line);
}

static void relay_esp_line(void)
{
	/* Line of the ESP32 while capture payloads are expected (QSPI). */
	usblink_write(esp_line, esp_len);
	unsigned n;
	if (esp_len > 5 && !strncmp(esp_line, "DATA ", 5) && sscanf(esp_line + 5, "%u", &n) == 1) {
		int bits  = relay_bits;
		int bytes = (bits == 8) ? 2*n : (bits == 10) ? (20*n + 7)/8 : 4*n;
		const int8_t *buf = esp32sdr_qspi_buf();
		usblink_write_dma(buf, bytes);
		relay_frames++;
		relay_bytes += bytes;
		if (--relay_captures == 0)
			relay_bits = 0;
		/* Display (8-bit captures, ~10 updates/s). */
		if (bits == 8 && n >= FFT_SIZE &&
			esp32sdr_ms() - relay_display_ms >= 100) {
			relay_display_ms = esp32sdr_ms();
			flush_cpu_dcache();
			show_spectrum(buf, (n < SAMPLES) ? n : SAMPLES);
		}
	} else if (!strncmp(esp_line, "ERR", 3)) {
		relay_captures = 0;
		relay_bits     = 0;
	}
	esp_len = 0;
}

static void relay_poll(void)
{
	int c;
	/* Host -> ESP32 (lines). */
	while ((c = usblink_getc()) >= 0) {
		host_last_ms = esp32sdr_ms();
		if (c == '\r')
			continue;
		if (c == '\n') {
			host_line[host_len] = 0;
			if (host_len)
				host_command(host_line);
			host_len = 0;
		} else if (host_len < (int)sizeof(host_line) - 1)
			host_line[host_len++] = c;
	}
	/* ESP32 -> host: raw bytes, lines when capture payloads are expected. */
	uint8_t buf[64];
	int     len = 0;
	while (len < (int)sizeof(buf) && (c = esp32sdr_rx()) >= 0) {
		if (relay_captures) {
			if (len) {
				usblink_write(buf, len);
				len = 0;
			}
			if (esp_len < (int)sizeof(esp_line))
				esp_line[esp_len++] = c;
			if (c == '\n' || esp_len == (int)sizeof(esp_line))
				relay_esp_line();
		} else
			buf[len++] = c;
	}
	if (len)
		usblink_write(buf, len);
}

/* Main ----------------------------------------------------------------------------------------- */

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
	tone_init();
	lcd_text_scaled(24, 50, COLOR_WHITE, "CHROMATIX", 2);
	lcd_text_scaled(56, 66, COLOR_TRACE, "SDR", 2);
	lcd_text(26, 90, COLOR_DIM, "WAITING FOR ESP-SDR...");
	lcd_text(14, 116, COLOR_DIM, "I/Q CAPTURES: ESP-SDR BY ESPARGOS");
	lcd_text(42, 124, COLOR_DIM, "ESPARGOS.NET/ESPSDR");
	lcd_present();
	uint32_t splash_ms = esp32sdr_ms();

	esp32sdr_cmd("SYNC 1", reply, sizeof(reply), 200);
	if (esp32sdr_cmd("INFO", esp_info, sizeof(esp_info), 200) < 0)
		snprintf(esp_info, sizeof(esp_info), "?");
	/* kHz tuning (Chromatic ESP-SDR fork), else Wi-Fi channel frequencies only. */
	unsigned kmin, kmax;
	if (esp32sdr_cmd("RANGEK?", reply, sizeof(reply), 200) > 0 &&
		sscanf(reply, "RANGEK %u %u", &kmin, &kmax) == 2) {
		freqk    = 1;
		app_freq_min = kmin;
		app_freq_max = kmax;
	}
	/* Captures over QSPI (Chromatic ESP-SDR fork) if supported, else over the UART. */
	qspi = (esp32sdr_qspi((void *)(MAIN_RAM_BASE + LAYOUT_DATA_OFFSET),
		LAYOUT_PSRAM_OFFSET + LAYOUT_DATA_OFFSET) == 0);
	set_freq_khz(freq_khz);
	set_gain();
	set_filter(0);
	while (esp32sdr_ms() - splash_ms < 2000); /* Startup screen (credits) shown for 2s. */
	lcd_rect(0, 0, LCD_WIDTH, LCD_HEIGHT, COLOR_BLACK);
	app_toast("MENU: TOOLS/HELP  SELECT: VIEW");
	/* USB link: ESP-SDR protocol relay for the host. */
	int usb = (usblink_init() == 0);

	uint32_t errors[6] = {0};
	uint32_t frames = 0, fps_frames = 0, fps_t0 = esp32sdr_ms();
	for (;;) {
		/* USB host relay (paused during host debug sessions: 1200 baud touch, until the port is
		   closed). */
		if (usb && !usblink_touched()) {
			relay_poll();
			int active = host_len || relay_captures ||
				(esp32sdr_ms() - host_last_ms < HOST_TIMEOUT_MS && host_last_ms);
			if (active && !host) {
				host      = 1;
				menu_open = 0;
				page      = 0;
				tool      = 0;
				tool_sel  = 0;
				if (mode == MODE_SCAN)
					mode = MODE_SPECTRUM;
				reset_levels();
			}
			if (active)
				continue;
			if (host) {
				/* Host gone: local settings back. */
				host = 0;
				esp32sdr_flush();
				span = (span < SPANS) ? span : 1;
				set_filter(spans[span].mhz == 80);
				tune(freq_khz);
				set_gain();
				app_toast("USB HOST DONE");
			}
		}

		/* Controls. */
		handle_buttons();

		/* Capture/display. */
		uint32_t t_capture = esp32sdr_ms();
		int r, peak_x = 0;
		if (tool)
			r = tools[tool]->update();
		else if (mode == MODE_SCAN)
			r = scan_update();
		else {
			r = esp32sdr_capture(SAMPLES, spans[span].index, iq, NULL);
			if (r == 0)
				peak_x = show_spectrum(iq, SAMPLES);
		}
		if (r != 0) {
			errors[(r < 0 && r >= -5) ? -r : 0]++;
			memcpy(overlay_saved, lcd_screen, sizeof(overlay_saved));
			draw_message("ESP-SDR: NO CAPTURE", COLOR_RED);
			lcd_present();
			restore_overlays(1);
			esp32sdr_flush();
			continue;
		}
		uint32_t t_end = esp32sdr_ms();

		/* Statistics. */
		frames++;
		fps_frames++;
		uint32_t t = esp32sdr_ms();
		if (t - fps_t0 >= 2000) {
			fps10      = fps_frames*10000/(t - fps_t0);
			fps_frames = 0;
			fps_t0     = t;
			log_status("%s mode %d frames %lu errors %lu fps %d.%d freq %dkHz span %d gain %d "
				"ref %d peak %d@%d\nms: update %lu, relay: %lu frames %lu bytes\n",
				qspi ? "qspi" : "uart", mode, (unsigned long)frames,
				(unsigned long)(errors[1] + errors[2] + errors[3] + errors[4] + errors[5]),
				fps10/10, fps10 % 10, freq_khz, spans[span].mhz, gain_db, ref_db4/4,
				col_db4[peak_x]/4, peak_x, (unsigned long)(t_end - t_capture),
				(unsigned long)relay_frames, (unsigned long)relay_bytes);
		}
	}
	return 0;
}

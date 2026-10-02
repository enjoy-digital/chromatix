// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// ChromatiX SDR (--with-app build, ESP-SDR on the ESP32): 2.4GHz spectrum analyzer/waterfall on the
// LCD. I/Q bursts captured by the ESP32 (CAP16 over the ESP32 UART), FFT on the CPU.
//
// Controls: Left/Right: tune (5MHz), Up/Down: reference level (5dB, manual), A: span (16/40MHz),
// B: gain (AGC/manual), Start: peak hold, Select: auto reference level (default), Menu: RSSI tone
// (pitch following the peak level above the noise floor in the center quarter of the span: tune
// to an emitter and hunt it down). Waterfall scaled from the noise floor.
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
	{ESP32SDR_RATE_80MSPS, 80}, /* Host captures only: no wider view (ESP32 RX filter: ~40MHz). */
};
#define SPANS 2 /* Local spans. */

static const int gains[] = {-1, 20, 30, 40, 50, 60, 70}; /* -1: AGC (hardware gain). */
#define GAINS (int)(sizeof(gains)/sizeof(gains[0]))

static int freq_khz  = 2437000;
static int freqk     = 0; /* ESP32 kHz tuning (FREQK, Chromatic ESP-SDR fork). */
static int freq_min  = 2386000; /* FREQK range (kHz). */
static int freq_max  = 2504000;
static int span      = 1;
static int gain      = 0;
static int ref_db4   = -1; /* < 0: to set. */
static int ref_auto  = 1;  /* Reference level following the peak level (else Up/Down). */
static int peak_hold = 0;
static int sound     = 0; /* RSSI tone. */
static int qspi      = 0; /* Captures over QSPI (else UART). */
static int host      = 0; /* USB host relay active. */
static int gain_db   = -1; /* Displayed gain (-1: AGC). */

/* Stock ESP-SDR: the ESP32 only tunes reliably on the Wi-Fi channel frequencies (2412-2472MHz/5MHz,
   2484MHz): its out of channel frequencies don't move the LO (measured on the console crystal
   harmonics). The Chromatic ESP-SDR fork tunes 2386-2504MHz in kHz steps (FREQK). */
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

static void set_freq(void)
{
	char cmd[32], reply[32];
	if (freqk)
		snprintf(cmd, sizeof(cmd), "FREQK %d", freq_khz);
	else
		snprintf(cmd, sizeof(cmd), "FREQ %d", freq_khz/1000);
	esp32sdr_cmd(cmd, reply, sizeof(reply), 200);
}

static void set_gain(void)
{
	char cmd[32], reply[32];
	gain_db = gains[gain];
	if (gain_db < 0)
		snprintf(cmd, sizeof(cmd), "GAIN HARDWARE");
	else
		snprintf(cmd, sizeof(cmd), "GAIN MANUAL %d", gain_db);
	esp32sdr_cmd(cmd, reply, sizeof(reply), 200);
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

static int freq_to_x(int khz)
{
	/* Column of a frequency (kHz), -1 outside of the span. */
	int lo = freq_khz - 500*spans[span].mhz;
	int x  = (khz - lo)*LCD_WIDTH/(1000*spans[span].mhz);
	return (khz < lo || x >= LCD_WIDTH) ? -1 : x;
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
	const char *link = host ? "USB" : qspi ? "QSPI" : "UART";
	char freq[16], gain_text[8];
	if (freq_khz % 1000)
		snprintf(freq, sizeof(freq), "%d.%03d", freq_khz/1000, freq_khz % 1000);
	else
		snprintf(freq, sizeof(freq), "%d", freq_khz/1000);
	if (gain_db < 0)
		snprintf(gain_text, sizeof(gain_text), "AGC");
	else
		snprintf(gain_text, sizeof(gain_text), "%d", gain_db);
	snprintf(line, sizeof(line), "%s MHZ SPAN %d GAIN %s %s", freq, spans[span].mhz, gain_text,
		link);
	lcd_text(1, HEADER_Y + 1, COLOR_WHITE, line);
	int peak_khz = freq_khz - 500*spans[span].mhz + (2*peak_x + 1)*500*spans[span].mhz/LCD_WIDTH;
	snprintf(line, sizeof(line), "REF %d PK %d.%d %d %s%d.%dFPS", ref_db4/4, peak_khz/1000,
		(peak_khz % 1000)/100, col_db4[peak_x]/4, sound ? "SND " : peak_hold ? "HOLD " : "",
		fps10/10, fps10 % 10);
	lcd_text(1, HEADER_Y + 8, COLOR_WHITE, line);
}

static void draw_spectrum(void)
{
	lcd_rect(0, SPEC_Y, LCD_WIDTH, SPEC_H, COLOR_BLACK);
	/* Grid: 10dB lines, 10MHz ticks. */
	for (int db = 1; db < 6; db++)
		for (int x = 0; x < LCD_WIDTH; x += 2)
			lcd_screen[SPEC_Y + db*SPEC_H/6][x] = COLOR_GRID;
	int f_lo = freq_khz/1000 - spans[span].mhz/2;
	int f_hi = freq_khz/1000 + spans[span].mhz/2;
	for (int f = (f_lo + 9)/10*10; f <= f_hi; f += 10) {
		int x = freq_to_x(1000*f);
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
		int x    = freq_to_x(1000*f);
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
		int x = freq_to_x(1000*ble[i]);
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
	/* Pitch: 200Hz + 40Hz/dB of the center quarter peak above the noise floor. */
	if (!sound) {
		tone_freq = 0;
		return;
	}
	int peak = 0;
	for (int x = 3*LCD_WIDTH/8; x < 5*LCD_WIDTH/8; x++)
		peak = (col_db4[x] > peak) ? col_db4[x] : peak;
	int db = (peak - floor_db4)/4;
	db = (db < 0) ? 0 : (db > 50) ? 50 : db;
	tone_freq = 200 + 40*db;
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

static int fps10;

static int show_spectrum(const int8_t *data, int samples)
{
	/* Spectrum: FFT bins -> columns (max). */
	dsp_power_spectrum(data, samples, power);
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
	/* Auto reference: 5dB above the peak (5dB steps), followed when off by 10dB or more. */
	int ref_target = (col_db4[peak_x] + 5*4 + 19)/20*20;
	if (ref_db4 < 0 || (ref_auto && (ref_target - ref_db4 >= 10*4 || ref_db4 - ref_target >= 10*4)))
		ref_db4 = ref_target;
	tone_update();

	/* Display. */
	draw_spectrum();
	draw_markers();
	draw_waterfall();
	draw_header(fps10, peak_x);
	lcd_present();
	return peak_x;
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
		freq_khz  = (line[4] == 'K') ? (int)n : (int)n*1000;
		floor_db4 = -1;
		if (ref_auto)
			ref_db4 = -1;
		memset(peak_db4, 0, sizeof(peak_db4));
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
			span      = i;
			ref_db4   = -1;
			floor_db4 = -1;
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
	lcd_text(1, 1, COLOR_WHITE, "CHROMATIX SDR: WAITING FOR ESP-SDR...");
	lcd_present();

	esp32sdr_cmd("SYNC 1", reply, sizeof(reply), 200);
	/* kHz tuning (Chromatic ESP-SDR fork), else Wi-Fi channel frequencies only. */
	unsigned kmin, kmax;
	if (esp32sdr_cmd("RANGEK?", reply, sizeof(reply), 200) > 0 &&
		sscanf(reply, "RANGEK %u %u", &kmin, &kmax) == 2) {
		freqk    = 1;
		freq_min = kmin;
		freq_max = kmax;
	}
	/* Captures over QSPI (Chromatic ESP-SDR fork) if supported, else over the UART. */
	qspi = (esp32sdr_qspi((void *)(MAIN_RAM_BASE + LAYOUT_DATA_OFFSET),
		LAYOUT_PSRAM_OFFSET + LAYOUT_DATA_OFFSET) == 0);
	set_freq();
	set_gain();
	/* USB link: ESP-SDR protocol relay for the host. */
	int usb = (usblink_init() == 0);

	uint32_t errors[6] = {0};
	uint32_t frames = 0, fps_frames = 0, fps_t0 = esp32sdr_ms();
	for (;;) {
		/* USB host relay. */
		/* Relay paused during host debug sessions (1200 baud touch, until the port is closed). */
		if (usb && !usblink_touched()) {
			relay_poll();
			int active = host_len || relay_captures ||
				(esp32sdr_ms() - host_last_ms < HOST_TIMEOUT_MS && host_last_ms);
			if (active && !host) {
				host = 1;
				memset(peak_db4, 0, sizeof(peak_db4));
			}
			if (active)
				continue;
			if (host) {
				/* Host gone: local settings back. */
				host = 0;
				esp32sdr_flush();
				span      = (span < SPANS) ? span : 1;
				ref_db4   = -1;
				floor_db4 = -1;
				set_freq();
				set_gain();
			}
		}

		/* Controls. */
		uint32_t pressed = buttons_pressed();
		if (pressed & ((1 << BTN_LEFT) | (1 << BTN_RIGHT))) {
			int dir = (pressed & (1 << BTN_RIGHT)) ? 1 : -1;
			if (freqk) {
				freq_khz += dir*5000;
				freq_khz  = (freq_khz < freq_min) ? freq_khz + 5000 :
				            (freq_khz > freq_max) ? freq_khz - 5000 : freq_khz;
			} else
				freq_khz = 1000*next_channel(freq_khz/1000, dir);
			if (ref_auto)
				ref_db4 = -1;
			floor_db4 = -1;
			set_freq();
		}
		if (pressed & (1 << BTN_UP)) {
			ref_db4 += 5*4;
			ref_auto = 0;
		}
		if (pressed & (1 << BTN_DOWN)) {
			ref_db4 -= 5*4;
			ref_auto = 0;
		}
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
		if (pressed & (1 << BTN_SEL)) {
			ref_db4  = -1;
			ref_auto = 1;
		}
		if (pressed & (1 << BTN_MENU))
			sound = !sound;
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

		/* Spectrum/display. */
		uint32_t t_display = esp32sdr_ms();
		int peak_x = show_spectrum(iq, SAMPLES);
		uint32_t t_end = esp32sdr_ms();

		/* Statistics. */
		frames++;
		fps_frames++;
		uint32_t t = esp32sdr_ms();
		if (t - fps_t0 >= 2000) {
			fps10      = fps_frames*10000/(t - fps_t0);
			fps_frames = 0;
			fps_t0     = t;
			log_status("%s frames %lu errors %lu fps %d.%d freq %dkHz span %d gain %d ref %d peak %d@%d\n"
				"ms: capture %lu display %lu, relay: %lu frames %lu bytes\n",
				qspi ? "qspi" : "uart", (unsigned long)frames,
				(unsigned long)(errors[1] + errors[2] + errors[3] + errors[4] + errors[5]),
				fps10/10, fps10 % 10, freq_khz, spans[span].mhz, gain_db, ref_db4/4,
				col_db4[peak_x]/4, peak_x,
				(unsigned long)(t_display - t_capture), (unsigned long)(t_end - t_display),
				(unsigned long)relay_frames, (unsigned long)relay_bytes);
		}
	}
	return 0;
}

// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// ChromatiX SDR application services (main.c) for the tools (tool_*.c): radio, display, controls.
// A tool is selected from the menu (TOOL): it captures, processes and draws in its update, the
// menu/pages/toasts are drawn on top by app_present.

#ifndef APP_H
#define APP_H

#include <stdint.h>

#include "esp32sdr.h"
#include "lcd.h"

/* Colors (palette). */
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
	COLOR_CURSOR,
	COLOR_DIM,
	COLOR_CELL,
	COLOR_ISM,
	COLOR_MENU,
	COLOR_SELECT,
	COLOR_GREEN,
	COLOR_HEAT  = 64, /* 64-255: heat map. */
	HEAT_COLORS = 192,
};

/* Buttons (bits). */
enum {
	BTN_A = 0, BTN_B, BTN_DOWN, BTN_LEFT, BTN_RIGHT, BTN_UP, BTN_SEL, BTN_START, BTN_MENU,
};

struct tool {
	const char  *name;
	void       (*enter)(void);              /* Selected: radio setup, state reset. */
	int        (*update)(void);             /* Capture, process, draw, app_present: 0 or error. */
	void       (*buttons)(uint32_t pressed); /* Pressed buttons (Left/Right/Up/Down repeated). */
	const char *const *help;                /* Help page lines (NULL terminated). */
};

extern const struct tool tool_cell;
extern const struct tool tool_ble;
extern const struct tool tool_signals;
extern const struct tool tool_dect;
extern const struct tool tool_zigbee;
extern const struct tool tool_hunt;
extern const struct tool tool_wifi;

/* Radio. */
extern int app_freq_min; /* Tuning range (kHz). */
extern int app_freq_max;
void app_tune(int khz);
void app_filter(int wide);           /* ESP32 RX filter opened (80MS/s). */
void app_gain(int db);               /* Manual gain (dB), -1: AGC. */
int  app_capture(int samples, int rate, int8_t *iq); /* 0: ok. */

/* Display. */
#define APP_HEADER_H 14
void app_header(const char *title, const char *status); /* Tool header (14 lines). */
void app_present(void);              /* Overlays (menu, pages, toasts) and LCD update. */
void app_toast(const char *fmt, ...);
void app_format_khz(char *s, int len, int khz, int decimals);
void app_heat_line(int y, const int *db4, int floor_db4, int range_db); /* Heat map line. */

/* Sound: beep (Hz, ms). */
void app_beep(int hz, int ms);

/* Shared capture buffer (16380 I/Q samples). */
extern int8_t app_iq[];
#define APP_SAMPLES 16380

/* Time (ms). */
static inline uint32_t app_ms(void)
{
	return esp32sdr_ms();
}

#endif

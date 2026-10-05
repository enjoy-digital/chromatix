// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Transmit tool (bounded, legitimate): the Chromatic ESP-SDR fork is receive only, deliberately (an
// arbitrary-waveform transmitter on the ESP32 modem path is a jamming risk). This exposes only two
// bounded transmitters, both on documented ESP-IDF paths, each duration capped and auto-stopped, to
// close the loop with the receive tools (a known emitter to find with HUNT/SIGNALS, and to measure
// the LO error against): a single-carrier CW test tone (Wi-Fi or BLE band, the RF certification test
// path, not an arbitrary waveform) and a self-identifying open SoftAP beacon ("ChromatiX-TX"). Each
// transmission is explicit (A), nothing is emitted on its own.

#include <stdio.h>
#include <string.h>

#include "app.h"

enum { MODE_TONE, MODE_TONE_BLE, MODE_BEACON, MODES };

static const char *const mode_name[MODES] = { "CW TONE", "CW TONE BLE", "BEACON (AP)" };

/* Wi-Fi channel centre frequencies (MHz), index 0..13 -> channels 1..14. */
static const int wifi_mhz[14] = {
	2412, 2417, 2422, 2427, 2432, 2437, 2442, 2447, 2452, 2457, 2462, 2467, 2472, 2484,
};

static const int tone_ms[]   = { 200, 500, 1000, 2000 };
static const int beacon_ms[] = { 500, 1000, 2000, 5000 };
#define DURATIONS (int)(sizeof(tone_ms)/sizeof(tone_ms[0]))

/* Transmit attenuation (ESP-SDR backoff, 0.25dB units): larger = lower power. */
static const struct { int backoff; const char *name; } power[] = {
	{40, "LOW"}, {24, "MED"}, {12, "HIGH"},
};
#define POWERS (int)(sizeof(power)/sizeof(power[0]))

static int mode;
static int chan     = 5;  /* Wi-Fi channel index (0..13) / BLE channel 0..39. */
static int ble_chan = 19; /* BLE: 2402 + 2*ch -> channel 19 = 2440 MHz. */
static int duration = 1;  /* Index into the duration tables. */
static int pwr      = 1;  /* Index into power[]. */
static int fire;          /* A transmission requested (handled in update). */

static const char *const help[] = {
	"TX: BOUNDED TEST TRANSMIT.",
	"CW TONE (WIFI/BLE BAND) OR",
	"A SELF-ID SOFTAP BEACON,",
	"DURATION CAPPED, AUTO-STOP.",
	"USE ON YOUR OWN HARDWARE;",
	"2.4GHZ EMISSION IS REGULATED.",
	"",
	"SEL         MODE",
	"LEFT/RIGHT  CHANNEL",
	"UP/DOWN     DURATION",
	"B           POWER (TONE)",
	"A           TRANSMIT",
	NULL,
};

static int freq_mhz(void)
{
	if (mode == MODE_TONE_BLE)
		return 2402 + 2*ble_chan;
	return wifi_mhz[chan];
}

static int ms(void)
{
	return (mode == MODE_BEACON) ? beacon_ms[duration] : tone_ms[duration];
}

static void enter(void)
{
	fire = 0;
}

static void transmit(void)
{
	char cmd[32], reply[48];
	int t = ms();
	if (mode == MODE_TONE)
		snprintf(cmd, sizeof(cmd), "WTONE %d %d %d", chan + 1, t, power[pwr].backoff);
	else if (mode == MODE_TONE_BLE)
		snprintf(cmd, sizeof(cmd), "WTONEB %d %d %d", ble_chan, t, power[pwr].backoff);
	else
		snprintf(cmd, sizeof(cmd), "WTX %d %d", chan + 1, t);
	/* Blocks for the transmit window; the reply confirms (and the ESP32 restores the receive path). */
	int r = esp32sdr_cmd(cmd, reply, sizeof(reply), t + 2000);
	if (r < 0)
		app_toast("TX: NO REPLY (OLD FIRMWARE?)");
	else if (!strncmp(reply, "ERR", 3))
		app_toast("TX REJECTED: %s", reply);
	else
		app_toast("TX DONE (%s)", mode_name[mode]);
}

static int update(void)
{
	char line[48];
	lcd_rect(0, 0, LCD_WIDTH, LCD_HEIGHT, COLOR_BLACK);

	if (fire) {
		/* Show the transmitting state before the (blocking) command. */
		snprintf(line, sizeof(line), "%d MHZ", freq_mhz());
		app_header("TX", line);
		lcd_text_scaled(10, APP_HEADER_H + 24, COLOR_RED, "ON AIR", 3);
		snprintf(line, sizeof(line), "%s %d MS", mode_name[mode], ms());
		lcd_text(10, APP_HEADER_H + 52, COLOR_WHITE, line);
		app_present();
		transmit();
		fire = 0;
		return 0;
	}

	snprintf(line, sizeof(line), "%d MHZ", freq_mhz());
	app_header("TX", line);

	/* Mode. */
	lcd_text(6, APP_HEADER_H + 4, COLOR_DIM, "MODE");
	lcd_text(44, APP_HEADER_H + 4, COLOR_CELL, mode_name[mode]);

	/* Channel (big) + frequency. */
	if (mode == MODE_TONE_BLE)
		snprintf(line, sizeof(line), "BLE %d", ble_chan);
	else
		snprintf(line, sizeof(line), "CH %d", chan + 1);
	lcd_text_scaled(6, APP_HEADER_H + 18, COLOR_WHITE, line, 2);
	snprintf(line, sizeof(line), "%d MHZ", freq_mhz());
	lcd_text(6, APP_HEADER_H + 36, COLOR_TRACE, line);

	/* Duration and (tone) power. */
	snprintf(line, sizeof(line), "DURATION  %d MS", ms());
	lcd_text(6, APP_HEADER_H + 52, COLOR_WHITE, line);
	if (mode != MODE_BEACON) {
		snprintf(line, sizeof(line), "POWER     %s", power[pwr].name);
		lcd_text(6, APP_HEADER_H + 62, COLOR_WHITE, line);
	} else {
		lcd_text(6, APP_HEADER_H + 62, COLOR_DIM, "SSID      CHROMATIX-TX");
	}

	lcd_rect(0, LCD_HEIGHT - 22, LCD_WIDTH, 1, COLOR_GRID);
	lcd_text(6, LCD_HEIGHT - 17, COLOR_SELECT, "A: TRANSMIT");
	lcd_text(6, LCD_HEIGHT - 9, COLOR_DIM, "OWN HARDWARE / REGULATED BAND");

	app_present();
	return 0;
}

static void buttons(uint32_t p)
{
	int dir = (p & (1 << BTN_RIGHT)) ? 1 : (p & (1 << BTN_LEFT)) ? -1 : 0;
	if (dir) {
		if (mode == MODE_TONE_BLE)
			ble_chan = (ble_chan + dir + 40) % 40;
		else
			chan = (chan + dir + 14) % 14;
	}
	if (p & (1 << BTN_UP))
		duration = (duration < DURATIONS - 1) ? duration + 1 : duration;
	if (p & (1 << BTN_DOWN))
		duration = (duration > 0) ? duration - 1 : duration;
	if (p & (1 << BTN_SEL)) {
		mode = (mode + 1) % MODES;
		app_toast("MODE %s", mode_name[mode]);
	}
	if ((p & (1 << BTN_B)) && mode != MODE_BEACON) {
		pwr = (pwr + 1) % POWERS;
		app_toast("POWER %s", power[pwr].name);
	}
	if (p & (1 << BTN_A))
		fire = 1;
}

const struct tool tool_tx = {
	.name    = "TX",
	.enter   = enter,
	.update  = update,
	.buttons = buttons,
	.help    = help,
};

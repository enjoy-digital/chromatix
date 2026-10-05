// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// BLE scanner tool: advertising channels captured in turn (16MS/s, 1ms), packets decoded (ble.c):
// devices list (address, name/vendor/service, level, packets), trackers (Find My, SmartTag, Tile,
// Chipolo...) highlighted, device details, and a find mode (beep on each packet of the selected
// device, pitch following its level).

#include <stdio.h>
#include <string.h>

#include "app.h"
#include "ble.h"

#define OFFSET_KHZ  2000           /* Channel offset from the tuned frequency (DC away). */
#define MAX_DEVICES 48
#define MAX_PKTS    8

struct device {
	uint64_t address;
	int      txadd;
	int      type;
	int      tracker;
	char     text[24];
	int      level_db4;
	int      packets;
	uint32_t first_ms;
	uint32_t last_ms;
	int      length;
	uint8_t  pdu[BLE_PDU_MAX];
};

static struct device     devices[MAX_DEVICES];
static int               ndevices;
static int               order[MAX_DEVICES];
static int               channel = 37;
static int               selected;
static int               details;
static int               find;      /* Find mode (selected device). */
static uint64_t          find_address;
static int               by_level;  /* Sorted by level (else by last packet). */
static uint32_t          packets;
static struct ble_packet pkts[MAX_PKTS];

static const char *const help[] = {
	"BLE SCANNER: ADVERTISING",
	"CHANNELS 37/38/39, DEVICES",
	"(NAME/VENDOR/SERVICE), TRACKERS",
	"IN RED (FINDMY, SMARTTAG, TILE).",
	"",
	"UP/DOWN     SELECT DEVICE",
	"A           DETAILS",
	"START       FIND (BEEP/LEVEL)",
	"SELECT      SORT: RECENT/LEVEL",
	"B           CLEAR LIST",
	NULL,
};

static void enter(void)
{
	app_filter(0);
	app_gain(-1);
	details = 0;
}

static struct device *lookup(uint64_t address)
{
	for (int i = 0; i < ndevices; i++)
		if (devices[i].address == address)
			return &devices[i];
	if (ndevices < MAX_DEVICES)
		return &devices[ndevices++];
	/* Full: oldest replaced. */
	struct device *d = &devices[0];
	for (int i = 1; i < ndevices; i++)
		if ((int32_t)(devices[i].last_ms - d->last_ms) < 0)
			d = &devices[i];
	return d;
}

static void add_packet(const struct ble_packet *p)
{
	uint64_t address = ble_address(p);
	if (!address)
		return;
	struct device *d = lookup(address);
	uint32_t       t = app_ms();
	if (d->address != address) {
		memset(d, 0, sizeof(*d));
		d->address  = address;
		d->first_ms = t;
	}
	char text[32];
	int  tracker = ble_describe(p, text, sizeof(text));
	/* Keep the most specific description (scan responses: names). */
	if (!d->text[0] || tracker || strcmp(text, ble_type_name(p->type)) != 0) {
		snprintf(d->text, sizeof(d->text), "%s", text);
		d->tracker |= tracker;
	}
	d->txadd     = p->txadd;
	d->type      = p->type;
	d->level_db4 = d->packets ? (d->level_db4*3 + p->level_db4)/4 : p->level_db4;
	d->packets++;
	d->last_ms   = t;
	d->length    = p->length;
	memcpy(d->pdu, p->pdu, sizeof(d->pdu));
	packets++;
	/* Find mode: beep, pitch following the level. */
	if (find && address == find_address)
		app_beep(400 + 20*(p->level_db4/4 - 30), 40);
}

static void sort(void)
{
	for (int i = 0; i < ndevices; i++)
		order[i] = i;
	for (int i = 1; i < ndevices; i++)
		for (int j = i; j > 0; j--) {
			struct device *a = &devices[order[j - 1]], *b = &devices[order[j]];
			int swap = by_level ? (b->level_db4 > a->level_db4) :
				((int32_t)(b->last_ms - a->last_ms) > 0);
			if (!swap)
				break;
			int t = order[j]; order[j] = order[j - 1]; order[j - 1] = t;
		}
}

/* Display -------------------------------------------------------------------------------------- */

#define LIST_Y (APP_HEADER_H + 10)
#define ROWS   ((LCD_HEIGHT - LIST_Y - 8)/8)

static void draw_details(const struct device *d)
{
	char line[48];
	int  y = APP_HEADER_H + 2;
	lcd_text(1, y, d->tracker ? COLOR_RED : COLOR_WHITE, d->text);
	y += 8;
	snprintf(line, sizeof(line), "ADDR %02X:%02X:%02X:%02X:%02X:%02X %s",
		(int)(d->address >> 40) & 0xff, (int)(d->address >> 32) & 0xff,
		(int)(d->address >> 24) & 0xff, (int)(d->address >> 16) & 0xff,
		(int)(d->address >>  8) & 0xff, (int)(d->address >>  0) & 0xff,
		d->txadd ? "RANDOM" : "PUBLIC");
	lcd_text(1, y, COLOR_WHITE, line);
	y += 8;
	snprintf(line, sizeof(line), "%s  LEN %d  LEVEL %d DB", ble_type_name(d->type), d->length,
		d->level_db4/4);
	lcd_text(1, y, COLOR_WHITE, line);
	y += 8;
	snprintf(line, sizeof(line), "%d PACKETS, SEEN %lus, LAST %lus AGO", d->packets,
		(unsigned long)(d->last_ms - d->first_ms)/1000, (unsigned long)(app_ms() - d->last_ms)/1000);
	lcd_text(1, y, COLOR_DIM, line);
	y += 10;
	/* Payload (hex). */
	for (int i = 0; i < 2 + d->length && y < LCD_HEIGHT - 7; i += 13, y += 8) {
		char *s = line;
		for (int k = i; k < i + 13 && k < 2 + d->length; k++)
			s += sprintf(s, "%02X", d->pdu[k]);
		lcd_text(1, y, COLOR_DIM, line);
	}
}

static void draw(void)
{
	char line[48];
	int  trackers = 0;
	for (int i = 0; i < ndevices; i++)
		trackers += devices[i].tracker;
	snprintf(line, sizeof(line), "CH%d %d DEV %luPKT", channel, ndevices, (unsigned long)packets);
	lcd_rect(0, 0, LCD_WIDTH, LCD_HEIGHT, COLOR_BLACK);
	app_header("BLE", line);
	if (trackers) {
		snprintf(line, sizeof(line), "%d TRACKER%s", trackers, (trackers > 1) ? "S" : "");
		lcd_text(LCD_WIDTH - 4*(int)strlen(line), 7, COLOR_RED, line);
	} else if (find)
		lcd_text(LCD_WIDTH - 4*4, 7, COLOR_PEAK, "FIND");
	sort();
	selected = (selected >= ndevices) ? ndevices - 1 : (selected < 0) ? 0 : selected;
	if (details && ndevices) {
		draw_details(&devices[order[selected]]);
		return;
	}
	lcd_text(1, APP_HEADER_H + 1, COLOR_DIM, by_level ? "ADDR   DEVICE             >DB PKT" :
		"ADDR   DEVICE              DB PKT");
	int first = (selected >= ROWS) ? selected - ROWS + 1 : 0;
	for (int r = 0; r < ROWS && first + r < ndevices; r++) {
		const struct device *d = &devices[order[first + r]];
		int age = (app_ms() - d->last_ms)/1000;
		snprintf(line, sizeof(line), "%02X%02X%02X %-19.19s %3d %3d", (int)(d->address >> 16) & 0xff,
			(int)(d->address >> 8) & 0xff, (int)d->address & 0xff, d->text, d->level_db4/4,
			(d->packets > 999) ? 999 : d->packets);
		int y = LIST_Y + 8*r;
		if (first + r == selected)
			lcd_rect(0, y - 1, LCD_WIDTH, 8, COLOR_SELECT);
		lcd_text(1, y, d->tracker ? COLOR_RED : (age > 30) ? COLOR_DIM : COLOR_WHITE, line);
	}
	if (!ndevices)
		lcd_text(1, LIST_Y, COLOR_DIM, "LISTENING...");
}

/* Tool ----------------------------------------------------------------------------------------- */

static int update(void)
{
	/* Advertising channels in turn. */
	channel = (channel >= 39) ? 37 : channel + 1;
	app_tune(1000*ble_channel_mhz(channel) + OFFSET_KHZ);
	int r = app_capture(APP_SAMPLES, ESP32SDR_RATE_16MSPS, app_iq);
	if (r == 0) {
		int n = ble_decode(app_iq, APP_SAMPLES, -OFFSET_KHZ*1000, channel, pkts, MAX_PKTS);
		for (int i = 0; i < n; i++)
			add_packet(&pkts[i]);
	}
	draw();
	app_present();
	return r;
}

static void buttons(uint32_t p)
{
	if (p & (1 << BTN_UP))
		selected--;
	if (p & (1 << BTN_DOWN))
		selected++;
	if (p & (1 << BTN_A))
		details = !details;
	if (p & (1 << BTN_SEL)) {
		by_level = !by_level;
		app_toast("SORT: %s", by_level ? "LEVEL" : "RECENT");
	}
	if ((p & (1 << BTN_START)) && ndevices) {
		sort();
		find         = !find;
		find_address = devices[order[selected]].address;
		app_toast(find ? "FIND: BEEP ON ITS PACKETS" : "FIND OFF");
	}
	if (p & (1 << BTN_B)) {
		if (details)
			details = 0;
		else {
			ndevices = 0;
			packets  = 0;
			find     = 0;
			app_toast("LIST CLEARED");
		}
	}
}

const struct tool tool_ble = {
	.name    = "BLE SCANNER",
	.enter   = enter,
	.update  = update,
	.buttons = buttons,
	.help    = help,
};

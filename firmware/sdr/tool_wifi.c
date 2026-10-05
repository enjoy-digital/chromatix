// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Wi-Fi scanner tool: the ESP32 Wi-Fi receiver (WSNIFF command, Chromatic ESP-SDR fork), channels
// 1-13 scanned in turn. Access points (SSID, channel, security, signal, beacons), stations
// (associated or probing, signal), channel occupancy, and a deauthentication/disassociation alert
// (attack indicator). Uses the ESP32's own Wi-Fi demodulator (full frames), not the raw I/Q
// captures.

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app.h"
#include "esp32sdr.h"

#define MAX_APS   40
#define MAX_STAS  40
#define DWELL_MS  350
#define REPLY_MS  600

struct ap {
	char     bssid[18];
	char     ssid[20];
	int      channel;
	int      rssi;
	char     security[8];
	int      beacons;
	int      clients;
	uint32_t last_ms;
};

struct sta {
	char     mac[18];
	char     bssid[18];   /* "-" if not associated. */
	char     probe[16];
	int      rssi;
	int      frames;
	uint32_t last_ms;
};

enum {
	VIEW_APS = 0,
	VIEW_STATIONS,
	VIEW_CHANNELS,
	VIEWS,
};

static int        channel = 1;       /* Channel being scanned (1-13). */
static int        view;
static int        by_rssi = 1;       /* APs sorted by signal (else by channel). */
static int        scroll;
static struct ap  aps[MAX_APS];
static struct sta stas[MAX_STAS];
static int        naps, nstas;
static int        ch_aps[14];        /* APs seen per channel. */
static int        ch_frames[14];     /* Frames seen per channel (occupancy). */
static int        deauth;            /* Deauth/disassoc frames in the last scan. */
static char       deauth_src[18];
static uint32_t   deauth_ms;

static const char *const help[] = {
	"WIFI SCANNER (ESP32 RADIO):",
	"ACCESS POINTS (SSID, SECURITY,",
	"SIGNAL), STATIONS, CHANNEL USE,",
	"DEAUTH ALERT. NOT SDR: THE",
	"ESP32 WIFI DEMODULATOR.",
	"",
	"SELECT      VIEW: AP/STA/CHAN",
	"UP/DOWN     SCROLL",
	"A           SORT RSSI/CHANNEL",
	"B           CLEAR",
	NULL,
};

static void enter(void)
{
	/* The ESP32 switches to Wi-Fi reception during WSNIFF and restores the SDR setup after; no
	   local capture here. */
	esp32sdr_flush();
}

static struct ap *ap_get(const char *bssid)
{
	for (int i = 0; i < naps; i++)
		if (!strcmp(aps[i].bssid, bssid))
			return &aps[i];
	struct ap *a;
	if (naps < MAX_APS)
		a = &aps[naps++];
	else {
		/* Full: reuse the oldest entry. */
		a = &aps[0];
		for (int i = 1; i < naps; i++)
			if ((int32_t)(aps[i].last_ms - a->last_ms) < 0)
				a = &aps[i];
	}
	memset(a, 0, sizeof(*a));
	snprintf(a->bssid, sizeof(a->bssid), "%s", bssid);
	return a;
}

static struct sta *sta_get(const char *mac)
{
	for (int i = 0; i < nstas; i++)
		if (!strcmp(stas[i].mac, mac))
			return &stas[i];
	struct sta *s;
	if (nstas < MAX_STAS)
		s = &stas[nstas++];
	else {
		/* Full (MAC-randomized probe requests): reuse the oldest entry. */
		s = &stas[0];
		for (int i = 1; i < nstas; i++)
			if ((int32_t)(stas[i].last_ms - s->last_ms) < 0)
				s = &stas[i];
	}
	memset(s, 0, sizeof(*s));
	snprintf(s->mac, sizeof(s->mac), "%s", mac);
	return s;
}

static void parse_ap(char *line)
{
	/* "WAP <bssid> <channel> <rssi> <security> <beacons> <ssid...>". */
	char *bssid = strtok(line, " ");
	char *ch    = strtok(NULL, " ");
	char *rssi  = strtok(NULL, " ");
	char *sec   = strtok(NULL, " ");
	char *bcn   = strtok(NULL, " ");
	char *ssid  = strtok(NULL, "");
	if (!bssid || !ch || !rssi || !sec || !bcn)
		return;
	struct ap *a = ap_get(bssid);
	if (!a)
		return;
	a->channel = atoi(ch);
	a->rssi    = atoi(rssi);
	a->beacons = atoi(bcn);
	snprintf(a->security, sizeof(a->security), "%s", sec);
	snprintf(a->ssid, sizeof(a->ssid), "%s", ssid ? ssid : "");
	a->last_ms = app_ms();
}

static void parse_sta(char *line)
{
	/* "WST <mac> <bssid or -> <rssi> <frames> <probed ssid...>". */
	char *mac   = strtok(line, " ");
	char *bssid = strtok(NULL, " ");
	char *rssi  = strtok(NULL, " ");
	char *fr    = strtok(NULL, " ");
	char *probe = strtok(NULL, "");
	if (!mac || !bssid || !rssi || !fr)
		return;
	struct sta *s = sta_get(mac);
	if (!s)
		return;
	snprintf(s->bssid, sizeof(s->bssid), "%s", bssid);
	s->rssi   = atoi(rssi);
	s->frames = atoi(fr);
	snprintf(s->probe, sizeof(s->probe), "%s", probe ? probe : "");
	s->last_ms = app_ms();
}

static void count_clients(void)
{
	/* Per sweep: client counts, and APs per home channel (recent APs: adjacent-channel leakage
	   would otherwise inflate neighbouring channels). */
	for (int i = 0; i < naps; i++)
		aps[i].clients = 0;
	for (int i = 0; i < nstas; i++)
		if (stas[i].bssid[0] != '-')
			for (int j = 0; j < naps; j++)
				if (!strcmp(aps[j].bssid, stas[i].bssid)) {
					aps[j].clients++;
					break;
				}
	memset(ch_aps, 0, sizeof(ch_aps));
	for (int i = 0; i < naps; i++)
		if (aps[i].channel >= 1 && aps[i].channel <= 13 && app_ms() - aps[i].last_ms < 30000)
			ch_aps[aps[i].channel]++;
}

static int scan_channel(void)
{
	/* One channel: WSNIFF, reply lines until WEND. */
	char cmd[32], line[128];
	snprintf(cmd, sizeof(cmd), "WSNIFF %d %d", channel, DWELL_MS);
	esp32sdr_send(cmd);
	for (;;) {
		int n = esp32sdr_read_line(line, sizeof(line), DWELL_MS + REPLY_MS);
		if (n < 0)
			return -1;
		if (!strncmp(line, "WAP ", 4)) {
			parse_ap(line + 4);
		} else if (!strncmp(line, "WST ", 4)) {
			parse_sta(line + 4);
		} else if (!strncmp(line, "WDA ", 4)) {
			char *src = strtok(line + 4, " ");
			char *dst = strtok(NULL, " ");
			char *cnt = strtok(NULL, " ");
			(void)dst;
			deauth    += cnt ? atoi(cnt) : 1;
			deauth_ms  = app_ms();
			if (src)
				snprintf(deauth_src, sizeof(deauth_src), "%s", src);
		} else if (!strncmp(line, "WEND ", 5)) {
			int f = 0;
			sscanf(line + 5, "%d", &f);
			ch_frames[channel] = f;
			break;
		}
	}
	return 0;
}

/* Display -------------------------------------------------------------------------------------- */

#define LIST_Y (APP_HEADER_H + 2)

static int order[MAX_APS];

static void sort_aps(void)
{
	for (int i = 0; i < naps; i++)
		order[i] = i;
	for (int i = 1; i < naps; i++)
		for (int j = i; j > 0; j--) {
			struct ap *a = &aps[order[j - 1]], *b = &aps[order[j]];
			int swap = by_rssi ? (b->rssi > a->rssi) :
				(b->channel < a->channel || (b->channel == a->channel && b->rssi > a->rssi));
			if (!swap)
				break;
			int t = order[j]; order[j] = order[j - 1]; order[j - 1] = t;
		}
}

static uint8_t rssi_color(int rssi)
{
	return (rssi > -55) ? COLOR_GREEN : (rssi > -72) ? COLOR_PEAK : COLOR_RED;
}

static void draw_bar(int x, int y, int w, int rssi)
{
	/* RSSI -90..-30 dBm. */
	int v = (rssi + 90)*w/60;
	v = (v < 1) ? 1 : (v > w) ? w : v;
	lcd_rect(x, y, v, 3, rssi_color(rssi));
}

static void draw_aps(void)
{
	char line[40];
	lcd_text(1, LIST_Y, COLOR_DIM, by_rssi ? "SSID            CH SEC   CL >SIG" :
		"SSID            CH SEC   CL  SIG");
	int rows = (LCD_HEIGHT - LIST_Y - 16)/9;
	sort_aps();
	for (int r = 0; r < rows && scroll + r < naps; r++) {
		struct ap *a = &aps[order[scroll + r]];
		int y = LIST_Y + 9 + 9*r;
		snprintf(line, sizeof(line), "%-15.15s %2d %-5s %2d", a->ssid[0] ? a->ssid : "(HIDDEN)",
			a->channel, a->security, a->clients);
		lcd_text(1, y, (app_ms() - a->last_ms < 8000) ? COLOR_WHITE : COLOR_DIM, line);
		draw_bar(LCD_WIDTH - 28, y + 1, 26, a->rssi);
	}
}

static void draw_stations(void)
{
	char line[40];
	lcd_text(1, LIST_Y, COLOR_DIM, "STATION (MAC)  AP/PROBE      SIG");
	int rows = (LCD_HEIGHT - LIST_Y - 16)/9;
	for (int r = 0; r < rows && scroll + r < nstas; r++) {
		struct sta *s = &stas[scroll + r];
		int y = LIST_Y + 9 + 9*r;
		const char *tail = s->bssid[0] != '-' ? s->bssid + 9 : s->probe[0] ? s->probe : "(PROBE)";
		snprintf(line, sizeof(line), "%s %-13.13s", s->mac + 9, tail);
		lcd_text(1, y, (app_ms() - s->last_ms < 8000) ? COLOR_WHITE : COLOR_DIM, line);
		draw_bar(LCD_WIDTH - 28, y + 1, 26, s->rssi);
	}
	if (!nstas)
		lcd_text(1, LIST_Y + 10, COLOR_DIM, "NO STATION SEEN YET");
}

static void draw_channels(void)
{
	char line[32];
	int  busiest = 1, best = 1, best_score = 0x7fffffff;
	lcd_text(1, LIST_Y, COLOR_DIM, "CHANNEL USE (APS, TRAFFIC)");
	for (int c = 1; c <= 13; c++)
		if (ch_frames[c] > ch_frames[busiest])
			busiest = c;
	int y0 = LIST_Y + 12, h = LCD_HEIGHT - y0 - 26;
	for (int c = 1; c <= 13; c++) {
		int x = 4 + (c - 1)*12;
		int v = ch_frames[busiest] ? ch_frames[c]*h/ch_frames[busiest] : 0;
		v = (ch_aps[c] && v < 2) ? 2 : v;
		lcd_rect(x, y0 + h - v, 9, v, (c == 1 || c == 6 || c == 11) ? COLOR_WIFI : COLOR_FILL);
		snprintf(line, sizeof(line), "%d", c);
		lcd_text(x + 4 - 2*(c >= 10), y0 + h + 2, COLOR_DIM, line);
		if (ch_aps[c]) {
			snprintf(line, sizeof(line), "%d", ch_aps[c]);
			lcd_text(x + 4 - 2*(ch_aps[c] >= 10), y0 + h - v - 7, COLOR_WHITE, line);
		}
	}
	/* Least busy of 1/6/11 (traffic + the overlapping channels' APs). */
	for (int i = 0; i < 3; i++) {
		int c = 1 + 5*i, score = ch_frames[c]*4;
		for (int k = c - 4; k <= c + 4; k++)
			if (k >= 1 && k <= 13)
				score += ch_aps[k]*(5 - abs(k - c));
		if (score < best_score) {
			best_score = score;
			best       = c;
		}
	}
	snprintf(line, sizeof(line), "BEST OF 1/6/11: CH %d", best);
	lcd_text(1, LCD_HEIGHT - 7, COLOR_GREEN, line);
}

static int update(void)
{
	int r = scan_channel();
	channel++;
	if (channel > 13) {
		channel = 1;
		count_clients();
	}

	char line[40];
	lcd_rect(0, 0, LCD_WIDTH, LCD_HEIGHT, COLOR_BLACK);
	snprintf(line, sizeof(line), "CH%d %dAP %dST", channel, naps, nstas);
	app_header("WIFI", line);
	if (view == VIEW_APS)
		draw_aps();
	else if (view == VIEW_STATIONS)
		draw_stations();
	else
		draw_channels();
	/* Deauth alert (attack indicator), 10s. */
	if (deauth && app_ms() - deauth_ms < 10000) {
		snprintf(line, sizeof(line), "DEAUTH x%d FROM %s", deauth, deauth_src + 9);
		lcd_rect(0, LCD_HEIGHT - 9, LCD_WIDTH, 9, COLOR_RED);
		lcd_text(2, LCD_HEIGHT - 7, COLOR_WHITE, line);
	}
	app_present();
	if (r != 0)
		esp32sdr_flush();
	return 0;
}

static void buttons(uint32_t p)
{
	int count = (view == VIEW_STATIONS) ? nstas : naps;
	if (p & (1 << BTN_SEL)) {
		view   = (view + 1) % VIEWS;
		scroll = 0;
		app_toast(view == VIEW_APS ? "ACCESS POINTS" :
			view == VIEW_STATIONS ? "STATIONS" : "CHANNELS");
	}
	if (p & (1 << BTN_A)) {
		by_rssi = !by_rssi;
		app_toast("SORT: %s", by_rssi ? "SIGNAL" : "CHANNEL");
	}
	if (p & (1 << BTN_UP))
		scroll = (scroll > 0) ? scroll - 1 : 0;
	if (p & (1 << BTN_DOWN))
		scroll = (scroll < count - 1) ? scroll + 1 : scroll;
	if (p & (1 << BTN_B)) {
		naps = nstas = 0;
		deauth = 0;
		memset(ch_aps, 0, sizeof(ch_aps));
		memset(ch_frames, 0, sizeof(ch_frames));
		app_toast("CLEARED");
	}
}

const struct tool tool_wifi = {
	.name    = "WIFI SCANNER",
	.enter   = enter,
	.update  = update,
	.buttons = buttons,
	.help    = help,
};

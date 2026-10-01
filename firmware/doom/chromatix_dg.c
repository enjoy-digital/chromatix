// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// doomgeneric platform layer for the Chromatic (over hal.h): 320x200 Doom screen downscaled to the
// 160x144 LCD, buttons to Doom keys, WAD used in place from memory, frames rate on the console.

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <strings.h>

#include "doomgeneric.h"
#include "doomkeys.h"
#include "doomtype.h"
#include "g_game.h"
#include "i_video.h"
#include "w_file.h"
#include "z_zone.h"

#include "hal.h"

/* Display -------------------------------------------------------------------------------------- */

/* Doom screen (320x200, shown 4:3 on a CRT) -> LCD: 160 columns (1 out of 2) and DISPLAY_LINES
   lines (120: 4:3 aspect ratio, 100: 1 out of 2, 144: full height), centered. */
#ifndef DISPLAY_LINES
#define DISPLAY_LINES 120
#endif

#define DOOM_WIDTH  320
#define DOOM_HEIGHT 200

static uint16_t display_src[DISPLAY_LINES]; /* Doom line of each displayed line. */
static int      display_top;

static void display_init(void)
{
	uint8_t black[HAL_LCD_WIDTH];

	display_top = (HAL_LCD_HEIGHT - DISPLAY_LINES)/2;
	for (int i = 0; i < DISPLAY_LINES; i++)
		display_src[i] = (i*DOOM_HEIGHT + DOOM_HEIGHT/(2*DISPLAY_LINES))/DISPLAY_LINES;
	/* Borders (palette index 0: black in Doom's palettes). */
	memset(black, 0, sizeof(black));
	for (int y = 0; y < HAL_LCD_HEIGHT; y++)
		if (y < display_top || y >= display_top + DISPLAY_LINES)
			hal_lcd_line(y, black);
}

void DG_SetPalette(const uint8_t *rgb)
{
	uint16_t palette[256];

	for (int i = 0; i < 256; i++)
		palette[i] = (rgb[3*i + 2] >> 3) << 10 | (rgb[3*i + 1] >> 3) << 5 | (rgb[3*i + 0] >> 3);
	hal_lcd_palette(palette);
}

/* Frames rate (console, and frames counter for the host), timedemo length limit (-benchtics N). */
static uint32_t fps_time;
static uint32_t fps_frames;
static int      bench_tics;

extern int     gametic;
extern boolean timingdemo;

void DG_DrawFrame(void)
{
	uint8_t line[HAL_LCD_WIDTH];

	for (int i = 0; i < DISPLAY_LINES; i++) {
		const uint8_t *src = I_VideoBuffer + display_src[i]*DOOM_WIDTH;
		for (int x = 0; x < HAL_LCD_WIDTH; x++)
			line[x] = src[2*x];
		hal_lcd_line(display_top + i, line);
	}
	hal_lcd_present();

	hal_frame();
	fps_frames++;
	if (bench_tics && timingdemo && gametic >= bench_tics)
		G_CheckDemoStatus();
	if (hal_ticks_ms() - fps_time >= 5000) {
		uint32_t tenths = fps_frames*10000/(hal_ticks_ms() - fps_time);
		printf("Doom: %lu.%lu fps\n", (unsigned long)(tenths/10), (unsigned long)(tenths%10));
		fps_frames = 0;
		fps_time   = hal_ticks_ms();
	}
}

/* Time ----------------------------------------------------------------------------------------- */

uint32_t DG_GetTicksMs(void)
{
	return hal_ticks_ms();
}

void DG_SleepMs(uint32_t ms)
{
	hal_sleep_ms(ms);
}

void DG_SetWindowTitle(const char *title)
{
	(void)title;
}

/* Input ---------------------------------------------------------------------------------------- */

/*
 * Buttons -> Doom keys:
 * - D-pad: move/turn (menus: navigation).
 * - A: fire (menus: select/yes), B: use (menus: back), Start: menu.
 * - Select + Left/Right: strafe, Select + Up/Down: next/previous weapon, Select alone: automap.
 * Always run. The Menu button is left to the ESP32 menu.
 */

#define KEY_NEXT_WEAPON ']'
#define KEY_PREV_WEAPON '['

extern boolean menuactive;
extern int     joybspeed;
extern int     detailLevel;
extern int     key_nextweapon;
extern int     key_prevweapon;

#define KEY_QUEUE_SIZE 32

static uint16_t key_queue[KEY_QUEUE_SIZE]; /* {pressed, key}. */
static unsigned key_rd, key_wr;
static uint32_t buttons_last;
static uint8_t  buttons_key[HAL_BTN_COUNT]; /* Key sent on press (released with the button). */
static int      select_used;                /* Select used as a modifier since pressed. */

static void key_push(int pressed, unsigned char key)
{
	if (key_wr - key_rd < KEY_QUEUE_SIZE)
		key_queue[key_wr++ % KEY_QUEUE_SIZE] = (pressed << 8) | key;
}

static void key_tap(unsigned char key)
{
	key_push(1, key);
	key_push(0, key);
}

static unsigned char button_key(int button, uint32_t buttons)
{
	int select = (buttons >> HAL_BTN_SELECT) & 1;

	switch (button) {
	case HAL_BTN_UP:    return KEY_UPARROW;
	case HAL_BTN_DOWN:  return KEY_DOWNARROW;
	case HAL_BTN_LEFT:  return (select && !menuactive) ? KEY_STRAFE_L : KEY_LEFTARROW;
	case HAL_BTN_RIGHT: return (select && !menuactive) ? KEY_STRAFE_R : KEY_RIGHTARROW;
	case HAL_BTN_A:     return menuactive ? KEY_ENTER : KEY_FIRE;
	case HAL_BTN_B:     return menuactive ? KEY_BACKSPACE : KEY_USE;
	case HAL_BTN_START: return KEY_ESCAPE;
	default:            return 0;
	}
}

static void buttons_update(void)
{
	uint32_t buttons = hal_buttons();
	uint32_t changed = buttons ^ buttons_last;
	int      select  = (buttons >> HAL_BTN_SELECT) & 1;

	for (int i = 0; i < HAL_BTN_COUNT; i++) {
		if (!(changed & (1 << i)))
			continue;
		int pressed = (buttons >> i) & 1;
		if (i == HAL_BTN_SELECT) {
			/* Automap when released without being used as a modifier. */
			if (pressed)
				select_used = 0;
			else if (!select_used)
				key_tap(KEY_TAB);
			continue;
		}
		if (pressed && select && !menuactive && (i == HAL_BTN_UP || i == HAL_BTN_DOWN)) {
			key_tap(i == HAL_BTN_UP ? KEY_NEXT_WEAPON : KEY_PREV_WEAPON);
			select_used = 1;
			continue;
		}
		if (pressed) {
			buttons_key[i] = button_key(i, buttons);
			if (buttons_key[i]) {
				key_push(1, buttons_key[i]);
				/* Menus: A also confirms the yes/no messages. */
				if (i == HAL_BTN_A && menuactive)
					key_tap('y');
			}
			if (select && (i == HAL_BTN_LEFT || i == HAL_BTN_RIGHT))
				select_used = 1;
		} else if (buttons_key[i]) {
			key_push(0, buttons_key[i]);
			buttons_key[i] = 0;
		}
	}
	buttons_last = buttons;
}

int DG_GetKey(int *pressed, unsigned char *key)
{
	if (key_rd == key_wr) {
		if (!hal_poll())
			I_Quit();
		buttons_update();
	}
	if (key_rd == key_wr)
		return 0;
	uint16_t event = key_queue[key_rd++ % KEY_QUEUE_SIZE];
	*pressed = event >> 8;
	*key     = event & 0xff;
	return 1;
}

/* WAD ------------------------------------------------------------------------------------------ */

#define IWAD_NAME "doom1.wad"

const uint8_t *DG_MemWAD(const char *path, unsigned int *size)
{
	const char *name = strrchr(path, '/');

	name = name ? name + 1 : path;
	if (strcasecmp(name, IWAD_NAME) != 0)
		return NULL;
	return hal_wad(size);
}

static wad_file_t *mem_wad_open(char *path);
static void        mem_wad_close(wad_file_t *wad);
static size_t      mem_wad_read(wad_file_t *wad, unsigned int offset, void *buffer, size_t length);

wad_file_class_t mem_wad_file = {
	mem_wad_open,
	mem_wad_close,
	mem_wad_read,
};

static wad_file_t *mem_wad_open(char *path)
{
	unsigned int   size;
	const uint8_t *data = DG_MemWAD(path, &size);
	wad_file_t    *wad;

	if (data == NULL)
		return NULL;
	wad             = Z_Malloc(sizeof(wad_file_t), PU_STATIC, 0);
	wad->file_class = &mem_wad_file;
	wad->mapped     = (byte *)data; /* Lumps used in place (aligned by the loader). */
	wad->length     = size;
	return wad;
}

static void mem_wad_close(wad_file_t *wad)
{
	Z_Free(wad);
}

static size_t mem_wad_read(wad_file_t *wad, unsigned int offset, void *buffer, size_t length)
{
	if (offset >= wad->length)
		return 0;
	if (length > wad->length - offset)
		length = wad->length - offset;
	memcpy(buffer, wad->mapped + offset, length);
	return length;
}

/* Main ----------------------------------------------------------------------------------------- */

void DG_Init(void)
{
	display_init();
	/* Low detail (160 columns rendered: the displayed ones, see DOOMGENERIC_LOWDETAIL_HALF). */
	detailLevel    = 1;
	/* Controls: always run, weapons keys. */
	joybspeed      = 29;
	key_nextweapon = KEY_NEXT_WEAPON;
	key_prevweapon = KEY_PREV_WEAPON;
	fps_time       = hal_ticks_ms();
}

void DG_TimedemoDone(int gametics, int realtics)
{
	hal_bench(gametics, realtics);
}

int main(int argc, char **argv)
{
	/* Default arguments: memory IWAD, 2MB zone; extra arguments (command line, host) appended.
	   -profile: PC sampling profiler (host: scripts/chromatic.py doom-bench). */
	static char *args[32] = {"doom", "-iwad", IWAD_NAME, "-mb", "2"};
	static char  extra[1024];
	const char  *host_args;
	int          nargs = 5, profile = 0;

#ifdef HAL_NO_ARGV
	/* Bare metal: main() called without arguments (argc/argv not set). */
	argc = 0;
#endif
	for (int i = 1; i < argc && nargs < 31; i++)
		args[nargs++] = argv[i];

	hal_init();
	host_args = hal_args();
	if (host_args) {
		strncpy(extra, host_args, sizeof(extra) - 1);
		for (char *p = strtok(extra, " "); p && nargs < 31; p = strtok(NULL, " ")) {
			if (strcmp(p, "-profile") == 0)
				profile = 1;
			else if (strcmp(p, "-benchtics") == 0 && (p = strtok(NULL, " ")))
				bench_tics = atoi(p);
			else
				args[nargs++] = p;
		}
	}
	hal_status((host_args != NULL) | (profile << 1) | (bench_tics << 16));
	hal_profile(profile);
	printf("ChromatiX Doom\n");
	doomgeneric_Create(nargs, args);
	for (;;)
		doomgeneric_Tick();
	return 0;
}

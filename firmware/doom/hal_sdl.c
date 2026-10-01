// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Doom port HAL, host emulation of the Chromatic (SDL2): 160x144 indexed LCD in a scaled window,
// keyboard/gamepad as the buttons, WAD loaded in memory from DOOM_WAD (default: doom1.wad, aligned
// as done by the hardware loader), audio queued to SDL.
//
// Keys: arrows, X: A, Z: B, Enter: Start, Backspace/Right Shift: Select, Escape: quit.
// DOOM_SNAP=frame,frame,...: saves the LCD (snap_<frame>.bmp) at these frames (tests).

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <SDL.h>

#include "hal.h"
#include "wad_align.h"

#define SCALE 4

static SDL_Window        *window;
static SDL_Renderer      *renderer;
static SDL_Texture       *texture;
static SDL_GameController *pad;
static SDL_AudioDeviceID  audio;
static uint8_t            lcd[HAL_LCD_HEIGHT][HAL_LCD_WIDTH];
static uint32_t           palette[256];
static uint32_t           keys;
static uint8_t           *wad;
static unsigned int       wad_size;
static unsigned int       frames;

void hal_init(void)
{
	SDL_AudioSpec spec = {0};

	if (SDL_Init(SDL_INIT_VIDEO | SDL_INIT_AUDIO | SDL_INIT_GAMECONTROLLER) != 0) {
		fprintf(stderr, "SDL: %s\n", SDL_GetError());
		exit(1);
	}
	window   = SDL_CreateWindow("ChromatiX Doom (host)", SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED,
		HAL_LCD_WIDTH*SCALE, HAL_LCD_HEIGHT*SCALE, 0);
	renderer = SDL_CreateRenderer(window, -1, 0);
	texture  = SDL_CreateTexture(renderer, SDL_PIXELFORMAT_ARGB8888, SDL_TEXTUREACCESS_STREAMING,
		HAL_LCD_WIDTH, HAL_LCD_HEIGHT);
	for (int i = 0; i < SDL_NumJoysticks() && !pad; i++)
		if (SDL_IsGameController(i))
			pad = SDL_GameControllerOpen(i);

	spec.freq     = HAL_AUDIO_RATE;
	spec.format   = AUDIO_S16SYS;
	spec.channels = 2;
	spec.samples  = 512;
	audio = SDL_OpenAudioDevice(NULL, 0, &spec, NULL, 0);
	if (audio)
		SDL_PauseAudioDevice(audio, 0);

	/* WAD (same lumps alignment as the hardware loader). */
	const char *name = getenv("DOOM_WAD") ? getenv("DOOM_WAD") : "doom1.wad";
	FILE *f = fopen(name, "rb");
	if (f) {
		fseek(f, 0, SEEK_END);
		long size = ftell(f);
		fseek(f, 0, SEEK_SET);
		uint8_t *data = malloc(size);
		if (fread(data, 1, size, f) != (size_t)size)
			size = 0;
		fclose(f);
		wad = wad_align(data, size, &wad_size);
		free(data);
	}
	if (!wad)
		fprintf(stderr, "WAD %s not found or invalid (set DOOM_WAD).\n", name);
}

int hal_poll(void)
{
	static const struct { SDL_Keycode key; int button; } keymap[] = {
		{SDLK_x, HAL_BTN_A}, {SDLK_z, HAL_BTN_B}, {SDLK_DOWN, HAL_BTN_DOWN},
		{SDLK_LEFT, HAL_BTN_LEFT}, {SDLK_RIGHT, HAL_BTN_RIGHT}, {SDLK_UP, HAL_BTN_UP},
		{SDLK_BACKSPACE, HAL_BTN_SELECT}, {SDLK_RSHIFT, HAL_BTN_SELECT}, {SDLK_RETURN, HAL_BTN_START},
	};
	SDL_Event event;

	while (SDL_PollEvent(&event)) {
		if (event.type == SDL_QUIT)
			return 0;
		if (event.type == SDL_KEYDOWN || event.type == SDL_KEYUP) {
			if (event.key.keysym.sym == SDLK_ESCAPE)
				return 0;
			for (unsigned i = 0; i < sizeof(keymap)/sizeof(keymap[0]); i++)
				if (event.key.keysym.sym == keymap[i].key) {
					if (event.type == SDL_KEYDOWN)
						keys |= 1 << keymap[i].button;
					else
						keys &= ~(1 << keymap[i].button);
				}
		}
	}
	return 1;
}

uint32_t hal_ticks_ms(void)
{
	return SDL_GetTicks();
}

void hal_sleep_ms(uint32_t ms)
{
	SDL_Delay(ms);
}

uint32_t hal_buttons(void)
{
	uint32_t buttons = keys;

	if (pad) {
		static const struct { SDL_GameControllerButton button; int bit; } padmap[] = {
			{SDL_CONTROLLER_BUTTON_A, HAL_BTN_A}, {SDL_CONTROLLER_BUTTON_B, HAL_BTN_B},
			{SDL_CONTROLLER_BUTTON_DPAD_DOWN, HAL_BTN_DOWN}, {SDL_CONTROLLER_BUTTON_DPAD_LEFT, HAL_BTN_LEFT},
			{SDL_CONTROLLER_BUTTON_DPAD_RIGHT, HAL_BTN_RIGHT}, {SDL_CONTROLLER_BUTTON_DPAD_UP, HAL_BTN_UP},
			{SDL_CONTROLLER_BUTTON_BACK, HAL_BTN_SELECT}, {SDL_CONTROLLER_BUTTON_START, HAL_BTN_START},
		};
		for (unsigned i = 0; i < sizeof(padmap)/sizeof(padmap[0]); i++)
			if (SDL_GameControllerGetButton(pad, padmap[i].button))
				buttons |= 1 << padmap[i].bit;
	}
	return buttons;
}

void hal_lcd_palette(const uint16_t *rgb555)
{
	for (int i = 0; i < 256; i++) {
		uint32_t r = (rgb555[i] >>  0) & 0x1f;
		uint32_t g = (rgb555[i] >>  5) & 0x1f;
		uint32_t b = (rgb555[i] >> 10) & 0x1f;
		palette[i] = 0xff000000 | (r << 19 | (r >> 2) << 16) | (g << 11 | (g >> 2) << 8) | (b << 3 | b >> 2);
	}
}

void hal_lcd_line(int y, const uint8_t *pixels)
{
	memcpy(lcd[y], pixels, HAL_LCD_WIDTH);
}

void hal_lcd_present(void)
{
	uint32_t *pixels;
	int       pitch;

	SDL_LockTexture(texture, NULL, (void **)&pixels, &pitch);
	for (int y = 0; y < HAL_LCD_HEIGHT; y++)
		for (int x = 0; x < HAL_LCD_WIDTH; x++)
			pixels[y*(pitch/4) + x] = palette[lcd[y][x]];
	SDL_UnlockTexture(texture);
	SDL_RenderCopy(renderer, texture, NULL, NULL);
	SDL_RenderPresent(renderer);

	/* Snapshots. */
	const char *snap = getenv("DOOM_SNAP");
	frames++;
	while (snap && *snap) {
		if ((unsigned int)strtoul(snap, NULL, 10) == frames) {
			char name[64];
			SDL_Surface *surface = SDL_CreateRGBSurfaceWithFormat(0, HAL_LCD_WIDTH, HAL_LCD_HEIGHT, 32,
				SDL_PIXELFORMAT_ARGB8888);
			for (int y = 0; y < HAL_LCD_HEIGHT; y++)
				for (int x = 0; x < HAL_LCD_WIDTH; x++)
					((uint32_t *)surface->pixels)[y*(surface->pitch/4) + x] = palette[lcd[y][x]];
			snprintf(name, sizeof(name), "snap_%04u.bmp", frames);
			SDL_SaveBMP(surface, name);
			SDL_FreeSurface(surface);
		}
		snap = strchr(snap, ',');
		snap = snap ? snap + 1 : NULL;
	}
}

int hal_audio_free(void)
{
	/* Keep ~100ms queued. */
	if (!audio)
		return 0;
	int queued = SDL_GetQueuedAudioSize(audio)/4;
	return queued < HAL_AUDIO_RATE/10 ? HAL_AUDIO_RATE/10 - queued : 0;
}

void hal_audio_write(uint32_t sample)
{
	if (audio)
		SDL_QueueAudio(audio, &sample, 4);
}

const uint8_t *hal_wad(unsigned int *size)
{
	*size = wad_size;
	return wad;
}

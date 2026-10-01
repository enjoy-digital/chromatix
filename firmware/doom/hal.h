// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Doom port hardware abstraction: ChromatiX SoC (hal_litex.c) or PC/SDL emulation of the Chromatic
// (hal_sdl.c, to develop/check the port on the host).

#ifndef HAL_H
#define HAL_H

#include <stdint.h>

/* LCD: 160x144, 8-bit indexed pixels, 256 colors RGB555 palette ({B[4:0], G[4:0], R[4:0]}). */
#define HAL_LCD_WIDTH  160
#define HAL_LCD_HEIGHT 144

/* Buttons (1: pressed), ChromatiX ButtonsCSR order. */
enum {
	HAL_BTN_A = 0, HAL_BTN_B, HAL_BTN_DOWN, HAL_BTN_LEFT, HAL_BTN_RIGHT, HAL_BTN_UP, HAL_BTN_SELECT,
	HAL_BTN_START, HAL_BTN_MENU,
	HAL_BTN_COUNT
};

/* Audio: stereo signed 16-bit samples ({right, left}) at HAL_AUDIO_RATE. */
#define HAL_AUDIO_RATE 11025

void           hal_init(void);
int            hal_poll(void);                              /* 0: quit requested (host). */
uint32_t       hal_ticks_ms(void);
void           hal_sleep_ms(uint32_t ms);
uint32_t       hal_buttons(void);
void           hal_lcd_palette(const uint16_t *rgb555);
void           hal_lcd_line(int y, const uint8_t *pixels); /* HAL_LCD_WIDTH pixels. */
void           hal_lcd_present(void);
void           hal_audio_start(void (*fill)(void));         /* Fill called when samples are needed. */
int            hal_audio_free(void);                        /* Samples that can be queued. */
void           hal_audio_write(uint32_t sample);
const uint8_t *hal_wad(unsigned int *size);                 /* WAD in memory (NULL: none). */

#endif

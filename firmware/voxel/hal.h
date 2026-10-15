// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Voxel game hardware abstraction: ChromatiX SoC (hal_litex.c, --with-app/--with-voxel) or PC/SDL
// emulation of the Chromatic (hal_sdl.c, to develop the game on the host).

#ifndef HAL_H
#define HAL_H

#include <stdint.h>

#include "vr.h"

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

void        hal_init(void);
int         hal_poll(void);                       /* 0: quit requested (host). */
uint32_t    hal_ticks_ms(void);
uint32_t    hal_buttons(void);
void        hal_lcd_palette(const uint16_t *rgb555);
void        hal_lcd_line(int y, const uint8_t *pixels); /* HAL_LCD_WIDTH pixels. */
void        hal_lcd_present(void);
void        hal_audio_start(void (*fill)(void));  /* Fill called when samples are needed. */
int         hal_audio_free(void);                 /* Samples that can be queued. */
void        hal_audio_write(uint32_t sample);

/* 3D view (VR_HEIGHT rows from the LCD top): VoxelRenderer gateware or software (vr.c). The world
   window/textures/blocks are written to shadow copies, then pushed to the hardware RAMs. */
uint32_t   *hal_vr_world(void);                   /* VR_WORLD_WORDS. */
void        hal_vr_world_word(unsigned word);     /* World word changed. */
uint32_t   *hal_vr_textures(void);                /* VR_TEXTURE_WORDS. */
uint16_t   *hal_vr_blocks(void);                  /* VR_BLOCKS. */
void        hal_vr_upload(void);                  /* Textures/blocks changed. */
void        hal_vr_render(const struct vr_camera *cam);
int         hal_vr_hardware(void);                /* 1: VoxelRenderer gateware. */
uint32_t    hal_vr_cycles(void);                  /* Last hardware render duration (cycles). */

/* Host interface (SoC: host block in the main RAM, see layout.h). */
const char *hal_args(void);                       /* Extra arguments (NULL: none). */
void        hal_frame(void);                      /* Frame rendered (counter). */
void        hal_profile(int enable);              /* PC sampling profiler (1kHz). */

#endif

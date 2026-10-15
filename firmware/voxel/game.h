// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Voxel game: blocks, world (256x256x32, wrapping on X/Z), renderer window, art, sound.

#ifndef GAME_H
#define GAME_H

#include <stdint.h>

#include "vr.h"

/* Blocks (4-bit, renderer block types). */
enum {
	BLOCK_AIR = 0, BLOCK_STONE, BLOCK_DIRT, BLOCK_GRASS, BLOCK_SAND, BLOCK_LOG, BLOCK_LEAVES,
	BLOCK_PLANKS, BLOCK_COBBLE, BLOCK_WATER, BLOCK_GLASS, BLOCK_BRICK, BLOCK_BEDROCK, BLOCK_SNOW,
	BLOCK_GOLD, BLOCK_SNOWY_GRASS,
	BLOCK_COUNT
};

/* Palette: 32 colors x 8 shades (index (color << 3) | shade), color 0: transparent, 31: sky. */
enum {
	COLOR_WHITE = 28, COLOR_BLACK = 29, COLOR_GRAY = 30, COLOR_SKY = 31,
};

#define WORLD_SIZE   256 /* X/Z (cells, wrapping). */
#define WORLD_HEIGHT VR_WINDOW
#define WATER_LEVEL  11

/* Art (art.c): palette, textures, block textures. */
void     art_init(void);
void     art_palette(int daylight); /* 0 (night) - 256 (day). */
unsigned art_texel(unsigned texture, unsigned u, unsigned v);
unsigned art_block_texture(unsigned block, int face); /* face: 0 top, 1 side, 2 bottom. */

/* World (world.c). */
void     world_generate(uint32_t seed);
unsigned world_get(int x, int y, int z);   /* X/Z wrap, Y out of range: air (above), bedrock. */
void     world_set(int x, int y, int z, unsigned block);
int      world_height(int x, int z);       /* Highest non-air cell. */
int      block_solid(unsigned block);      /* Collisions. */
void     window_update(int ox, int oz);   /* Renderer window origin (world cells). */
int      window_ox(void);
int      window_oz(void);

/* Sound (sound.c). */
enum { SOUND_BREAK, SOUND_PLACE, SOUND_STEP, SOUND_JUMP };
void     sound_init(void);
void     sound_play(int sound);

#endif

// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Augustin game: the name in giant bricks to destroy (voxel game engine: VoxelRenderer, world
// 256x256x32 wrapping on X/Z), blocks, art, font, sound.

#ifndef GAME_H
#define GAME_H

#include <stdint.h>

#include "vr.h"

/* Blocks (4-bit, renderer block types). */
enum {
	BLOCK_AIR = 0, BLOCK_GRASS, BLOCK_DIRT, BLOCK_LOG, BLOCK_LEAVES,
	BLOCK_LETTER0, /* 8 letter colors: BLOCK_LETTER0 + 0-7. */
	BLOCK_TNT = BLOCK_LETTER0 + 8, BLOCK_SPARKLE, BLOCK_GOLD,
	BLOCK_COUNT
};
#define LETTERS 8

/* World cells: block (bits 3:0) and surprise (bits 7:4, letter bricks). */
enum { SURPRISE_NONE = 0, SURPRISE_BOING, SURPRISE_PROUT };

/* Palette: 32 colors x 8 shades (index (color << 3) | shade), color 0: transparent, 31: sky. */
enum {
	COLOR_LETTER0 = 9, /* 8 letter colors. */
	COLOR_MORTAR  = 17, COLOR_TNT_RED, COLOR_TNT_WHITE, COLOR_GOLD, COLOR_GOLD_DARK, COLOR_SKIN,
	COLOR_STEEL, COLOR_DARK, COLOR_BROWN, COLOR_BANNER,
	COLOR_WHITE = 28, COLOR_BLACK = 29, COLOR_GRAY = 30, COLOR_SKY = 31,
};
#define PIXEL(color) ((uint8_t)((color) << 3)) /* Brightest shade. */

#define WORLD_SIZE   256 /* X/Z (cells, wrapping). */
#define WORLD_HEIGHT VR_WINDOW
#define GROUND       4   /* Arena grass level. */

/* Art (art.c): palette, textures, block textures. */
void     art_init(void);
void     art_palette(int flash);           /* flash: TNT red -> white. */
unsigned art_texel(unsigned texture, unsigned u, unsigned v);
unsigned art_block_texture(unsigned block, int face); /* face: 0 top, 1 side, 2 bottom. */

/* Font (font.c): 5x7 glyphs (digits, some capitals, '!', ':', ' '). */
#define FONT_W 5
#define FONT_H 7
unsigned font_row(char c, int row);        /* Bits 4:0, bit 4: left column. */
void     font_text(uint8_t *line, int x, int row, const char *text, int scale, const uint8_t *colors);
int      font_width(const char *text, int scale);

/* World (world.c). */
void     world_generate(uint32_t seed);
unsigned world_get(int x, int y, int z);   /* Block. X/Z wrap, Y out of range: air (above), dirt. */
unsigned world_raw(int x, int y, int z);   /* Block and surprise. */
void     world_set(int x, int y, int z, unsigned cell);
int      world_height(int x, int z);       /* Highest non-air cell. */
int      block_solid(unsigned block);      /* Collisions. */
int      block_breakable(unsigned block);  /* Letters, gold. */
void     window_update(int ox, int oz);    /* Renderer window origin (world cells). */
int      window_ox(void);
int      window_oz(void);

/* Sound (sound.c). */
enum {
	SOUND_BREAK, SOUND_STEP, SOUND_JUMP, SOUND_PLOP, SOUND_THUD, SOUND_BOOM, SOUND_HAMMER,
	SOUND_BOING, SOUND_PROUT, SOUND_DING, SOUND_TICK, SOUND_BONK, SOUND_TOK, SOUND_SWITCH,
	SOUND_FANFARE,
};
void     sound_init(void);
void     sound_play(int sound);

#endif

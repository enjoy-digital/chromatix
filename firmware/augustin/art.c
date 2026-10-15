// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Augustin game art, all procedural (no assets): 32 colors x 8 shades palette (faces/fog/sky), 16x16
// block textures (colored letter bricks, TNT, gold, rainbow sparkles), block faces -> textures.

#include <stdint.h>

#include "hal.h"
#include "game.h"

enum {
	TEX_GRASS_TOP = 0, TEX_GRASS_SIDE, TEX_DIRT, TEX_LOG, TEX_LEAVES,
	TEX_LETTER0, /* 8 letter bricks. */
	TEX_TNT = TEX_LETTER0 + LETTERS, TEX_SPARKLE, TEX_GOLD,
};

/* Colors (RGB888), 1-30 (0: transparent, 31: sky). */
static const uint8_t colors[32][3] = {
	{  0,   0,   0},
	/* Dirt, grass, log, leaves (light, dark). */
	{134,  96,  67}, {108,  77,  52}, {105, 175,  58}, { 80, 140,  44},
	{120,  88,  54}, { 86,  62,  36}, { 64, 140,  44}, { 44, 104,  30},
	/* Letters: red, orange, yellow, green, cyan, blue, purple, pink. */
	{232,  48,  48}, {250, 140,  30}, {250, 222,  40}, { 60, 200,  70},
	{ 40, 210, 220}, { 50,  90, 235}, {160,  70, 220}, {250, 110, 190},
	/* Mortar, TNT red/white, gold, gold dark, skin, steel, dark, brown, banner. */
	{236, 228, 214}, {210,  30,  30}, {245, 245, 245}, {255, 214,  40}, {200, 150,  20},
	{246, 196, 150}, {170, 176, 186}, { 56,  56,  64}, {130,  84,  44}, { 70,  30, 110},
	/* Unused, UI: white, black, gray (unshaded); sky. */
	{  0,   0,   0}, {255, 255, 255}, {  0,   0,   0}, { 88,  88,  88}, {  0,   0,   0},
};

/* Block -> textures (top, side, bottom). */
static uint8_t block_textures[BLOCK_COUNT][3] = {
	[BLOCK_AIR]     = {TEX_DIRT,      TEX_DIRT,       TEX_DIRT},
	[BLOCK_GRASS]   = {TEX_GRASS_TOP, TEX_GRASS_SIDE, TEX_DIRT},
	[BLOCK_DIRT]    = {TEX_DIRT,      TEX_DIRT,       TEX_DIRT},
	[BLOCK_LOG]     = {TEX_LOG,       TEX_LOG,        TEX_LOG},
	[BLOCK_LEAVES]  = {TEX_LEAVES,    TEX_LEAVES,     TEX_LEAVES},
	[BLOCK_TNT]     = {TEX_TNT,       TEX_TNT,        TEX_TNT},
	[BLOCK_SPARKLE] = {TEX_SPARKLE,   TEX_SPARKLE,    TEX_SPARKLE},
	[BLOCK_GOLD]    = {TEX_GOLD,      TEX_GOLD,       TEX_GOLD},
};

static uint32_t hash(uint32_t x)
{
	x ^= x >> 16; x *= 0x7feb352d;
	x ^= x >> 15; x *= 0x846ca68b;
	x ^= x >> 16;
	return x;
}

/* "TNT" in 3x5 glyphs, centered on the TNT band (u 2-12, v 5-9). */
static int tnt_text(unsigned u, unsigned v)
{
	static const uint8_t t[5] = {7, 2, 2, 2, 2}, n[5] = {5, 7, 7, 7, 5};
	if (v < 6 || v > 10 || u < 2 || u > 12 || (u - 2) % 4 == 3)
		return 0;
	const uint8_t *g = (u - 2)/4 == 1 ? n : t;
	return (g[v - 6] >> (2 - (u - 2) % 4)) & 1;
}

/* Texel color of a texture at (u, v), v = 0: top. */
static unsigned texel_gen(unsigned t, unsigned u, unsigned v)
{
	uint32_t h = hash(t*7919 + v*16 + u);
	unsigned n = h & 0xff; /* Noise. */

	if (t >= TEX_LETTER0 && t < TEX_LETTER0 + LETTERS) {
		/* Bricks: 2 rows of 8x8, offset by 4, mortar lines, a lighter top edge per brick. */
		unsigned row = v/8, off = (row & 1)*4;
		if (v % 8 == 7 || (u + off) % 8 == 7)
			return COLOR_MORTAR;
		return COLOR_LETTER0 + t - TEX_LETTER0;
	}
	switch (t) {
	case TEX_GRASS_TOP:  return n < 96 ? 4 : 3;
	case TEX_GRASS_SIDE: {
		unsigned edge = 3 + (hash(u + 100) & 1) + (hash(u*3 + 7) & 1);
		if (v < edge)
			return n < 96 ? 4 : 3;
		return n < 80 ? 2 : 1;
	}
	case TEX_DIRT:       return n < 80 ? 2 : 1;
	case TEX_LOG:        return ((u + (hash(v/4 + u*13) & 1)) % 4 == 0 || n < 40) ? 6 : 5;
	case TEX_LEAVES:     return n < 56 ? 0 : n < 140 ? 8 : 7;
	case TEX_TNT:
		if (v >= 4 && v <= 12)
			return tnt_text(u, v) ? COLOR_BLACK : COLOR_TNT_WHITE;
		return (u % 4 == 3) ? COLOR_DARK : COLOR_TNT_RED;
	case TEX_SPARKLE:
		/* Rainbow diagonal stripes, holes. */
		if (n < 90)
			return 0;
		return COLOR_LETTER0 + ((u + v)/2) % LETTERS;
	case TEX_GOLD:
		if (u == 0 || u == 15 || v == 0 || v == 15)
			return COLOR_GOLD_DARK;
		if ((u == v || u + v == 15) && u > 3 && u < 12 && (u == 7 || u == 8 || n < 120))
			return COLOR_WHITE;
		return n < 70 ? COLOR_GOLD_DARK : COLOR_GOLD;
	}
	return 1;
}

unsigned art_texel(unsigned texture, unsigned u, unsigned v)
{
	unsigned addr = (texture << 8) | (v << 4) | u;
	return (hal_vr_textures()[addr >> 2] >> (8*(addr & 3))) & 0xff;
}

unsigned art_block_texture(unsigned block, int face)
{
	return block_textures[block][face];
}

void art_init(void)
{
	uint32_t *textures = hal_vr_textures();
	uint16_t *blocks   = hal_vr_blocks();

	for (int i = 0; i < LETTERS; i++)
		for (int f = 0; f < 3; f++)
			block_textures[BLOCK_LETTER0 + i][f] = TEX_LETTER0 + i;
	for (unsigned addr = 0; addr < VR_TEXTURES*256; addr += 4) {
		uint32_t word = 0;
		for (unsigned b = 0; b < 4; b++) {
			unsigned a = addr + b;
			word |= (uint32_t)texel_gen(a >> 8, a & 15, (a >> 4) & 15) << (8*b);
		}
		textures[addr >> 2] = word;
	}
	for (int b = 0; b < BLOCK_COUNT; b++)
		blocks[b] = block_textures[b][0] | block_textures[b][1] << 4 | block_textures[b][2] << 8;
	hal_vr_upload();
	art_palette(0);
}

static unsigned mix(unsigned a, unsigned b, unsigned w) /* w: 0 (a) - 256 (b). */
{
	return (a*(256 - w) + b*w) >> 8;
}

static uint16_t rgb555(unsigned r, unsigned g, unsigned b)
{
	return (r >> 3) | (g >> 3) << 5 | (b >> 3) << 10;
}

void art_palette(int flash)
{
	static const uint8_t zenith[3] = { 92, 140, 236}, horizon[3] = {188, 220, 250};
	uint16_t palette[256];

	for (int c = 0; c < 32; c++)
		for (int s = 0; s < 8; s++) {
			unsigned rgb[3];
			for (int i = 0; i < 3; i++) {
				unsigned v = colors[flash && c == COLOR_TNT_RED ? COLOR_TNT_WHITE : c][i];
				if (c == COLOR_SKY) {
					/* Sky: zenith (0) to horizon (4-7: fog color). */
					rgb[i] = mix(zenith[i], horizon[i], s >= 4 ? 256 : s*64);
				} else if (c >= COLOR_WHITE) {
					rgb[i] = v; /* UI. */
				} else {
					/* Faces darker with the shade (face levels 0-3), then fog toward the horizon color. */
					unsigned dim = s < 4 ? 256 - s*28 : 256 - 3*28;
					unsigned fog = s <= 3 ? 0 : (s - 3)*64;
					rgb[i] = mix(v*dim >> 8, horizon[i], fog > 256 ? 256 : fog);
				}
			}
			palette[(c << 3) | s] = rgb555(rgb[0], rgb[1], rgb[2]);
		}
	hal_lcd_palette(palette);
}

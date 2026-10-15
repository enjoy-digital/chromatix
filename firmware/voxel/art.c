// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Voxel game art, all procedural (no assets): 32 colors x 8 shades palette (faces/fog/sky, day/night),
// 16x16 block textures, block faces -> textures.

#include <stdint.h>

#include "hal.h"
#include "game.h"

enum {
	TEX_STONE = 0, TEX_DIRT, TEX_GRASS_TOP, TEX_GRASS_SIDE, TEX_SAND, TEX_LOG_SIDE, TEX_LOG_TOP,
	TEX_LEAVES, TEX_PLANKS, TEX_COBBLE, TEX_WATER, TEX_GLASS, TEX_BRICK, TEX_BEDROCK, TEX_SNOW,
	TEX_GOLD,
};

/* Colors (RGB888), 1-30 (0: transparent, 31: sky). */
static const uint8_t colors[32][3] = {
	{  0,   0,   0}, {128, 128, 128}, {100, 100, 100}, {134,  96,  67}, {108,  77,  52},
	{ 95, 159,  53}, { 72, 128,  40}, {219, 207, 163}, {194, 180, 136}, {104,  82,  50},
	{ 76,  60,  36}, {170, 138,  86}, { 58, 128,  38}, { 40,  96,  28}, {182, 146,  92},
	{140, 108,  64}, {118, 118, 118}, { 72,  72,  72}, { 48,  88, 200}, { 72, 116, 224},
	{205, 232, 242}, {152,  70,  54}, {176, 170, 160}, { 64,  64,  64}, { 36,  36,  36},
	{240, 245, 250}, {212, 222, 234}, {244, 204,  64}, {255, 255, 255}, {  0,   0,   0},
	{ 88,  88,  88}, {  0,   0,   0},
};

/* Block -> textures (top, side, bottom). */
static const uint8_t block_textures[BLOCK_COUNT][3] = {
	[BLOCK_AIR]         = {TEX_STONE,     TEX_STONE,      TEX_STONE},
	[BLOCK_STONE]       = {TEX_STONE,     TEX_STONE,      TEX_STONE},
	[BLOCK_DIRT]        = {TEX_DIRT,      TEX_DIRT,       TEX_DIRT},
	[BLOCK_GRASS]       = {TEX_GRASS_TOP, TEX_GRASS_SIDE, TEX_DIRT},
	[BLOCK_SAND]        = {TEX_SAND,      TEX_SAND,       TEX_SAND},
	[BLOCK_LOG]         = {TEX_LOG_TOP,   TEX_LOG_SIDE,   TEX_LOG_TOP},
	[BLOCK_LEAVES]      = {TEX_LEAVES,    TEX_LEAVES,     TEX_LEAVES},
	[BLOCK_PLANKS]      = {TEX_PLANKS,    TEX_PLANKS,     TEX_PLANKS},
	[BLOCK_COBBLE]      = {TEX_COBBLE,    TEX_COBBLE,     TEX_COBBLE},
	[BLOCK_WATER]       = {TEX_WATER,     TEX_WATER,      TEX_WATER},
	[BLOCK_GLASS]       = {TEX_GLASS,     TEX_GLASS,      TEX_GLASS},
	[BLOCK_BRICK]       = {TEX_BRICK,     TEX_BRICK,      TEX_BRICK},
	[BLOCK_BEDROCK]     = {TEX_BEDROCK,   TEX_BEDROCK,    TEX_BEDROCK},
	[BLOCK_SNOW]        = {TEX_SNOW,      TEX_SNOW,       TEX_SNOW},
	[BLOCK_GOLD]        = {TEX_GOLD,      TEX_GOLD,       TEX_GOLD},
	[BLOCK_SNOWY_GRASS] = {TEX_SNOW,      TEX_GRASS_SIDE, TEX_DIRT},
};

static uint32_t hash(uint32_t x)
{
	x ^= x >> 16; x *= 0x7feb352d;
	x ^= x >> 15; x *= 0x846ca68b;
	x ^= x >> 16;
	return x;
}

/* Texel color of a texture at (u, v), v = 0: top. */
static unsigned texel_gen(unsigned t, unsigned u, unsigned v)
{
	uint32_t h = hash(t*7919 + v*16 + u);
	unsigned n = h & 0xff; /* Noise. */

	switch (t) {
	case TEX_STONE:      return n < 96 ? 2 : 1;
	case TEX_DIRT:       return n < 80 ? 4 : 3;
	case TEX_GRASS_TOP:  return n < 96 ? 6 : 5;
	case TEX_GRASS_SIDE: {
		unsigned edge = 3 + (hash(u + 100) & 1) + (hash(u*3 + 7) & 1);
		if (v < edge)
			return n < 96 ? 6 : 5;
		return n < 80 ? 4 : 3;
	}
	case TEX_SAND:       return n < 64 ? 8 : 7;
	case TEX_LOG_SIDE:   return ((u + (hash(v/4 + u*13) & 1)) % 4 == 0 || n < 40) ? 10 : 9;
	case TEX_LOG_TOP: {
		int du = (int)u*2 - 15, dv = (int)v*2 - 15;
		unsigned r = (unsigned)(du*du + dv*dv);
		if (u == 0 || u == 15 || v == 0 || v == 15)
			return 9;
		return (r/40) % 2 ? 11 : 15;
	}
	case TEX_LEAVES:     return n < 56 ? 0 : n < 140 ? 13 : 12;
	case TEX_PLANKS:
		if (v % 4 == 3)
			return 15;
		if (u == ((v/4)*5 + 3) % 16)
			return 15;
		return n < 40 ? 15 : 14;
	case TEX_COBBLE: {
		/* Stones: cells of a jittered grid, mortar on cell edges. */
		unsigned cu = (u + (hash(v/5 + 31) & 3))%16, cv = (v + (hash(u/5 + 17) & 3))%16;
		if (cu % 5 == 0 || cv % 5 == 0)
			return 17;
		return n < 100 ? 16 : 1;
	}
	case TEX_WATER:      return ((u + v*3 + (n >> 6)) % 7) < 2 ? 19 : 18;
	case TEX_GLASS:
		if (u == 0 || u == 15 || v == 0 || v == 15)
			return 20;
		return (u == v + 3 && u > 5 && u < 11) ? 20 : 0;
	case TEX_BRICK: {
		unsigned row = v/4, off = (row & 1)*4;
		if (v % 4 == 3 || (u + off) % 8 == 7)
			return 22;
		return n < 48 ? 4 : 21;
	}
	case TEX_BEDROCK:    return n < 128 ? 24 : 23;
	case TEX_SNOW:       return n < 64 ? 26 : 25;
	case TEX_GOLD:       return (hash(t + (u/3)*5 + (v/3)*31) & 7) == 0 && n < 200 ? 27 : (n < 96 ? 2 : 1);
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
	art_palette(256);
}

static unsigned mix(unsigned a, unsigned b, unsigned w) /* w: 0 (a) - 256 (b). */
{
	return (a*(256 - w) + b*w) >> 8;
}

static uint16_t rgb555(unsigned r, unsigned g, unsigned b)
{
	return (r >> 3) | (g >> 3) << 5 | (b >> 3) << 10;
}

void art_palette(int daylight)
{
	static const uint8_t zenith_day[3]  = { 92, 140, 236}, horizon_day[3]   = {178, 212, 248};
	static const uint8_t zenith_night[3] = { 10,  14,  40}, horizon_night[3] = { 40,  52,  96};
	uint16_t palette[256];
	unsigned zenith[3], horizon[3];
	/* Blocks brightness: night ~1/2, moonlight blue. */
	unsigned light   = 120 + daylight*136/256;
	unsigned light_b = light + (256 - light)/3;

	for (int i = 0; i < 3; i++) {
		zenith[i]  = mix(zenith_night[i],  zenith_day[i],  daylight);
		horizon[i] = mix(horizon_night[i], horizon_day[i], daylight);
	}
	for (int c = 0; c < 32; c++)
		for (int s = 0; s < 8; s++) {
			unsigned rgb[3];
			for (int i = 0; i < 3; i++) {
				if (c == COLOR_SKY) {
					/* Sky: zenith (0) to horizon (4-7: fog color). */
					rgb[i] = mix(zenith[i], horizon[i], s >= 4 ? 256 : s*64);
				} else if (c >= COLOR_WHITE) {
					rgb[i] = colors[c][i]; /* UI. */
				} else {
					/* Faces darker with the shade (face levels 0-3), then fog toward the horizon color. */
					unsigned v   = colors[c][i]*(i == 2 ? light_b : light) >> 8;
					unsigned dim = s < 4 ? 256 - s*28 : 256 - 3*28;
					unsigned fog = s <= 3 ? 0 : (s - 3)*64;
					rgb[i] = mix(v*dim >> 8, horizon[i], fog > 256 ? 256 : fog);
				}
			}
			palette[(c << 3) | s] = rgb555(rgb[0], rgb[1], rgb[2]);
		}
	hal_lcd_palette(palette);
}

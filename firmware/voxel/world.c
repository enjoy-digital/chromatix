// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Voxel game world: 256x256x32 cells (1 byte, columns of 32 contiguous cells, wrapping on X/Z),
// generated from value noise (hills, mountains with snow, water, beaches, trees, ores), and the
// renderer window (32x32x32, toroidal: only the columns entering the window are written).

#include <stdint.h>

#include "hal.h"
#include "game.h"

static uint8_t world[WORLD_SIZE*WORLD_SIZE*WORLD_HEIGHT];
static int     win_ox, win_oz, win_valid;

static inline uint8_t *column(int x, int z)
{
	return &world[(((z & (WORLD_SIZE - 1)) << 8) | (x & (WORLD_SIZE - 1)))*WORLD_HEIGHT];
}

static uint32_t hash3(uint32_t x, uint32_t y, uint32_t seed)
{
	uint32_t h = x*0x8da6b343 ^ y*0xd8163841 ^ seed*0xcb1ab31f;
	h ^= h >> 15; h *= 0x2c1b3c6d;
	h ^= h >> 12; h *= 0x297a2d39;
	h ^= h >> 15;
	return h;
}

/* Value noise (0-255), lattice 2^shift cells, wrapping with the world. */
static int noise(int x, int z, int shift, uint32_t seed)
{
	int      size = WORLD_SIZE >> shift, mask = size - 1;
	int      ix = x >> shift, iz = z >> shift;
	int      fx = x & ((1 << shift) - 1), fz = z & ((1 << shift) - 1);
	uint32_t a = hash3(ix & mask, iz & mask, seed) & 0xff, b = hash3((ix + 1) & mask, iz & mask, seed) & 0xff;
	uint32_t c = hash3(ix & mask, (iz + 1) & mask, seed) & 0xff, d = hash3((ix + 1) & mask, (iz + 1) & mask, seed) & 0xff;

	/* Smoothstep weights (Q8). */
	int one = 1 << shift;
	int wx = (fx << 8)/one, wz = (fz << 8)/one;
	wx = (wx*wx*(768 - 2*wx)) >> 16;
	wz = (wz*wz*(768 - 2*wz)) >> 16;
	int top    = a*(256 - wx) + b*wx;
	int bottom = c*(256 - wx) + d*wx;
	return (top*(256 - wz) + bottom*wz) >> 16;
}

static int terrain_height(int x, int z, uint32_t seed)
{
	int base     = noise(x, z, 6, seed);       /* Continents (64 cells). */
	int hills    = noise(x, z, 4, seed + 1);   /* Hills (16 cells). */
	int detail   = noise(x, z, 2, seed + 2);   /* Bumps (4 cells). */
	int mountain = noise(x, z, 5, seed + 3);   /* Mountains (32 cells), only the highest part. */
	int h = 4 + base*12/256 + hills*7/256 + detail*2/256;
	if (mountain > 150)
		h += (mountain - 150)*12/105;
	return h < 2 ? 2 : h > WORLD_HEIGHT - 6 ? WORLD_HEIGHT - 6 : h;
}

static void tree(int x, int y, int z, uint32_t h)
{
	int trunk = 4 + (h & 1);

	for (int dy = trunk - 2; dy <= trunk + 1; dy++) {
		int r = dy >= trunk ? 1 : 2;
		for (int dz = -r; dz <= r; dz++)
			for (int dx = -r; dx <= r; dx++) {
				if (r == 2 && (dx == -2 || dx == 2) && (dz == -2 || dz == 2) && ((h >> (dx + dz + 8)) & 1))
					continue; /* Some corners removed. */
				if (dy == trunk + 1 && dx != 0 && dz != 0)
					continue;
				if (world_get(x + dx, y + dy, z + dz) == BLOCK_AIR)
					world_set(x + dx, y + dy, z + dz, BLOCK_LEAVES);
			}
	}
	for (int dy = 0; dy < trunk; dy++)
		world_set(x, y + dy, z, BLOCK_LOG);
}

void world_generate(uint32_t seed)
{
	for (int z = 0; z < WORLD_SIZE; z++)
		for (int x = 0; x < WORLD_SIZE; x++) {
			uint8_t *col = column(x, z);
			int      h   = terrain_height(x, z, seed);
			for (int y = 0; y < WORLD_HEIGHT; y++) {
				uint8_t b = BLOCK_AIR;
				if (y == 0)
					b = BLOCK_BEDROCK;
				else if (y < h - 3)
					b = (hash3(x*32 + y, z, seed + 4) % 97) == 0 ? BLOCK_GOLD : BLOCK_STONE;
				else if (y < h)
					b = h <= WATER_LEVEL + 1 ? BLOCK_SAND : BLOCK_DIRT;
				else if (y == h)
					b = h <= WATER_LEVEL + 1 ? BLOCK_SAND : h >= 22 ? BLOCK_SNOWY_GRASS : BLOCK_GRASS;
				else if (y <= WATER_LEVEL)
					b = BLOCK_WATER;
				col[y] = b;
			}
		}
	/* Trees on grass (not too close to each other: one candidate per 5x5 cells). */
	for (int z = 0; z < WORLD_SIZE; z += 5)
		for (int x = 0; x < WORLD_SIZE; x += 5) {
			uint32_t h  = hash3(x, z, seed + 5);
			int      tx = x + (h & 3), tz = z + ((h >> 2) & 3);
			int      ty = world_height(tx, tz);
			if ((h >> 8) % 100 < 22 && world_get(tx, ty, tz) == BLOCK_GRASS && ty < WORLD_HEIGHT - 8)
				tree(tx, ty + 1, tz, h >> 16);
		}
	win_valid = 0;
}

unsigned world_get(int x, int y, int z)
{
	if (y < 0)
		return BLOCK_BEDROCK;
	if (y >= WORLD_HEIGHT)
		return BLOCK_AIR;
	return column(x, z)[y];
}

int world_height(int x, int z)
{
	uint8_t *col = column(x, z);

	for (int y = WORLD_HEIGHT - 1; y > 0; y--)
		if (col[y] != BLOCK_AIR)
			return y;
	return 0;
}

int block_solid(unsigned block)
{
	return block != BLOCK_AIR && block != BLOCK_WATER;
}

/* Renderer window ------------------------------------------------------------------------------ */

static int in_window(int x, int z)
{
	return win_valid && ((x - win_ox) & (WORLD_SIZE - 1)) < VR_WINDOW && ((z - win_oz) & (WORLD_SIZE - 1)) < VR_WINDOW;
}

static void window_set(uint32_t *win, int x, int y, int z, unsigned block)
{
	vr_voxel_set(win, x, y, z, block);
	hal_vr_world_word(((unsigned)y << 7) | ((unsigned)(z & 31) << 2) | ((unsigned)(x & 31) >> 3));
}

void world_set(int x, int y, int z, unsigned block)
{
	if (y < 0 || y >= WORLD_HEIGHT)
		return;
	column(x, z)[y] = block;
	if (in_window(x, z))
		window_set(hal_vr_world(), x, y, z, block);
}

static void window_column(uint32_t *win, int x, int z)
{
	const uint8_t *col = column(x, z);

	for (int y = 0; y < WORLD_HEIGHT; y++)
		window_set(win, x, y, z, col[y]);
}

void window_update(int ox, int oz)
{
	uint32_t *win = hal_vr_world();

	ox &= WORLD_SIZE - 1;
	oz &= WORLD_SIZE - 1;
	if (win_valid && ox == win_ox && oz == win_oz)
		return;
	/* Columns entering the window (all of them on the first update or a large move). */
	for (int dz = 0; dz < VR_WINDOW; dz++)
		for (int dx = 0; dx < VR_WINDOW; dx++) {
			int x = ox + dx, z = oz + dz;
			if (!in_window(x, z))
				window_column(win, x, z);
		}
	win_ox    = ox;
	win_oz    = oz;
	win_valid = 1;
}

int window_ox(void)
{
	return win_ox;
}

int window_oz(void)
{
	return win_oz;
}

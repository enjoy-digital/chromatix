// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Augustin game: AUGUSTIN in giant colored bricks (AUGU on a log shelf, STIN below) to destroy with
// a punch, a big hammer or TNT. Unsupported bricks fall, some bricks hide surprises (boing, prout),
// gold bricks are bonuses; when every letter brick is gone: fanfare, confetti, "BRAVO AUGUSTIN !"
// and the time, then the name rebuilds itself. Voxel game engine (VoxelRenderer bitstream,
// --with-voxel, or the software renderer).
//
// Arguments: seed=n (world), win (test: all bricks break after the intro).
//
// Controls: D-pad: walk/turn, Select + D-pad: look up/down/strafe, B: use the tool (held: repeat),
// A: next tool, Start: jump (fly: up, Select + Start: down), Start twice: fly on/off.

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#include "hal.h"
#include "game.h"

/* Fixed point: positions Q8 (cells), angles 1024 per turn, vectors Q14. */
#define ANGLES       1024
#define PLAYER_HW    77   /* Half width (0.3). */
#define PLAYER_H     461  /* Height (1.8). */
#define PLAYER_EYE   410  /* Eye height (1.6). */
#define WALK_SPEED   1300 /* Q8/s. */
#define FLY_SPEED    2000
#define TURN_SPEED   420  /* Angles/s. */
#define LOOK_SPEED   280
#define PITCH_MAX    220
#define GRAVITY      5120 /* Q8/s^2. */
#define JUMP_SPEED   1850
#define FALL_MAX     7680
#define REACH        (12*256)
#define PIXEL_SCALE  143  /* tan(35 deg)/80 (Q14): 70 deg horizontal field of view. */

static int16_t sin_table[ANGLES];

static int isin(int a) { return sin_table[a & (ANGLES - 1)]; }
static int icos(int a) { return sin_table[(a + ANGLES/4) & (ANGLES - 1)]; }

static void trig_init(void)
{
	/* Taylor series (init only). */
	for (int i = 0; i < ANGLES; i++) {
		double x = (double)(i % (ANGLES/2))*6.283185307179586/ANGLES, s, term;
		if (x > 3.141592653589793/2)
			x = 3.141592653589793 - x;
		s = term = x;
		for (int n = 1; n < 8; n++) {
			term *= -x*x/((2*n)*(2*n + 1));
			s    += term;
		}
		sin_table[i] = (int16_t)((i < ANGLES/2 ? 1 : -1)*(int)(s*16384 + 0.5));
	}
}

static uint32_t rng = 0x2545f491;

static uint32_t rnd(void)
{
	rng ^= rng << 13; rng ^= rng >> 17; rng ^= rng << 5;
	return rng;
}

/* Player ---------------------------------------------------------------------------------------- */

static struct {
	int32_t x, y, z;     /* Feet (Q8). */
	int32_t vy;          /* Q8/s. */
	int     yaw, pitch;
	int     on_ground, fly;
	int     walked;      /* Q8, steps sounds. */
} player;

static int collides(int32_t x, int32_t y, int32_t z)
{
	int x0 = (x - PLAYER_HW) >> 8, x1 = (x + PLAYER_HW - 1) >> 8;
	int y0 = y >> 8,               y1 = (y + PLAYER_H - 1) >> 8;
	int z0 = (z - PLAYER_HW) >> 8, z1 = (z + PLAYER_HW - 1) >> 8;

	for (int cy = y0; cy <= y1; cy++)
		for (int cz = z0; cz <= z1; cz++)
			for (int cx = x0; cx <= x1; cx++)
				if (block_solid(world_get(cx, cy, cz)))
					return 1;
	return 0;
}

/* Cell inside the player's box. */
static int player_inside(int x, int y, int z)
{
	int x0 = (player.x - PLAYER_HW) >> 8, x1 = (player.x + PLAYER_HW - 1) >> 8;
	int z0 = (player.z - PLAYER_HW) >> 8, z1 = (player.z + PLAYER_HW - 1) >> 8;
	int y0 = player.y >> 8,               y1 = (player.y + PLAYER_H - 1) >> 8;

	return ((x - x0) & 255) <= ((x1 - x0) & 255) && ((z - z0) & 255) <= ((z1 - z0) & 255) &&
	       y >= y0 && y <= y1;
}

/* Moves along one axis (0: X, 1: Y, 2: Z) in steps of at most 1/4 cell, stopping on contact.
   Returns 0 when blocked. */
static int move_axis(int axis, int32_t delta)
{
	int32_t *p = axis == 0 ? &player.x : axis == 1 ? &player.y : &player.z;

	while (delta) {
		int32_t step = delta > 64 ? 64 : delta < -64 ? -64 : delta;
		*p += step;
		if (collides(player.x, player.y, player.z)) {
			/* Back to the contact (1/256 resolution). */
			int32_t back = step > 0 ? -1 : 1;
			while (collides(player.x, player.y, player.z) && (step > 0 ? back > -step - 1 : back < -step + 1))
				*p += back;
			return 0;
		}
		delta -= step;
	}
	return 1;
}

/* Name layout: AUGU on a log shelf, STIN below, letters 5x7 bricks, 2 bricks thick. */
#define NAME_X0   (WORLD_SIZE/2 - 11)
#define NAME_Z0   (WORLD_SIZE/2)
#define NAME_W    23
#define SHELF_Y   (GROUND + 8)
#define SPAWN_X   (NAME_X0 + 8) /* In front of the T. */
#define SPAWN_Z   (NAME_Z0 - 22)

static const char name[LETTERS + 1] = "AUGUSTIN";

static void player_spawn(void)
{
	player.x     = SPAWN_X*256 + 128;
	player.z     = SPAWN_Z*256 + 128;
	player.y     = (world_height(SPAWN_X, SPAWN_Z) + 1)*256;
	player.vy    = 0;
	player.yaw   = 0;
	player.pitch = 50;
	player.fly   = 0;
}

/* Block picking: first solid cell along the view (1/16 cell steps), and the free cell before it
   (placement). */
static struct {
	int valid;
	int x, y, z;
	int px, py, pz;
} pick;

static void player_pick(const int16_t *fwd)
{
	int32_t x = player.x, y = player.y + PLAYER_EYE, z = player.z;
	int     lx = x >> 8, ly = y >> 8, lz = z >> 8;

	pick.valid = 0;
	for (int d = 0; d < REACH; d += 16) {
		x += fwd[0] >> 10;
		y += fwd[1] >> 10;
		z += fwd[2] >> 10;
		int cx = x >> 8, cy = y >> 8, cz = z >> 8;
		if (cx == lx && cy == ly && cz == lz)
			continue;
		if (cy < 0 || cy >= WORLD_HEIGHT)
			return;
		if (block_solid(world_get(cx, cy, cz))) {
			pick.valid = 1;
			pick.x  = cx & (WORLD_SIZE - 1); pick.y  = cy; pick.z  = cz & (WORLD_SIZE - 1);
			pick.px = lx & (WORLD_SIZE - 1); pick.py = ly; pick.pz = lz & (WORLD_SIZE - 1);
			return;
		}
		lx = cx; ly = cy; lz = cz;
	}
}

/* Effects --------------------------------------------------------------------------------------- */

/* Screen shake: camera jitter decaying until shake_end. */
static uint32_t shake_end, shake_len;
static int      shake_amp;

static void shake(uint32_t now, int amp, int ms)
{
	if (now < shake_end && shake_amp*(int)(shake_end - now)/(int)shake_len > amp)
		return;
	shake_amp = amp;
	shake_len = ms;
	shake_end = now + ms;
}

/* Sparkles: broken bricks become rainbow sparkles (non-solid) for a moment. */
#define SPARKLES   512
#define SPARKLE_MS 300
static struct { uint8_t x, y, z; uint32_t end; } sparkles[SPARKLES];
static unsigned sparkle_head, sparkle_tail;

static void sparkle_pop(void)
{
	int x = sparkles[sparkle_tail].x, y = sparkles[sparkle_tail].y, z = sparkles[sparkle_tail].z;
	if (world_get(x, y, z) == BLOCK_SPARKLE)
		world_set(x, y, z, BLOCK_AIR);
	sparkle_tail = (sparkle_tail + 1) % SPARKLES;
}

static void sparkle_add(int x, int y, int z, uint32_t now)
{
	if (y < 0 || y >= WORLD_HEIGHT || world_get(x, y, z) != BLOCK_AIR)
		return;
	if ((sparkle_head + 1) % SPARKLES == sparkle_tail)
		sparkle_pop();
	world_set(x, y, z, BLOCK_SPARKLE);
	sparkles[sparkle_head].x   = x & 255;
	sparkles[sparkle_head].y   = y;
	sparkles[sparkle_head].z   = z & 255;
	sparkles[sparkle_head].end = now + SPARKLE_MS;
	sparkle_head = (sparkle_head + 1) % SPARKLES;
}

static void sparkles_update(uint32_t now, int all)
{
	while (sparkle_tail != sparkle_head && (all || (int32_t)(now - sparkles[sparkle_tail].end) >= 0))
		sparkle_pop();
}

/* Game state ------------------------------------------------------------------------------------ */

enum { PHASE_INTRO, PHASE_PLAY, PHASE_VICTORY };
enum { TOOL_PUNCH, TOOL_HAMMER, TOOL_TNT, TOOLS };

static int      phase, tool, quick_win; /* quick_win: test, all bricks break after the intro. */
static uint32_t phase_ms, play_ms, best_ms;
static int      record;
static int      gold;
static int      letter_left[LETTERS], letter_total[LETTERS], letters_left;

/* Name bricks (intro build order: letter by letter, rows from the bottom). */
#define BRICKS_MAX (LETTERS*FONT_W*FONT_H*2)
static struct { uint8_t x, y, z, cell, letter; } bricks[BRICKS_MAX];
static int bricks_count, build_index;

static void name_layout(void)
{
	int prouts = 7, boings = 5, golds = 6;

	bricks_count = 0;
	memset(letter_total, 0, sizeof(letter_total));
	for (int i = 0; i < LETTERS; i++) {
		int x0 = NAME_X0 + (i % 4)*(FONT_W + 1);
		int y0 = i < 4 ? SHELF_Y + 1 : GROUND + 1;
		for (int r = FONT_H - 1; r >= 0; r--) {
			unsigned bits = font_row(name[i], r);
			for (int c = 0; c < FONT_W; c++)
				if ((bits >> (FONT_W - 1 - c)) & 1)
					for (int dz = 0; dz < 2; dz++) {
						bricks[bricks_count].x    = x0 + c;
						bricks[bricks_count].y    = y0 + FONT_H - 1 - r;
						bricks[bricks_count].z    = NAME_Z0 + dz;
						bricks[bricks_count].cell   = BLOCK_LETTER0 + i;
						bricks[bricks_count].letter = i;
						bricks_count++;
					}
		}
	}
	/* Surprises/gold on random bricks (the front face: dz 0). */
	while (prouts + boings + golds) {
		int i = rnd() % bricks_count;
		if (bricks[i].cell != (bricks[i].cell & 0xf) || bricks[i].cell == BLOCK_GOLD || bricks[i].z != NAME_Z0)
			continue;
		if (golds)
			bricks[i].cell = BLOCK_GOLD, golds--;
		else if (prouts)
			bricks[i].cell |= SURPRISE_PROUT << 4, prouts--;
		else
			bricks[i].cell |= SURPRISE_BOING << 4, boings--;
	}
	for (int i = 0; i < bricks_count; i++)
		if (bricks[i].cell != BLOCK_GOLD)
			letter_total[bricks[i].letter]++;
	memcpy(letter_left, letter_total, sizeof(letter_left));
	letters_left = bricks_count;
}

static void shelf_build(void)
{
	for (int dz = 0; dz < 2; dz++) {
		for (int x = NAME_X0 - 1; x <= NAME_X0 + NAME_W; x++)
			world_set(x, SHELF_Y, NAME_Z0 + dz, BLOCK_LOG);
		for (int y = GROUND + 1; y < SHELF_Y; y++) {
			world_set(NAME_X0 - 1,      y, NAME_Z0 + dz, BLOCK_LOG);
			world_set(NAME_X0 + NAME_W, y, NAME_Z0 + dz, BLOCK_LOG);
		}
	}
}

/* Gravity: bricks not connected (through bricks) to a brick resting on the ground, the shelf or a
   TNT fall by one cell per tick. Also counts the remaining bricks. */
#define BOX_X0 (NAME_X0 - 8)
#define BOX_Z0 (NAME_Z0 - 8)
#define BOX_Y0 (GROUND + 1)
#define BOX_W  (NAME_W + 16)
#define BOX_D  18
#define BOX_H  (WORLD_HEIGHT - BOX_Y0)
#define BOX_INDEX(x, y, z) ((((y) - BOX_Y0)*BOX_D + ((z) - BOX_Z0))*BOX_W + ((x) - BOX_X0))

static uint8_t  support[BOX_W*BOX_D*BOX_H];
static uint16_t stack[BOX_W*BOX_D*BOX_H];
static int      gravity_dirty, falling;

static int in_box(int x, int y, int z)
{
	return x >= BOX_X0 && x < BOX_X0 + BOX_W && z >= BOX_Z0 && z < BOX_Z0 + BOX_D && y >= BOX_Y0 && y < WORLD_HEIGHT;
}

static void brick_count(void)
{
	memset(letter_left, 0, sizeof(letter_left));
	letters_left = 0;
	for (int y = BOX_Y0; y < WORLD_HEIGHT; y++)
		for (int z = BOX_Z0; z < BOX_Z0 + BOX_D; z++)
			for (int x = BOX_X0; x < BOX_X0 + BOX_W; x++) {
				unsigned b = world_get(x, y, z);
				if (b >= BLOCK_LETTER0 && b < BLOCK_LETTER0 + LETTERS) {
					letter_left[b - BLOCK_LETTER0]++;
					letters_left++;
				}
			}
}

static void gravity_tick(uint32_t now)
{
	static const int8_t dirs[6][3] = {{1, 0, 0}, {-1, 0, 0}, {0, 1, 0}, {0, -1, 0}, {0, 0, 1}, {0, 0, -1}};
	int sp = 0, moved = 0;

	/* Supported bricks: flood fill from the bricks resting on a non-brick solid. */
	memset(support, 0, sizeof(support));
	for (int y = BOX_Y0; y < WORLD_HEIGHT; y++)
		for (int z = BOX_Z0; z < BOX_Z0 + BOX_D; z++)
			for (int x = BOX_X0; x < BOX_X0 + BOX_W; x++) {
				unsigned below = world_get(x, y - 1, z);
				if (block_breakable(world_get(x, y, z)) && block_solid(below) && !block_breakable(below)) {
					support[BOX_INDEX(x, y, z)] = 1;
					stack[sp++] = BOX_INDEX(x, y, z);
				}
			}
	while (sp) {
		int i = stack[--sp];
		int x = BOX_X0 + i % BOX_W, z = BOX_Z0 + (i/BOX_W) % BOX_D, y = BOX_Y0 + i/(BOX_W*BOX_D);
		for (int d = 0; d < 6; d++) {
			int nx = x + dirs[d][0], ny = y + dirs[d][1], nz = z + dirs[d][2];
			if (!in_box(nx, ny, nz) || ny < BOX_Y0)
				continue;
			int j = BOX_INDEX(nx, ny, nz);
			if (!support[j] && block_breakable(world_get(nx, ny, nz))) {
				support[j]  = 1;
				stack[sp++] = j;
			}
		}
	}

	/* Unsupported bricks down by one cell (from the bottom: the cells below are already free). */
	for (int y = BOX_Y0; y < WORLD_HEIGHT; y++)
		for (int z = BOX_Z0; z < BOX_Z0 + BOX_D; z++)
			for (int x = BOX_X0; x < BOX_X0 + BOX_W; x++) {
				unsigned cell = world_raw(x, y, z);
				if (!block_breakable(cell & 0xf) || support[BOX_INDEX(x, y, z)])
					continue;
				unsigned below = world_get(x, y - 1, z);
				if (below != BLOCK_AIR && below != BLOCK_SPARKLE)
					continue;
				world_set(x, y, z, BLOCK_AIR);
				if (player_inside(x, y - 1, z)) {
					/* On the head: bonk, the brick pops. */
					sound_play(SOUND_BONK);
					sparkle_add(x, y - 1, z, now);
					shake(now, 24, 200);
				} else
					world_set(x, y - 1, z, cell);
				moved = 1;
			}
	if (falling && !moved) {
		sound_play(SOUND_THUD);
		shake(now, 16, 150);
	}
	falling       = moved;
	gravity_dirty = moved;
	brick_count();
}

/* Breaking bricks. */
static void boing(void)
{
	player.vy        = JUMP_SPEED*5/2;
	player.on_ground = 0;
	sound_play(SOUND_BOING);
}

static void brick_break(int x, int y, int z, uint32_t now, int quiet)
{
	unsigned cell = world_raw(x, y, z), b = cell & 0xf;

	if (!block_breakable(b))
		return;
	world_set(x, y, z, BLOCK_AIR);
	sparkle_add(x, y, z, now);
	gravity_dirty = 1;
	if (b == BLOCK_GOLD) {
		gold++;
		sound_play(SOUND_DING);
	} else if ((cell >> 4) == SURPRISE_BOING)
		boing();
	else if ((cell >> 4) == SURPRISE_PROUT)
		sound_play(SOUND_PROUT);
	else if (!quiet)
		sound_play(SOUND_BREAK);
}

/* TNT: fuses, explosions (sphere of radius 3, chain reactions). */
#define FUSES 16
static struct { uint8_t x, y, z, lit; uint32_t end; } fuses[FUSES];
static int flash;

static void tnt_ignite(int x, int y, int z, uint32_t now, int ms)
{
	int free = -1;

	for (int i = 0; i < FUSES; i++) {
		if (fuses[i].lit && fuses[i].x == (x & 255) && fuses[i].y == y && fuses[i].z == (z & 255)) {
			if ((int32_t)(now + ms - fuses[i].end) < 0)
				fuses[i].end = now + ms;
			return;
		}
		if (!fuses[i].lit)
			free = i;
	}
	if (free < 0)
		return;
	fuses[free].x   = x & 255;
	fuses[free].y   = y;
	fuses[free].z   = z & 255;
	fuses[free].end = now + ms;
	fuses[free].lit = 1;
}

static void tnt_explode(int x, int y, int z, uint32_t now)
{
	world_set(x, y, z, BLOCK_AIR);
	for (int dy = -3; dy <= 3; dy++)
		for (int dz = -3; dz <= 3; dz++)
			for (int dx = -3; dx <= 3; dx++) {
				int r2 = dx*dx + dy*dy + dz*dz;
				int cx = x + dx, cy = y + dy, cz = z + dz;
				if (r2 > 10 || cy < 0 || cy >= WORLD_HEIGHT)
					continue;
				unsigned b = world_get(cx, cy, cz);
				if (b == BLOCK_TNT)
					tnt_ignite(cx, cy, cz, now, 150);
				else if (block_breakable(b))
					brick_break(cx, cy, cz, now, 1);
				else if (b == BLOCK_AIR && r2 <= 4 && (rnd() & 1))
					sparkle_add(cx, cy, cz, now);
			}
	sound_play(SOUND_BOOM);

	/* Close to the player: big shake, hop. */
	int px = (player.x >> 8) - x, py = (player.y >> 8) - y, pz = (player.z >> 8) - z;
	px = (px + 128) % 256 - 128; pz = (pz + 128) % 256 - 128;
	int d2 = px*px + py*py + pz*pz;
	shake(now, d2 < 36 ? 70 : d2 < 400 ? 40 : 16, 700);
	if (d2 < 25 && !player.fly) {
		player.vy        = JUMP_SPEED*2;
		player.on_ground = 0;
	}
	gravity_dirty = 1;
}

static void fuses_update(uint32_t now)
{
	int lit = 0, soon = 0;

	for (int i = 0; i < FUSES; i++) {
		if (!fuses[i].lit)
			continue;
		if (world_get(fuses[i].x, fuses[i].y, fuses[i].z) != BLOCK_TNT) {
			fuses[i].lit = 0;
			continue;
		}
		if ((int32_t)(now - fuses[i].end) >= 0) {
			fuses[i].lit = 0;
			tnt_explode(fuses[i].x, fuses[i].y, fuses[i].z, now);
			continue;
		}
		lit = 1;
		if (fuses[i].end - now < 800)
			soon = 1;
	}
	/* Flashing TNT (faster at the end), ticks. */
	int f = lit && ((now / (soon ? 80 : 200)) & 1);
	if (f != flash) {
		flash = f;
		art_palette(flash);
		if (flash)
			sound_play(SOUND_TICK);
	}
}

/* Tools. */
static void tool_use(uint32_t now)
{
	if (!pick.valid)
		return;
	unsigned b = world_get(pick.x, pick.y, pick.z);
	switch (tool) {
	case TOOL_PUNCH:
		if (b == BLOCK_TNT)
			tnt_ignite(pick.x, pick.y, pick.z, now, 1200);
		else if (block_breakable(b))
			brick_break(pick.x, pick.y, pick.z, now, 0);
		else
			sound_play(SOUND_TOK);
		break;
	case TOOL_HAMMER:
		for (int dy = -1; dy <= 1; dy++)
			for (int dz = -1; dz <= 1; dz++)
				for (int dx = -1; dx <= 1; dx++) {
					int x = pick.x + dx, y = pick.y + dy, z = pick.z + dz;
					unsigned c = world_get(x, y, z);
					if (c == BLOCK_TNT)
						tnt_ignite(x, y, z, now, 800);
					else
						brick_break(x, y, z, now, 1);
				}
		sound_play(SOUND_HAMMER);
		shake(now, 28, 250);
		break;
	case TOOL_TNT: {
		unsigned p = world_get(pick.px, pick.py, pick.pz);
		if ((p == BLOCK_AIR || p == BLOCK_SPARKLE) && pick.py >= 0 && pick.py < WORLD_HEIGHT &&
		    !player_inside(pick.px, pick.py, pick.pz)) {
			world_set(pick.px, pick.py, pick.pz, BLOCK_TNT);
			tnt_ignite(pick.px, pick.py, pick.pz, now, 2500);
			sound_play(SOUND_PLOP);
		} else
			sound_play(SOUND_TOK);
		break;
	}
	}
}

/* Confetti (victory): colored bricks falling from the sky, sparkling on landing. */
#define CONFETTI 96
static struct { uint8_t x, y, z, b; } confetti[CONFETTI];
static int confetti_count;

static void confetti_update(uint32_t now, int spawn)
{
	for (int i = 0; i < confetti_count; i++) {
		int x = confetti[i].x, y = confetti[i].y, z = confetti[i].z;
		unsigned below = world_get(x, y - 1, z);
		world_set(x, y, z, BLOCK_AIR);
		if ((below == BLOCK_AIR || below == BLOCK_SPARKLE) && !player_inside(x, y - 1, z)) {
			world_set(x, y - 1, z, confetti[i].b);
			confetti[i].y--;
		} else {
			sparkle_add(x, y, z, now);
			confetti[i--] = confetti[--confetti_count];
		}
	}
	/* New ones around a point 8 cells ahead of the player. */
	int cx = (player.x >> 8) + (isin(player.yaw)*8 >> 14), cz = (player.z >> 8) + (icos(player.yaw)*8 >> 14);
	for (int n = 0; spawn && n < 4 && confetti_count < CONFETTI; n++) {
		int x = cx - 10 + rnd() % 21, z = cz - 10 + rnd() % 21, y = WORLD_HEIGHT - 1 - rnd() % 4;
		if (world_get(x, y, z) != BLOCK_AIR)
			continue;
		confetti[confetti_count].x = x & 255;
		confetti[confetti_count].y = y;
		confetti[confetti_count].z = z & 255;
		confetti[confetti_count].b = BLOCK_LETTER0 + rnd() % LETTERS;
		world_set(x, y, z, confetti[confetti_count].b);
		confetti_count++;
	}
}

static void confetti_clear(void)
{
	for (int i = 0; i < confetti_count; i++)
		world_set(confetti[i].x, confetti[i].y, confetti[i].z, BLOCK_AIR);
	confetti_count = 0;
}

/* Round: clear the arena around the name, new surprises, build the name again. */
static void round_start(uint32_t now)
{
	confetti_clear();
	sparkles_update(now, 1);
	memset(fuses, 0, sizeof(fuses));
	for (int y = BOX_Y0; y < WORLD_HEIGHT; y++)
		for (int z = BOX_Z0; z < BOX_Z0 + BOX_D; z++)
			for (int x = BOX_X0; x < BOX_X0 + BOX_W; x++) {
				unsigned b = world_get(x, y, z);
				if (block_breakable(b) || b == BLOCK_TNT)
					world_set(x, y, z, BLOCK_AIR);
			}
	shelf_build();
	name_layout();
	player_spawn();
	build_index   = 0;
	gravity_dirty = 0;
	falling       = 0;
	phase         = PHASE_INTRO;
	phase_ms      = now;
}

/* Intro: one letter row (both bricks deep) every 70ms. */
static void intro_update(uint32_t now)
{
	if (now - phase_ms < 70)
		return;
	phase_ms = now;
	if (build_index >= bricks_count) {
		phase    = PHASE_PLAY;
		play_ms  = now;
		phase_ms = now;
		sound_play(SOUND_DING);
		for (int i = 0; quick_win && i < bricks_count; i++)
			brick_break(bricks[i].x, bricks[i].y, bricks[i].z, now, 1);
		return;
	}
	int y = bricks[build_index].y, letter = bricks[build_index].letter;
	while (build_index < bricks_count && bricks[build_index].y == y && bricks[build_index].letter == letter) {
		world_set(bricks[build_index].x, bricks[build_index].y, bricks[build_index].z, bricks[build_index].cell);
		build_index++;
	}
	sound_play(SOUND_PLOP);
}

/* HUD/banners ----------------------------------------------------------------------------------- */

static const char *const tool_icons[TOOLS][12] = {
	{ /* Fist. */
		"............", "..DDDDDDDD..", ".DSSDSSDSSD.", ".DSSDSSDSSD.", ".DSSDSSDSSD.", ".DDDDDDDDSD.",
		".DSSSSSSSSD.", ".DSSSSSSSSD.", "..DSSSSSSD..", "...DSSSSD...", "...DSSSSD...", "...DDDDDD...",
	}, { /* Hammer. */
		"............", ".DDDDDDDDDD.", ".DGGGGGGGGD.", ".DGGGGGGGGD.", ".DDDDBBDDDD.", ".....BB.....",
		".....BB.....", ".....BB.....", ".....BB.....", ".....BB.....", ".....BB.....", ".....DD.....",
	}, { /* TNT. */
		"......WW....", ".....K......", "..DDDKDDDD..", "..DRRRRRRD..", "..DRRRRRRD..", "..DWWWWWWD..",
		"..DWKWKWKD..", "..DWWWWWWD..", "..DRRRRRRD..", "..DRRRRRRD..", "..DDDDDDDD..", "............",
	},
};

static uint8_t icon_color(char c)
{
	switch (c) {
	case 'D': return PIXEL(COLOR_DARK);
	case 'S': return PIXEL(COLOR_SKIN);
	case 'G': return PIXEL(COLOR_STEEL);
	case 'B': return PIXEL(COLOR_BROWN);
	case 'R': return PIXEL(COLOR_TNT_RED);
	case 'W': return PIXEL(COLOR_WHITE);
	case 'K': return PIXEL(COLOR_BLACK);
	}
	return PIXEL(COLOR_BLACK);
}

static void time_text(char *s, uint32_t ms)
{
	unsigned t = ms/1000;
	if (t > 599)
		t = 599;
	s[0] = '0' + t/60;
	s[1] = ':';
	s[2] = '0' + (t % 60)/10;
	s[3] = '0' + t % 10;
	s[4] = 0;
}

static void hud_draw(uint32_t now)
{
	static struct { int tool, gold, seconds, left[LETTERS]; } shown = {.tool = -1};
	uint8_t line[HAL_LCD_WIDTH];
	char    time[8], count[12];
	uint32_t elapsed = phase == PHASE_INTRO ? 0 : phase == PHASE_PLAY ? now - play_ms : phase_ms - play_ms;

	/* Redrawn on changes only. */
	if (shown.tool == tool && shown.gold == gold && shown.seconds == (int)(elapsed/1000) &&
	    !memcmp(shown.left, letter_left, sizeof(letter_left)))
		return;
	shown.tool    = tool;
	shown.gold    = gold;
	shown.seconds = elapsed/1000;
	memcpy(shown.left, letter_left, sizeof(letter_left));
	time_text(time, elapsed);
	snprintf(count, sizeof(count), "%d", gold > 99 ? 99 : gold);

	for (int row = 0; row < HAL_LCD_HEIGHT - VR_HEIGHT; row++) {
		memset(line, PIXEL(COLOR_BLACK), sizeof(line));
		/* Letters, remaining bricks bars. */
		for (int i = 0; i < LETTERS; i++) {
			int     x = 2 + i*12;
			uint8_t color = letter_left[i] ? PIXEL(COLOR_LETTER0 + i) : PIXEL(COLOR_GRAY);
			char    glyph[2] = {name[i], 0};
			uint8_t colors[2] = {color, 0};
			font_text(line, x + 3, row - 1, glyph, 1, colors);
			if (row >= 10 && row <= 12) {
				int fill = letter_total[i] ? (letter_left[i]*10 + letter_total[i] - 1)/letter_total[i] : 0;
				for (int j = 0; j < 10; j++)
					line[x + 1 + j] = j < fill ? PIXEL(COLOR_LETTER0 + i) : PIXEL(COLOR_DARK);
			}
		}
		/* Tool. */
		if (row >= 2 && row < 14)
			for (int j = 0; j < 12; j++)
				line[100 + j] = icon_color(tool_icons[tool][row - 2][j]);
		/* Time, gold. */
		uint8_t white[2] = {PIXEL(COLOR_WHITE), 0}, yellow[2] = {PIXEL(COLOR_GOLD), 0};
		font_text(line, 122, row - 1, time, 1, white);
		if (row >= 10 && row <= 14)
			for (int j = 0; j < 5; j++)
				line[122 + j] = (row == 10 || row == 14 || j == 0 || j == 4) ? PIXEL(COLOR_GOLD_DARK) : PIXEL(COLOR_GOLD);
		font_text(line, 130, row - 8, count, 1, yellow);
		hal_lcd_line(VR_HEIGHT + row, line);
	}
}

/* Banner over the 3D view (full rows): text lines, 2x font, rainbow letters cycling. */
static void banner_draw(const char *const *lines, int count, int y0, uint32_t now)
{
	uint8_t colors[LETTERS + 1], line[HAL_LCD_WIDTH];
	int     height = 4 + count*(FONT_H*2 + 4);

	for (int i = 0; i < LETTERS; i++)
		colors[i] = PIXEL(COLOR_LETTER0 + (i + now/120) % LETTERS);
	colors[LETTERS] = 0;
	for (int y = 0; y < height; y++) {
		memset(line, (y == 0 || y == height - 1) ? PIXEL(COLOR_WHITE) : PIXEL(COLOR_BANNER), sizeof(line));
		for (int l = 0; l < count; l++)
			font_text(line, (HAL_LCD_WIDTH - font_width(lines[l], 2))/2, y - 4 - l*(FONT_H*2 + 4), lines[l], 2, colors);
		hal_lcd_line(y0 + y, line);
	}
}

/* Main ------------------------------------------------------------------------------------------ */

int main(void)
{
	uint32_t seed = 1234;
	uint32_t last, prev_buttons = 0, start_ms = 0, repeat_ms = 0, gravity_ms = 0, confetti_ms = 0;
	uint32_t fps_ms, fps_frames = 0;
	const char *args;

	hal_init();
	printf("ChromatiX Augustin\n");
	if ((args = hal_args()) && strstr(args, "seed="))
		seed = strtoul(strstr(args, "seed=") + 5, NULL, 0);
	quick_win = args && strstr(args, "win");
	trig_init();
	art_init();
	sound_init();
	world_generate(seed);
	rng ^= seed*0x9e3779b9 + hal_ticks_ms();
	printf("Renderer: %s\n", hal_vr_hardware() ? "VoxelRenderer" : "software");

	last = fps_ms = hal_ticks_ms();
	round_start(last);
	while (hal_poll()) {
		uint32_t now = hal_ticks_ms();
		int      dt  = now - last > 50 ? 50 : (int)(now - last);
		uint32_t buttons = hal_buttons(), pressed = buttons & ~prev_buttons;
		int      sel = buttons & (1 << HAL_BTN_SELECT);
		last = now;

		/* Turn/look. */
		if (!sel && (buttons & (1 << HAL_BTN_LEFT)))  player.yaw -= TURN_SPEED*dt/1000;
		if (!sel && (buttons & (1 << HAL_BTN_RIGHT))) player.yaw += TURN_SPEED*dt/1000;
		if (sel && (buttons & (1 << HAL_BTN_UP)))     player.pitch += LOOK_SPEED*dt/1000;
		if (sel && (buttons & (1 << HAL_BTN_DOWN)))   player.pitch -= LOOK_SPEED*dt/1000;
		player.yaw  &= ANGLES - 1;
		player.pitch = player.pitch > PITCH_MAX ? PITCH_MAX : player.pitch < -PITCH_MAX ? -PITCH_MAX : player.pitch;

		/* Walk/strafe. */
		int s = isin(player.yaw), c = icos(player.yaw);
		int forward = 0, strafe = 0;
		if (!sel && (buttons & (1 << HAL_BTN_UP)))   forward++;
		if (!sel && (buttons & (1 << HAL_BTN_DOWN))) forward--;
		if (sel && (buttons & (1 << HAL_BTN_RIGHT))) strafe++;
		if (sel && (buttons & (1 << HAL_BTN_LEFT)))  strafe--;
		int speed = (player.fly ? FLY_SPEED : WALK_SPEED)*dt/1000;
		int32_t dx = (forward*s + strafe*c)*speed >> 14;
		int32_t dz = (forward*c - strafe*s)*speed >> 14;
		int moved_x = move_axis(0, dx);
		int moved_z = move_axis(2, dz);
		int moved   = moved_x | moved_z;
		/* Auto-jump: walking into a 1 cell step (free above it). */
		if (!player.fly && player.on_ground && (!moved_x || !moved_z) &&
		    !collides(player.x + (dx > 0 ? 64 : dx < 0 ? -64 : 0), player.y + 256 + 8,
		              player.z + (dz > 0 ? 64 : dz < 0 ? -64 : 0))) {
			player.vy = JUMP_SPEED;
			player.on_ground = 0;
		}
		if (player.on_ground && (dx || dz) && moved) {
			player.walked += (dx < 0 ? -dx : dx) + (dz < 0 ? -dz : dz);
			if (player.walked > 160) {
				player.walked = 0;
				sound_play(SOUND_STEP);
			}
		}
		player.x &= WORLD_SIZE*256 - 1;
		player.z &= WORLD_SIZE*256 - 1;

		/* Jump/fly. */
		if (pressed & (1 << HAL_BTN_START) && !sel) {
			if (now - start_ms < 300) {
				player.fly ^= 1;
				player.vy   = 0;
				start_ms    = 0; /* The next press starts a new double press. */
			} else
				start_ms = now;
		}
		if (player.fly) {
			int vy = 0;
			if (buttons & (1 << HAL_BTN_START))
				vy = sel ? -FLY_SPEED : FLY_SPEED;
			move_axis(1, vy*dt/1000);
		} else {
			if (buttons & (1 << HAL_BTN_START) && !sel && player.on_ground) {
				player.vy = JUMP_SPEED;
				sound_play(SOUND_JUMP);
			}
			player.vy -= GRAVITY*dt/1000;
			if (player.vy < -FALL_MAX)
				player.vy = -FALL_MAX;
			int free = move_axis(1, player.vy*dt/1000);
			player.on_ground = !free && player.vy < 0;
			if (!free)
				player.vy = 0;
		}
		if (player.y > (WORLD_HEIGHT*256 - PLAYER_H - 1))
			player.y = WORLD_HEIGHT*256 - PLAYER_H - 1;

		/* Camera (shake: eye jitter). */
		struct vr_camera cam;
		int sp = isin(player.pitch), cp = icos(player.pitch);
		int32_t ex = player.x, ey = player.y + PLAYER_EYE, ez = player.z;
		if ((int32_t)(now - shake_end) < 0) {
			int amp = shake_amp*(int)(shake_end - now)/(int)shake_len;
			ex += (int)(rnd() % (2*amp + 1)) - amp;
			ey += (int)(rnd() % (2*amp + 1)) - amp;
			ez += (int)(rnd() % (2*amp + 1)) - amp;
			ex &= WORLD_SIZE*256 - 1;
			ez &= WORLD_SIZE*256 - 1;
		}
		ey = ey < 0 ? 0 : ey > WORLD_HEIGHT*256 - 1 ? WORLD_HEIGHT*256 - 1 : ey;
		memset(&cam, 0, sizeof(cam));
		cam.cx = ex >> 8; cam.cy = ey >> 8; cam.cz = ez >> 8;
		cam.fx = ex;      cam.fy = ey;      cam.fz = ez;
		cam.fwd[0]   = s*cp >> 14;              cam.fwd[1]   = sp;                  cam.fwd[2]   = c*cp >> 14;
		cam.right[0] = c*PIXEL_SCALE >> 14;     cam.right[1] = 0;                   cam.right[2] = -s*PIXEL_SCALE >> 14;
		cam.down[0]  = (sp*s >> 14)*PIXEL_SCALE >> 14;
		cam.down[1]  = -cp*PIXEL_SCALE >> 14;
		cam.down[2]  = (sp*c >> 14)*PIXEL_SCALE >> 14;

		/* Window: ahead of the camera (up to 8 cells), camera always inside. */
		int ox = cam.cx - VR_WINDOW/2 + (cam.fwd[0]*8 >> 14);
		int oz = cam.cz - VR_WINDOW/2 + (cam.fwd[2]*8 >> 14);
		window_update(ox, oz);
		cam.ox        = window_ox();
		cam.oz        = window_oz();
		cam.t_far     = 30 << 12;
		cam.fog_start = 22 << 12;
		cam.fog_shift = 13;
		cam.sky_base  = COLOR_SKY << 3;
		cam.cross_en  = 1;
		cam.cross_index = COLOR_WHITE << 3;

		/* Tools: A: next, B: use (on press, then repeated while held). */
		player_pick(cam.fwd);
		if (phase == PHASE_PLAY) {
			static const int repeat[TOOLS] = {220, 450, 600};
			if (pressed & (1 << HAL_BTN_A) && !sel) {
				tool = (tool + 1) % TOOLS;
				sound_play(SOUND_SWITCH);
			}
			if (!sel && ((pressed & (1 << HAL_BTN_B)) || ((buttons & (1 << HAL_BTN_B)) && now - repeat_ms > (uint32_t)repeat[tool]))) {
				repeat_ms = now;
				tool_use(now);
				player_pick(cam.fwd);
			}
		}
		if (pick.valid) {
			cam.sel_en = 1;
			cam.sel_x  = pick.x; cam.sel_y = pick.y; cam.sel_z = pick.z;
			cam.sel_index = COLOR_WHITE << 3;
			cam.sel_shift = 10; /* ~2 pixels. */
		}
		prev_buttons = buttons;

		/* World updates. */
		sparkles_update(now, 0);
		fuses_update(now);
		switch (phase) {
		case PHASE_INTRO:
			intro_update(now);
			break;
		case PHASE_PLAY:
			if (gravity_dirty && now - gravity_ms >= 45) {
				gravity_ms = now;
				gravity_tick(now);
				if (!gravity_dirty && letters_left == 0) {
					/* Victory. */
					phase    = PHASE_VICTORY;
					phase_ms = now;
					record   = !best_ms || now - play_ms < best_ms;
					if (record)
						best_ms = now - play_ms;
					sound_play(SOUND_FANFARE);
					printf("Victory: %lu ms%s\n", (unsigned long)(now - play_ms), record ? " (record)" : "");
				}
			}
			break;
		case PHASE_VICTORY:
			if (now - confetti_ms >= 50) {
				confetti_ms = now;
				confetti_update(now, now - phase_ms < 6000);
			}
			if (now - phase_ms > 8000)
				round_start(now);
			break;
		}

		/* Frame. */
		hal_vr_render(&cam);
		if (phase == PHASE_PLAY && now - play_ms < 2500) {
			static const char *const go[] = {"CASSE", "TOUT !"};
			banner_draw(go, 2, 16, now);
		} else if (phase == PHASE_VICTORY && now - phase_ms > 600) {
			char        time[16], line[24];
			const char *bravo[] = {"BRAVO", "AUGUSTIN !", line};
			time_text(time, phase_ms - play_ms);
			snprintf(line, sizeof(line), "%s%s", record ? "RECORD " : "", time);
			banner_draw(bravo, 3, 8, now);
		}
		hud_draw(now);
		hal_lcd_present();
		hal_frame();
		fps_frames++;
		if (now - fps_ms >= 5000) {
			printf("%lu.%lu fps, render %lu us, bricks %d, gold %d\n", (unsigned long)(fps_frames*1000/(now - fps_ms)),
				(unsigned long)(fps_frames*10000/(now - fps_ms) % 10),
				(unsigned long)(hal_vr_cycles()/67), letters_left, gold);
			fps_ms     = now;
			fps_frames = 0;
		}
	}
	return 0;
}

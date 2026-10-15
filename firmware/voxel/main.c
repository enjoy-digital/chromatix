// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Voxel game (creative mode): player physics/controls, block picking/editing, camera -> 3D view
// (VoxelRenderer or software), hotbar, day/night cycle.
//
// Controls: D-pad: walk/turn, Select + D-pad: look up/down/strafe, A: place, B: break, Start: jump
// (fly: up, Select + Start: down), Start twice: fly on/off, Select + A/B: next/previous block.

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
#define WALK_SPEED   1100 /* Q8/s. */
#define FLY_SPEED    2000
#define TURN_SPEED   420  /* Angles/s. */
#define PITCH_MAX    220
#define GRAVITY      5120 /* Q8/s^2. */
#define JUMP_SPEED   1850
#define FALL_MAX     7680
#define REACH        (5*256)
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

/* Player ---------------------------------------------------------------------------------------- */

static struct {
	int32_t x, y, z;     /* Feet (Q8). */
	int32_t vy;          /* Q8/s. */
	int     yaw, pitch;
	int     on_ground, fly, in_water;
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

static void player_spawn(int x, int z)
{
	player.x     = x*256 + 128;
	player.z     = z*256 + 128;
	player.y     = (world_height(x, z) + 1)*256;
	player.vy    = 0;
	player.yaw   = 0;
	player.pitch = -40;
	player.fly   = 0;
}

/* Block picking: first solid (non-air, non-water) cell along the view (1/16 cell steps), and the
   free cell before it (placement). */
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
		unsigned b = world_get(cx, cy, cz);
		if (b != BLOCK_AIR && b != BLOCK_WATER) {
			pick.valid = 1;
			pick.x  = cx & (WORLD_SIZE - 1); pick.y  = cy; pick.z  = cz & (WORLD_SIZE - 1);
			pick.px = lx;                    pick.py = ly; pick.pz = lz;
			return;
		}
		lx = cx; ly = cy; lz = cz;
	}
}

/* Hotbar ---------------------------------------------------------------------------------------- */

static const uint8_t hotbar_blocks[] = {
	BLOCK_GRASS, BLOCK_DIRT, BLOCK_STONE, BLOCK_COBBLE, BLOCK_PLANKS, BLOCK_LOG, BLOCK_LEAVES,
	BLOCK_GLASS, BLOCK_BRICK, BLOCK_SAND, BLOCK_SNOW, BLOCK_GOLD, BLOCK_WATER,
};
#define HOTBAR_COUNT ((int)sizeof(hotbar_blocks))
#define HOTBAR_SLOTS 9
static int hotbar = 0;

static void hud_draw(void)
{
	uint8_t line[HAL_LCD_WIDTH];
	int     first = hotbar - HOTBAR_SLOTS/2;

	if (first < 0)
		first = 0;
	if (first > HOTBAR_COUNT - HOTBAR_SLOTS)
		first = HOTBAR_COUNT - HOTBAR_SLOTS;
	for (int y = VR_HEIGHT; y < HAL_LCD_HEIGHT; y++) {
		int row = y - VR_HEIGHT;
		memset(line, COLOR_BLACK << 3, sizeof(line));
		for (int s = 0; s < HOTBAR_SLOTS; s++) {
			int     item = first + s, x0 = 8 + s*16;
			int     selected = item == hotbar;
			uint8_t border = selected ? COLOR_WHITE << 3 : (COLOR_GRAY << 3);
			for (int dx = 0; dx < 16; dx++) {
				uint8_t p;
				if (row == 0 || row == 15 || dx == 0 || dx == 15)
					p = border;
				else if (row == 1 || row == 14 || dx == 1 || dx == 14)
					p = selected ? border : COLOR_BLACK << 3;
				else {
					unsigned tex = art_block_texture(hotbar_blocks[item], 1);
					unsigned c   = art_texel(tex, (dx - 2)*16/12, (row - 2)*16/12) & 0x1f;
					p = c ? (uint8_t)(c << 3) : COLOR_BLACK << 3;
				}
				line[x0 + dx] = p;
			}
		}
		hal_lcd_line(y, line);
	}
}

/* Main ------------------------------------------------------------------------------------------ */

static int daylight_at(uint32_t ms)
{
	/* 8 minutes: day 5 min, dusk 30s, night 2 min, dawn 30s. */
	uint32_t t = ms % (8*60*1000);
	if (t < 5*60*1000)
		return 256;
	t -= 5*60*1000;
	if (t < 30*1000)
		return 256 - (int)(t*256/(30*1000));
	t -= 30*1000;
	if (t < 120*1000)
		return 0;
	t -= 120*1000;
	return (int)(t*256/(30*1000));
}

int main(void)
{
	uint32_t seed = 1234;
	uint32_t last, prev_buttons = 0, start_ms = 0, repeat_ms = 0, fps_ms, fps_frames = 0;
	int      daylight = 256;
	const char *args;

	hal_init();
	printf("ChromatiX voxel\n");
	if ((args = hal_args()) && strstr(args, "seed="))
		seed = strtoul(strstr(args, "seed=") + 5, NULL, 0);
	trig_init();
	art_init();
	sound_init();
	printf("World generation (seed %lu)...\n", (unsigned long)seed);
	last = hal_ticks_ms();
	world_generate(seed);
	printf("Done (%lu ms), renderer: %s\n", (unsigned long)(hal_ticks_ms() - last),
		hal_vr_hardware() ? "VoxelRenderer" : "software");
	player_spawn(WORLD_SIZE/2, WORLD_SIZE/2);
	hud_draw();

	last = fps_ms = hal_ticks_ms();
	while (hal_poll()) {
		uint32_t now = hal_ticks_ms();
		int      dt  = now - last > 50 ? 50 : (int)(now - last);
		uint32_t buttons = hal_buttons(), pressed = buttons & ~prev_buttons;
		int      sel = buttons & (1 << HAL_BTN_SELECT);
		last = now;

		/* Turn/look. */
		if (!sel && (buttons & (1 << HAL_BTN_LEFT)))  player.yaw -= TURN_SPEED*dt/1000;
		if (!sel && (buttons & (1 << HAL_BTN_RIGHT))) player.yaw += TURN_SPEED*dt/1000;
		if (sel && (buttons & (1 << HAL_BTN_UP)))     player.pitch += TURN_SPEED*dt/1000;
		if (sel && (buttons & (1 << HAL_BTN_DOWN)))   player.pitch -= TURN_SPEED*dt/1000;
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
		if (player.in_water && !player.fly)
			speed /= 2;
		int32_t dx = (forward*s + strafe*c)*speed >> 14;
		int32_t dz = (forward*c - strafe*s)*speed >> 14;
		int moved = move_axis(0, dx);
		moved    |= move_axis(2, dz);
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
			}
			start_ms = now;
		}
		player.in_water = world_get(player.x >> 8, (player.y + 128) >> 8, player.z >> 8) == BLOCK_WATER;
		if (player.fly) {
			int vy = 0;
			if (buttons & (1 << HAL_BTN_START))
				vy = sel ? -FLY_SPEED : FLY_SPEED;
			move_axis(1, vy*dt/1000);
		} else {
			if (buttons & (1 << HAL_BTN_START) && !sel) {
				if (player.on_ground) {
					player.vy = JUMP_SPEED;
					sound_play(SOUND_JUMP);
				} else if (player.in_water)
					player.vy = JUMP_SPEED/2;
			}
			player.vy -= (player.in_water ? GRAVITY/4 : GRAVITY)*dt/1000;
			if (player.vy < -FALL_MAX)
				player.vy = -FALL_MAX;
			if (player.in_water && player.vy < -FALL_MAX/8)
				player.vy = -FALL_MAX/8;
			int free = move_axis(1, player.vy*dt/1000);
			player.on_ground = !free && player.vy < 0;
			if (!free)
				player.vy = 0;
		}
		if (player.y > (WORLD_HEIGHT*256 - PLAYER_H - 1))
			player.y = WORLD_HEIGHT*256 - PLAYER_H - 1;

		/* Camera. */
		struct vr_camera cam;
		int sp = isin(player.pitch), cp = icos(player.pitch);
		int32_t eye = player.y + PLAYER_EYE;
		if (eye > WORLD_HEIGHT*256 - 1)
			eye = WORLD_HEIGHT*256 - 1;
		memset(&cam, 0, sizeof(cam));
		cam.cx = player.x >> 8; cam.cy = eye >> 8;      cam.cz = player.z >> 8;
		cam.fx = player.x;      cam.fy = eye;           cam.fz = player.z;
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
		cam.t_far     = 26 << 12;
		cam.fog_start = 10 << 12;
		cam.fog_shift = 13;
		cam.sky_base  = COLOR_SKY << 3;
		cam.cross_en  = 1;
		cam.cross_index = COLOR_WHITE << 3;

		/* Picking/editing (A/B: on press, then repeated every 250ms). */
		player_pick(cam.fwd);
		int act = (pressed & 3) || ((buttons & 3) && now - repeat_ms > 250);
		if (sel && (pressed & (1 << HAL_BTN_A)))
			hotbar = (hotbar + 1) % HOTBAR_COUNT, hud_draw();
		else if (sel && (pressed & (1 << HAL_BTN_B)))
			hotbar = (hotbar + HOTBAR_COUNT - 1) % HOTBAR_COUNT, hud_draw();
		else if (!sel && act && pick.valid) {
			repeat_ms = now;
			if (buttons & (1 << HAL_BTN_B)) {
				if (world_get(pick.x, pick.y, pick.z) != BLOCK_BEDROCK) {
					world_set(pick.x, pick.y, pick.z, BLOCK_AIR);
					sound_play(SOUND_BREAK);
				}
			} else if (buttons & (1 << HAL_BTN_A)) {
				int px0 = (player.x - PLAYER_HW) >> 8, px1 = (player.x + PLAYER_HW - 1) >> 8;
				int pz0 = (player.z - PLAYER_HW) >> 8, pz1 = (player.z + PLAYER_HW - 1) >> 8;
				int py0 = player.y >> 8,               py1 = (player.y + PLAYER_H - 1) >> 8;
				int inside = ((pick.px & 255) >= (px0 & 255) && (pick.px & 255) <= (px1 & 255) &&
				              (pick.pz & 255) >= (pz0 & 255) && (pick.pz & 255) <= (pz1 & 255) &&
				              pick.py >= py0 && pick.py <= py1);
				unsigned b = hotbar_blocks[hotbar];
				if ((!inside || !block_solid(b)) && pick.py >= 0 && pick.py < WORLD_HEIGHT) {
					world_set(pick.px, pick.py, pick.pz, b);
					sound_play(SOUND_PLACE);
				}
			}
			player_pick(cam.fwd);
		}
		if (pick.valid) {
			cam.sel_en = 1;
			cam.sel_x  = pick.x; cam.sel_y = pick.y; cam.sel_z = pick.z;
			cam.sel_index = COLOR_BLACK << 3;
		}
		prev_buttons = buttons;

		/* Day/night. */
		int light = daylight_at(now);
		if (light != daylight && (light - daylight > 3 || daylight - light > 3 || light == 0 || light == 256)) {
			daylight = light;
			art_palette(daylight);
		}

		/* Frame. */
		hal_vr_render(&cam);
		hal_lcd_present();
		hal_frame();
		fps_frames++;
		if (now - fps_ms >= 5000) {
			printf("%lu.%lu fps, render %lu us (%d, %d, %d)%s\n", (unsigned long)(fps_frames*1000/(now - fps_ms)),
				(unsigned long)(fps_frames*10000/(now - fps_ms) % 10),
				(unsigned long)(hal_vr_cycles()/67),
				(int)(player.x >> 8), (int)(player.y >> 8), (int)(player.z >> 8), player.fly ? " fly" : "");
			fps_ms     = now;
			fps_frames = 0;
		}
	}
	return 0;
}

// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Voxel raycaster: reference model of the VoxelRenderer gateware (chromatix/gateware/voxel.py),
// bit exact (also the software renderer of the firmware and the golden model of the tests).
//
// World window: 32x32x32 voxels, 4-bit block types (0: air), toroidal on X/Z (cell & 31), word
// address (y << 7) | (z << 2) | (x >> 3), nibble x & 7. Textures: 16x16 texels, 8-bit (bits 4:0:
// color, 0: transparent), word address (texture << 6) | (v << 2) | (u >> 2), byte u & 3. Block
// textures: 16 entries, bits 3:0 top, 7:4 side, 11:8 bottom.
//
// Fixed point: camera fraction Q8, ray vectors Q14 (forward . right = forward . down = 0, |forward|
// = 1: t is the view depth), t Q12 (24-bit, VR_TINF: never). Pixel: (color << 3) | shade, shade =
// min(7, face level + (max(t - fog_start, 0) >> fog_shift)), sky: sky_base | clamp(4 - (dy >> 12), 0, 7).

#ifndef VR_H
#define VR_H

#include <stdint.h>

#define VR_WIDTH      160
#define VR_HEIGHT     128 /* 3D view rows (LCD rows below: HUD). */
#define VR_CX         80
#define VR_CY         64
#define VR_WINDOW     32 /* X/Z/Y size (cells). */
#define VR_MAX_STEPS  64
#define VR_TINF       0xffffff

#define VR_WORLD_WORDS   (VR_WINDOW*VR_WINDOW*VR_WINDOW/8)
#define VR_TEXTURES      16
#define VR_TEXTURE_WORDS (VR_TEXTURES*16*16/4)
#define VR_BLOCKS        16

struct vr_camera {
	uint8_t  cx, cy, cz;    /* Camera cell (X/Z: world cell mod 256, Y: 0-31). */
	uint8_t  fx, fy, fz;    /* Camera position in the cell (Q8). */
	int16_t  fwd[3];        /* View vectors (Q14): ray = fwd + (x - 80)*right + (y - 64)*down. */
	int16_t  right[3];
	int16_t  down[3];
	uint8_t  ox, oz;        /* Window origin (world cells): rays end out of [o, o + 31]. */
	uint32_t t_far;         /* Fog end (Q12). */
	uint32_t fog_start;     /* Fog start (Q12). */
	uint8_t  fog_shift;     /* Fog: +1 shade every 2^fog_shift (Q12) after fog_start. */
	uint8_t  sky_base;      /* Sky palette base (8 levels). */
	uint8_t  sel_en, sel_x, sel_y, sel_z, sel_index;  /* Selected block (edges highlighted). */
	uint8_t  cross_en, cross_index;                   /* Crosshair. */
};

struct vr_scene {
	const uint32_t *world;     /* VR_WORLD_WORDS. */
	const uint32_t *textures;  /* VR_TEXTURE_WORDS. */
	const uint16_t *blocks;    /* VR_BLOCKS. */
};

uint8_t vr_pixel(const struct vr_scene *scene, const struct vr_camera *cam, int x, int y);
void    vr_line(const struct vr_scene *scene, const struct vr_camera *cam, int y, uint8_t *line);

/* Helpers (window/texture layouts). */
static inline unsigned vr_voxel_get(const uint32_t *world, int x, int y, int z)
{
	unsigned addr = ((unsigned)y << 10) | ((unsigned)(z & 31) << 5) | (unsigned)(x & 31);
	return (world[addr >> 3] >> (4*(addr & 7))) & 0xf;
}

static inline void vr_voxel_set(uint32_t *world, int x, int y, int z, unsigned v)
{
	unsigned addr = ((unsigned)y << 10) | ((unsigned)(z & 31) << 5) | (unsigned)(x & 31);
	world[addr >> 3] = (world[addr >> 3] & ~(0xfu << (4*(addr & 7)))) | (v & 0xf) << (4*(addr & 7));
}

#endif

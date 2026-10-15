// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Voxel raycaster reference model (see vr.h): 3D DDA (Amanatides & Woo) in fixed point, exactly as
// done by the gateware (any change here must be done there, test/test_voxel.py checks both).

#include "vr.h"

/* Face levels (shade offsets): top, X sides, Z sides, bottom. */
enum { FACE_TOP = 0, FACE_X = 2, FACE_Z = 1, FACE_BOTTOM = 3 };

static int32_t clamp_dir(int32_t v)
{
	return v > 32767 ? 32767 : v < -32767 ? -32767 : v;
}

static uint32_t sat(uint32_t v)
{
	return v > VR_TINF ? VR_TINF : v;
}

static unsigned texel(const struct vr_scene *scene, unsigned texture, unsigned u, unsigned v)
{
	unsigned addr = (texture << 8) | (v << 4) | u;
	return (scene->textures[addr >> 2] >> (8*(addr & 3))) & 0xff;
}

uint8_t vr_pixel(const struct vr_scene *scene, const struct vr_camera *cam, int x, int y)
{
	int32_t  d[3];
	int      step[3];
	uint32_t delta[3], tmax[3];
	unsigned frac[3] = {cam->fx, cam->fy, cam->fz};
	int      c[3]    = {cam->cx, cam->cy, cam->cz};

	/* Crosshair. */
	if (cam->cross_en &&
	    ((y == VR_CY && x >= VR_CX - 2 && x <= VR_CX + 2) || (x == VR_CX && y >= VR_CY - 2 && y <= VR_CY + 2)))
		return cam->cross_index;

	/* Ray setup. */
	for (int a = 0; a < 3; a++) {
		d[a] = clamp_dir(cam->fwd[a] + (x - VR_CX)*cam->right[a] + (y - VR_CY)*cam->down[a]);
		if (d[a] == 0) {
			step[a]  = 0;
			delta[a] = VR_TINF;
			tmax[a]  = VR_TINF;
			continue;
		}
		uint32_t mag  = d[a] > 0 ? d[a] : -d[a];
		uint32_t dist = d[a] > 0 ? 256 - frac[a] : frac[a];
		step[a]  = d[a] > 0 ? 1 : -1;
		delta[a] = sat((1u << 26)/mag);
		tmax[a]  = sat((dist*delta[a]) >> 8);
	}

	/* DDA. */
	for (int n = 0; n < VR_MAX_STEPS; n++) {
		int      a = (tmax[0] <= tmax[1] && tmax[0] <= tmax[2]) ? 0 : (tmax[1] <= tmax[2] ? 1 : 2);
		uint32_t t = tmax[a];
		if (t > cam->t_far)
			break;
		c[a]    += step[a];
		tmax[a]  = sat(tmax[a] + delta[a]);
		c[0]    &= 0xff;
		c[2]    &= 0xff;
		if (c[1] < 0 || c[1] >= VR_WINDOW)
			break;
		if (((c[0] - cam->ox) & 0xff) >= VR_WINDOW || ((c[2] - cam->oz) & 0xff) >= VR_WINDOW)
			break;
		unsigned v = vr_voxel_get(scene->world, c[0], c[1], c[2]);
		if (v == 0)
			continue;

		/* Hit: position on the face (other axes, Q8), texture coordinates, face. */
		unsigned pos[3];
		for (int b = 0; b < 3; b++)
			pos[b] = (unsigned)(frac[b] + (int32_t)(((int64_t)t*d[b]) >> 18)) & 0xff;
		unsigned u, w, level, texture, blocktex = scene->blocks[v];
		if (a == 0) {
			u = pos[2] >> 4; w = 15 - (pos[1] >> 4); level = FACE_X; texture = (blocktex >> 4) & 0xf;
		} else if (a == 2) {
			u = pos[0] >> 4; w = 15 - (pos[1] >> 4); level = FACE_Z; texture = (blocktex >> 4) & 0xf;
		} else if (step[1] < 0) {
			u = pos[0] >> 4; w = pos[2] >> 4; level = FACE_TOP; texture = blocktex & 0xf;
		} else {
			u = pos[0] >> 4; w = pos[2] >> 4; level = FACE_BOTTOM; texture = (blocktex >> 8) & 0xf;
		}
		if (cam->sel_en && c[0] == cam->sel_x && c[1] == cam->sel_y && c[2] == cam->sel_z &&
		    (u == 0 || u == 15 || w == 0 || w == 15))
			return cam->sel_index;
		unsigned color = texel(scene, texture, u, w) & 0x1f;
		if (color == 0)
			continue; /* Transparent texel (leaves, glass). */
		uint32_t shade = level + (t > cam->fog_start ? (t - cam->fog_start) >> (cam->fog_shift & 31) : 0);
		return (color << 3) | (shade > 7 ? 7 : shade);
	}

	/* Sky. */
	int level = 4 - (d[1] >> 12);
	return cam->sky_base | (level < 0 ? 0 : level > 7 ? 7 : level);
}

void vr_line(const struct vr_scene *scene, const struct vr_camera *cam, int y, uint8_t *line)
{
	for (int x = 0; x < VR_WIDTH; x++)
		line[x] = vr_pixel(scene, cam, x, y);
}

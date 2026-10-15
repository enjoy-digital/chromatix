// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Voxel game sound effects, procedural: filtered noise bursts with a decaying envelope (break,
// place, steps) and a short sweep (jump), mixed in the audio callback (4 voices).

#include <stdint.h>

#include "hal.h"
#include "game.h"

#define VOICES 4

struct voice {
	int      length;  /* Remaining samples. */
	int      total;
	int      volume;  /* Q8. */
	int      filter;  /* Low-pass coefficient (Q8, higher: brighter). */
	int      lp;      /* Filter state. */
	int      phase, freq; /* Tone (jump), Q16 per sample, 0: noise. */
};

static struct voice voices[VOICES];
static uint32_t     rng = 0x12345678;

static void fill(void)
{
	int n = hal_audio_free();

	while (n-- > 0) {
		int mix = 0;
		for (int i = 0; i < VOICES; i++) {
			struct voice *v = &voices[i];
			if (v->length <= 0)
				continue;
			int s;
			if (v->freq) {
				v->phase += v->freq;
				v->freq  -= v->freq >> 11;
				s = (v->phase & 0x8000) ? 8000 : -8000;
			} else {
				rng ^= rng << 13; rng ^= rng >> 17; rng ^= rng << 5;
				s = (int16_t)rng;
			}
			v->lp += ((s - v->lp)*v->filter) >> 8;
			int env = (v->length << 8)/v->total;
			mix += (v->lp*env >> 8)*v->volume >> 8;
			v->length--;
		}
		mix = mix > 32767 ? 32767 : mix < -32767 ? -32767 : mix;
		hal_audio_write((uint16_t)mix | (uint32_t)(uint16_t)mix << 16);
	}
}

void sound_init(void)
{
	hal_audio_start(fill);
}

void sound_play(int sound)
{
	static const struct { int ms, volume, filter, freq; } sounds[] = {
		[SOUND_BREAK] = {140, 200,  90, 0},
		[SOUND_PLACE] = { 60, 220,  40, 0},
		[SOUND_STEP]  = { 40,  90,  30, 0},
		[SOUND_JUMP]  = { 90,  50, 128, 0x0f00},
	};
	struct voice *v = &voices[0];

	/* Free voice (or the one closest to its end). */
	for (int i = 0; i < VOICES; i++)
		if (voices[i].length < v->length)
			v = &voices[i];
	v->total  = v->length = sounds[sound].ms*HAL_AUDIO_RATE/1000;
	v->volume = sounds[sound].volume;
	v->filter = sounds[sound].filter;
	v->freq   = sounds[sound].freq;
	v->phase  = 0;
	v->lp     = 0;
}

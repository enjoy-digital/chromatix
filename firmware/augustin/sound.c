// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Augustin game sound effects, procedural: square tones (frequency slides, vibrato) mixed with
// filtered noise, decaying or sustained envelopes (breaks, booms, boing, prout...), and short
// melodies (ding, fanfare) played note by note, mixed in the audio callback (6 voices).

#include <stdint.h>

#include "hal.h"
#include "game.h"

#define VOICES 6
#define HZ(f)  ((int32_t)((f)*16777216LL/HAL_AUDIO_RATE)) /* Q24 phase per sample. */

struct sound {
	int16_t ms, volume, filter;  /* Volume/filter: Q8 (filter: low-pass coefficient, higher: brighter). */
	int16_t hz, slide;           /* Tone (0: noise only), slide: frequency *= 1 + slide/65536 per sample. */
	int16_t noise;               /* Noise mix (Q8, tones). */
	int16_t wobble_hz, wobble;   /* Vibrato rate, depth (Q8). */
	int16_t sustain;             /* Envelope: flat, then decay over the last quarter. */
};

static const struct sound sounds[] = {
	[SOUND_BREAK]  = {.ms = 140, .volume = 200, .filter =  90},
	[SOUND_STEP]   = {.ms =  40, .volume =  90, .filter =  30},
	[SOUND_JUMP]   = {.ms =  90, .volume =  50, .filter = 128, .hz =  646, .slide = -32},
	[SOUND_PLOP]   = {.ms =  70, .volume = 120, .filter = 100, .hz =  500, .slide = -40},
	[SOUND_THUD]   = {.ms = 180, .volume = 255, .filter =  25, .hz =   70, .slide = -20, .noise = 160},
	[SOUND_BOOM]   = {.ms = 900, .volume = 255, .filter =  50},
	[SOUND_HAMMER] = {.ms = 220, .volume = 230, .filter = 110, .hz =  140, .slide = -30, .noise = 170},
	[SOUND_BOING]  = {.ms = 450, .volume = 150, .filter = 140, .hz =  140, .slide =  22,
	                  .wobble_hz = 12, .wobble = 40, .sustain = 1},
	[SOUND_PROUT]  = {.ms = 800, .volume = 255, .filter =  45, .hz =   90, .slide =  -6, .noise = 60,
	                  .wobble_hz = 23, .wobble = 110, .sustain = 1},
	[SOUND_DING]   = {0}, /* Melody. */
	[SOUND_TICK]   = {.ms =  15, .volume = 140, .filter = 220},
	[SOUND_BONK]   = {.ms = 150, .volume = 160, .filter = 160, .hz =  900, .slide = -60},
	[SOUND_TOK]    = {.ms =  40, .volume = 120, .filter =  80, .hz =  220, .slide = -20},
	[SOUND_SWITCH] = {.ms =  30, .volume =  80, .filter = 200, .hz = 1000},
	[SOUND_FANFARE] = {0}, /* Melody. */
};
static const struct sound boom_low = {.ms = 700, .volume = 255, .filter = 40, .hz = 55, .slide = -8, .noise = 128};

/* Melodies: {Hz, ms}, 0 terminated. */
static const int16_t ding[][2]    = {{1319, 90}, {1760, 250}, {0, 0}};
static const int16_t fanfare[][2] = {{523, 120}, {659, 120}, {784, 120}, {1047, 300}, {784, 120}, {1047, 600}, {0, 0}};

struct voice {
	int32_t length, total;  /* Remaining/total samples. */
	int32_t volume, filter, lp, noise, sustain;
	int32_t phase, freq, slide;
	int32_t wobble_phase, wobble_rate, wobble;
};

static struct voice    voices[VOICES];
static uint32_t        rng = 0x12345678;
static const int16_t (*melody)[2];
static int32_t         melody_left;

static void voice_start(const struct sound *s, int hz, int ms)
{
	struct voice *v = &voices[0];

	/* Free voice (or the one closest to its end). */
	for (int i = 0; i < VOICES; i++)
		if (voices[i].length < v->length)
			v = &voices[i];
	v->length       = 0;
	v->total        = ms*HAL_AUDIO_RATE/1000;
	v->volume       = s->volume;
	v->filter       = s->filter;
	v->noise        = hz ? s->noise : 256;
	v->sustain      = s->sustain;
	v->freq         = HZ(hz);
	v->slide        = s->slide;
	v->wobble_rate  = s->wobble_hz*65536/HAL_AUDIO_RATE;
	v->wobble       = s->wobble;
	v->wobble_phase = 0;
	v->phase        = 0;
	v->lp           = 0;
	v->length       = v->total; /* Last: the voice starts (audio interrupt). */
}

static void melody_note(void)
{
	static const struct sound note = {.volume = 110, .filter = 150};

	if (!melody[0][0]) {
		melody = 0;
		return;
	}
	voice_start(&note, melody[0][0], melody[0][1]);
	melody_left = melody[0][1]*HAL_AUDIO_RATE/1000;
	melody++;
}

static void fill(void)
{
	int n = hal_audio_free();

	while (n-- > 0) {
		int mix = 0;
		if (melody && --melody_left <= 0)
			melody_note();
		for (int i = 0; i < VOICES; i++) {
			struct voice *v = &voices[i];
			if (v->length <= 0)
				continue;
			int s = 0;
			if (v->noise < 256) {
				int32_t f = v->freq;
				if (v->wobble) {
					/* Triangle vibrato (+-256). */
					v->wobble_phase = (v->wobble_phase + v->wobble_rate) & 0xffff;
					int tri = v->wobble_phase < 0x8000 ? (v->wobble_phase >> 6) - 256 : 768 - (v->wobble_phase >> 6);
					f += (int32_t)(((int64_t)f*v->wobble*tri) >> 16);
				}
				v->phase += f;
				v->freq  += (int32_t)(((int64_t)v->freq*v->slide) >> 16);
				s = ((v->phase & 0x800000) ? 8000 : -8000)*(256 - v->noise);
			}
			if (v->noise) {
				rng ^= rng << 13; rng ^= rng >> 17; rng ^= rng << 5;
				s += (int16_t)rng*v->noise;
			}
			s >>= 8;
			v->lp += ((s - v->lp)*v->filter) >> 8;
			int env = v->sustain ? (v->length*4 > v->total ? 256 : (v->length << 10)/v->total) :
			                       (v->length << 8)/v->total;
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
	if (sound == SOUND_DING || sound == SOUND_FANFARE) {
		melody_left = 1;
		melody      = sound == SOUND_DING ? ding : fanfare;
		return;
	}
	voice_start(&sounds[sound], sounds[sound].hz, sounds[sound].ms);
	if (sound == SOUND_BOOM)
		voice_start(&boom_low, boom_low.hz, boom_low.ms);
}

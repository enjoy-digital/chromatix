// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Doom sound effects: software mixer (8 channels) of the DMX sound lumps (8-bit unsigned, used in
// place from the WAD), resampled to HAL_AUDIO_RATE, Doom volume/stereo separation. Mixing is done
// from the HAL fill callback (SoC: PCM FIFO interrupt), independently of the frames rate. No music.

#include <stdio.h>
#include <string.h>

#include "doomtype.h"
#include "i_sound.h"
#include "w_wad.h"
#include "z_zone.h"

#include "hal.h"

#ifdef __riscv
#include <irq.h>
#define SOUND_LOCK()   irq_setie(0)
#define SOUND_UNLOCK() irq_setie(1)
#else
#define SOUND_LOCK()
#define SOUND_UNLOCK()
#endif

#define NUM_CHANNELS 8

typedef struct {
	const uint8_t *data;   /* NULL: idle. */
	uint32_t       length; /* Samples. */
	uint32_t       pos;    /* 16.16 fixed point. */
	uint32_t       step;   /* 16.16 fixed point. */
	int            left;   /* Gains (0..254*127). */
	int            right;
} channel_t;

static volatile channel_t channels[NUM_CHANNELS];
static boolean            use_prefix;

/* Config variables of the SDL sound backend (bound by i_sound.c, unused). */
int   use_libsamplerate   = 0;
float libsamplerate_scale = 0.65f;

/* Mixer ---------------------------------------------------------------------------------------- */

static void sound_fill(void)
{
	int n = hal_audio_free();

	while (n-- > 0) {
		int left = 0, right = 0;
		for (int i = 0; i < NUM_CHANNELS; i++) {
			volatile channel_t *c = &channels[i];
			if (!c->data)
				continue;
			int sample = (int)c->data[c->pos >> 16] - 128;
			left  += sample*c->left;
			right += sample*c->right;
			c->pos += c->step;
			if ((c->pos >> 16) >= c->length)
				c->data = NULL;
		}
		left  >>= 7;
		right >>= 7;
		left  = left  > 32767 ? 32767 : left  < -32768 ? -32768 : left;
		right = right > 32767 ? 32767 : right < -32768 ? -32768 : right;
		hal_audio_write((uint32_t)(uint16_t)right << 16 | (uint16_t)left);
	}
}

/* Sound module --------------------------------------------------------------------------------- */

static snddevice_t sound_devices[] = {
	SNDDEVICE_SB, SNDDEVICE_PAS, SNDDEVICE_GUS, SNDDEVICE_WAVEBLASTER, SNDDEVICE_SOUNDCANVAS,
	SNDDEVICE_AWE32,
};

static boolean sound_init(boolean prefix)
{
	use_prefix = prefix;
	memset((void *)channels, 0, sizeof(channels));
	hal_audio_start(sound_fill);
	return true;
}

static void sound_shutdown(void)
{
}

static int sound_get_lump(sfxinfo_t *sfx)
{
	char name[9];

	if (sfx->link != NULL)
		sfx = sfx->link;
	snprintf(name, sizeof(name), use_prefix ? "ds%s" : "%s", sfx->name);
	return W_GetNumForName(name);
}

static void sound_update(void)
{
}

static void sound_set_params(int channel, int vol, int sep)
{
	if (channel < 0 || channel >= NUM_CHANNELS)
		return;
	SOUND_LOCK();
	channels[channel].left  = (254 - sep)*vol/2;
	channels[channel].right = sep*vol/2;
	SOUND_UNLOCK();
}

static int sound_start(sfxinfo_t *sfx, int channel, int vol, int sep)
{
	const uint8_t *lump;
	uint32_t       rate, length;

	if (channel < 0 || channel >= NUM_CHANNELS)
		return -1;
	/* DMX lump: format (3), sample rate, samples count, 16 padding samples at both ends. */
	lump   = W_CacheLumpNum(sfx->lumpnum, PU_STATIC);
	if (W_LumpLength(sfx->lumpnum) < 8 || lump[0] != 3 || lump[1] != 0)
		return -1;
	rate   = lump[2] | lump[3] << 8;
	length = lump[4] | lump[5] << 8 | lump[6] << 16 | (uint32_t)lump[7] << 24;
	if (length > (uint32_t)W_LumpLength(sfx->lumpnum) - 8)
		length = W_LumpLength(sfx->lumpnum) - 8;
	if (length <= 32)
		return -1;

	SOUND_LOCK();
	channels[channel].data   = lump + 8 + 16;
	channels[channel].length = length - 32;
	channels[channel].pos    = 0;
	channels[channel].step   = (rate << 16)/HAL_AUDIO_RATE;
	SOUND_UNLOCK();
	sound_set_params(channel, vol, sep);
	return channel;
}

static void sound_stop(int channel)
{
	if (channel < 0 || channel >= NUM_CHANNELS)
		return;
	SOUND_LOCK();
	channels[channel].data = NULL;
	SOUND_UNLOCK();
}

static boolean sound_is_playing(int channel)
{
	if (channel < 0 || channel >= NUM_CHANNELS)
		return false;
	return channels[channel].data != NULL;
}

static void sound_cache(sfxinfo_t *sounds, int num_sounds)
{
	(void)sounds; (void)num_sounds; /* Lumps used in place. */
}

sound_module_t DG_sound_module = {
	sound_devices,
	sizeof(sound_devices)/sizeof(sound_devices[0]),
	sound_init,
	sound_shutdown,
	sound_get_lump,
	sound_update,
	sound_set_params,
	sound_start,
	sound_stop,
	sound_is_playing,
	sound_cache,
};

/* Music module (none) -------------------------------------------------------------------------- */

static boolean music_init(void)               { return false; }
static void    music_shutdown(void)           { }
static void    music_volume(int volume)       { (void)volume; }
static void    music_pause(void)              { }
static void    music_resume(void)             { }
static void   *music_register(void *data, int len) { (void)data; (void)len; return NULL; }
static void    music_unregister(void *handle) { (void)handle; }
static void    music_play(void *handle, boolean looping) { (void)handle; (void)looping; }
static void    music_stop(void)               { }
static boolean music_playing(void)            { return false; }
static void    music_poll(void)               { }

music_module_t DG_music_module = {
	NULL, 0,
	music_init,
	music_shutdown,
	music_volume,
	music_pause,
	music_resume,
	music_register,
	music_unregister,
	music_play,
	music_stop,
	music_playing,
	music_poll,
};

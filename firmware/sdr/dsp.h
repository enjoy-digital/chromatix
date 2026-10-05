// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// SDR DSP: fixed point FFTs (int16/int32 block scaled, Q15 twiddles), power spectrum of 8-bit I/Q
// captures (Hann window, segments averaged), dB conversion, NCO, channelizer (frequency shift +
// polyphase resampler). Hardware independent (also built on the host for the tests).
//
// Complex samples are interleaved (re, im): the CPU data cache is direct mapped (8KB), separate
// re/im arrays a multiple of 8KB apart would evict each other on each access.

#ifndef DSP_H
#define DSP_H

#include <stdint.h>

#include "tables.h"

/* Averaged power spectrum of samples I/Q pairs (segments of FFT_SIZE, ESP32 inverted spectrum
   corrected), DC centered: power[0] is -fs/2, power[FFT_SIZE/2] is DC (interpolated from its
   neighbours: LO leakage removed). */
void dsp_power_spectrum(const int8_t *iq, int samples, uint64_t *power);
/* 10*log10(power) in 1/4 dB. */
int  dsp_db4(uint64_t power);

/* Largest capture (ESP-SDR burst). */
#define DSP_MAX_SAMPLES 16384

/* In place FFT of 2^log2n complex points (<= 2^FFT_MAX_LOG2), scaled by 1/2^log2n (forward and
   inverse). */
void dsp_fft(int32_t *x, int log2n, int inverse);

/* NCO: cos/sin (Q15) of phase (2^32: 2*pi). */
void dsp_nco(uint32_t phase, int32_t *c, int32_t *s);
/* Frequency shift in place: x[i]*exp(j*(phase + phase_step*i)). */
void dsp_rotate(int32_t *x, int n, uint32_t phase, uint32_t phase_step);
/* Phase step of a frequency at a sample rate. */
uint32_t dsp_phase_step(int32_t freq_hz, int32_t rate_hz);

/* Channel filter: polyphase lowpass (tables.h), rate x up/down. */
struct dsp_filter {
	const int16_t *h;    /* Polyphase ordered: h[p*taps + k] = prototype[p + k*up], Q15. */
	int            taps; /* Taps per phase. */
	int            up;
	int            down;
};

/* Channelizer: 8-bit I/Q capture (DC removed, ESP32 inverted spectrum corrected) shifted by
   phase_step (the channel moved to DC: -f), filtered and resampled: complex output (8-bit samples
   scaled by 2^7), returns the output samples count (<= max). */
int  dsp_channelize(const int8_t *iq, int samples, uint32_t phase_step, const struct dsp_filter *f,
	int32_t *out, int max);

/* FM soft symbols: discriminator (disc[i]: Im(y[i]*conj(y[i - 1])), > 0: positive frequency)
   summed over spb samples (soft[i]: samples i..i + spb - 1), returns the soft symbols count. */
int  dsp_fm_soft(const int32_t *y, int n, int spb, int32_t *disc, int32_t *soft);

/* Integer square root. */
int  dsp_isqrt64(uint64_t x);

#endif

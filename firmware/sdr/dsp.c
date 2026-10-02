// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// SDR DSP, see dsp.h.

#include <stdint.h>
#include <string.h>

#include "dsp.h"

#define FFT_LOG2 9

/* int16 working data (block scaled: 1/2 per stage), fits in the CPU data cache with the tables. */
static int16_t re[FFT_SIZE];
static int16_t im[FFT_SIZE];

static void fft(void)
{
	/* Bit reversal. */
	for (int i = 1, j = 0; i < FFT_SIZE; i++) {
		int bit = FFT_SIZE >> 1;
		for (; j & bit; bit >>= 1)
			j ^= bit;
		j ^= bit;
		if (i < j) {
			int16_t t;
			t = re[i]; re[i] = re[j]; re[j] = t;
			t = im[i]; im[i] = im[j]; im[j] = t;
		}
	}
	/* Radix-2 butterflies, scaled by 1/2 per stage (no overflow: |x| < 2^15, 16x16-bit products). */
	for (int len = 2; len <= FFT_SIZE; len <<= 1) {
		int half = len >> 1;
		int step = FFT_SIZE/len;
		for (int k = 0; k < half; k++) {
			int32_t wr =  twiddle_cos[k*step];
			int32_t wi = -twiddle_sin[k*step];
			for (int i = k; i < FFT_SIZE; i += len) {
				int32_t xr = re[i + half];
				int32_t xi = im[i + half];
				int32_t tr = (wr*xr - wi*xi + (1 << 14)) >> 15;
				int32_t ti = (wr*xi + wi*xr + (1 << 14)) >> 15;
				int32_t ar = re[i];
				int32_t ai = im[i];
				re[i + half] = (ar - tr) >> 1;
				im[i + half] = (ai - ti) >> 1;
				re[i]        = (ar + tr) >> 1;
				im[i]        = (ai + ti) >> 1;
			}
		}
	}
}

void dsp_power_spectrum(const int8_t *iq, int samples, uint64_t *power)
{
	memset(power, 0, FFT_SIZE*sizeof(uint64_t));
	for (int s = 0; s + FFT_SIZE <= samples; s += FFT_SIZE) {
		const int8_t *x = &iq[2*s];
		/* DC removal, window (8-bit samples scaled to 15-bit). */
		int32_t dc_i = 0, dc_q = 0;
		for (int n = 0; n < FFT_SIZE; n++) {
			dc_i += x[2*n + 0];
			dc_q += x[2*n + 1];
		}
		dc_i /= FFT_SIZE;
		dc_q /= FFT_SIZE;
		/* Conjugated: the ESP32 I/Q spectrum is inverted (measured on the console crystal
		   harmonics). */
		for (int n = 0; n < FFT_SIZE; n++) {
			re[n] =  ((x[2*n + 0] - dc_i)*hann[n]) >> 9;
			im[n] = -(((x[2*n + 1] - dc_q)*hann[n]) >> 9);
		}
		fft();
		/* DC centered. */
		for (int k = 0; k < FFT_SIZE; k++) {
			int     b = (k + FFT_SIZE/2) & (FFT_SIZE - 1);
			int32_t r = re[b], i = im[b];
			power[k] += (uint32_t)(r*r) + (uint32_t)(i*i);
		}
	}
	/* DC bin (LO leakage, rounding bias): interpolated from its neighbours. */
	power[FFT_SIZE/2] = (power[FFT_SIZE/2 - 1] + power[FFT_SIZE/2 + 1])/2;
}

int dsp_db4(uint64_t power)
{
	/* 10*log10(p) = 10*log10(2)*log2(p), log2 with a linear fraction (< 0.09 dB error). */
	if (power == 0)
		return 0;
	int      e = 63 - __builtin_clzll(power);
	uint64_t m = (e >= 16) ? power >> (e - 16) : power << (16 - e);
	uint32_t f = (uint32_t)m & 0xffff;
	int32_t  log2_q16 = (e << 16) + f;
	return (int)(((int64_t)log2_q16*12330) >> 16 >> 10); /* 4*10*log10(2) = 12.041 (12330 in Q10). */
}

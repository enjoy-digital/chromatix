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

static int32_t re[FFT_SIZE];
static int32_t im[FFT_SIZE];

static inline int32_t q15_mul(int32_t a, int32_t b)
{
	return (int32_t)(((int64_t)a*b) >> 15);
}

static void fft(void)
{
	/* Bit reversal. */
	for (int i = 1, j = 0; i < FFT_SIZE; i++) {
		int bit = FFT_SIZE >> 1;
		for (; j & bit; bit >>= 1)
			j ^= bit;
		j ^= bit;
		if (i < j) {
			int32_t t;
			t = re[i]; re[i] = re[j]; re[j] = t;
			t = im[i]; im[i] = im[j]; im[j] = t;
		}
	}
	/* Radix-2 butterflies (no scaling: int32 headroom). */
	for (int len = 2; len <= FFT_SIZE; len <<= 1) {
		int half = len >> 1;
		int step = FFT_SIZE/len;
		for (int i = 0; i < FFT_SIZE; i += len)
			for (int k = 0; k < half; k++) {
				int32_t wr =  twiddle_cos[k*step];
				int32_t wi = -twiddle_sin[k*step];
				int32_t xr = re[i + k + half];
				int32_t xi = im[i + k + half];
				int32_t tr = q15_mul(wr, xr) - q15_mul(wi, xi);
				int32_t ti = q15_mul(wr, xi) + q15_mul(wi, xr);
				re[i + k + half] = re[i + k] - tr;
				im[i + k + half] = im[i + k] - ti;
				re[i + k] += tr;
				im[i + k] += ti;
			}
	}
}

void dsp_power_spectrum(const int8_t *iq, int samples, uint64_t *power)
{
	memset(power, 0, FFT_SIZE*sizeof(uint64_t));
	for (int s = 0; s + FFT_SIZE <= samples; s += FFT_SIZE) {
		/* Window (8-bit samples scaled to 16-bit). */
		int32_t dc_i = 0, dc_q = 0;
		for (int n = 0; n < FFT_SIZE; n++) {
			dc_i += iq[2*(s + n) + 0];
			dc_q += iq[2*(s + n) + 1];
		}
		dc_i /= FFT_SIZE;
		dc_q /= FFT_SIZE;
		for (int n = 0; n < FFT_SIZE; n++) {
			re[n] = ((iq[2*(s + n) + 0] - dc_i)*hann[n]) >> 7;
			im[n] = ((iq[2*(s + n) + 1] - dc_q)*hann[n]) >> 7;
		}
		fft();
		/* DC centered. */
		for (int k = 0; k < FFT_SIZE; k++) {
			int     b = (k + FFT_SIZE/2) % FFT_SIZE;
			int64_t r = re[b] >> 4, i = im[b] >> 4;
			power[k] += (uint64_t)(r*r + i*i);
		}
	}
}

int dsp_db4(uint64_t power)
{
	/* 10*log10(p) = 10*log10(2)*log2(p), log2 with a linear fraction (< 0.09 dB error). */
	if (power == 0)
		return 0;
	int      e = 63 - __builtin_clzll(power);
	uint32_t f = (e >= 16) ? (uint32_t)(power >> (e - 16)) & 0xffff : (uint32_t)(power << (16 - e)) & 0xffff;
	int32_t  log2_q16 = (e << 16) + f;
	return (int)(((int64_t)log2_q16*12330) >> 16 >> 10); /* 4*10*log10(2) = 12.041 (12330 in Q10). */
}

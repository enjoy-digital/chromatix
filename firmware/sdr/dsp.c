// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// SDR DSP, see dsp.h.

#include <stdint.h>
#include <string.h>

#include "dsp.h"

/* Spectrum FFT --------------------------------------------------------------------------------- */

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
		int step = (1 << FFT_MAX_LOG2)/len;
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

/* FFT ------------------------------------------------------------------------------------------ */

void dsp_fft(int32_t *x, int log2n, int inverse)
{
	int n = 1 << log2n;
	/* Bit reversal. */
	for (int i = 1, j = 0; i < n; i++) {
		int bit = n >> 1;
		for (; j & bit; bit >>= 1)
			j ^= bit;
		j ^= bit;
		if (i < j) {
			int32_t t;
			t = x[2*i + 0]; x[2*i + 0] = x[2*j + 0]; x[2*j + 0] = t;
			t = x[2*i + 1]; x[2*i + 1] = x[2*j + 1]; x[2*j + 1] = t;
		}
	}
	/* Radix-2 butterflies, scaled by 1/2 per stage. */
	for (int len = 2; len <= n; len <<= 1) {
		int half = len >> 1;
		int step = (1 << FFT_MAX_LOG2)/len;
		for (int k = 0; k < half; k++) {
			int64_t wr = twiddle_cos[k*step];
			int64_t wi = inverse ? twiddle_sin[k*step] : -twiddle_sin[k*step];
			for (int i = k; i < n; i += len) {
				int32_t *a = &x[2*i], *b = &x[2*(i + half)];
				int32_t  tr = (int32_t)((wr*b[0] - wi*b[1] + (1 << 14)) >> 15);
				int32_t  ti = (int32_t)((wr*b[1] + wi*b[0] + (1 << 14)) >> 15);
				int32_t  ar = a[0];
				int32_t  ai = a[1];
				b[0] = (ar - tr) >> 1;
				b[1] = (ai - ti) >> 1;
				a[0] = (ar + tr) >> 1;
				a[1] = (ai + ti) >> 1;
			}
		}
	}
}

/* NCO ------------------------------------------------------------------------------------------ */

void dsp_nco(uint32_t phase, int32_t *c, int32_t *s)
{
	/* cos/sin (Q15) of phase (2^32: 2*pi), from the FFT twiddles (half circle). */
	int k = phase >> (32 - FFT_MAX_LOG2);
	int h = 1 << (FFT_MAX_LOG2 - 1);
	if (k < h) {
		*c = twiddle_cos[k];
		*s = twiddle_sin[k];
	} else {
		*c = -twiddle_cos[k - h];
		*s = -twiddle_sin[k - h];
	}
}

void dsp_rotate(int32_t *x, int n, uint32_t phase, uint32_t phase_step)
{
	/* Frequency shift: x[i]*exp(j*(phase + phase_step*i)) (phase_step: f/fs*2^32). */
	for (int i = 0; i < n; i++) {
		int32_t c, s;
		dsp_nco(phase, &c, &s);
		int64_t r = x[2*i + 0], q = x[2*i + 1];
		x[2*i + 0] = (int32_t)((r*c - q*s) >> 15);
		x[2*i + 1] = (int32_t)((r*s + q*c) >> 15);
		phase += phase_step;
	}
}

/* Channelizer ---------------------------------------------------------------------------------- */

static int16_t mix[2*DSP_MAX_SAMPLES];

int dsp_channelize(const int8_t *iq, int samples, uint32_t phase_step, const struct dsp_filter *f,
	int32_t *out, int max)
{
	samples = (samples < DSP_MAX_SAMPLES) ? samples : DSP_MAX_SAMPLES;
	/* DC removal (LO leakage), conjugation (ESP32 inverted spectrum), frequency shift (input
	   scaled by 2^7: int16). */
	int32_t dc_i = 0, dc_q = 0;
	for (int i = 0; i < samples; i++) {
		dc_i += iq[2*i + 0];
		dc_q += iq[2*i + 1];
	}
	dc_i = (dc_i << 7)/samples;
	dc_q = (dc_q << 7)/samples;
	/* NCO: periodic table when the shift is a multiple of fs/64 (common offsets), else computed. */
	static int16_t nco[2*64];
	int period = 0;
	for (int k = 1; k <= 64; k++)
		if ((uint32_t)(phase_step*k) == 0) {
			period = k;
			break;
		}
	for (int k = 0; k < period; k++) {
		int32_t c, s;
		dsp_nco(phase_step*k, &c, &s);
		nco[2*k + 0] = (c > 32767) ? 32767 : c;
		nco[2*k + 1] = s;
	}
	uint32_t phase = 0;
	for (int i = 0, k = 0; i < samples; i++) {
		int32_t c, s;
		if (period) {
			c = nco[2*k + 0];
			s = nco[2*k + 1];
			k = (k + 1 == period) ? 0 : k + 1;
		} else {
			dsp_nco(phase, &c, &s);
			phase += phase_step;
		}
		int32_t r =  (iq[2*i + 0] << 7) - dc_i;
		int32_t q = -((iq[2*i + 1] << 7) - dc_q);
		mix[2*i + 0] = (r*c - q*s) >> 15;
		mix[2*i + 1] = (r*s + q*c) >> 15;
	}
	/* Polyphase resampler: output m at m*down/up input samples (taps[p*K + k] = h[p + k*up]). */
	int n = 0, base = f->taps, p = 0;
	while (n < max && base < samples) {
		const int16_t *h = &f->h[p*f->taps];
		const int16_t *x = &mix[2*base];
		int32_t ar = 0, ai = 0;
		for (int k = 0; k < f->taps; k++) {
			ar += h[k]*x[-2*k + 0];
			ai += h[k]*x[-2*k + 1];
		}
		out[2*n + 0] = ar >> 15;
		out[2*n + 1] = ai >> 15;
		n++;
		/* Next output: down/up input samples later. */
		p    += f->down;
		base += p/f->up;
		p    %= f->up;
	}
	return n;
}

/* FM demodulation ------------------------------------------------------------------------------ */

int dsp_fm_soft(const int32_t *y, int n, int spb, int32_t *disc, int32_t *soft)
{
	/* FM discriminator: Im(y[i]*conj(y[i - 1])) (|y|^2*sin(dphi) >> 12), summed over a symbol. */
	disc[0] = 0;
	for (int i = 1; i < n; i++) {
		const int32_t *x = &y[2*i];
		disc[i] = ((int64_t)x[1]*x[-2] - (int64_t)x[0]*x[-1]) >> 12;
	}
	int32_t s = 0;
	for (int i = 0; i < n; i++) {
		s += disc[i];
		if (i >= spb)
			s -= disc[i - spb];
		if (i >= spb - 1)
			soft[i - spb + 1] = s;
	}
	return (n >= spb) ? n - spb + 1 : 0;
}

/* Helpers -------------------------------------------------------------------------------------- */

uint32_t dsp_phase_step(int32_t freq_hz, int32_t rate_hz)
{
	return (uint32_t)(int32_t)(((int64_t)freq_hz << 32)/rate_hz);
}

int dsp_isqrt64(uint64_t x)
{
	uint64_t r = 0, b = (uint64_t)1 << 62;
	while (b > x)
		b >>= 2;
	while (b) {
		if (x >= r + b) {
			x -= r + b;
			r  = (r >> 1) + b;
		} else
			r >>= 1;
		b >>= 2;
	}
	return (int)r;
}

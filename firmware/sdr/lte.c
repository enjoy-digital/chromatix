// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// LTE cell search, see lte.h (3GPP TS 36.211 6.11: PSS Zadoff-Chu sequences, SSS m-sequences).

#include <stdint.h>
#include <string.h>

#include "dsp.h"
#include "lte.h"

#define FFT_LOG2  11
#define FFT_N     (1 << FFT_LOG2)
#define SYM       128               /* Symbol (1.92MS/s). */
#define SYM_CP    (SYM + 9)         /* Symbol + normal CP (symbols 1-6). */
#define SSS_FDD   SYM_CP            /* SSS start before the PSS start: FDD. */
#define SSS_TDD   (3*SYM_CP + 1)    /* TDD (a slot's first symbol CP: 10). */
#define CFO_BINS  8                 /* Wide search hypotheses: +-7.5kHz (8 bins of 937.5Hz). */
#define CFO_STEP  500               /* Refined frequency offset step (Hz). */
#define CFO_SPAN  4000              /* Refined frequency offset range (Hz, around the hypothesis). */
#define PSS_MIN   12                /* PSS detection threshold (%). */
#define SSS_MIN   45                /* SSS detection threshold (%). */

static const struct dsp_filter filter = {filter_lte, FILTER_LTE_TAPS, FILTER_LTE_UP, FILTER_LTE_DOWN};

/* Buffers: complex interleaved, the ones accessed together offset in the (direct mapped 8KB) data
   cache. */
static struct {
	int32_t y[2*FFT_N];             /* Channelized capture.             +0KB  */
	int32_t pad0[512];
	int32_t f[2*FFT_N];             /* Its spectrum.                    +2KB  */
	int32_t pad1[512];
	int32_t c[2*FFT_N];             /* Correlation.                     +4KB  */
	int32_t pad2[512];
	int32_t pss_f[3][2*FFT_N];      /* PSS spectra (correlation).       +6KB  */
	int32_t pad3[256];
	int64_t energy[FFT_N];          /* Energy of the SYM samples at i.  +7KB  */
} b;

/* Sequences ------------------------------------------------------------------------------------ */

static int32_t  pss_t[3][2*SYM];     /* PSS symbols (time, peaks < 2^14). */
static int32_t  pss_d[3][2*62];      /* PSS subcarriers (Q15). */
static uint64_t sss_bits[3][2][168]; /* SSS subcarriers (bit set: -1). */
static int64_t  pss_energy;

static int subcarrier(int n)
{
	/* Sequence element n (0-61) -> FFT index (subcarriers -31..-1, 1..31). */
	return (n < 31) ? SYM - 31 + n : n - 30;
}

static void mseq(int *x, int taps)
{
	/* m-sequence x(i + 5) = sum of x(i + tap) (taps: bit mask), x(0..4) = 0, 0, 0, 0, 1. */
	memset(x, 0, 31*sizeof(int));
	x[4] = 1;
	for (int i = 0; i < 26; i++) {
		int v = 0;
		for (int t = 0; t < 5; t++)
			if (taps & (1 << t))
				v ^= x[i + t];
		x[i + 5] = v;
	}
}

void lte_init(void)
{
	static const int roots[3] = {25, 29, 34};
	/* PSS: d(n) = exp(-j*pi*u*n*(n + 1)/63) (n < 31), exp(-j*pi*u*(n + 1)*(n + 2)/63). */
	for (int i = 0; i < 3; i++) {
		int32_t *x = b.c;
		memset(x, 0, 2*SYM*sizeof(int32_t));
		for (int n = 0; n < 62; n++) {
			int     m = (n < 31) ? n : n + 1;
			int32_t c, s;
			dsp_nco(-(uint32_t)((uint64_t)((roots[i]*m*(m + 1)) % 126)*0x100000000ull/126), &c, &s);
			pss_d[i][2*n + 0] = c;
			pss_d[i][2*n + 1] = s;
			x[2*subcarrier(n) + 0] = c;
			x[2*subcarrier(n) + 1] = s;
		}
		/* Time symbol (inverse FFT scaled by 1/128). */
		dsp_fft(x, 7, 1);
		memcpy(pss_t[i], x, sizeof(pss_t[i]));
		/* Spectrum for the FFT correlation (zero padded, x2^8). */
		memset(b.pss_f[i], 0, sizeof(b.pss_f[i]));
		for (int n = 0; n < 2*SYM; n++)
			b.pss_f[i][n] = pss_t[i][n] << 8;
		dsp_fft(b.pss_f[i], FFT_LOG2, 0);
	}
	pss_energy = 0;
	for (int n = 0; n < 2*SYM; n++)
		pss_energy += (int64_t)pss_t[0][n]*pss_t[0][n];
	/* SSS (bits: element = -1). */
	int s[31], c[31], z[31];
	mseq(s, (1 << 2) | (1 << 0));
	mseq(c, (1 << 3) | (1 << 0));
	mseq(z, (1 << 4) | (1 << 2) | (1 << 1) | (1 << 0));
	for (int nid1 = 0; nid1 < 168; nid1++) {
		int q1 = nid1/30;
		int q  = (nid1 + q1*(q1 + 1)/2)/30;
		int mp = nid1 + q*(q + 1)/2;
		int m0 = mp % 31;
		int m1 = (m0 + mp/31 + 1) % 31;
		for (int nid2 = 0; nid2 < 3; nid2++)
			for (int sf = 0; sf < 2; sf++) {
				uint64_t bits = 0;
				for (int n = 0; n < 31; n++) {
					int s0 = s[(n + m0) % 31], s1 = s[(n + m1) % 31];
					int c0 = c[(n + nid2) % 31], c1 = c[(n + nid2 + 3) % 31];
					int z0 = z[(n + (m0 % 8)) % 31], z1 = z[(n + (m1 % 8)) % 31];
					int even = sf ? (s1 ^ c0) : (s0 ^ c0);
					int odd  = sf ? (s0 ^ c1 ^ z1) : (s1 ^ c1 ^ z0);
					bits |= (uint64_t)even << (2*n);
					bits |= (uint64_t)odd << (2*n + 1);
				}
				sss_bits[nid2][sf][nid1] = bits;
			}
	}
}

/* Search --------------------------------------------------------------------------------------- */

static void symbol(int pos, uint32_t step, int32_t *x, int shift)
{
	/* Symbol at pos, frequency offset removed (x2^shift). */
	memcpy(x, &b.y[2*pos], 2*SYM*sizeof(int32_t));
	dsp_rotate(x, SYM, -step*(uint32_t)pos, -step);
	if (shift)
		for (int n = 0; n < 2*SYM; n++)
			x[n] <<= shift;
}

static int pss_metric(int nid2, int pos, uint32_t step)
{
	/* Normalized PSS correlation (%) at pos, frequency offset removed (phase step per sample). */
	static int32_t x[2*SYM];
	int64_t ar = 0, ai = 0, e = 0;
	symbol(pos, step, x, 0);
	const int32_t *p = pss_t[nid2];
	for (int n = 0; n < SYM; n++) {
		int32_t xr = x[2*n + 0], xi = x[2*n + 1];
		ar += xr*p[2*n + 0] + xi*p[2*n + 1];
		ai += xi*p[2*n + 0] - xr*p[2*n + 1];
		e  += xr*xr + xi*xi;
	}
	ar >>= 10;
	ai >>= 10;
	int64_t d = (e >> 10)*(pss_energy >> 10);
	return d ? (int)((ar*ar + ai*ai)*100/d) : 0;
}

static int sss_decode(int nid2, int pos, int dist, uint32_t step, int *nid1, int *sf)
{
	/* SSS at pos - dist, equalized by the PSS (z = sss*conj(pss)*d): best sequence, metric (%). */
	static int32_t p[2*SYM], s[2*SYM];
	static int32_t z[2*62];
	int64_t ez = 0;
	if (pos - dist < 0)
		return 0;
	symbol(pos, step, p, 6);
	dsp_fft(p, 7, 0);
	symbol(pos - dist, step, s, 6);
	dsp_fft(s, 7, 0);
	for (int n = 0; n < 62; n++) {
		int     k  = subcarrier(n);
		int64_t pr = p[2*k + 0], pi = p[2*k + 1];
		int64_t dr = pss_d[nid2][2*n + 0], di = pss_d[nid2][2*n + 1];
		/* Channel h = p*conj(d), z = s*conj(h). */
		int64_t hr = (pr*dr + pi*di) >> 15;
		int64_t hi = (pi*dr - pr*di) >> 15;
		z[2*n + 0] = (int32_t)((s[2*k + 0]*hr + s[2*k + 1]*hi) >> 16);
		z[2*n + 1] = (int32_t)((s[2*k + 1]*hr - s[2*k + 0]*hi) >> 16);
		ez += ((int64_t)z[2*n]*z[2*n] + (int64_t)z[2*n + 1]*z[2*n + 1]) >> 8;
	}
	int64_t d    = 62*ez/100;
	int     best = 0;
	if (!d)
		return 0;
	for (int f = 0; f < 2; f++)
		for (int i = 0; i < 168; i++) {
			uint64_t bits = sss_bits[nid2][f][i];
			int32_t  mr = 0, mi = 0;
			for (int n = 0; n < 62; n++) {
				if ((bits >> n) & 1) {
					mr -= z[2*n + 0];
					mi -= z[2*n + 1];
				} else {
					mr += z[2*n + 0];
					mi += z[2*n + 1];
				}
			}
			int64_t r = mr >> 4, q = mi >> 4;
			int     m = (int)((r*r + q*q)/d);
			if (m > best) {
				best  = m;
				*nid1 = i;
				*sf   = f;
			}
		}
	return best;
}

int lte_search(const int8_t *iq, int samples, int offset_hz, int wide, struct lte_cell *cell)
{
	/* Channelize (carrier to DC), scaled to peaks of 2^12-2^13. */
	int n = dsp_channelize(iq, samples, dsp_phase_step(-offset_hz, 16000000), &filter, b.y, FFT_N);
	if (n < 4*SYM)
		return 0;
	int32_t peak = 1;
	for (int i = 0; i < 2*n; i++) {
		int32_t a = (b.y[i] < 0) ? -b.y[i] : b.y[i];
		peak = (a > peak) ? a : peak;
	}
	int up = 0, down = 0;
	while ((peak << up) < (1 << 12))
		up++;
	while ((peak >> down) > (1 << 13))
		down++;
	for (int i = 0; i < 2*n; i++)
		b.y[i] = (b.y[i] << up) >> down;
	/* Window energies. */
	int     positions = n - SYM;
	int64_t e = 0;
	for (int i = 0; i < 2*SYM; i++)
		e += b.y[i]*b.y[i];
	for (int i = 0; i < positions; i++) {
		const int32_t *a = &b.y[2*i], *z = &b.y[2*(i + SYM)];
		b.energy[i] = e;
		e += z[0]*z[0] + z[1]*z[1] - a[0]*a[0] - a[1]*a[1];
	}
	/* Spectrum (x2^10: FFT scaled by 1/2048). */
	for (int i = 0; i < 2*FFT_N; i++)
		b.f[i] = (i < 2*n) ? b.y[i] << 10 : 0;
	dsp_fft(b.f, FFT_LOG2, 0);
	/* PSS: correlation (F*conj(P) shifted by the frequency hypothesis), best |c|^2/energy. */
	int     best_nid2 = -1, best_pos = 0, best_cfo = 0, best_m = 0;
	int     hyps = wide ? 1 : 0;
	for (int nid2 = 0; nid2 < 3; nid2++)
		for (int h = -hyps; h <= hyps; h++) {
			const int32_t *p = b.pss_f[nid2];
			for (int k = 0; k < FFT_N; k++) {
				int     kp = (k - h*CFO_BINS) & (FFT_N - 1);
				int64_t fr = b.f[2*k + 0], fi = b.f[2*k + 1];
				int64_t pr = p[2*kp + 0], pi = p[2*kp + 1];
				b.c[2*k + 0] = (int32_t)((fr*pr + fi*pi) >> 12);
				b.c[2*k + 1] = (int32_t)((fi*pr - fr*pi) >> 12);
			}
			dsp_fft(b.c, FFT_LOG2, 1);
			for (int i = 0; i < positions; i++) {
				/* |c|^2/energy (log: no overflow/division). */
				int64_t c2 = (int64_t)b.c[2*i]*b.c[2*i] + (int64_t)b.c[2*i + 1]*b.c[2*i + 1];
				int     m  = dsp_db4(c2 + 1) - dsp_db4(b.energy[i] + 1);
				if (best_nid2 < 0 || m > best_m) {
					best_m    = m;
					best_nid2 = nid2;
					best_pos  = i;
					best_cfo  = h*CFO_BINS*LTE_RATE/FFT_N;
				}
			}
		}
	if (best_nid2 < 0)
		return 0;
	/* Refinement: frequency offset and position, exact metric. */
	int best = 0, pos = best_pos, cfo = best_cfo;
	for (int f = best_cfo - CFO_SPAN; f <= best_cfo + CFO_SPAN; f += CFO_STEP)
		for (int p = best_pos - 2; p <= best_pos + 2; p++) {
			if (p < 0 || p >= positions)
				continue;
			int m = pss_metric(best_nid2, p, dsp_phase_step(f, LTE_RATE));
			if (m > best) {
				best = m;
				pos  = p;
				cfo  = f;
			}
		}
	if (best < PSS_MIN)
		return 0;
	/* SSS: FDD/TDD. */
	uint32_t step = dsp_phase_step(cfo, LTE_RATE);
	int nid1 = 0, sf = 0, tdd = 0;
	int sss  = sss_decode(best_nid2, pos, SSS_FDD, step, &nid1, &sf);
	int nid1_tdd = 0, sf_tdd = 0;
	int sss_tdd  = sss_decode(best_nid2, pos, SSS_TDD, step, &nid1_tdd, &sf_tdd);
	if (sss_tdd > sss) {
		sss  = sss_tdd;
		nid1 = nid1_tdd;
		sf   = sf_tdd;
		tdd  = 1;
	}
	if (sss < SSS_MIN)
		return 0;
	cell->nid1     = nid1;
	cell->nid2     = best_nid2;
	cell->pci      = 3*nid1 + best_nid2;
	cell->tdd      = tdd;
	cell->subframe = sf ? 5 : 0;
	cell->cfo_hz   = cfo;
	cell->pss      = best;
	cell->sss      = sss;
	cell->pos      = pos;
	return 1;
}

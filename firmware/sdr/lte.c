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

/* MIB ------------------------------------------------------------------------------------------ */

#define PBCH_RE   240                 /* PBCH resource elements per frame (normal CP). */
#define PBCH_BITS 480
#define MIB_BITS  24

static void gold(uint32_t cinit, int n0, int n, uint8_t *c)
{
	/* Gold sequence c(n0..n0 + n - 1) (36.211 7.2): x1(0) = 1, x2 from cinit, Nc = 1600. */
	uint32_t x1 = 1, x2 = cinit;
	for (int i = 0; i < 1600 + n0 + n; i++) {
		if (i >= 1600 + n0)
			c[i - 1600 - n0] = (x1 ^ x2) & 1;
		uint32_t f1 = ((x1 >> 3) ^ x1) & 1;
		uint32_t f2 = ((x2 >> 3) ^ (x2 >> 2) ^ (x2 >> 1) ^ x2) & 1;
		x1 = (x1 >> 1) | (f1 << 30);
		x2 = (x2 >> 1) | (f2 << 30);
	}
}

static int pbch_subcarrier(int k)
{
	/* Central 72 subcarriers (k = 0..71) -> FFT index (DC skipped). */
	return (k < 36) ? SYM - 36 + k : k - 35;
}

static uint8_t rm[120];               /* Rate matching: coded bit (stream*40 + k) of w (no nulls). */

static void rm_init(void)
{
	/* Sub-block interleaver (convolutional codes, 36.212 5.1.4.2): 2 rows x 32 columns, 24 nulls
	   first, columns permuted, read column by column; streams one after the other. */
	static const uint8_t perm[32] = {
		1, 17, 9, 25, 5, 21, 13, 29, 3, 19, 11, 27, 7, 23, 15, 31,
		0, 16, 8, 24, 4, 20, 12, 28, 2, 18, 10, 26, 6, 22, 14, 30,
	};
	int n = 0;
	for (int s = 0; s < 3; s++)
		for (int col = 0; col < 32; col++)
			for (int row = 0; row < 2; row++) {
				int i = row*32 + perm[col] - 24;
				if (i >= 0)
					rm[n++] = s*40 + i;
			}
}

static int conv_parity(int reg, int g)
{
	/* Output bit of generator g for the 7-bit register (bit 6: current input). */
	int v = reg & g;
	v ^= v >> 4;
	v ^= v >> 2;
	v ^= v >> 1;
	return v & 1;
}

static void viterbi(const int32_t *llr, uint8_t *bits)
{
	/* Tail-biting K=7 rate 1/3 (133, 171, 165 octal) decoder: wrap-around Viterbi (2 passes over the
	   40 bits, all states equally likely at the start, traceback from the best end state).
	   llr[3*k + i] > 0: coded bit 0. */
	static const int g[3] = {0133, 0171, 0165};
	static int32_t metric[64], next[64];
	static uint8_t from[80][64];
	memset(metric, 0, sizeof(metric));
	for (int t = 0; t < 80; t++) {
		const int32_t *l = &llr[3*(t % 40)];
		for (int s = 0; s < 64; s++)
			next[s] = INT32_MIN;
		for (int s = 0; s < 64; s++)
			for (int b = 0; b < 2; b++) {
				int reg = (b << 6) | s, ns = (s >> 1) | (b << 5);
				int32_t m = metric[s];
				for (int i = 0; i < 3; i++)
					m += conv_parity(reg, g[i]) ? -l[i] : l[i];
				if (m > next[ns]) {
					next[ns]     = m;
					from[t][ns]  = s;
				}
			}
		memcpy(metric, next, sizeof(metric));
	}
	int s = 0;
	for (int i = 1; i < 64; i++)
		s = (metric[i] > metric[s]) ? i : s;
	for (int t = 79; t >= 0; t--) {
		if (t < 40 + 40 && t >= 40)
			bits[t - 40] = (s >> 5) & 1;
		s = from[t][s];
	}
}

int lte_mib(const struct lte_cell *cell, struct lte_mib *mib)
{
	static uint8_t c[1920];
	static int32_t sym[4][2*SYM];
	static int32_t h[2][2*72];
	static int32_t re[2*PBCH_RE], hre[2][2*PBCH_RE];
	static int32_t soft[PBCH_BITS], llr[120];
	if (!rm[1])
		rm_init();
	/* PBCH symbols (subframe 0 slot 1, 0-3): FDD after the PSS (end of slot 0), TDD before it
	   (subframe 1 slot 2 symbol 2). */
	if (cell->subframe != 0)
		return 0;
	int slot1 = cell->tdd ? cell->pos - 9 - 275 - 960 : cell->pos + SYM;
	if (slot1 < 0 || slot1 + 10 + 3*137 + SYM > FFT_N)
		return 0;
	uint32_t step = dsp_phase_step(cell->cfo_hz, LTE_RATE);
	for (int l = 0; l < 4; l++) {
		symbol(slot1 + 10 + l*137, step, sym[l], 6);
		dsp_fft(sym[l], 7, 0);
	}
	/* Channel (ports 0/1): CRS of symbol 0 (every 6 subcarriers, shift PCI mod 6), linear
	   interpolation; CRS: QPSK from c(2m), c(2m + 1), m = 104..115 (central 6 RBs). */
	int vshift = cell->pci % 6;
	gold((1 << 10)*(7*(1 + 1) + 0 + 1)*(2*cell->pci + 1) + 2*cell->pci + 1, 2*104, 24, c);
	for (int p = 0; p < 2; p++) {
		int32_t pr[12], pi[12];
		for (int n = 0; n < 12; n++) {
			int k = 6*n + (3*p + vshift) % 6, f = pbch_subcarrier(k);
			int32_t yr = sym[0][2*f] >> 4, yi = sym[0][2*f + 1] >> 4;
			int32_t rr = c[2*n] ? -1 : 1, ri = c[2*n + 1] ? -1 : 1;
			/* h = y*conj(r). */
			pr[n] = yr*rr + yi*ri;
			pi[n] = yi*rr - yr*ri;
		}
		for (int k = 0; k < 72; k++) {
			int k0 = (3*p + vshift) % 6, n = (k - k0)/6;
			n = (k < k0) ? 0 : (n > 10) ? 10 : n;
			int f = k - (6*n + k0);
			f = (f < 0) ? 0 : (f > 6) ? 6 : f;
			h[p][2*k + 0] = (pr[n]*(6 - f) + pr[n + 1]*f)/6;
			h[p][2*k + 1] = (pi[n]*(6 - f) + pi[n + 1]*f)/6;
		}
	}
	/* PBCH resource elements: k then l, CRS positions (4 ports) of symbols 0/1 excluded. */
	int n = 0;
	for (int l = 0; l < 4; l++)
		for (int k = 0; k < 72; k++) {
			if (l < 2 && (k - vshift + 6) % 3 == 0)
				continue;
			int f = pbch_subcarrier(k);
			re[2*n + 0] = sym[l][2*f + 0] >> 4;
			re[2*n + 1] = sym[l][2*f + 1] >> 4;
			for (int p = 0; p < 2; p++) {
				hre[p][2*n + 0] = h[p][2*k + 0] >> 4;
				hre[p][2*n + 1] = h[p][2*k + 1] >> 4;
			}
			n++;
		}
	for (int ports = 1; ports <= 2; ports++) {
		/* Equalization: single port (x = r*conj(h0)) or SFBC pairs (Alamouti). */
		for (int i = 0; i < PBCH_RE; i += (ports == 1) ? 1 : 2) {
			const int32_t *r0 = &re[2*i], *h0 = &hre[0][2*i], *h1 = &hre[1][2*i];
			if (ports == 1) {
				soft[2*i + 0] = (r0[0]*h0[0] + r0[1]*h0[1]) >> 8;
				soft[2*i + 1] = (r0[1]*h0[0] - r0[0]*h0[1]) >> 8;
				continue;
			}
			const int32_t *r1 = &re[2*i + 2];
			/* x0 = conj(h0)*r0 + h1*conj(r1), x1 = -h1*conj(r0) + conj(h0)*r1. */
			soft[2*i + 0] = ((h0[0]*r0[0] + h0[1]*r0[1]) + (h1[0]*r1[0] + h1[1]*r1[1])) >> 8;
			soft[2*i + 1] = ((h0[0]*r0[1] - h0[1]*r0[0]) + (h1[1]*r1[0] - h1[0]*r1[1])) >> 8;
			soft[2*i + 2] = ((h0[0]*r1[0] + h0[1]*r1[1]) - (h1[0]*r0[0] + h1[1]*r0[1])) >> 8;
			soft[2*i + 3] = ((h0[0]*r1[1] - h0[1]*r1[0]) - (h1[1]*r0[0] - h1[0]*r0[1])) >> 8;
		}
		/* Descrambling (c_init = PCI) for each frame of the 4 (40ms PBCH TTI), rate dematching
		   (4 repetitions per frame), Viterbi, CRC (masks: 1/2/4 ports). */
		gold(cell->pci, 0, 1920, c);
		for (int f = 0; f < 4; f++) {
			memset(llr, 0, sizeof(llr));
			for (int j = 0; j < PBCH_BITS; j++) {
				int     k = rm[j % 120];
				int32_t v = c[PBCH_BITS*f + j] ? -soft[j] : soft[j];
				/* llr order for the decoder: [k][stream]. */
				llr[3*(k % 40) + k/40] += v;
			}
			/* Normalized (|llr| < 2^12: path metrics in 32 bits). */
			int32_t top = 1;
			for (int i = 0; i < 120; i++)
				top = (llr[i] > top) ? llr[i] : (-llr[i] > top) ? -llr[i] : top;
			for (int i = 0; i < 120; i++)
				llr[i] = (int32_t)(((int64_t)llr[i] << 12)/top);
			uint8_t bits[40];
			viterbi(llr, bits);
			uint16_t crc = 0, rx = 0;
			for (int i = 0; i < MIB_BITS; i++) {
				int fb = ((crc >> 15) & 1) ^ bits[i];
				crc <<= 1;
				if (fb)
					crc ^= 0x1021;
			}
			for (int i = 0; i < 16; i++)
				rx = (rx << 1) | bits[MIB_BITS + i];
			int mask_ports = (rx ^ crc) == 0 ? 1 : (rx ^ crc) == 0xffff ? 2 : (rx ^ crc) == 0x5555 ? 4 : 0;
			if (mask_ports != ports)
				continue;
			static const int rbs[8] = {6, 15, 25, 50, 75, 100, 0, 0};
			int a = 0;
			for (int i = 0; i < MIB_BITS; i++)
				a = (a << 1) | bits[i];
			mib->ports          = ports;
			mib->rbs            = rbs[(a >> 21) & 7];
			mib->phich_extended = (a >> 20) & 1;
			mib->phich_ng       = (a >> 18) & 3;
			mib->sfn            = 4*((a >> 10) & 0xff) + f;
			return mib->rbs != 0;
		}
	}
	return 0;
}

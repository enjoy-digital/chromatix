// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// 2.4GHz signal identification, see classify.h.

#include <stdint.h>
#include <string.h>

#include "dsp.h"
#include "classify.h"

#define NF        SIG_BINS
#define LOG2_NF   7
#define RATE      80000000
#define BIN_KHZ   (RATE/1000/NF)                 /* 625kHz. */
#define FRAME_NS  (1000000000/(RATE/NF))         /* 1600ns. */
#define FRAMES    (DSP_MAX_SAMPLES/NF)
#define USABLE    (SIG_USABLE_KHZ/BIN_KHZ)       /* +-35 bins. */
#define B0        (NF/2 - USABLE)
#define B1        (NF/2 + USABLE)
#define SMOOTH    8                              /* Frames averaged (12.8us). */
#define THRESHOLD (6*4)                          /* Above the floor (dB4). */
#define MIN_US    30                             /* Shorter bursts ignored (glitches). */
#define SLOTS     4
#define MAX_COMP  64

/* Buffers (offset in the direct mapped 8KB data cache). */
static struct {
	int32_t  x[2*NF];                            /* FFT. */
	uint32_t p[FRAMES][NF];                      /* Power (>> 4). */
	int16_t  db[FRAMES][NF];                     /* Smoothed (dB4). */
	uint8_t  mask[FRAMES][NF];                   /* Cells above the floor, then labels. */
} b;

static uint16_t spur[SLOTS][NF];                 /* Persistent bins average (Q8). */
static uint16_t stack[FRAMES*NF];

static const char *const names[SIG_CLASSES] = {
	"WIFI", "WIFI 40MHZ", "10MHZ OFDM", "DJI DRONEID", "ANALOG VIDEO", "MICROWAVE OVEN", "802.15.4",
	"BLE ADV", "NARROWBAND", "CARRIER", "BROADBAND", "UNKNOWN",
};

const char *sig_name(int cls)
{
	return (cls < SIG_CLASSES) ? names[cls] : "?";
}

int sig_drone(int cls)
{
	return cls == SIG_OFDM10 || cls == SIG_DRONEID || cls == SIG_VIDEO;
}

void sig_init(void)
{
	memset(spur, 0, sizeof(spur));
}

static int floor_db(int f, int frames)
{
	/* Bin noise floor: 30th percentile over the frames (histogram of the dB4 values). */
	static uint8_t hist[512];
	memset(hist, 0, sizeof(hist));
	for (int t = 0; t < frames; t++) {
		int v = b.db[t][f] >> 1;
		hist[(v < 0) ? 0 : (v > 511) ? 511 : v]++;
	}
	int target = frames*30/100, sum = 0;
	for (int i = 0; i < 512; i++) {
		sum += hist[i];
		if (sum > target)
			return 2*i;
	}
	return 1022;
}

static int near(int khz, int base, int step, int count, int tolerance)
{
	/* Frequency within tolerance of base + k*step (k < count), returns k or -1. */
	int k = (khz - base + step/2)/step;
	if (khz - base + step/2 < 0 || k >= count)
		return -1;
	int d = khz - (base + k*step);
	return (d >= -tolerance && d <= tolerance) ? k : -1;
}

static int classify(int khz, int bw, int us, int continuous, int edge, int drift_khz)
{
	/* Wideband first (Wi-Fi channel centers before drone/video/oven classes), bursts clipped at the
	   band edge: unknown (signals outside of the band, LTE B40 TDD below 2400MHz). */
	static const int droneid[] = {2399500, 2414500, 2429500, 2444500, 2459500, 2474500};
	int wifi = near(khz, 2412000, 5000, 13, 2500) >= 0;
	if (bw >= 41000)
		return SIG_BROADBAND;
	if (bw >= 30000)
		return SIG_WIFI40;
	if (bw >= 14000) {
		if (continuous && !wifi)
			return (drift_khz > 3000) ? SIG_MICROWAVE : SIG_VIDEO;
		return SIG_WIFI;
	}
	if (edge)
		return SIG_UNKNOWN;
	if (bw >= 7000) {
		if (continuous)
			return wifi ? SIG_WIFI : (drift_khz > 3000) ? SIG_MICROWAVE : SIG_VIDEO;
		for (int i = 0; i < 6; i++)
			if (khz - droneid[i] >= -1000 && khz - droneid[i] <= 1000 && us >= 150 && bw <= 11500)
				return SIG_DRONEID;
		if (wifi || us < 150)
			return SIG_WIFI; /* Wi-Fi frame partially above the floor. */
		return SIG_OFDM10;
	}
	if (bw >= 4000)
		return SIG_UNKNOWN;
	if (continuous && bw <= 1300)
		return SIG_CARRIER;
	if (bw >= 1800 && us >= 120 && near(khz, 2405000, 5000, 16, 700) >= 0)
		return SIG_ZIGBEE;
	if (bw <= 2600) {
		if (near(khz, 2402000, 24000, 2, 600) >= 0 || (khz >= 2479400 && khz <= 2480600))
			return SIG_BLE_ADV;
		return SIG_NARROW;
	}
	return SIG_UNKNOWN;
}

int sig_detect(const int8_t *iq, int samples, int center_khz, int slot, struct sig *sigs, int max,
	int16_t *columns)
{
	int frames = samples/NF;
	frames = (frames > FRAMES) ? FRAMES : frames;
	slot  %= SLOTS;
	/* Spectrogram (DC removed per frame, ESP32 inverted spectrum corrected, DC centered). */
	for (int t = 0; t < frames; t++) {
		const int8_t *s = &iq[2*NF*t];
		int32_t dc_i = 0, dc_q = 0;
		for (int n = 0; n < NF; n++) {
			dc_i += s[2*n + 0];
			dc_q += s[2*n + 1];
		}
		for (int n = 0; n < NF; n++) {
			/* Hann window (spectrum table subsampled: leakage of strong narrowband bursts). */
			int32_t w = hann[n*(FFT_SIZE/NF)];
			b.x[2*n + 0] =  (((s[2*n + 0] << 7) - (dc_i << 7)/NF)*w) >> 11;
			b.x[2*n + 1] = -((((s[2*n + 1] << 7) - (dc_q << 7)/NF)*w) >> 11);
		}
		dsp_fft(b.x, LOG2_NF, 0);
		for (int k = 0; k < NF; k++) {
			int     i = (k + NF/2) & (NF - 1);
			int64_t r = b.x[2*i + 0], q = b.x[2*i + 1];
			b.p[t][k] = (uint32_t)((r*r + q*q) >> 12);
		}
	}
	/* Smoothed (SMOOTH frames), dB4; columns (max). */
	for (int f = 0; f < NF; f++) {
		uint32_t sum = 0;
		int16_t  top = 0;
		for (int t = 0; t < frames; t++) {
			sum += b.p[t][f] >> 2;
			if (t >= SMOOTH)
				sum -= b.p[t - SMOOTH][f] >> 2;
			int n = (t < SMOOTH) ? t + 1 : SMOOTH;
			b.db[t][f] = dsp_db4(sum/n + 1);
			top = (b.db[t][f] > top) ? b.db[t][f] : top;
		}
		if (columns)
			columns[f] = top;
	}
	/* Mask: above the bin floor, persistent lines (receiver spurs: runs of <= 2 persistent bins)
	   excluded, gaps < 3 frames filled. */
	/* Floor: per bin (filter shape), at most the band floor + 2dB (bins occupied most of the
	   capture: long bursts). */
	static int floors[NF], sorted[NF];
	for (int f = B0; f < B1; f++) {
		floors[f] = floor_db(f, frames);
		int j = f - B0;
		for (; j > 0 && sorted[j - 1] > floors[f]; j--)
			sorted[j] = sorted[j - 1];
		sorted[j] = floors[f];
	}
	int band = sorted[(B1 - B0)/2] + 2*4;
	memset(b.mask, 0, sizeof(b.mask));
	for (int f = B0; f < B1; f++) {
		int threshold = ((floors[f] < band) ? floors[f] : band) + THRESHOLD;
		for (int t = 0; t < frames; t++)
			b.mask[t][f] = (b.db[t][f] > threshold);
		/* Always on (floor 6dB above the band floor) over the captures: spur (fast rise, slow
		   decay). */
		if (floors[f] > band + 4*4)
			spur[slot][f] += (256 - spur[slot][f])/2;
		else
			spur[slot][f] -= spur[slot][f]/16;
	}
	for (int f = B0; f < B1; ) {
		int n = 0;
		while (f + n < B1 && spur[slot][f + n] > 128)
			n++;
		if (n > 0 && n <= 2)
			for (int k = f; k < f + n; k++)
				for (int t = 0; t < frames; t++)
					b.mask[t][k] = 0;
		f += n ? n : 1;
	}
	for (int f = B0; f < B1; f++)
		for (int t = 1, last = -1; t < frames; t++)
			if (b.mask[t][f]) {
				if (last >= 0 && t - last > 1 && t - last <= 3)
					for (int k = last + 1; k < t; k++)
						b.mask[k][f] = 1;
				last = t;
			}
	/* Bursts: connected cells (labels 2..). */
	int count = 0, label = 2;
	for (int t0 = 0; t0 < frames; t0++)
		for (int f0 = B0; f0 < B1; f0++) {
			if (b.mask[t0][f0] != 1 || label > MAX_COMP + 1)
				continue;
			int sp = 0;
			int tmin = t0, tmax = t0, fmin = f0, fmax = f0, top = 0;
			stack[sp++] = t0*NF + f0;
			b.mask[t0][f0] = label;
			while (sp) {
				int c = stack[--sp], t = c/NF, f = c % NF;
				tmin = (t < tmin) ? t : tmin;
				tmax = (t > tmax) ? t : tmax;
				fmin = (f < fmin) ? f : fmin;
				fmax = (f > fmax) ? f : fmax;
				top  = (b.db[t][f] > top) ? b.db[t][f] : top;
				static const int dt[4] = {-1, 1, 0, 0}, df[4] = {0, 0, -1, 1};
				for (int d = 0; d < 4; d++) {
					int tt = t + dt[d], ff = f + df[d];
					if (tt < 0 || tt >= frames || ff < B0 || ff >= B1 || b.mask[tt][ff] != 1)
						continue;
					b.mask[tt][ff] = label;
					stack[sp++] = tt*NF + ff;
				}
			}
			int dur = tmax - tmin + 1, us = dur*FRAME_NS/1000;
			if (us < MIN_US || count >= max) {
				label++;
				continue;
			}
			/* Bandwidth: bins active for >= 30% of the burst, mean level within 12dB of the
			   strongest bin's (window leakage, GFSK sidelobes of strong bursts excluded). Drift:
			   power centroid at the start and the end. */
			static int level[NF];
			int g0 = -1, g1 = -1, peak = 0;
			for (int f = fmin; f <= fmax; f++) {
				int n = 0, s = 0;
				for (int t = tmin; t <= tmax; t++)
					if (b.mask[t][f] == label) {
						n++;
						s += b.db[t][f];
					}
				level[f] = (n*10 >= dur*3) ? s/n : -1;
				peak = (level[f] > peak) ? level[f] : peak;
			}
			for (int f = fmin; f <= fmax; f++)
				if (level[f] >= 0 && level[f] >= peak - 12*4) {
					g0 = (g0 < 0) ? f : g0;
					g1 = f;
				}
			if (g0 < 0) {
				label++;
				continue;
			}
			int64_t c[2] = {0, 0}, w[2] = {0, 0};
			for (int t = tmin; t <= tmax; t++) {
				int h = (t - tmin)*2/dur;
				for (int f = fmin; f <= fmax; f++)
					if (b.mask[t][f] == label) {
						c[h] += (int64_t)f*b.db[t][f];
						w[h] += b.db[t][f];
					}
			}
			int drift = (w[0] && w[1]) ? (int)((c[1]*w[0] - c[0]*w[1])*BIN_KHZ/(w[0]*w[1])) : 0;
			drift = (drift < 0) ? -drift : drift;
			struct sig *s = &sigs[count++];
			s->khz       = center_khz + ((g0 + g1 + 1)*BIN_KHZ)/2 - (NF/2)*BIN_KHZ;
			s->bw_khz    = (g1 - g0 + 1)*BIN_KHZ;
			s->us        = us;
			s->level_db4 = top;
			s->truncated = (tmin == 0) || (tmax == frames - 1);
			s->cls       = classify(s->khz, s->bw_khz, us, dur*10 >= frames*9,
				fmin == B0 || fmax == B1 - 1, drift);
			/* Narrowband on bins occupied in most captures: carrier/interferer. */
			int persistent = 0;
			for (int f = g0; f <= g1; f++)
				persistent += spur[slot][f];
			if (s->bw_khz < 4000 && persistent > 128*(g1 - g0 + 1))
				s->cls = SIG_CARRIER;
			label++;
		}
	return count;
}

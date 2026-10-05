// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// DECT bursts decoder, see dect.h (ETSI EN 300 175-2: physical layer, 300 175-3: MAC layer A-field,
// R-CRC).

#include <stdint.h>
#include <string.h>

#include "dsp.h"
#include "dect.h"

#define SPB       4                       /* Samples per bit (4.608MS/s). */
#define MAX_OUT   4800
#define SYNC_RFP  0xAAAAE98Au             /* S-field: preamble + sync word (MSB first). */
#define SYNC_PP   0x55551675u
#define SYNC_ERRS 2
#define CRC_POLY  0x0589                  /* x^16 + x^10 + x^8 + x^7 + x^3 + 1. */

static const struct dsp_filter filter = {filter_dect, FILTER_DECT_TAPS, FILTER_DECT_UP,
	FILTER_DECT_DOWN};

/* Buffers (offset in the direct mapped 8KB data cache). */
static struct {
	int32_t y[2*MAX_OUT];                 /* Channelized capture (complex interleaved). */
	int32_t pad0[512];
	int32_t disc[MAX_OUT];
	int32_t pad1[512];
	int32_t soft[MAX_OUT];
} b;

int dect_carriers(int region)
{
	return (region == DECT_US) ? 5 : 10;
}

int dect_carrier_khz(int region, int carrier)
{
	/* F_c = F_0 - c*1.728MHz. */
	return ((region == DECT_US) ? 1928448 : 1897344) - 1728*carrier;
}

const char *dect_ta_name(int ta)
{
	static const char *names[8] = {"CT", "CT", "NT CL", "NT", "QT", "ESC", "MT", "PT"};
	return names[ta & 7];
}

static int popcount32(uint32_t x)
{
	int n = 0;
	for (; x; x &= x - 1)
		n++;
	return n;
}

static uint16_t rcrc(const uint8_t *a)
{
	/* R-CRC: CRC-16 (CRC_POLY) of the A-field header + tail (48 bits), last bit inverted. */
	uint16_t crc = 0;
	for (int i = 0; i < 6; i++)
		for (int k = 7; k >= 0; k--) {
			int fb = ((crc >> 15) & 1) ^ ((a[i] >> k) & 1);
			crc <<= 1;
			if (fb)
				crc ^= CRC_POLY;
		}
	return crc ^ 1;
}

int dect_decode(const int8_t *iq, int samples, int offset_hz, struct dect_burst *bursts, int max)
{
	int n = dsp_channelize(iq, samples, dsp_phase_step(-offset_hz, 16000000), &filter, b.y, MAX_OUT);
	n = dsp_fm_soft(b.y, n, SPB, b.disc, b.soft) - 1;
	/* S-field search (32 bits ending at i, MSB first): RFP/PP, both polarities. */
	int count = 0, skip = 0;
	for (int i = 32*SPB; i + 64*SPB < n && count < max; i++) {
		if (i < skip)
			continue;
		uint32_t sr = 0;
		for (int k = 0; k < 32; k++)
			sr = (sr << 1) | (b.soft[i - (32 - k)*SPB] > 0);
		int rfp = -1, invert = 0;
		if (popcount32(sr ^ SYNC_RFP) <= SYNC_ERRS)
			rfp = 1;
		else if (popcount32(sr ^ SYNC_PP) <= SYNC_ERRS)
			rfp = 0;
		else if (popcount32(~sr ^ SYNC_RFP) <= SYNC_ERRS)
			rfp = 1, invert = 1;
		else if (popcount32(~sr ^ SYNC_PP) <= SYNC_ERRS)
			rfp = 0, invert = 1;
		if (rfp < 0)
			continue;
		/* A-field (64 bits), R-CRC. */
		struct dect_burst *d = &bursts[count];
		memset(d->a, 0, sizeof(d->a));
		for (int k = 0; k < 64; k++)
			d->a[k/8] |= ((b.soft[i + k*SPB] > 0) ^ invert) << (7 - k % 8);
		if (rcrc(d->a) != ((d->a[6] << 8) | d->a[7]))
			continue;
		d->rfp = rfp;
		d->ta  = d->a[0] >> 5;
		d->rfpi = 0;
		if (rfp && d->ta == DECT_TA_NT)
			for (int k = 1; k < 6; k++)
				d->rfpi = (d->rfpi << 8) | d->a[k];
		d->pos = i;
		int64_t e = 0;
		for (int k = i - 32*SPB; k < i + 64*SPB; k++)
			e += (int64_t)b.y[2*k]*b.y[2*k] + (int64_t)b.y[2*k + 1]*b.y[2*k + 1];
		d->level_db4 = dsp_db4(e/(96*SPB));
		count++;
		skip = i + 64*SPB;
	}
	return count;
}

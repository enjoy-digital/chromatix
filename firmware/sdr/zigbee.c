// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// IEEE 802.15.4 frames decoder, see zigbee.h (IEEE 802.15.4-2020 12.2: O-QPSK PHY, 7.2: MAC frame).

#include <stdint.h>
#include <string.h>

#include "dsp.h"
#include "zigbee.h"

#define SPC       4                        /* Samples per chip (8MS/s). */
#define SYM       (32*SPC)                 /* Samples per symbol. */
#define MAX_OUT   8200
#define MAX_ERRS  6                        /* Chip transition errors per symbol (of 31). */

static const struct dsp_filter filter = {filter_zigbee, FILTER_ZIGBEE_TAPS, FILTER_ZIGBEE_UP,
	FILTER_ZIGBEE_DOWN};

/* Chip sequences (c0 first, MSB). */
static const uint32_t chips[16] = {
	0xd9c3522e, 0xed9c3522, 0x2ed9c352, 0x22ed9c35, 0x522ed9c3, 0x3522ed9c, 0xc3522ed9, 0x9c3522ed,
	0x8c96077b, 0xb8c96077, 0x7b8c9607, 0x77b8c960, 0x077b8c96, 0x6077b8c9, 0x96077b8c, 0xc96077b8,
};

/* Buffers (offset in the direct mapped 8KB data cache). */
static struct {
	int32_t  y[2*MAX_OUT];                 /* Channelized capture (complex interleaved). */
	int32_t  pad0[512];
	int32_t  disc[MAX_OUT];
	int32_t  pad1[512];
	int32_t  soft[MAX_OUT];
	int32_t  pad2[512];
	uint32_t word[MAX_OUT];                /* Frequency signs of the 31 transitions from i. */
} b;

static uint32_t patterns[16];              /* Frequency signs of each symbol's transitions. */

int zb_channel_khz(int channel)
{
	return 2405000 + 5000*(channel - 11);
}

static int popcount32(uint32_t x)
{
	int n = 0;
	for (; x; x &= x - 1)
		n++;
	return n;
}

static void init(void)
{
	/* Transition k (0-30): positive frequency if !(c[k] ^ c[k + 1] ^ (k odd)) (bit 30 - k). */
	for (int s = 0; s < 16; s++) {
		uint32_t p = 0;
		for (int k = 0; k < 31; k++) {
			int ck  = (chips[s] >> (31 - k)) & 1;
			int ck1 = (chips[s] >> (30 - k)) & 1;
			p = (p << 1) | !(ck ^ ck1 ^ (k & 1));
		}
		patterns[s] = p;
	}
}

static int symbol(int pos, int *errors)
{
	/* Best matching symbol at pos (chip 0 start). */
	uint32_t w = b.word[pos];
	int best = 0, err = 32;
	for (int s = 0; s < 16; s++) {
		int e = popcount32(w ^ patterns[s]);
		if (e < err) {
			err  = e;
			best = s;
		}
	}
	*errors = err;
	return best;
}

static int parse(struct zb_frame *f)
{
	/* MAC header: frame control, sequence, addressing fields. */
	const uint8_t *p = f->psdu;
	int fc  = p[0] | (p[1] << 8), i = 3;
	f->type     = fc & 7;
	f->seq      = p[2];
	f->dst_mode = (fc >> 10) & 3;
	f->src_mode = (fc >> 14) & 3;
	f->pan      = -1;
	f->dst      = 0;
	f->src      = 0;
	int compress = (fc >> 6) & 1;
	if (f->dst_mode) {
		f->pan = p[i] | (p[i + 1] << 8);
		i += 2;
		for (int k = (f->dst_mode == 3) ? 7 : 1; k >= 0; k--)
			f->dst = (f->dst << 8) | p[i + k];
		i += (f->dst_mode == 3) ? 8 : 2;
	}
	if (f->src_mode) {
		if (!compress || !f->dst_mode) {
			f->pan = (f->pan < 0) ? (p[i] | (p[i + 1] << 8)) : f->pan;
			i += 2;
		}
		for (int k = (f->src_mode == 3) ? 7 : 1; k >= 0; k--)
			f->src = (f->src << 8) | p[i + k];
		i += (f->src_mode == 3) ? 8 : 2;
	}
	return i <= f->length - 2;
}

int zb_decode(const int8_t *iq, int samples, int offset_hz, struct zb_frame *frames, int max)
{
	if (!patterns[0])
		init();
	int n = dsp_channelize(iq, samples, dsp_phase_step(-offset_hz, 16000000), &filter, b.y, MAX_OUT);
	n = dsp_fm_soft(b.y, n, SPC, b.disc, b.soft);
	/* Transition signs words: interval k of a symbol at i is soft[i + SPC + k*SPC]. */
	int words = n - 32*SPC;
	for (int i = 0; i < words; i++) {
		uint32_t w = 0;
		for (int k = 0; k < 31; k++)
			w = (w << 1) | (b.soft[i + SPC + k*SPC] > 0);
		b.word[i] = w;
	}
	/* Sync: preamble symbol 0, SFD (0xa7: symbols 7, 10). */
	int count = 0;
	for (int i = SYM; i + 3*SYM < words && count < max; i++) {
		if (popcount32(b.word[i] ^ patterns[7]) > MAX_ERRS)
			continue;
		int e0, e1, e2;
		if (symbol(i - SYM, &e0) != 0 || symbol(i + SYM, &e1) != 10 || e0 > MAX_ERRS || e1 > MAX_ERRS)
			continue;
		/* Best chip alignment around i (+-1 sample). */
		int pos = i, best = 32*3;
		for (int k = i - 1; k <= i + 1; k++) {
			int a, c, d;
			symbol(k - SYM, &a);
			symbol(k, &c);
			symbol(k + SYM, &d);
			if (a + c + d < best) {
				best = a + c + d;
				pos  = k;
			}
		}
		/* PHR, PSDU (symbols: low nibble first). */
		struct zb_frame *f = &frames[count];
		int s = pos + 2*SYM, worst = 0;
		int lo = symbol(s, &e1), hi = symbol(s + SYM, &e2);
		f->length = (lo | (hi << 4)) & 0x7f;
		if (f->length < 5 || f->length > ZB_PSDU_MAX || s + (2 + 2*f->length)*SYM > words)
			continue;
		for (int k = 0; k < f->length; k++) {
			int ea, eb;
			int l = symbol(s + (2 + 2*k)*SYM, &ea);
			int h = symbol(s + (3 + 2*k)*SYM, &eb);
			f->psdu[k] = l | (h << 4);
			worst = (ea > worst) ? ea : worst;
			worst = (eb > worst) ? eb : worst;
		}
		/* FCS: CRC-16 ITU-T (LSB first, init 0). */
		uint16_t crc = 0;
		for (int k = 0; k < f->length - 2; k++)
			for (int j = 0; j < 8; j++) {
				int fb = (crc ^ (f->psdu[k] >> j)) & 1;
				crc >>= 1;
				if (fb)
					crc ^= 0x8408;
			}
		if (crc != (f->psdu[f->length - 2] | (f->psdu[f->length - 1] << 8)))
			continue;
		if (f->length > 5 && !parse(f))
			continue;
		if (f->length == 5) {
			/* ACK: frame control, sequence. */
			f->type = f->psdu[0] & 7;
			f->seq  = f->psdu[2];
			f->pan  = -1;
			f->dst_mode = f->src_mode = 0;
			f->dst = f->src = 0;
		}
		f->pos    = pos;
		f->errors = worst;
		int     end = s + (2 + 2*f->length)*SYM;
		int64_t e = 0;
		for (int k = pos - 8*SYM; k < end; k++)
			if (k >= 0)
				e += (int64_t)b.y[2*k]*b.y[2*k] + (int64_t)b.y[2*k + 1]*b.y[2*k + 1];
		f->level_db4 = dsp_db4(e/(end - pos + 8*SYM));
		count++;
		i = end;
	}
	return count;
}

const char *zb_type_name(int type)
{
	static const char *names[8] = {"BEACON", "DATA", "ACK", "CMD", "RSV", "MPURP", "FRAG", "EXT"};
	return names[type & 7];
}

const char *zb_network(const struct zb_frame *f)
{
	/* Data frames: network layer from the first payload byte(s). */
	if (f->type == 0)
		return "BEACON";
	if (f->type != 1)
		return "";
	int hdr = 3 + (f->dst_mode ? 2 + ((f->dst_mode == 3) ? 8 : 2) : 0) +
		(f->src_mode ? ((((f->psdu[0] >> 6) & 1) && f->dst_mode) ? 0 : 2) +
		((f->src_mode == 3) ? 8 : 2) : 0);
	if (hdr + 2 >= f->length - 2)
		return "";
	uint8_t d = f->psdu[hdr];
	/* 6LoWPAN dispatch: IPHC (011x xxxx), mesh (10xx xxxx), fragments (11x0 0xxx). */
	if ((d & 0xe0) == 0x60 || (d & 0xc0) == 0x80 || (d & 0xd8) == 0xc0)
		return "6LOWPAN";
	/* Zigbee NWK frame control: protocol version 2 (bits 2-5). */
	if (((d >> 2) & 0xf) == 2)
		return "ZIGBEE";
	return "";
}

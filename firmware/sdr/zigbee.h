// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// IEEE 802.15.4 (2.4GHz O-QPSK: Zigbee, Thread, Matter) frames decoder on 16MS/s captures (1ms:
// frames up to ~25 bytes, ACKs, short data/command frames): channel channelized to 8MS/s (4
// samples/chip), FM discriminator (O-QPSK half-sine = MSK: the frequency sign of each chip interval
// is the chips transition, alternated), symbols matched against the 16 chip sequences, preamble and
// SFD sync, PHR, PSDU and FCS check, MAC header parsed. Hardware independent (also built on the
// host for the tests).

#ifndef ZIGBEE_H
#define ZIGBEE_H

#include <stdint.h>

#define ZB_PSDU_MAX 32

/* Channels 11-26: 2405 + 5*(channel - 11) MHz. */
int zb_channel_khz(int channel);

struct zb_frame {
	int      length;                   /* PSDU length (with the FCS). */
	uint8_t  psdu[ZB_PSDU_MAX];
	int      type;                     /* MAC frame type (0: beacon, 1: data, 2: ack, 3: command). */
	int      seq;
	int      pan;                      /* Destination (else source) PAN ID, -1 if none. */
	uint64_t dst, src;                 /* Addresses (short or extended), 0 if none. */
	int      dst_mode, src_mode;       /* 0: none, 2: short, 3: extended. */
	int      pos;                      /* SFD position (8MS/s samples). */
	int      level_db4;
	int      errors;                   /* Chip errors (worst symbol). */
};

/* Frames with a valid FCS of a 16MS/s capture, channel at offset_hz from the tuned frequency:
   returns their count (<= max). */
int zb_decode(const int8_t *iq, int samples, int offset_hz, struct zb_frame *frames, int max);
/* MAC frame type name, network layer guess (Zigbee NWK, 6LoWPAN/Thread). */
const char *zb_type_name(int type);
const char *zb_network(const struct zb_frame *f);

#endif

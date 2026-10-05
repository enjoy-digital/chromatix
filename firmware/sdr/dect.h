// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// DECT bursts decoder on 16MS/s captures (1ms: a whole 417us slot): carrier channelized to
// 4.608MS/s (4 samples/bit, 1.152Mb/s GFSK), FM discriminator, S-field sync search (RFP: fixed
// part/base, PP: portable part/handset, both polarities), A-field (header, 40-bit tail, R-CRC)
// checked: tail type and the base identity (RFPI, from the N_T tails of the base's beacons). Only
// the A-field (not scrambled, no user data) is decoded. Hardware independent (also built on the
// host for the tests).

#ifndef DECT_H
#define DECT_H

#include <stdint.h>

/* Carriers (kHz): EU 1880-1900MHz (10), US DECT 6.0 1920-1930MHz (5). */
enum {
	DECT_EU = 0,
	DECT_US,
};
int dect_carriers(int region);
int dect_carrier_khz(int region, int carrier);

/* A-field tail types (TA). */
enum {
	DECT_TA_CT0 = 0,
	DECT_TA_CT1,
	DECT_TA_NT_CL,                       /* Identities (connectionless). */
	DECT_TA_NT,                          /* Identities: RFPI. */
	DECT_TA_QT,                          /* System information. */
	DECT_TA_ESC,
	DECT_TA_MT,                          /* MAC control. */
	DECT_TA_PT,                          /* Paging (RFP) / first transmission (PP). */
};

struct dect_burst {
	int      rfp;                        /* Fixed part (base) transmission, else portable part. */
	int      ta;
	uint8_t  a[8];                       /* A-field. */
	uint64_t rfpi;                       /* RFPI (N_T tails), 0 if none. */
	int      pos;                        /* Sync end position (4.608MS/s samples). */
	int      level_db4;
};

/* Bursts with a valid A-field of a 16MS/s capture, carrier at offset_hz from the tuned frequency:
   returns their count (<= max). */
int dect_decode(const int8_t *iq, int samples, int offset_hz, struct dect_burst *bursts, int max);
/* Tail type name. */
const char *dect_ta_name(int ta);

#endif

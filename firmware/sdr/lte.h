// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// LTE cell search on 16MS/s captures (1ms: the PSS, sent every 5ms, is in ~1 capture out of 5):
// carrier channelized to 1.92MS/s, PSS (N_ID2) found by FFT correlation over 3 frequency offset
// hypotheses (+-7.5kHz), refined in the time domain (frequency offset in 500Hz steps), then SSS
// (N_ID1, FDD/TDD, subframe) decoded coherently (channel from the PSS): physical cell ID, frequency
// offset (receiver LO error) and PSS SNR. Hardware independent (also built on the host for the
// tests).

#ifndef LTE_H
#define LTE_H

#include <stdint.h>

#define LTE_RATE 1920000 /* Channelized rate (128-point symbols). */

struct lte_cell {
	int pci;      /* Physical cell ID: 3*nid1 + nid2. */
	int nid1;
	int nid2;
	int tdd;      /* TDD (SSS 3 symbols before the PSS), else FDD (SSS just before). */
	int subframe; /* 0 or 5. */
	int cfo_hz;   /* Carrier frequency offset (received - expected). */
	int pss;      /* PSS correlation (%). */
	int sss;      /* SSS correlation (%). */
	int snr_db;   /* PSS SNR estimate. */
	int pos;      /* PSS position (1.92MS/s samples). */
};

void lte_init(void);
/* Cell search in a 16MS/s capture, carrier at offset_hz from the tuned frequency (receiver LO
   error included if known), frequency offset searched over +-11.5kHz if wide (unknown LO error),
   else +-4kHz: returns 1 if a cell was found (PSS and SSS above their thresholds), 0 if not. */
int  lte_search(const int8_t *iq, int samples, int offset_hz, int wide, struct lte_cell *cell);

#endif

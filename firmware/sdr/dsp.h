// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// SDR DSP: fixed point FFT (FFT_SIZE, int16 block scaled, Q15 twiddles), power spectrum of 8-bit
// I/Q captures (Hann window, segments averaged), dB conversion.

#ifndef DSP_H
#define DSP_H

#include <stdint.h>

#include "tables.h"

/* Averaged power spectrum of samples I/Q pairs (segments of FFT_SIZE, ESP32 inverted spectrum
   corrected), DC centered: power[0] is -fs/2, power[FFT_SIZE/2] is DC (interpolated from its
   neighbours: LO leakage removed). */
void dsp_power_spectrum(const int8_t *iq, int samples, uint64_t *power);
/* 10*log10(power) in 1/4 dB. */
int  dsp_db4(uint64_t power);

#endif

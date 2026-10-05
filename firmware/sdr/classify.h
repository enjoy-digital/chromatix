// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// 2.4GHz signal identification on 80MS/s captures (205us, +-22MHz used): spectrogram (128-point
// FFTs, Hann window: 625kHz x 1.6us cells, smoothed over 12.8us), cells above the per-bin noise floor + 6dB
// (persistent lines: receiver spurs, masked), bursts = connected cells, classified from their
// bandwidth, duration, frequency (Wi-Fi/BLE/802.15.4/DJI DroneID channels) and drift: Wi-Fi 20/40,
// 10MHz OFDM (drone links), DJI DroneID, continuous wideband (analog video), microwave oven,
// 802.15.4, BLE advertising, narrowband (BLE/Bluetooth/RC hopping), carriers. Hardware independent
// (also built on the host for the tests).

#ifndef CLASSIFY_H
#define CLASSIFY_H

#include <stdint.h>

#define SIG_BINS       128             /* Spectrogram bins (625kHz). */
#define SIG_USABLE_KHZ 22000           /* Used: +-22MHz of the 80MS/s captures. */

enum {
	SIG_WIFI = 0,
	SIG_WIFI40,
	SIG_OFDM10,                        /* Non Wi-Fi 10MHz OFDM: drone video links (OcuSync...). */
	SIG_DRONEID,                       /* 10MHz burst on a DJI DroneID frequency. */
	SIG_VIDEO,                         /* Continuous wideband (>= 7MHz): analog video. */
	SIG_MICROWAVE,                     /* Continuous wideband, drifting. */
	SIG_ZIGBEE,                        /* 802.15.4 (Zigbee, Thread). */
	SIG_BLE_ADV,                       /* Narrowband on a BLE advertising channel. */
	SIG_NARROW,                        /* Narrowband burst: BLE, Bluetooth, RC links (hopping). */
	SIG_CARRIER,                       /* Continuous/persistent narrowband (carriers, interferers). */
	SIG_BROADBAND,                     /* Whole band (impulse, AGC step). */
	SIG_UNKNOWN,
	SIG_CLASSES,
};

struct sig {
	int cls;
	int khz;                           /* Center frequency. */
	int bw_khz;
	int us;                            /* Duration (truncated: at the capture start/end). */
	int level_db4;                     /* Peak level (1/4 dB, arbitrary). */
	int truncated;
};

void sig_init(void);
/* Bursts of an 80MS/s capture (center_khz: tuned frequency, slot: spurs memory, one per tuned
   frequency) classified: returns their count (<= max); columns: spectrum max (dB4) per bin (bin 0:
   center - 40MHz), NULL if not needed. */
int  sig_detect(const int8_t *iq, int samples, int center_khz, int slot, struct sig *sigs, int max,
	int16_t *columns);
const char *sig_name(int cls);
/* Drone related classes. */
int  sig_drone(int cls);

#endif

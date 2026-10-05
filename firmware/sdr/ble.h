// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// BLE advertising decoder on 16MS/s captures (1ms: whole advertising packets): channel channelized
// to 4MS/s (4 samples/bit), FM discriminator, access address search (0x8E89BED6, 4 sample phases,
// up to 2 bit errors), dewhitening, CRC check, advertising data description (names, vendors,
// trackers). Hardware independent (also built on the host for the tests).

#ifndef BLE_H
#define BLE_H

#include <stdint.h>

#define BLE_PDU_MAX 64 /* Header + payload (legacy advertising: 2 + 37). */

/* Advertising channels (index, MHz): 37: 2402, 38: 2426, 39: 2480. */
int ble_channel_mhz(int channel);

struct ble_packet {
	int     channel;
	int     type;           /* PDU type (ADV_IND: 0, ... ADV_EXT_IND: 7). */
	int     txadd;          /* Advertiser address random. */
	int     length;         /* Payload length. */
	uint8_t pdu[BLE_PDU_MAX];
	int     pos;            /* Access address position (4MS/s samples). */
	int     level_db4;      /* Packet power (1/4 dB, arbitrary). */
};

/* Advertising packets with a valid CRC of a 16MS/s capture, channel at offset_hz from the tuned
   frequency: returns the packets count (<= max). */
int  ble_decode(const int8_t *iq, int samples, int offset_hz, int channel, struct ble_packet *pkts,
	int max);

/* Advertiser address (payload bytes 0-5, packets with one), 0 if none. */
uint64_t ble_address(const struct ble_packet *p);
/* PDU type name. */
const char *ble_type_name(int type);
/* Short description of the advertising data: local name, else vendor/service (tracker: returns
   1). */
int  ble_describe(const struct ble_packet *p, char *text, int len);

#endif

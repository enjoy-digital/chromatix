// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// BLE advertising decoder, see ble.h (Bluetooth Core Specification Vol 6 Part B: link layer packet,
// whitening, CRC; Assigned Numbers: AD types, company identifiers, 16-bit UUIDs).

#include <stdio.h>
#include <stdint.h>
#include <string.h>

#include "dsp.h"
#include "ble.h"

#define SPB        4                       /* Samples per bit (4MS/s). */
#define MAX_OUT    (DSP_MAX_SAMPLES/4)
#define ACCESS     0x8E89BED6u             /* Advertising access address. */
#define AA_ERRORS  2                       /* Access address bit errors allowed. */
#define CRC_INIT   0x555555
#define CRC_POLY   0x00065B                /* x^24 + x^10 + x^9 + x^6 + x^4 + x^3 + x + 1. */

static const struct dsp_filter filter = {filter_ble, FILTER_BLE_TAPS, FILTER_BLE_UP,
	FILTER_BLE_DOWN};

/* Buffers (offset in the direct mapped 8KB data cache). */
static struct {
	int32_t y[2*MAX_OUT];              /* Channelized capture (complex interleaved). +0KB */
	int32_t pad0[512];
	int32_t disc[MAX_OUT];             /* FM discriminator.                          +2KB */
	int32_t pad1[512];
	int32_t soft[MAX_OUT];             /* Discriminator summed over a bit.           +4KB */
} b;

int ble_channel_mhz(int channel)
{
	return (channel == 37) ? 2402 : (channel == 38) ? 2426 : (channel == 39) ? 2480 :
		(channel <= 10) ? 2404 + 2*channel : 2428 + 2*(channel - 11);
}

static int popcount32(uint32_t x)
{
	int n = 0;
	for (; x; x &= x - 1)
		n++;
	return n;
}

static int decode_packet(int start, int n, int channel, struct ble_packet *p)
{
	/* PDU bits from soft[start] (1 bit every SPB samples): dewhitened, CRC checked. */
	uint8_t  lfsr = 0x40 | channel; /* Whitening LFSR (x^7 + x^4 + 1), position 0 = bit 6. */
	uint32_t crc  = CRC_INIT, crc_rx = 0;
	int      bytes = 2, bit = 0;
	memset(p->pdu, 0, sizeof(p->pdu));
	for (;; bit++) {
		int s = start + bit*SPB;
		if (s >= n)
			return 0;
		/* Dewhitening (position 6 out, fed back to positions 0 and 4). */
		int w = lfsr & 1;
		int v = (b.soft[s] > 0) ^ w;
		lfsr = (lfsr >> 1) | (w << 6);
		if (w)
			lfsr ^= 1 << 2;
		if (bit < 8*bytes) {
			p->pdu[bit/8] |= v << (bit % 8);
			int fb = ((crc >> 23) & 1) ^ v;
			crc = (crc << 1) & 0xffffff;
			if (fb)
				crc ^= CRC_POLY;
			if (bit == 15) {
				bytes = 2 + p->pdu[1];
				if (bytes > BLE_PDU_MAX)
					return 0;
			}
		} else {
			crc_rx = (crc_rx << 1) | v;
			if (bit == 8*bytes + 23)
				break;
		}
	}
	if (crc != crc_rx)
		return 0;
	p->type   = p->pdu[0] & 0xf;
	p->txadd  = (p->pdu[0] >> 6) & 1;
	p->length = p->pdu[1];
	return 8*bytes + 24;
}

int ble_decode(const int8_t *iq, int samples, int offset_hz, int channel, struct ble_packet *pkts,
	int max)
{
	int n = dsp_channelize(iq, samples, dsp_phase_step(-offset_hz, 16000000), &filter, b.y,
		MAX_OUT);
	/* FM discriminator summed over a bit. */
	n = dsp_fm_soft(b.y, n, SPB, b.disc, b.soft) - 1;
	/* Access address search on the 4 sample phases (bits LSB first). */
	int count = 0, skip = 0;
	for (int i = 32*SPB; i < n && count < max; i++) {
		if (i < skip)
			continue;
		uint32_t sr = 0;
		for (int k = 0; k < 32; k++)
			sr |= (uint32_t)(b.soft[i - (32 - k)*SPB] > 0) << k;
		if (popcount32(sr ^ ACCESS) > AA_ERRORS)
			continue;
		struct ble_packet *p = &pkts[count];
		int bits = decode_packet(i, n, channel, p);
		if (!bits)
			continue;
		/* Level over the packet. */
		int     first = i - 32*SPB, last = i + bits*SPB;
		int64_t e = 0;
		for (int k = first; k < last; k++)
			e += (int64_t)b.y[2*k]*b.y[2*k] + (int64_t)b.y[2*k + 1]*b.y[2*k + 1];
		p->channel   = channel;
		p->pos       = i;
		p->level_db4 = dsp_db4(e/(last - first));
		count++;
		skip = last;
	}
	return count;
}

/* Description ---------------------------------------------------------------------------------- */

uint64_t ble_address(const struct ble_packet *p)
{
	/* Advertiser address: ADV_IND, ADV_DIRECT_IND, ADV_NONCONN_IND, SCAN_RSP, ADV_SCAN_IND. */
	if (p->length < 6 || p->type == 3 || p->type == 5 || p->type == 7)
		return 0;
	uint64_t a = 0;
	for (int i = 5; i >= 0; i--)
		a = (a << 8) | p->pdu[2 + i];
	return a;
}

const char *ble_type_name(int type)
{
	static const char *names[] = {
		"ADV_IND", "ADV_DIRECT", "ADV_NONCONN", "SCAN_REQ", "SCAN_RSP", "CONNECT", "ADV_SCAN",
		"ADV_EXT",
	};
	return (type < 8) ? names[type] : "?";
}

static const struct {
	uint16_t    id;
	const char *name;
} companies[] = {
	{0x004c, "APPLE"},     {0x0006, "MICROSOFT"}, {0x0075, "SAMSUNG"},  {0x00e0, "GOOGLE"},
	{0x0059, "NORDIC"},    {0x0087, "GARMIN"},    {0x0157, "HUAMI"},    {0x038f, "XIAOMI"},
	{0x0499, "RUUVI"},     {0x02e5, "ESPRESSIF"}, {0x0310, "SGL"},      {0x00d2, "DIALOG"},
	{0x0131, "CYPRESS"},   {0x000d, "TI"},        {0x0002, "INTEL"},    {0x0171, "AMAZON"},
	{0x067c, "TILE"},      {0x0822, "ADIDAS"},    {0x0397, "LEGO"},     {0x012d, "SONY"},
	{0x00c4, "LG"},        {0x0001, "NOKIA"},     {0x009e, "BOSE"},     {0x0080, "DEEZER"},
};

static const struct {
	uint16_t    uuid;
	const char *name;
	int         tracker;
} services[] = {
	{0xfd5a, "SAMSUNG SMARTTAG", 1}, {0xfeed, "TILE TRACKER", 1},   {0xfeec, "TILE TRACKER", 1},
	{0xfe33, "CHIPOLO TRACKER", 1},  {0xfcb2, "GOOGLE FINDHUB", 1}, {0xfe2c, "GOOGLE FASTPAIR", 0},
	{0xfd6f, "EXPOSURE NOTIF", 0},   {0xfeaa, "EDDYSTONE", 0},      {0xfe9f, "GOOGLE", 0},
	{0xfef3, "GOOGLE", 0},           {0x180f, "BATTERY SVC", 0},    {0x180d, "HEART RATE", 0},
	{0x1812, "HID", 0},              {0xfe07, "SONOS", 0},          {0xfdf7, "HP", 0},
	{0xfe95, "XIAOMI", 0},           {0xfd81, "CHIPOLO", 1},        {0xfe78, "HP", 0},
};

static const char *apple_type(int type, int len, int *tracker)
{
	/* Apple Continuity message types. */
	switch (type) {
	case 0x02: return "APPLE IBEACON";
	case 0x05: return "APPLE AIRDROP";
	case 0x07: return "APPLE AIRPODS";
	case 0x09: return "APPLE AIRPLAY";
	case 0x0c: return "APPLE HANDOFF";
	case 0x0d: return "APPLE HOTSPOT";
	case 0x0f: return "APPLE NEARBY ACT";
	case 0x10: return "APPLE NEARBY";
	case 0x12:
		/* Find My: 25-byte payload when separated from its owner (2 when nearby). */
		*tracker = 1;
		return (len >= 25) ? "FINDMY SEPARATED" : "FINDMY (OWNER)";
	case 0x16: return "APPLE NEARBY";
	default:   return "APPLE";
	}
}

int ble_describe(const struct ble_packet *p, char *text, int len)
{
	const uint8_t *d   = &p->pdu[2 + 6];
	int            n   = p->length - 6;
	int            tracker = 0;
	const char    *what = NULL;
	char           name[32] = "";
	if (!ble_address(p)) {
		snprintf(text, len, "%s", ble_type_name(p->type));
		return 0;
	}
	/* AD structures: length, type, data. */
	for (int i = 0; i + 1 < n && d[i]; i += d[i] + 1) {
		int l = d[i], t = d[i + 1];
		const uint8_t *v = &d[i + 2];
		if (i + 1 + l > n)
			break;
		if ((t == 0x08 || t == 0x09) && !name[0]) {
			/* Local name (printable ASCII, upper case: 4x6 font). */
			int k = 0;
			for (int j = 0; j < l - 1 && k < (int)sizeof(name) - 1; j++) {
				char c = v[j];
				if (c >= 'a' && c <= 'z')
					c -= 32;
				name[k++] = (c >= 0x20 && c < 0x7f) ? c : '.';
			}
			name[k] = 0;
		} else if (t == 0xff && l >= 3) {
			/* Manufacturer data: company ID. */
			int id = v[0] | (v[1] << 8);
			if (id == 0x004c && l >= 4)
				what = apple_type(v[2], l - 1, &tracker);
			else if (!what) {
				for (int j = 0; j < (int)(sizeof(companies)/sizeof(companies[0])); j++)
					if (companies[j].id == id)
						what = companies[j].name;
				if (id == 0x0006 && l >= 4 && v[2] == 0x01)
					what = "MICROSOFT CDP";
			}
		} else if ((t == 0x02 || t == 0x03 || t == 0x16) && l >= 3) {
			/* 16-bit service UUIDs/service data. */
			int uuid = v[0] | (v[1] << 8);
			for (int j = 0; j < (int)(sizeof(services)/sizeof(services[0])); j++)
				if (services[j].uuid == uuid && (!what || services[j].tracker)) {
					what     = services[j].name;
					tracker |= services[j].tracker;
				}
		}
	}
	if (name[0] && what && tracker)
		snprintf(text, len, "%s %s", what, name);
	else if (name[0])
		snprintf(text, len, "%s", name);
	else if (what)
		snprintf(text, len, "%s", what);
	else
		snprintf(text, len, "%s", ble_type_name(p->type));
	return tracker;
}

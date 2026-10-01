// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// WAD lumps alignment: the WAD is used in place (lumps not copied), lumps are moved to 4-byte
// aligned positions (RISC-V: no misaligned accesses). Same as align_wad() in scripts/chromatic.py.

#ifndef WAD_ALIGN_H
#define WAD_ALIGN_H

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

static inline uint32_t wad_le32(const uint8_t *p)
{
	return p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24;
}

static inline void wad_set_le32(uint8_t *p, uint32_t v)
{
	p[0] = v; p[1] = v >> 8; p[2] = v >> 16; p[3] = v >> 24;
}

/* Returns the aligned WAD (malloc'ed) and its size, NULL if not a valid WAD. */
static inline uint8_t *wad_align(const uint8_t *wad, size_t size, unsigned int *new_size)
{
	if (size < 12 || (memcmp(wad, "IWAD", 4) && memcmp(wad, "PWAD", 4)))
		return NULL;
	uint32_t numlumps = wad_le32(wad + 4);
	uint32_t dir      = wad_le32(wad + 8);
	if ((uint64_t)dir + 16ull*numlumps > size)
		return NULL;

	/* Size: header, lumps (padded to 4 bytes), directory. */
	size_t total = 12;
	for (uint32_t i = 0; i < numlumps; i++)
		total += (wad_le32(wad + dir + 16*i + 4) + 3) & ~3u;
	total += 16*numlumps;

	uint8_t *out = calloc(1, total);
	if (!out)
		return NULL;
	memcpy(out, wad, 4);
	wad_set_le32(out + 4, numlumps);
	size_t pos = 12;
	uint8_t *out_dir = out + total - 16*numlumps;
	wad_set_le32(out + 8, total - 16*numlumps);
	for (uint32_t i = 0; i < numlumps; i++) {
		const uint8_t *entry = wad + dir + 16*i;
		uint32_t filepos = wad_le32(entry + 0);
		uint32_t length  = wad_le32(entry + 4);
		if ((uint64_t)filepos + length > size) {
			free(out);
			return NULL;
		}
		memcpy(out + pos, wad + filepos, length);
		wad_set_le32(out_dir + 16*i + 0, pos);
		wad_set_le32(out_dir + 16*i + 4, length);
		memcpy(out_dir + 16*i + 8, entry + 8, 8);
		pos += (length + 3) & ~3u;
	}
	*new_size = total;
	return out;
}

#endif

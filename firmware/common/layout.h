// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// --with-app firmwares main RAM layout (offsets from the main RAM start, see main_ram.ld and
// scripts/chromatic.py): firmware/heap, profiler histogram, host block (arguments, results), stack,
// data (ex: files loaded with the firmware).

#ifndef LAYOUT_H
#define LAYOUT_H

#define LAYOUT_PROF_OFFSET  0x34f000 /* Profiler histogram (64KB). */
#define LAYOUT_PROF_SIZE    0x10000
#define LAYOUT_HOST_OFFSET  0x35f000 /* Host block (4KB). */
#define LAYOUT_STACK_OFFSET 0x360000 /* Stack (64KB). */
#define LAYOUT_DATA_OFFSET  0x370000 /* Data (files loaded with the firmware). */

/* Host block: arguments written by the host before the CPU starts, results written by the firmware
   (read by the host over the bridge). */
#define HOST_ARGS_MAGIC  0x53475241 /* "ARGS". */
#define HOST_BENCH_MAGIC 0x48434e42 /* "BNCH". */

struct host_block {
	unsigned int magic;      /* HOST_ARGS_MAGIC when args is valid. */
	char         args[1020]; /* Space separated arguments. */
	/* 0x400: results. */
	unsigned int bench_magic;
	unsigned int gametics;
	unsigned int realtics;
	unsigned int frames;
	unsigned int ms;
	unsigned int prof_magic;  /* HOST_BENCH_MAGIC when the histogram is valid. */
	unsigned int prof_base;   /* Histogram: bucket = (pc - prof_base) >> prof_shift. */
	unsigned int prof_shift;
	unsigned int prof_buckets;
	unsigned int prof_samples;
	/* Main RAM counters (MemoryCounters CSRs, at the timedemo end). */
	unsigned int mem_cycles;
	unsigned int mem_accesses;
	unsigned int mem_requests;
	unsigned int mem_busy;
	unsigned int mem_latency;
	/* Firmware status (diagnostics): bit 0: arguments read, bit 1: profile requested, bit 2:
	   first frame, bit 3: profiler started, bits 16-31: timedemo length (gametics, 0: whole). */
	unsigned int status;
};

#endif

// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Doom port HAL, ChromatiX SoC (--with-doom): LCDFramebuffer, ButtonsCSR, PCMAudio, timer0, WAD
// loaded by the host at the main RAM data area (scripts/chromatic.py run --wad).
//
// Without these peripherals (ex: litex_sim, to check the firmware): LCD in RAM (LCD_DUMP_FRAME:
// frame dumped on the console, hex lines), no buttons/audio.

#include <stdio.h>
#include <stdint.h>
#include <string.h>

#include <irq.h>
#include <libbase/uart.h>
#include <generated/csr.h>
#include <generated/mem.h>
#include <generated/soc.h>

#include "hal.h"
#include "layout.h"
#include "wad_align.h"

#define FB_PALETTE  0x2000   /* Words. */
#define PCM_DEPTH   512
#define DATA_OFFSET LAYOUT_DATA_OFFSET

static volatile struct host_block *host = (volatile struct host_block *)(MAIN_RAM_BASE + LAYOUT_HOST_OFFSET);

#ifdef FRAMEBUFFER_BASE
static volatile uint32_t *fb = (volatile uint32_t *)FRAMEBUFFER_BASE;
#else
static uint32_t fb[FB_PALETTE + 256];
static uint32_t fb_frames;
#endif

/* Time: timer0 1kHz interrupt (milliseconds counter), also sampling the interrupted PC (profiler). */
static volatile uint32_t ms;
static volatile int      prof_enabled;
static int               prof_requested;
static volatile uint32_t *prof_hist = (volatile uint32_t *)(MAIN_RAM_BASE + LAYOUT_PROF_OFFSET);

extern char _ftext[];

static void timer_isr(void)
{
	timer0_ev_pending_write(1);
	ms++;
	if (prof_enabled) {
		uint32_t pc, bucket;
		__asm__ volatile ("csrr %0, mepc" : "=r"(pc));
		bucket = (pc - host->prof_base) >> host->prof_shift;
		if (bucket < host->prof_buckets) {
			prof_hist[bucket]++;
			host->prof_samples++;
		}
	}
}

void hal_init(void)
{
#ifdef CONFIG_CPU_HAS_INTERRUPT
	irq_setmask(0);
	irq_setie(1);
#endif
	uart_init();
	timer0_en_write(0);
	timer0_load_write(CONFIG_CLOCK_FREQUENCY/1000);
	timer0_reload_write(CONFIG_CLOCK_FREQUENCY/1000);
	timer0_en_write(1);
	timer0_ev_pending_write(1);
	timer0_ev_enable_write(1);
	irq_attach(TIMER0_INTERRUPT, timer_isr);
	irq_setmask(irq_getmask() | (1 << TIMER0_INTERRUPT));
	host->bench_magic = 0;
	host->frames      = 0;
	host->prof_magic  = 0;
	host->status      = 0;
}

int hal_poll(void)
{
	return 1;
}

uint32_t hal_ticks_ms(void)
{
	return ms;
}

void hal_sleep_ms(uint32_t ms)
{
	uint32_t start = hal_ticks_ms();
	while (hal_ticks_ms() - start < ms);
}

uint32_t hal_buttons(void)
{
#ifdef CSR_DEMO_BUTTONS_BASE
	return demo_buttons_status_read();
#else
	return 0;
#endif
}

void hal_lcd_palette(const uint16_t *rgb555)
{
	for (int i = 0; i < 256; i++)
		fb[FB_PALETTE + i] = rgb555[i];
}

void hal_lcd_line(int y, const uint8_t *pixels)
{
	volatile uint32_t *dst = &fb[y*HAL_LCD_WIDTH/4];
	for (int x = 0; x < HAL_LCD_WIDTH; x += 4)
		*dst++ = pixels[x] | pixels[x + 1] << 8 | pixels[x + 2] << 16 | (uint32_t)pixels[x + 3] << 24;
}

void hal_lcd_present(void)
{
#if !defined(FRAMEBUFFER_BASE) && defined(LCD_DUMP_FRAME)
	if (++fb_frames == LCD_DUMP_FRAME) {
		printf("LCD palette:");
		for (int i = 0; i < 256; i++)
			printf(" %04lx", (unsigned long)fb[FB_PALETTE + i]);
		printf("\n");
		for (int y = 0; y < HAL_LCD_HEIGHT; y++) {
			printf("LCD %03d:", y);
			for (int x = 0; x < HAL_LCD_WIDTH/4; x++)
				printf(" %08lx", (unsigned long)fb[y*HAL_LCD_WIDTH/4 + x]);
			printf("\n");
		}
	}
#endif
}

#ifdef CSR_PCM_BASE
static void (*audio_fill)(void);

static void pcm_isr(void)
{
	audio_fill();
}
#endif

void hal_audio_start(void (*fill)(void))
{
#if defined(CSR_PCM_BASE) && defined(PCM_INTERRUPT)
	audio_fill = fill;
	irq_attach(PCM_INTERRUPT, pcm_isr);
	pcm_ev_enable_write(1);
	irq_setmask(irq_getmask() | (1 << PCM_INTERRUPT));
#else
	(void)fill;
#endif
}

int hal_audio_free(void)
{
#ifdef CSR_PCM_BASE
	return PCM_DEPTH - 1 - pcm_level_read();
#else
	return 0;
#endif
}

void hal_audio_write(uint32_t sample)
{
#ifdef CSR_PCM_BASE
	pcm_data_write(sample);
#else
	(void)sample;
#endif
}

const uint8_t *hal_wad(unsigned int *size)
{
	const uint8_t *wad = (const uint8_t *)(MAIN_RAM_BASE + DATA_OFFSET);

	if (memcmp(wad, "IWAD", 4) && memcmp(wad, "PWAD", 4))
		return NULL;
	*size = wad_le32(wad + 8) + 16*wad_le32(wad + 4); /* Directory at the end (aligned WAD). */
	return wad;
}

/* Host interface ------------------------------------------------------------------------------- */

const char *hal_args(void)
{
	if (host->magic != HOST_ARGS_MAGIC)
		return NULL;
	host->args[sizeof(host->args) - 1] = 0;
	return (const char *)host->args;
}

void hal_frame(void)
{
	/* Measurements from the first frame (after the initialization). */
	if (host->frames == 0) {
		hal_status(1 << 2);
#ifdef CSR_MEMORY_COUNTERS_BASE
		memory_counters_control_write(1);
#endif
		if (prof_requested)
			hal_profile(2);
	}
	host->frames++;
	host->ms = ms;
}

void hal_bench(int gametics, int realtics)
{
	prof_enabled      = 0;
#ifdef CSR_MEMORY_COUNTERS_BASE
	host->mem_cycles   = memory_counters_cycles_read();
	host->mem_accesses = memory_counters_accesses_read();
	host->mem_requests = memory_counters_requests_read();
	host->mem_busy     = memory_counters_busy_read();
	host->mem_latency  = memory_counters_latency_read();
#endif
	host->gametics    = gametics;
	host->realtics    = realtics;
	host->ms          = ms;
	host->bench_magic = HOST_BENCH_MAGIC;
}

void hal_profile(int enable)
{
	/* 1: started at the first frame, 2: started now. */
	if (enable == 1) {
		prof_requested = 1;
		return;
	}
	if (enable) {
		host->prof_base    = (uint32_t)(uintptr_t)_ftext;
		host->prof_shift   = 5;
		host->prof_buckets = LAYOUT_PROF_SIZE/4;
		host->prof_samples = 0;
		for (unsigned i = 0; i < LAYOUT_PROF_SIZE/4; i++)
			prof_hist[i] = 0;
		host->prof_magic   = HOST_BENCH_MAGIC;
		hal_status(1 << 3);
	}
	prof_enabled = enable;
}

void hal_status(uint32_t set)
{
	host->status |= set;
}

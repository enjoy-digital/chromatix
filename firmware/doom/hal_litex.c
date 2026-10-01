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
#include "wad_align.h"

#define FB_PALETTE  0x2000   /* Words. */
#define PCM_DEPTH   512
#define DATA_OFFSET 0x370000 /* WAD (see firmware/common/main_ram.ld). */

#ifdef FRAMEBUFFER_BASE
static volatile uint32_t *fb = (volatile uint32_t *)FRAMEBUFFER_BASE;
#else
static uint32_t fb[FB_PALETTE + 256];
static uint32_t fb_frames;
#endif

/* Time: 32-bit timer0 down counter extended to 64-bit (hal_ticks_ms called often enough). */
static uint32_t timer_last;
static uint64_t timer_ticks;

void hal_init(void)
{
#ifdef CONFIG_CPU_HAS_INTERRUPT
	irq_setmask(0);
	irq_setie(1);
#endif
	uart_init();
	timer0_en_write(0);
	timer0_load_write(0);
	timer0_reload_write(0xffffffff);
	timer0_en_write(1);
	timer0_update_value_write(1);
	timer_last = timer0_value_read();
}

int hal_poll(void)
{
	return 1;
}

uint32_t hal_ticks_ms(void)
{
	timer0_update_value_write(1);
	uint32_t value = timer0_value_read();
	timer_ticks += (uint32_t)(timer_last - value);
	timer_last   = value;
	return timer_ticks/(CONFIG_CLOCK_FREQUENCY/1000);
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

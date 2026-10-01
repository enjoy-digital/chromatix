// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// ChromatiX --with-doom bring-up firmware: PSRAM main RAM benchmark (console), framebuffer test
// patterns (color bars, gradient, buttons, animation, frames counter), PCM audio (A/B: tones).

#include <stdio.h>
#include <stdint.h>
#include <string.h>

#include <irq.h>
#include <libbase/uart.h>
#include <generated/csr.h>
#include <generated/mem.h>
#include <generated/soc.h>

/* Hardware ------------------------------------------------------------------------------------- */

#define FB_WIDTH       160
#define FB_HEIGHT      144
#define FB_PALETTE     0x2000 /* Words. */
#define PCM_RATE       11025
#define PCM_DEPTH      512

enum {
	BTN_A = 0, BTN_B, BTN_DOWN, BTN_LEFT, BTN_RIGHT, BTN_UP, BTN_SEL, BTN_START, BTN_MENU,
	BTN_COUNT
};

static volatile uint32_t *fb = (volatile uint32_t *)FRAMEBUFFER_BASE;

static void timer_init(void)
{
	/* Free running down counter. */
	timer0_en_write(0);
	timer0_load_write(0);
	timer0_reload_write(0xffffffff);
	timer0_en_write(1);
}

static uint32_t ticks(void)
{
	timer0_update_value_write(1);
	return 0xffffffff - timer0_value_read();
}

static uint32_t elapsed_us(uint32_t start)
{
	return (uint64_t)(ticks() - start)*1000000/CONFIG_CLOCK_FREQUENCY;
}

/* Memory Benchmark ----------------------------------------------------------------------------- */

#define BENCH_SIZE (1024*1024)

extern char _heap_start[];

static void print_rate(const char *name, uint32_t bytes, uint32_t us)
{
	printf("%-24s %6lu KB/s\n", name, (unsigned long)((uint64_t)bytes*1000000/1024/(us ? us : 1)));
}

static void memory_bench(void)
{
	volatile uint32_t *buf = (volatile uint32_t *)(((uintptr_t)_heap_start + 4095) & ~4095);
	uint32_t t, sum = 0, errors = 0;

	printf("Main RAM benchmark (%d KB at 0x%08lx):\n", BENCH_SIZE/1024, (unsigned long)(uintptr_t)buf);

	t = ticks();
	for (uint32_t i = 0; i < BENCH_SIZE/4; i++)
		buf[i] = i*0x9e3779b9;
	print_rate("  Write (32-bit):", BENCH_SIZE, elapsed_us(t));

	t = ticks();
	for (uint32_t i = 0; i < BENCH_SIZE/4; i++)
		sum += buf[i];
	print_rate("  Read (32-bit):", BENCH_SIZE, elapsed_us(t));

	for (uint32_t i = 0; i < BENCH_SIZE/4; i++)
		errors += buf[i] != i*0x9e3779b9;
	printf("  Check: %lu errors\n", (unsigned long)errors);

	t = ticks();
	memcpy((void *)&buf[BENCH_SIZE/8], (void *)buf, BENCH_SIZE/2);
	print_rate("  memcpy:", BENCH_SIZE/2, elapsed_us(t));

	/* Random reads (4KB stride + offset, defeating the caches): latency. */
	t = ticks();
	uint32_t index = 0;
	for (uint32_t i = 0; i < 16384; i++) {
		sum   += buf[index];
		index  = (index + 1024 + 17) & (BENCH_SIZE/4 - 1);
	}
	printf("  Random read latency:   %6lu ns (sum %08lx)\n",
		(unsigned long)(elapsed_us(t)*1000/16384), (unsigned long)sum);
}

/* Framebuffer ---------------------------------------------------------------------------------- */

static uint16_t rgb555(uint32_t r, uint32_t g, uint32_t b)
{
	return (b >> 3) << 10 | (g >> 3) << 5 | (r >> 3);
}

static void palette_init(void)
{
	/* RGB332. */
	for (int i = 0; i < 256; i++)
		fb[FB_PALETTE + i] = rgb555(((i >> 5) & 7)*255/7, ((i >> 2) & 7)*255/7, (i & 3)*255/3);
}

static void fb_rect(int x0, int y0, int w, int h, uint8_t color)
{
	volatile uint8_t *p = (volatile uint8_t *)fb;
	for (int y = y0; y < y0 + h; y++)
		for (int x = x0; x < x0 + w; x++)
			p[y*FB_WIDTH + x] = color;
}

static void fb_patterns(void)
{
	static const uint8_t bars[8] = {0xff, 0xfc, 0x1f, 0x1c, 0xe3, 0xe0, 0x03, 0x00};
	volatile uint8_t *p = (volatile uint8_t *)fb;

	/* Color bars (white, yellow, cyan, green, magenta, red, blue, black). */
	for (int i = 0; i < 8; i++)
		fb_rect(20*i, 0, 20, 48, bars[i]);
	/* Gradients: red, green, blue ramps. */
	for (int y = 48; y < 96; y++)
		for (int x = 0; x < FB_WIDTH; x++)
			p[y*FB_WIDTH + x] = (y < 64) ? ((x*8/FB_WIDTH) << 5) : (y < 80) ? ((x*8/FB_WIDTH) << 2) : (x*4/FB_WIDTH);
	fb_rect(0, 96, FB_WIDTH, 48, 0x00);
}

static void fb_buttons(uint32_t buttons, uint32_t frame)
{
	/* Buttons: 9 squares (lit when pressed). */
	for (int i = 0; i < BTN_COUNT; i++)
		fb_rect(4 + 17*i, 100, 14, 14, (buttons & (1 << i)) ? 0xff : 0x49);
	/* Animation: square moving with the frames counter. */
	fb_rect(0, 124, FB_WIDTH, 16, 0x00);
	fb_rect((frame*2) % (FB_WIDTH - 16), 124, 16, 16, 0xe0);
}

/* Audio ---------------------------------------------------------------------------------------- */

static uint32_t pcm_phase;

static void pcm_fill(uint32_t freq)
{
	/* Square wave (or silence), keeping the FIFO half full. */
	while (pcm_level_read() < PCM_DEPTH/2) {
		int16_t sample = 0;
		if (freq) {
			pcm_phase += freq;
			sample = ((pcm_phase/(PCM_RATE/2)) & 1) ? 4000 : -4000;
		}
		pcm_data_write(((uint32_t)(uint16_t)sample << 16) | (uint16_t)sample);
	}
}

/* Main ----------------------------------------------------------------------------------------- */

int main(void)
{
	uint32_t frame, last_frame = 0, buttons;
	uint32_t fps_t, fps_frames = 0;

#ifdef CONFIG_CPU_HAS_INTERRUPT
	irq_setmask(0);
	irq_setie(1);
#endif
	uart_init();
	timer_init();

	printf("\nChromatiX fbtest (%s, %lu MHz)\n", CONFIG_CPU_HUMAN_NAME,
		(unsigned long)(CONFIG_CLOCK_FREQUENCY/1000000));
	memory_bench();

	palette_init();
	fb_patterns();

	fps_t = ticks();
	for (;;) {
		frame   = framebuffer_frame_read();
		buttons = demo_buttons_status_read();
		pcm_fill((buttons & (1 << BTN_A)) ? 440 : (buttons & (1 << BTN_B)) ? 880 : 0);
		if (frame != last_frame) {
			fb_buttons(buttons, frame);
			last_frame = frame;
			fps_frames++;
		}
		if (elapsed_us(fps_t) >= 2000000) {
			printf("LCD frames: %lu (%lu.%lu fps), buttons: 0x%03lx\n", (unsigned long)frame,
				(unsigned long)(fps_frames/2), (unsigned long)(fps_frames*5 % 10), (unsigned long)buttons);
			fps_frames = 0;
			fps_t      = ticks();
		}
	}

	return 0;
}

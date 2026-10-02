// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// LCD (LCDPSRAMFramebuffer), see lcd.h.

#include <stdint.h>

#include <generated/csr.h>
#include <generated/mem.h>

#include "lcd.h"
#include "tables.h"

#define FB_BUFFERS    3
#define FB_SLOTS      4
#define FB_LINE_WORDS (LCD_WIDTH/4)
#define FB_PALETTE    0x400 /* Words. */

uint8_t lcd_screen[LCD_HEIGHT][LCD_WIDTH] __attribute__((aligned(4)));

static volatile uint32_t *fb = (volatile uint32_t *)FRAMEBUFFER_BASE;
static int                fb_slot;
static int                fb_back = 1;

static uint32_t fb_status(void)
{
	return framebuffer_status_read();
}

void lcd_palette(int index, uint8_t r, uint8_t g, uint8_t b)
{
	fb[FB_PALETTE + index] = (b >> 3) << 10 | (g >> 3) << 5 | (r >> 3);
}

void lcd_rect(int x, int y, int w, int h, uint8_t color)
{
	for (int j = y; j < y + h; j++)
		for (int i = x; i < x + w; i++)
			if (i >= 0 && i < LCD_WIDTH && j >= 0 && j < LCD_HEIGHT)
				lcd_screen[j][i] = color;
}

void lcd_text(int x, int y, uint8_t color, const char *text)
{
	lcd_text_scaled(x, y, color, text, 1);
}

void lcd_text_scaled(int x, int y, uint8_t color, const char *text, int scale)
{
	/* 4x6 cells (3x6 glyphs + 1 column spacing), ASCII 0x20-0x7e, font pixels drawn as scale x scale
	   squares. */
	for (; *text; text++, x += 4*scale) {
		if (*text < 0x20 || *text > 0x7e)
			continue;
		uint32_t glyph = font4x6[*text - 0x20];
		for (int r = 0; r < 6; r++)
			for (int c = 0; c < 3; c++)
				if ((glyph >> (3*(5 - r) + 2 - c)) & 1)
					lcd_rect(x + c*scale, y + r*scale, scale, scale, color);
	}
}

void lcd_present(void)
{
	/* Screen -> back buffer, line by line through the line buffers (used in order). */
	for (int y = 0; y < LCD_HEIGHT; y++) {
		while ((fb_status() >> CSR_FRAMEBUFFER_STATUS_BUSY_OFFSET) & (1 << fb_slot));
		volatile uint32_t *dst = &fb[fb_slot*FB_LINE_WORDS];
		const uint32_t    *src = (const uint32_t *)lcd_screen[y];
		for (int x = 0; x < FB_LINE_WORDS; x++)
			dst[x] = src[x];
		framebuffer_line_write(
			(y       << CSR_FRAMEBUFFER_LINE_LINE_OFFSET)   |
			(fb_back << CSR_FRAMEBUFFER_LINE_BUFFER_OFFSET) |
			(fb_slot << CSR_FRAMEBUFFER_LINE_SLOT_OFFSET));
		fb_slot = (fb_slot + 1) % FB_SLOTS;
	}
	/* Displayed from the next frame; next back buffer: neither displayed nor requested. */
	while ((fb_status() >> CSR_FRAMEBUFFER_STATUS_BUSY_OFFSET) & ((1 << FB_SLOTS) - 1));
	framebuffer_control_write(fb_back << CSR_FRAMEBUFFER_CONTROL_FRONT_OFFSET);
	int displayed = (fb_status() >> CSR_FRAMEBUFFER_STATUS_FRONT_OFFSET) & 0x3;
	for (int b = 0; b < FB_BUFFERS; b++)
		if (b != fb_back && b != displayed) {
			fb_back = b;
			break;
		}
}

void lcd_init(void)
{
	lcd_rect(0, 0, LCD_WIDTH, LCD_HEIGHT, 0);
	for (int b = 0; b < FB_BUFFERS; b++)
		lcd_present();
}

// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// LCD (LCDPSRAMFramebuffer): 160x144 8-bit indexed screen in RAM, 4x6 text, palette, present (lines
// copied to the back buffer in the PSRAM, displayed from the next frame: tear-free).

#ifndef LCD_H
#define LCD_H

#include <stdint.h>

#define LCD_WIDTH  160
#define LCD_HEIGHT 144

extern uint8_t lcd_screen[LCD_HEIGHT][LCD_WIDTH];

void lcd_init(void);
void lcd_palette(int index, uint8_t r, uint8_t g, uint8_t b);
void lcd_rect(int x, int y, int w, int h, uint8_t color);
void lcd_text(int x, int y, uint8_t color, const char *text);
void lcd_present(void);

#endif

// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// ChromatiX firmware demo: the buttons play notes (tone generator: speaker/headphones and USB
// audio) and are shown on the console (USB CDC and LCD terminal). Menu plays a melody, Start + Select
// returns to the BIOS.

#include <stdio.h>
#include <stdint.h>

#include <irq.h>
#include <libbase/uart.h>
#include <generated/csr.h>
#include <generated/soc.h>

/* Hardware ------------------------------------------------------------------------------------- */

#define HCLK_FREQ (CONFIG_CLOCK_FREQUENCY/2) /* Tone generator clock (sys = pClk = 2 x hClk). */

enum {
	BTN_A = 0, BTN_B, BTN_DOWN, BTN_LEFT, BTN_RIGHT, BTN_UP, BTN_SEL, BTN_START, BTN_MENU,
	BTN_COUNT
};

static const char *btn_names[BTN_COUNT] = {
	"A", "B", "Down", "Left", "Right", "Up", "Select", "Start", "Menu",
};

/* Notes (Hz): C major scale from C5, one per button. */
static const uint32_t btn_notes[BTN_COUNT] = {
	523, 587, 659, 698, 784, 880, 988, 1047, 0,
};

static void tone(uint32_t freq, uint32_t volume)
{
	tone_period_write(freq ? HCLK_FREQ/(2*freq) : 0);
	tone_volume_write(volume);
}

static void delay_ms(uint32_t ms)
{
	timer0_en_write(0);
	timer0_reload_write(0);
	timer0_load_write(CONFIG_CLOCK_FREQUENCY/1000*ms);
	timer0_en_write(1);
	timer0_update_value_write(1);
	while (timer0_value_read())
		timer0_update_value_write(1);
}

/* Demo ----------------------------------------------------------------------------------------- */

static void melody(void)
{
	static const uint32_t notes[] = {659, 659, 0, 659, 0, 523, 659, 0, 784, 0, 0, 0, 392};
	for (unsigned i = 0; i < sizeof(notes)/sizeof(notes[0]); i++) {
		tone(notes[i], 4000);
		delay_ms(120);
		tone(0, 0);
		delay_ms(20);
	}
}

static void print_buttons(uint32_t buttons)
{
	printf("\rButtons:");
	for (int i = 0; i < BTN_COUNT; i++)
		if (buttons & (1 << i))
			printf(" %s", btn_names[i]);
	printf("                    ");
}

int main(void)
{
	uint32_t buttons, last = ~0;

#ifdef CONFIG_CPU_HAS_INTERRUPT
	irq_setmask(0);
	irq_setie(1);
#endif
	uart_init();

	/* Screen (LCD terminal, previous output scrolled away): 21 lines, then the buttons line. */
	static const char *screen[] = {
		"+-------------------------------------+",
		"|  ChromatiX demo   LiteX / RISC-V    |",
		"|  VexRiscv @ 33 MHz, PSRAM main RAM  |",
		"+-------------------------------------+",
		"",
		" Buttons play notes (speaker, USB",
		" audio), Menu plays a melody,",
		" Start+Select: back to the BIOS.",
		"",
		"       [^]               (B)  (A)",
		"    [<]   [>]",
		"       [v]         [SEL] [START]",
		"",
		"  A  C5    B  D5    v  E5    <  F5",
		"  >  G5    ^  A5   SEL B5  START C6",
		"",
		"", "", "", "", "",
	};
	for (int i = 0; i < 24; i++)
		printf("\n");
	for (unsigned i = 0; i < sizeof(screen)/sizeof(screen[0]); i++)
		printf("%s\n", screen[i]);

	for (;;) {
		buttons = demo_buttons_status_read();
		if (buttons != last) {
			int note = -1;
			print_buttons(buttons);
			for (int i = 0; i < BTN_COUNT; i++)
				if ((buttons & (1 << i)) && btn_notes[i])
					note = i;
			tone(note >= 0 ? btn_notes[note] : 0, 4000);
			if ((buttons & (1 << BTN_MENU)) && !(last & (1 << BTN_MENU)))
				melody();
			if ((buttons & (1 << BTN_START)) && (buttons & (1 << BTN_SEL))) {
				tone(0, 0);
				printf("\nBack to BIOS.\n");
				ctrl_reset_write(1);
			}
			last = buttons;
		}
		delay_ms(10);
	}

	return 0;
}

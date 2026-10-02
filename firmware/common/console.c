// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Non-blocking console (replaces LiteX's picolibc stdio glue): the --with-app console is a
// crossover UART read by the host over the USB debug bridge, the firmware must not block when
// nobody reads it. A character waits up to CONSOLE_TIMEOUT_US for space, then the output is
// dropped until there is space again.

#include <stdio.h>
#include <stdint.h>

#include <generated/csr.h>
#include <generated/soc.h>

#define CONSOLE_TIMEOUT_US 1000

static int console_dropping;

static int console_putc_raw(char c)
{
#ifdef CSR_UART_BASE
	uint32_t n = CONFIG_CLOCK_FREQUENCY/1000000*CONSOLE_TIMEOUT_US/16; /* ~16 cycles per poll. */

	if (console_dropping && uart_txfull_read())
		return c;
	console_dropping = 0;
	while (uart_txfull_read()) {
		if (n-- == 0) {
			console_dropping = 1;
			return c;
		}
	}
	uart_rxtx_write(c);
#endif
	return c;
}

static int console_putc(char c, FILE *file)
{
	(void)file;
	if (c == '\n')
		console_putc_raw('\r');
	return console_putc_raw(c);
}

static int console_getc(FILE *file)
{
	(void)file;
	return EOF; /* No console input. */
}

static FILE console = FDEV_SETUP_STREAM(console_putc, console_getc, NULL, _FDEV_SETUP_RW);

FILE *const stdout = &console;
FILE *const stderr = &console;
FILE *const stdin  = &console;

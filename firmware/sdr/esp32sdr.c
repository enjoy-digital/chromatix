// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// ESP-SDR client over the ESP32 UART (see esp32sdr.h).

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <generated/csr.h>
#include <generated/soc.h>

#include "esp32sdr.h"

/* Time ----------------------------------------------------------------------------------------- */

void esp32sdr_init(void)
{
	/* timer0: free running down counter (ms time base). */
	timer0_en_write(0);
	timer0_load_write(0);
	timer0_reload_write(0xffffffff);
	timer0_en_write(1);
	/* Drop pending bytes. */
	for (int i = 0; i < 4096 && !esp32_uart_rxempty_read(); i++)
		(void)esp32_uart_rxtx_read();
}

uint32_t esp32sdr_ms(void)
{
	timer0_update_value_write(1);
	return (0xffffffff - timer0_value_read())/(CONFIG_CLOCK_FREQUENCY/1000);
}

/* UART ----------------------------------------------------------------------------------------- */

static void uart_putc(char c)
{
	while (esp32_uart_txfull_read());
	esp32_uart_rxtx_write(c);
}

static int uart_getc(uint32_t deadline)
{
	while (esp32_uart_rxempty_read())
		if ((int32_t)(esp32sdr_ms() - deadline) > 0)
			return -1;
	int c = esp32_uart_rxtx_read();
#ifndef CONFIG_ESP32_UART_RX_FIFO_RX_WE
	esp32_uart_ev_pending_write(2); /* RX event clear: pops the RX FIFO. */
#endif
	return c;
}

static int read_line(char *line, int len, uint32_t deadline)
{
	int n = 0;
	for (;;) {
		int c = uart_getc(deadline);
		if (c < 0)
			return -1;
		if (c == '\r')
			continue;
		if (c == '\n') {
			if (n == 0)
				continue; /* Empty line. */
			break;
		}
		if (n < len - 1)
			line[n++] = c;
	}
	line[n] = 0;
	return n;
}

/* Protocol ------------------------------------------------------------------------------------- */

int esp32sdr_cmd(const char *cmd, char *reply, int len, uint32_t timeout_ms)
{
	for (const char *p = cmd; *p; p++)
		uart_putc(*p);
	uart_putc('\n');
	return read_line(reply, len, esp32sdr_ms() + timeout_ms);
}

uint32_t esp32sdr_crc32(const uint8_t *data, int len)
{
	static uint32_t table[256];
	if (!table[1])
		for (uint32_t i = 0; i < 256; i++) {
			uint32_t c = i;
			for (int k = 0; k < 8; k++)
				c = (c & 1) ? 0xedb88320 ^ (c >> 1) : c >> 1;
			table[i] = c;
		}
	uint32_t crc = 0xffffffff;
	for (int i = 0; i < len; i++)
		crc = table[(crc ^ data[i]) & 0xff] ^ (crc >> 8);
	return crc ^ 0xffffffff;
}

int esp32sdr_capture(int samples, int rate, int8_t *iq, uint32_t *capture_us)
{
	char line[64];
	char cmd[32];

	snprintf(cmd, sizeof(cmd), "CAP16 %d %d", samples, rate);
	if (esp32sdr_cmd(cmd, line, sizeof(line), 200) < 0)
		return -1;
	if (strncmp(line, "DATA ", 5) != 0)
		return -2;
	char *p = line + 5;
	int      count = strtol(p, &p, 10);
	uint32_t crc   = strtoul(p, &p, 16);
	uint32_t us    = strtoul(p, &p, 10);
	if (count != samples)
		return -3;
	uint32_t deadline = esp32sdr_ms() + 100 + 2*samples/100; /* 2Mbaud: ~200 bytes/ms. */
	for (int i = 0; i < 2*samples; i++) {
		int c = uart_getc(deadline);
		if (c < 0)
			return -4;
		iq[i] = (int8_t)c;
	}
	if (esp32sdr_crc32((const uint8_t *)iq, 2*samples) != crc)
		return -5;
	if (capture_us)
		*capture_us = us;
	return 0;
}

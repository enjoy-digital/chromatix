// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// ESP-SDR client over the ESP32 UART (see esp32sdr.h).

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <system.h>
#include <generated/csr.h>
#include <generated/mem.h>
#include <generated/soc.h>

#include "esp32sdr.h"

/* UART ----------------------------------------------------------------------------------------- */

static int uart_pop(void)
{
	/* Received byte or -1. Without rx_fifo_rx_we, the RX event clear pops the RX FIFO. */
	if (esp32_uart_rxempty_read())
		return -1;
	int c = esp32_uart_rxtx_read();
#ifndef CONFIG_ESP32_UART_RX_FIFO_RX_WE
	esp32_uart_ev_pending_write(2);
#endif
	return c;
}

/* Time ----------------------------------------------------------------------------------------- */

void esp32sdr_init(void)
{
	/* timer0: free running down counter (ms time base). */
	timer0_en_write(0);
	timer0_load_write(0);
	timer0_reload_write(0xffffffff);
	timer0_en_write(1);
	esp32sdr_flush();
}

void esp32sdr_flush(void)
{
	/* Drop pending bytes until the line is quiet (20ms: end of an ongoing transfer). */
	uint32_t deadline = esp32sdr_ms() + 20;
	while ((int32_t)(esp32sdr_ms() - deadline) <= 0)
		if (uart_pop() >= 0)
			deadline = esp32sdr_ms() + 20;
}

uint32_t esp32sdr_ms(void)
{
	/* 32-bit timer extended to 64 bits (wraps every ~64s at 67MHz: called more often). */
	static uint32_t last;
	static uint64_t high;
	timer0_update_value_write(1);
	uint32_t now = 0xffffffff - timer0_value_read();
	if (now < last)
		high += (uint64_t)1 << 32;
	last = now;
	return (high + now)/(CONFIG_CLOCK_FREQUENCY/1000);
}

/* Lines ---------------------------------------------------------------------------------------- */

static void uart_putc(char c)
{
	while (esp32_uart_txfull_read());
	esp32_uart_rxtx_write(c);
}

static int uart_getc(uint32_t deadline)
{
	int c;
	while ((c = uart_pop()) < 0)
		if ((int32_t)(esp32sdr_ms() - deadline) > 0)
			return -1;
	return c;
}

int esp32sdr_rx(void)
{
	return uart_pop();
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

void esp32sdr_send(const char *cmd)
{
	for (const char *p = cmd; *p; p++)
		uart_putc(*p);
	uart_putc('\n');
}

int esp32sdr_cmd(const char *cmd, char *reply, int len, uint32_t timeout_ms)
{
	esp32sdr_send(cmd);
	return read_line(reply, len, esp32sdr_ms() + timeout_ms);
}

int esp32sdr_read_line(char *line, int len, uint32_t timeout_ms)
{
	return read_line(line, len, esp32sdr_ms() + timeout_ms);
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

/* QSPI transport (Chromatic ESP-SDR fork): captures written by the ESP32 to a main RAM buffer. */
static const int8_t *qspi_buf;

int esp32sdr_qspi(void *buf, uint32_t psram_address)
{
	char cmd[32], reply[32];
	snprintf(cmd, sizeof(cmd), "QSPI %lu", (unsigned long)psram_address);
	if (esp32sdr_cmd(cmd, reply, sizeof(reply), 200) < 0 || strcmp(reply, "OK") != 0) {
		qspi_buf = NULL;
		return -1;
	}
	qspi_buf = psram_address ? buf : NULL;
	return 0;
}

const int8_t *esp32sdr_qspi_buf(void)
{
	return qspi_buf;
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
	if (qspi_buf) {
		/* Payload already in the main RAM (written before the header): drop the cached copies. */
		flush_cpu_dcache();
		flush_l2_cache();
		memcpy(iq, qspi_buf, 2*samples);
	} else {
		uint32_t deadline = esp32sdr_ms() + 100 + 2*samples/100; /* 2Mbaud: ~200 bytes/ms. */
		for (int i = 0; i < 2*samples; i++) {
			int c = uart_getc(deadline);
			if (c < 0)
				return -4;
			iq[i] = (int8_t)c;
		}
	}
	if (esp32sdr_crc32((const uint8_t *)iq, 2*samples) != crc)
		return -5;
	if (capture_us)
		*capture_us = us;
	return 0;
}

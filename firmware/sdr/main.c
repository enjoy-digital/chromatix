// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// ChromatiX SDR (--with-app build, ESP-SDR on the ESP32). Step 1: ESP32 link diagnostic, results in
// a text log at the host block + 0x800 (read by the host: scripts/chromatic.py).

#include <stdio.h>
#include <stdint.h>
#include <stdarg.h>
#include <string.h>

#include <irq.h>
#include <libbase/uart.h>
#include <generated/csr.h>
#include <generated/mem.h>
#include <generated/soc.h>

#include "layout.h"
#include "esp32sdr.h"

/* Log (host readable) -------------------------------------------------------------------------- */

#define LOG_OFFSET 0x800
#define LOG_SIZE   0x800

static char *log_buf = (char *)(MAIN_RAM_BASE + LAYOUT_HOST_OFFSET + LOG_OFFSET);
static int   log_len;

static void log_printf(const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	int n = vsnprintf(log_buf + log_len, LOG_SIZE - 1 - log_len, fmt, ap);
	va_end(ap);
	if (n > 0)
		log_len += n;
	if (log_len > LOG_SIZE - 1)
		log_len = LOG_SIZE - 1;
	log_buf[log_len] = 0;
}

/* Main ----------------------------------------------------------------------------------------- */

#define SAMPLES 4096

static int8_t iq[2*SAMPLES];

int main(void)
{
	char reply[256];

#ifdef CONFIG_CPU_HAS_INTERRUPT
	irq_setmask(0);
	irq_setie(1);
#endif
	uart_init();
	memset(log_buf, 0, LOG_SIZE);
	esp32sdr_init();

	log_printf("ChromatiX SDR: ESP32 link diagnostic\n");
	esp32sdr_cmd("SYNC 1", reply, sizeof(reply), 200);
	for (const char *c = "CAPS\0TRANSPORT?\0LIMITS?\0FREQ 2412\0"; *c; c += strlen(c) + 1) {
		int n = esp32sdr_cmd(c, reply, sizeof(reply), 500);
		log_printf("%s -> %s\n", c, n < 0 ? "(timeout)" : reply);
	}

	int      ok = 0, errors[6] = {0};
	uint32_t t0 = esp32sdr_ms(), us = 0;
	for (int i = 0; i < 50; i++) {
		int r = esp32sdr_capture(SAMPLES, ESP32SDR_RATE_40MSPS, iq, &us);
		if (r == 0)
			ok++;
		else
			errors[-r]++;
	}
	uint32_t ms = esp32sdr_ms() - t0;
	log_printf("50 captures of %d samples (40 MS/s): %d ok, errors %d/%d/%d/%d/%d, %lu ms (%lu KB/s), "
		"capture %lu us\n", SAMPLES, ok, errors[1], errors[2], errors[3], errors[4], errors[5],
		(unsigned long)ms, (unsigned long)(50*2*SAMPLES/(ms ? ms : 1)), (unsigned long)us);

	/* Power estimate of the last capture (I/Q mean square). */
	uint32_t power = 0;
	for (int i = 0; i < 2*SAMPLES; i++)
		power += iq[i]*iq[i];
	log_printf("Last capture mean square: %lu\n", (unsigned long)(power/(2*SAMPLES)));
	log_printf("Done.\n");

	for (;;);
	return 0;
}

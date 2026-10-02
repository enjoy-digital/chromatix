// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// ESP-SDR client (https://github.com/ESPARGOS/esp-sdr protocol) over the ESP32 UART (--with-app
// esp32_uart CSRs, 2Mbaud): newline-terminated ASCII commands, text replies, captures replied with
// "DATA <samples> <crc32> <capture-us>" followed by the binary I/Q payload. With the Chromatic
// ESP-SDR fork (firmware/esp32-sdr), the payload can be written to the main RAM over QSPI instead.

#ifndef ESP32SDR_H
#define ESP32SDR_H

#include <stdint.h>

/* Capture rate indexes (CAP16/CAP20). */
enum {
	ESP32SDR_RATE_80MSPS = 0,
	ESP32SDR_RATE_40MSPS = 1,
	ESP32SDR_RATE_16MSPS = 6,
};

void     esp32sdr_init(void);
uint32_t esp32sdr_ms(void);
/* Drop pending RX bytes (resync). */
void     esp32sdr_flush(void);
/* Command without reply (GAIN). */
void     esp32sdr_send(const char *cmd);
/* Command: reply line (without newline) in reply, returns its length or -1 (timeout). */
int      esp32sdr_cmd(const char *cmd, char *reply, int len, uint32_t timeout_ms);
/* QSPI transport (firmware/esp32-sdr): payloads written to buf (main RAM, at psram_address in the
   PSRAM, 1KB aligned, readable/writable up to the next 1KB after the payload), 0: UART. Returns -1
   if not supported (stock ESP-SDR: UART). */
int      esp32sdr_qspi(void *buf, uint32_t psram_address);
/* 8-bit I/Q capture (I, Q interleaved): returns 0 if complete and CRC valid. */
int      esp32sdr_capture(int samples, int rate, int8_t *iq, uint32_t *capture_us);
uint32_t esp32sdr_crc32(const uint8_t *data, int len);

#endif

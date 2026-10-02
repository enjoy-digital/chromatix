// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Part of the Chromatic port of ESP-SDR (https://espargos.net/espsdr/, ESPARGOS: Florian Euchner).
//
// ESP-SDR Chromatic transport: captures written to the FPGA PSRAM over the ESP32 -> FPGA QSPI link
// (the ModRetro menu/OSD link), commands and DATA headers stay on the UART.
//
// Commands: "QSPI <address>" (PSRAM byte address, 0: captures over the UART), "QSPI?".

#ifndef CHROMATIC_QSPI_H
#define CHROMATIC_QSPI_H

#include <stdbool.h>
#include <stddef.h>

/* PSRAM destination of the captures (0: UART). */
extern unsigned chromatic_qspi_address;

/* QSPI commands (replies on the burst serial): returns true if the line was handled. */
bool chromatic_qspi_command(const char *line);
/* Write data to the PSRAM at chromatic_qspi_address (1KB transfers, the last one padded with the
   following bytes of data: the buffer must be readable up to the next 1KB). */
bool chromatic_qspi_write(const void *data, size_t size);

#endif

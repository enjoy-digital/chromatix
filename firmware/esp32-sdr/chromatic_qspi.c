// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// ESP-SDR Chromatic transport, see chromatic_qspi.h.
//
// FPGA QSPI slave (chromatix/gateware/memory.py QSPISlave/QSPIBurstWrite), as driven by the
// ModRetro MCU firmware: VSPI (SPI3) at 40MHz, 11-bit command (write bit + 10-bit length),
// 32-bit address, 3 dummy bits on MOSI, then the data on the 4 lines; each transfer is written to
// the PSRAM as one 1KB burst at the end of the transfer (CS rising).

#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "driver/spi_master.h"
#include "esp_err.h"

#include "burst_serial.h"
#include "chromatic_qspi.h"

#define PIN_QSPI_CS   5
#define PIN_QSPI_CLK  18
#define PIN_QSPI_D0   23
#define PIN_QSPI_D1   19
#define PIN_QSPI_D2   22
#define PIN_QSPI_D3   21

#define XFER_BYTES    1024 /* FPGA burst length. */
#define XFER_QUEUE    16

unsigned chromatic_qspi_address;

static spi_device_handle_t spi;
static spi_transaction_t   trans[XFER_QUEUE];

static bool qspi_init(void)
{
    if (spi)
        return true;
    spi_bus_config_t bus = {
        .mosi_io_num     = PIN_QSPI_D0,
        .miso_io_num     = PIN_QSPI_D1,
        .quadwp_io_num   = PIN_QSPI_D2,
        .quadhd_io_num   = PIN_QSPI_D3,
        .sclk_io_num     = PIN_QSPI_CLK,
        .max_transfer_sz = XFER_BYTES,
        .flags           = SPICOMMON_BUSFLAG_QUAD | SPICOMMON_BUSFLAG_MASTER |
                           SPICOMMON_BUSFLAG_IOMUX_PINS,
    };
    spi_device_interface_config_t dev = {
        .clock_speed_hz   = 40*1000*1000,
        .mode             = 0,
        .spics_io_num     = PIN_QSPI_CS,
        .queue_size       = XFER_QUEUE,
        .flags            = SPI_DEVICE_HALFDUPLEX,
        .cs_ena_pretrans  = 3,
        .cs_ena_posttrans = 3,
        .command_bits     = 11,
        .address_bits     = 32,
        .dummy_bits       = 3,
    };
    if (spi_bus_initialize(SPI3_HOST, &bus, SPI_DMA_CH_AUTO) != ESP_OK)
        return false;
    if (spi_bus_add_device(SPI3_HOST, &dev, &spi) != ESP_OK) {
        spi = NULL;
        return false;
    }
    return true;
}

static void reply(const char *text)
{
    burst_serial_send(text, strlen(text));
}

bool chromatic_qspi_command(const char *line)
{
    char     text[32];
    unsigned address;
    char     extra;
    if (!strcmp(line, "QSPI?")) {
        snprintf(text, sizeof(text), "QSPI %u\n", chromatic_qspi_address);
        reply(text);
        return true;
    }
    if (strncmp(line, "QSPI ", 5))
        return false;
    /* 1KB aligned, not 0 (two transfers to address 0: ModRetro menu init). */
    if (sscanf(line, "QSPI %u %c", &address, &extra) != 1 || (address & (XFER_BYTES - 1))) {
        reply("ERR qspi_args\n");
        return true;
    }
    if (address && !qspi_init()) {
        reply("ERR qspi_init\n");
        return true;
    }
    chromatic_qspi_address = address;
    reply("OK\n");
    return true;
}

bool chromatic_qspi_write(const void *data, size_t size)
{
    const uint8_t *src    = data;
    size_t         xfers  = (size + XFER_BYTES - 1)/XFER_BYTES;
    size_t         queued = 0, done = 0;
    while (done < xfers) {
        /* Keep the queue full, collect the results. */
        while (queued < xfers && queued - done < XFER_QUEUE) {
            spi_transaction_t *t = &trans[queued % XFER_QUEUE];
            memset(t, 0, sizeof(*t));
            t->flags     = SPI_TRANS_MODE_QIO; /* Command/address on D0, data on D0-D3. */
            t->cmd       = 0x400 | (XFER_BYTES - 1);
            t->addr      = chromatic_qspi_address + queued*XFER_BYTES;
            t->tx_buffer = src + queued*XFER_BYTES;
            t->length    = XFER_BYTES*8;
            if (spi_device_queue_trans(spi, t, portMAX_DELAY) != ESP_OK)
                return false;
            queued++;
        }
        spi_transaction_t *r;
        if (spi_device_get_trans_result(spi, &r, portMAX_DELAY) != ESP_OK)
            return false;
        done++;
    }
    return true;
}

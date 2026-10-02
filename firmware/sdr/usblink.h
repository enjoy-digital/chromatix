// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// USB link (--with-app CDCLink): the USB CDC byte stream switched from the debug bridge to the
// application, bytes through a UART, bulk data from the main RAM by DMA. The host switches back to
// the debug bridge with a 1200 baud touch (open/close the port at 1200 baud).

#ifndef USBLINK_H
#define USBLINK_H

#include <stdint.h>

/* Select the USB link (returns -1 if the gateware has no USB link). */
int  usblink_init(void);
/* Host 1200 baud touch since usblink_init (link back to the debug bridge). */
int  usblink_touched(void);
/* Received byte or -1. */
int  usblink_getc(void);
void usblink_write(const void *data, int len);
/* Bulk data from the main RAM (L2 cache written back/invalidated first), after the bytes written
   before; returns when sent. */
void usblink_write_dma(const void *data, int len);

#endif

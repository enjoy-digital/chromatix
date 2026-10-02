// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// USB link, see usblink.h.

#include <stdint.h>

#include <system.h>
#include <generated/csr.h>

#include "usblink.h"

#ifdef CSR_USB_LINK_BASE

int usblink_init(void)
{
	usb_link_dma_enable_write(0);
	usb_link_control_write(1 << CSR_USB_LINK_CONTROL_APP_OFFSET);
	return 0;
}

int usblink_touched(void)
{
	return (usb_link_status_read() >> CSR_USB_LINK_STATUS_TOUCH_OFFSET) & 1;
}

int usblink_getc(void)
{
	if (usb_link_uart_rxempty_read())
		return -1;
	return usb_link_uart_rxtx_read(); /* Popped on read. */
}

void usblink_write(const void *data, int len)
{
	const uint8_t *p = data;
	for (int i = 0; i < len; i++) {
		while (usb_link_uart_txfull_read())
			if (usblink_touched())
				return;
		usb_link_uart_rxtx_write(p[i]);
	}
}

static void wait_dma_idle(void)
{
	while (!((usb_link_status_read() >> CSR_USB_LINK_STATUS_DMA_IDLE_OFFSET) & 1))
		if (usblink_touched())
			return;
}

void usblink_write_dma(const void *data, int len)
{
	int words = len & ~3;
	/* UART bytes sent first, DMA reads coherent with the L2 cache. */
	while (!usb_link_uart_txempty_read())
		if (usblink_touched())
			return;
	flush_l2_cache();
	if (words) {
		usb_link_dma_base_write((uint32_t)data);
		usb_link_dma_length_write(words);
		usb_link_dma_enable_write(1);
		wait_dma_idle();
		usb_link_dma_enable_write(0);
	}
	usblink_write((const uint8_t *)data + words, len - words);
}

#else

int  usblink_init(void) { return -1; }
int  usblink_touched(void) { return 1; }
int  usblink_getc(void) { return -1; }
void usblink_write(const void *data, int len) { (void)data; (void)len; }
void usblink_write_dma(const void *data, int len) { (void)data; (void)len; }

#endif

#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litex.gen import *

# Tick Generator -----------------------------------------------------------------------------------

class TickGenerator(LiteXModule):
    """
    1s / 0.5s / 1% enable pulses (from a 2**23 cycles period, ~1s at ~8.39MHz).

    The 1% counter is re-aligned on each 1s pulse.
    """
    def __init__(self, second_cycles=2**23, percent_cycles=83886):
        self.second      = Signal()
        self.half_second = Signal()
        self.percent     = Signal()
        self.counter     = counter = Signal(max=second_cycles, reset=0)

        # # #

        percent_counter = Signal(max=percent_cycles + 1, reset=0)

        self.sync += [
            self.percent.eq(0),
            If(percent_counter == percent_cycles,
                self.percent.eq(1),
                percent_counter.eq(0),
            ).Else(
                percent_counter.eq(percent_counter + 1),
            ),

            self.second.eq(0),
            self.half_second.eq(0),
            If(counter == (second_cycles//2 - 1),
                self.half_second.eq(1),
            ),
            If(counter == (second_cycles - 1),
                self.second.eq(1),
                self.half_second.eq(1),
                counter.eq(0),
                percent_counter.eq(0),
            ).Else(
                counter.eq(counter + 1),
            ),
        ]

# Status LED ---------------------------------------------------------------------------------------

class StatusLed(LiteXModule):
    """
    RGB status LED (active-low).

    Flashes white three times after configuration (so a custom build is obvious on hardware), then
    shows the system monitor status with priority: white > green > yellow (blinking) > red.
    """
    def __init__(self, pads):
        self.reset  = Signal()
        self.white  = Signal()
        self.green  = Signal()
        self.yellow = Signal()
        self.red    = Signal()
        self.blink  = Signal()

        # # #

        boot_counter = Signal(26)
        boot_active  = Signal()
        boot_white   = Signal()

        self.comb += pads.en.eq(1)

        self.sync += [
            If(self.reset,
                boot_counter.eq(0),
            ).Elif(boot_counter[23:26] != 6,
                boot_counter.eq(boot_counter + 1),
            )
        ]
        self.comb += [
            boot_active.eq(boot_counter[23:26] < 6),
            boot_white.eq(~boot_counter[23]),
        ]

        self.sync += [
            If(boot_active & boot_white,
                pads.r.eq(0), pads.b.eq(0), pads.g.eq(0),
            ).Elif(boot_active,
                pads.r.eq(1), pads.b.eq(1), pads.g.eq(1),
            ).Elif(self.white,
                pads.r.eq(0), pads.b.eq(0), pads.g.eq(0),
            ).Elif(self.green,
                pads.r.eq(1), pads.b.eq(1), pads.g.eq(0),
            ).Elif(self.yellow,
                pads.r.eq(0), pads.b.eq(1), pads.g.eq(self.blink),
            ).Elif(self.red,
                pads.r.eq(0), pads.b.eq(1), pads.g.eq(1),
            ).Else(
                pads.r.eq(1), pads.b.eq(1), pads.g.eq(1),
            ),
        ]

# ESP32 Control ------------------------------------------------------------------------------------

class ESP32Control(LiteXModule):
    """
    ESP32 UART passthrough and boot control from the USB CDC DTR/RTS lines (esptool style).

    - "phy" domain (USB PHY clock): resynchronizes the UART and computes EN/IO0 from RTS/DTR.
    - "sys" domain: EN release delayed/filtered through a shift register (IO0 is registered).
    """
    def __init__(self, esp32_pads, uart_pads):
        self.usb_locked = Signal()
        self.usb_txd    = Signal(reset=1) # To USB CDC (ESP32 TX).
        self.usb_rxd    = Signal()        # From USB CDC (to ESP32 RX).
        self.usb_dtr    = Signal()
        self.usb_rts    = Signal()

        # # #

        en_int  = Signal(reset=1)
        io0_int = Signal(reset=1)

        # PHY domain: UART resync + ESP32 EN/IO0 from USB DTR/RTS.
        self.sync.phy += [
            If(~self.usb_locked,
                self.usb_txd.eq(1),
                uart_pads.rx.eq(1),
                en_int.eq(1),
                io0_int.eq(1),
            ).Else(
                self.usb_txd.eq(uart_pads.tx),
                uart_pads.rx.eq(self.usb_rxd),
                en_int.eq(~self.usb_rts),
                io0_int.eq((self.usb_dtr == 0) & (self.usb_rts == 0)),
            ),
        ]

        # Sys domain: ESP32 boot delay (shift register debounces EN toggle).
        delay_cnt   = Signal(12, reset=0)
        delay_shift = Signal(8,  reset=0)
        self.sync += [
            esp32_pads.io0.eq(io0_int),
            delay_cnt.eq(delay_cnt + 1),
            If(delay_cnt == 0,
                delay_shift.eq(Cat(en_int, delay_shift[:7])),
                esp32_pads.en.eq(delay_shift[7]),
            ),
            If(~en_int,
                delay_shift.eq(0),
                esp32_pads.en.eq(0),
            ),
        ]

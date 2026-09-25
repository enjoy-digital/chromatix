#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

import os

from migen import *

from litex.gen import *

# Helpers ------------------------------------------------------------------------------------------

ST7785_REGS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "st7785_regs.bin")

def load_st7785_sequence(path=ST7785_REGS_PATH):
    """Load the ST7785 init sequence (one 9-bit {D/C, data} word per line, in binary)."""
    with open(path, encoding="utf-8") as f:
        sequence = [int(line.strip(), 2) for line in f if line.strip()]
    if not sequence:
        raise ValueError(f"No ST7785 init values found in {path}")
    return sequence

# ST7785 Init --------------------------------------------------------------------------------------

class ST7785Init(LiteXModule):
    """
    ST7785 LCD init sequencer (port of ST7785_init.v).

    Pulses the LCD reset, waits, then shifts the init sequence out on the 3-wire SPI (9-bit words,
    D/C bit first, SCK = sys_clk / (2*clk_div_2n), SDA sampled on SCK rising edges). The first
    len(sequence) - 5 words are sent, then a pause (delay_end 10-SCK word slots), then the following
    words; lcd_init_done rises when the before-last word is loaded (it is truncated and the last
    word is never sent, as in the original: the sequence ends with NOP padding). Delays are in SCK
    periods (shortened by issimu).
    """
    def __init__(self, sequence, issimu=False, clk_div_2n=6):
        self.reset         = Signal()
        self.lcd_sck       = Signal()
        self.lcd_cs        = Signal(reset=1)
        self.lcd_sda_sdi   = Signal()
        self.lcd_rst       = Signal(reset=1)
        self.lcd_init_done = Signal()

        # # #

        data_max_cnt = len(sequence) - 1
        reset_start  = 2000 if issimu else 0
        reset_end    = 3000 if issimu else 80000
        delay_max    = 5000 if issimu else 90000
        delay_end    = 500 if issimu else 2560

        div_counter  = Signal(max=clk_div_2n)
        sck          = Signal()
        tick         = Signal()
        cs           = Signal(reset=1)
        data_cnt     = Signal(max=len(sequence) + 1)
        bit_cnt      = Signal(5)
        delay_cnt    = Signal(24)
        delay_cnt2   = Signal(max=delay_end + 1)
        datasr       = Signal(8)
        max_cnt      = Signal(max=len(sequence) + 1, reset=max(data_max_cnt - 4, 0))
        txdata       = Signal(9)

        # Sequence ROM / SCK gated by CS (Mux(cs, 0, sck) rather than Mux(~cs, sck, 0): the Migen
        # simulator doesn't truncate ~cs in a Mux selector).
        self.comb += [
            txdata.eq(Array(Constant(value, 9) for value in sequence)[data_cnt]),
            self.lcd_sck.eq(Mux(cs, 0, sck)),
        ]

        # SCK divider (tick: first cycle of each SCK high phase).
        self.sync += [
            tick.eq(0),
            If(self.reset,
                div_counter.eq(0),
                sck.eq(0),
            ).Else(
                If(div_counter == 0,
                    div_counter.eq(clk_div_2n - 1),
                    If(~sck,
                        tick.eq(1),
                    ),
                    sck.eq(~sck),
                ).Else(
                    div_counter.eq(div_counter - 1),
                )
            )
        ]

        # Reset pulse, delays and SPI shifter.
        self.sync += [
            If(self.reset,
                self.lcd_rst.eq(1),
                self.lcd_init_done.eq(0),
                self.lcd_cs.eq(1),
                self.lcd_sda_sdi.eq(0),
                cs.eq(1),
                data_cnt.eq(0),
                bit_cnt.eq(0),
                delay_cnt.eq(0),
                delay_cnt2.eq(0),
                datasr.eq(0),
                max_cnt.eq(max(data_max_cnt - 4, 0)),
            ).Else(
                self.lcd_cs.eq(cs),
                If(data_cnt < data_max_cnt,
                    self.lcd_init_done.eq(0),
                ).Else(
                    self.lcd_init_done.eq(1),
                ),
                If(tick,
                    If(delay_cnt == reset_start,
                        self.lcd_rst.eq(0),
                    ),
                    If(delay_cnt == reset_end,
                        self.lcd_rst.eq(1),
                    ),
                    If(delay_cnt < delay_max,
                        delay_cnt.eq(delay_cnt + 1),
                    ),
                    If((data_cnt < data_max_cnt) & (delay_cnt == delay_max),
                        If(bit_cnt < 9,
                            bit_cnt.eq(bit_cnt + 1),
                        ).Else(
                            bit_cnt.eq(0),
                        ),
                    ).Else(
                        bit_cnt.eq(0),
                    ),
                    If(delay_cnt != delay_max,
                        data_cnt.eq(0),
                        datasr.eq(0),
                        cs.eq(1),
                        self.lcd_sda_sdi.eq(0),
                        delay_cnt2.eq(0),
                        max_cnt.eq(max(data_max_cnt - 4, 0)),
                    ).Else(
                        If(bit_cnt == 0,
                            If(data_cnt < max_cnt,
                                cs.eq(0),
                                datasr.eq(txdata[:8]),
                                self.lcd_sda_sdi.eq(txdata[8]),
                                data_cnt.eq(data_cnt + 1),
                            ).Else(
                                cs.eq(1),
                                If(delay_cnt2 < delay_end,
                                    delay_cnt2.eq(delay_cnt2 + 1),
                                ).Else(
                                    max_cnt.eq(data_max_cnt),
                                )
                            ),
                        ).Else(
                            If(bit_cnt < 9,
                                datasr.eq(Cat(Constant(0, 1), datasr[:7])),
                                self.lcd_sda_sdi.eq(datasr[7]),
                            ).Else(
                                cs.eq(1),
                            )
                        )
                    )
                )
            )
        ]

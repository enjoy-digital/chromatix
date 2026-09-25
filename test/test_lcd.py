#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from test.common import run_domain_simulation

from chromatix.gateware.lcd import ST7785Init, load_st7785_sequence

# ST7785 Init --------------------------------------------------------------------------------------

def spi_capture(dut, words, done, max_cycles=200000):
    """Capture 9-bit SPI words (D/C + 8 data bits, MSB first) on SCK rising edges while CS is low."""
    bits     = []
    prev_sck = 0
    for cycle in range(max_cycles):
        sck = (yield dut.lcd_sck)
        cs  = (yield dut.lcd_cs)
        if cs:
            bits = []
        elif sck and not prev_sck:
            bits.append((yield dut.lcd_sda_sdi))
            if len(bits) == 9:
                words.append(int("".join(map(str, bits)), 2))
                bits = []
        prev_sck = sck
        if (yield dut.lcd_init_done):
            done.append(cycle)
            return
        yield

def test_st7785_init_sequence():
    """The init sequence is shifted out as 9-bit words (the last 2 entries are never sent)."""
    sequence = [0x111, 0x02A, 0x155, 0x0FF, 0x100, 0x001, 0x1AB, 0x0CD, 0x1EF, 0x033]
    dut      = ST7785Init(sequence, issimu=True)
    words    = []
    done     = []
    run_domain_simulation(dut, {"pclk": spi_capture(dut, words, done)}, clocks={"pclk": 10})
    assert done, "lcd_init_done never asserted"
    assert words == sequence[:-2]

def test_st7785_register_image():
    """The ST7785 register image loads as 9-bit words."""
    sequence = load_st7785_sequence()
    assert len(sequence) == 128
    assert all(0 <= word < 2**9 for word in sequence)

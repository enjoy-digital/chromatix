#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litex.gen.sim import run_simulation

from chromatix.gateware.lcd import ST7785Init, load_st7785_sequence

# Helpers ------------------------------------------------------------------------------------------

SEQUENCE = [0x111, 0x02A, 0x155, 0x0FF, 0x100, 0x001, 0x1AB, 0x0CD, 0x1EF, 0x033]

def spi_monitor(dut, trace, cycles):
    """Sample the SPI/control outputs every cycle: (sck, cs, sda, rst, done)."""
    for cycle in range(cycles):
        trace.append((
            (yield dut.lcd_sck),
            (yield dut.lcd_cs),
            (yield dut.lcd_sda_sdi),
            (yield dut.lcd_rst),
            (yield dut.lcd_init_done),
        ))
        yield

def spi_decode(trace):
    """Decode 9-bit SPI words (D/C + 8 data bits, MSB first) sampled on SCK rising edges while CS
    is low. Returns (words, trailing bits of an incomplete word per CS frame)."""
    words    = []
    partial  = []
    bits     = []
    prev_sck = 0
    for sck, cs, sda, rst, done in trace:
        if cs:
            if bits:
                partial.append(bits)
            bits = []
        elif sck and not prev_sck:
            bits.append(sda)
            if len(bits) == 9:
                words.append(int("".join(map(str, bits)), 2))
                bits = []
        prev_sck = sck
    return words, partial

def first(trace, predicate, start=0):
    for i in range(start, len(trace)):
        if predicate(trace[i]):
            return i
    return None

# ST7785 Init --------------------------------------------------------------------------------------

def test_st7785_init_sequence():
    """The first words are shifted out as 9-bit SPI words; lcd_init_done rises when the before-last
    word is loaded (it is truncated after its D/C bit + 1 data bit) and the last word is never
    sent."""
    dut   = ST7785Init(SEQUENCE, issimu=True, clk_div_2n=2)
    trace = []
    run_simulation(dut, spi_monitor(dut, trace, 45000))
    done = first(trace, lambda s: s[4])
    assert done is not None, "lcd_init_done never asserted"
    words, partial = spi_decode(trace[:done])
    assert words == SEQUENCE[:-2]
    assert partial == []
    words, partial = spi_decode(trace[done:])
    assert words == []
    assert partial == [[SEQUENCE[-2] >> 8, (SEQUENCE[-2] >> 7) & 1]]
    # lcd_init_done stays asserted and SPI stays idle.
    assert all(s[4] for s in trace[done:])
    assert all(s[1] for s in trace[-1000:])

def test_st7785_init_timing():
    """Reset pulse / SCK period / CS gating / pause between the two parts of the sequence."""
    clk_div_2n = 3
    sck_period = 2*clk_div_2n
    dut        = ST7785Init(SEQUENCE, issimu=True, clk_div_2n=clk_div_2n)
    trace      = []
    run_simulation(dut, spi_monitor(dut, trace, 65000))

    # LCD reset: low from tick 2000 to tick 3000 (1000 SCK periods), released high otherwise.
    rst_fall = first(trace, lambda s: not s[3])
    rst_rise = first(trace, lambda s: s[3], rst_fall)
    assert rst_fall is not None and rst_rise is not None
    assert rst_rise - rst_fall == 1000*sck_period
    assert abs(rst_fall - 2000*sck_period) <= sck_period
    assert all(s[3] for s in trace[rst_rise:])

    # No SPI activity before the post-reset delay (5000 ticks), SCK only toggles while CS is low
    # (SCK is gated by the internal CS, one cycle ahead of the registered lcd_cs: SCK can rise one
    # cycle before lcd_cs falls, as in the original).
    cs_fall = first(trace, lambda s: not s[1])
    assert abs(cs_fall - 5000*sck_period) <= 2*sck_period
    assert all(s[0] == 0 for s in trace[:cs_fall - 1])
    for i in range(len(trace) - 1):
        if trace[i][0] and trace[i][1]:
            assert not trace[i + 1][1]

    # SCK period (rising edges within a CS low frame).
    periods = set()
    last    = None
    for i in range(1, len(trace)):
        if trace[i][1]:
            last = None
        elif trace[i][0] and not trace[i - 1][0]:
            if last is not None:
                periods.add(i - last)
            last = i
    assert periods == {sck_period}

    # Pause (500 word slots of 10 SCK periods) between the first len - 5 words and the others.
    frames = []
    for i in range(1, len(trace)):
        if not trace[i][1] and trace[i - 1][1]:
            frames.append(i)
    gaps = [b - a for a, b in zip(frames, frames[1:])]
    assert len(frames) == len(SEQUENCE) - 1
    assert gaps.index(max(gaps)) == len(SEQUENCE) - 6
    assert abs(max(gaps) - 500*10*sck_period) <= 20*sck_period

def test_st7785_init_reset():
    """reset restarts the whole sequence (reset pulse, delay, words)."""
    dut   = ST7785Init(SEQUENCE, issimu=True, clk_div_2n=2)
    trace = []

    def gen():
        for i in range(45000):
            if (yield dut.lcd_init_done):
                break
            yield
        assert (yield dut.lcd_init_done)
        yield dut.reset.eq(1)
        yield
        yield dut.reset.eq(0)
        yield
        assert not (yield dut.lcd_init_done)
        yield from spi_monitor(dut, trace, 45000)

    run_simulation(dut, gen())
    assert first(trace, lambda s: not s[3]) is not None # LCD reset pulsed again.
    done = first(trace, lambda s: s[4])
    assert done is not None
    words, partial = spi_decode(trace[:done])
    assert words == SEQUENCE[:-2]

def test_st7785_register_image():
    """The ST7785 register image loads as 9-bit words (commands first, NOP padding at the end)."""
    sequence = load_st7785_sequence()
    assert len(sequence) == 128
    assert all(0 <= word < 2**9 for word in sequence)
    assert sequence[0]  == 0x011  # SLPOUT (command).
    assert sequence[-2:] == [0, 0] # Truncated/unsent words are NOPs.

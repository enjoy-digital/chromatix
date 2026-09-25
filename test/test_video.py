#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

import random

from migen import *

from litex.gen.sim import run_simulation

from chromatix.gateware.video import ColorCorrection, GLYPHS_TIMER, glyph_lookup

# Color Correction ---------------------------------------------------------------------------------

def color_correction_model(r, g, b, correct_lcd, correct_uvc):
    """Python model of color_correction (vid_system_top.sv), with Verilog truncation semantics."""
    r10 = (r*13 + g*2 + b) & 0x3ff
    g8  = (g*3 + b) & 0xff
    b10 = (r*3 + g*2 + b*11) & 0x3ff
    r1, g1, b1 = r >> 1, g >> 1, b >> 1
    rlcd1 = (r1*216 + g1*30) & 0xffff
    rlcd2 = (b1*25) & 0xffff
    rlcd3 = 0 if rlcd1 < rlcd2 else rlcd1 - rlcd2
    glcd  = (r1*39 + g1*137 + b1*24) & 0xffff
    blcd  = (r1*21 + g1*24 + b1*125) & 0xffff
    clamp = lambda v: 0x3f if (v >> 13) & 1 else (v >> 7) & 0x3f
    pack  = lambda r, g, b: (b << 12) | (g << 6) | r
    lcd = pack(clamp(rlcd3), clamp(glcd), clamp(blcd)) if correct_lcd else pack(r, g, b)
    uvc = pack(r10 >> 4, g8 >> 2, b10 >> 4) if correct_uvc else pack(r, g, b)
    return lcd, uvc

def test_color_correction():
    """LCD/UVC color correction matches the original Verilog arithmetic."""
    dut    = ColorCorrection()
    pixels = [(0, 0, 0), (63, 63, 63), (63, 0, 0), (0, 63, 0), (0, 0, 63)]
    pixels += [tuple(random.randrange(64) for _ in range(3)) for _ in range(64)]
    modes  = [(c_lcd, c_uvc) for c_lcd in (0, 1) for c_uvc in (0, 1)]
    errors = []

    def gen():
        for correct_lcd, correct_uvc in modes:
            yield dut.correct_lcd.eq(correct_lcd)
            yield dut.correct_uvc.eq(correct_uvc)
            for r, g, b in pixels:
                yield dut.pixel.eq((b << 12) | (g << 6) | r)
                yield
                yield
                result = ((yield dut.pixel_lcd), (yield dut.pixel_uvc))
                if result != color_correction_model(r, g, b, correct_lcd, correct_uvc):
                    errors.append((r, g, b, correct_lcd, correct_uvc, result))

    run_simulation(dut, gen())
    assert errors == []

# Overlay Glyphs -----------------------------------------------------------------------------------

def test_timer_glyphs():
    """Timer glyph lookup returns the 3x5 bitmaps."""
    dut      = Module()
    number   = Signal(4)
    x        = Signal(2)
    y        = Signal(3)
    pixel    = Signal()
    dut.comb += pixel.eq(glyph_lookup(GLYPHS_TIMER, number, x, y))
    errors   = []

    def gen():
        for n, glyph in enumerate(GLYPHS_TIMER):
            for gy in range(5):
                for gx in range(3):
                    yield number.eq(n)
                    yield x.eq(gx)
                    yield y.eq(gy)
                    yield
                    if (yield pixel) != int(glyph[gy][gx]):
                        errors.append((n, gx, gy))

    run_simulation(dut, gen())
    assert errors == []

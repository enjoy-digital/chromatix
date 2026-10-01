#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""LCDFramebuffer: Wishbone pixels/palette writes -> 160x144 Game Boy LCD pixels."""

import random

from migen import *

from litex.gen.sim import run_simulation

from chromatix.gateware.framebuffer import LCDFramebuffer

# Helpers ------------------------------------------------------------------------------------------

def render(width, height, pixels, palette, byte_writes=False):
    """Writes pixels (width x height indexes) and palette (RGB555), returns a frame (144 x 160)."""
    dut = LCDFramebuffer(width=width, height=height, line_dots=166, h_start=2, frame_lines=146)
    dut.clock_domains.cd_hclk = ClockDomain()
    screen = []
    done   = []

    def writer():
        for i, color in enumerate(palette):
            yield from dut.bus.write(dut.PALETTE_OFFSET + i, color)
        flat = [p for line in pixels for p in line]
        if byte_writes:
            for i, p in enumerate(flat):
                yield from dut.bus.write(i//4, p << 8*(i%4), sel=1 << (i%4))
        else:
            for i in range(0, len(flat), 4):
                yield from dut.bus.write(i//4, sum(p << 8*j for j, p in enumerate(flat[i:i + 4])))
        done.append(True)

    def reader():
        # Wait for the writer, then capture a frame: 144 lines of 160 pixels from the vsync rising edge.
        while not done:
            yield
        while (yield dut.gb_vsync):
            yield
        while not (yield dut.gb_vsync):
            yield
        frame = []
        while len(frame) < 144*160:
            if (yield dut.gb_clkena):
                frame.append((yield dut.gb_data))
            yield
        screen.extend(frame[160*y:160*(y + 1)] for y in range(144))

    run_simulation(dut, {"sys": writer(), "hclk": reader()}, clocks={"sys": 20, "hclk": 10})
    return screen

def expected(width, height, pixels, palette):
    return [[palette[pixels[y][x]] if (y < height and x < width) else 0 for x in range(160)]
        for y in range(144)]

# Tests --------------------------------------------------------------------------------------------

def test_framebuffer_full():
    """160x144: random pixels and palette (word writes)."""
    random.seed(0)
    palette = [random.randrange(1 << 15) for _ in range(256)]
    pixels  = [[random.randrange(256) for x in range(160)] for y in range(144)]
    assert render(160, 144, pixels, palette) == expected(160, 144, pixels, palette)

def test_framebuffer_small_byte_writes():
    """Smaller framebuffer (black outside), byte writes."""
    random.seed(1)
    palette = [random.randrange(1, 1 << 15) for _ in range(256)]
    pixels  = [[(x + 3*y) % 256 for x in range(64)] for y in range(20)]
    assert render(64, 20, pixels, palette, byte_writes=True) == expected(64, 20, pixels, palette)

def test_framebuffer_frame_counter():
    dut = LCDFramebuffer(line_dots=166, h_start=2, frame_lines=146)
    dut.clock_domains.cd_hclk = ClockDomain()
    counts = []

    def gen():
        for _ in range(3*4*166*146 + 100):
            yield
        counts.append((yield dut._frame.status))

    run_simulation(dut, {"sys": gen()}, clocks={"sys": 10, "hclk": 10})
    assert counts[0] in [3, 4]

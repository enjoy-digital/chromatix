#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""LCDTerminal: console bytes -> 160x144 Game Boy LCD pixels, decoded back to text (font matching)."""

from migen import *

from litex.gen.sim import run_simulation

from chromatix.gateware.terminal import LCDTerminal, FONT_4X6, COLORS, TERM_COLS, TERM_LINES, rgb555

# Helpers ------------------------------------------------------------------------------------------

GLYPHS = {tuple(g): chr(0x20 + i) for i, g in enumerate(FONT_4X6)}
PIXELS = {rgb555(*c): i for i, c in enumerate(COLORS)}

def render(data):
    """Sends data to the terminal, returns the last frame as (text lines, color lines)."""
    dut    = LCDTerminal(line_dots=166, h_start=2, frame_lines=146) # Short blankings.
    dut.clock_domains.cd_hclk = ClockDomain()
    screen = []

    def writer():
        for b in data:
            yield dut.sink.valid.eq(1)
            yield dut.sink.data.eq(b)
            yield
            while not (yield dut.sink.ready):
                yield
        yield dut.sink.valid.eq(0)

    def reader():
        # Wait for the writer, then capture a frame: 144 lines of 160 pixels from the vsync rising edge.
        for _ in range(4*len(data) + 4*TERM_COLS*len(data)//8 + 100):
            yield
        while (yield dut.gb_vsync):
            yield
        while not (yield dut.gb_vsync):
            yield
        pixels = []
        while len(pixels) < 144*160:
            if (yield dut.gb_clkena):
                pixels.append((yield dut.gb_data))
            yield
        screen.extend(pixels[160*y:160*(y + 1)] for y in range(144))

    run_simulation(dut, {"sys": writer(), "hclk": reader()}, clocks={"sys": 20, "hclk": 10})
    assert len(screen) == 144 and all(len(l) == 160 for l in screen)
    texts, colors = [], []
    for ty in range(TERM_LINES):
        text, color = "", ""
        for tx in range(TERM_COLS):
            cell = [[screen[6*ty + y][4*tx + x] for x in range(4)] for y in range(6)]
            assert all(r[3] == 0 for r in cell) # Spacing column.
            mask = tuple(sum(((p != 0) << (2 - x)) for x, p in enumerate(r[:3])) for r in cell)
            text += GLYPHS[mask]
            fg    = {p for r in cell for p in r if p}
            assert len(fg) <= 1
            color += str(PIXELS[fg.pop()]) if fg else "."
        texts.append(text.rstrip())
        colors.append(color)
    return texts, colors

# Tests --------------------------------------------------------------------------------------------

def test_terminal_text_and_colors():
    """LiteX BIOS style output: CR/LF, bold banner, green prompt (SGR), backspace."""
    data  = b"\x1b[1m        __   _ __      _  __\x1b[0m\r\n"
    data += b" (c) Copyright 2012-2026 Enjoy-Digital\r\n"
    data += b"\r\n\x1b[92;1mlitex\x1b[0m> helo\x08\x08lp"
    texts, colors = render(data)
    assert texts[0] == "        __   _ __      _  __"
    assert texts[1] == " (c) Copyright 2012-2026 Enjoy-Digital"
    assert texts[2] == ""
    assert texts[3] == "litex> help"
    assert set(colors[0].replace(".", "")) == {"1"}  # Bold.
    assert set(colors[1].replace(".", "")) == {"0"}  # Normal.
    assert colors[3][:5] == "22222"                  # Green prompt.
    assert all(t == "" for t in texts[4:])

def test_terminal_clip_tab_and_scroll():
    """Lines longer than 40 characters are clipped, tabs move to 8 columns stops, more than 24 lines
    scroll."""
    data  = b"".join(b"line %02d\r\n" % i for i in range(30))
    data += b"--" + b"="*38 + b"--\r\n"
    data += b"CPU:\t\tVexRiscv"
    texts, colors = render(data)
    assert texts[:22] == ["line %02d" % i for i in range(8, 30)]
    assert texts[22] == "--" + "="*38
    assert texts[23] == "CPU:            VexRiscv"

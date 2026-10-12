#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
MiSTer-core shell: hosts a third-party FPGA core on the Chromatic, reusing the video pipeline, PSRAM,
audio and buttons unchanged.

A hosted core (NES, Sega Master System / Game Gear, ...) is wrapped so that it presents the same
"Game Boy LCD interface" (clkena/data/mode/on/vsync, hClk) as the Game Boy core at the input of the
video pipeline (OSD, color correction, LCD and UVC capture work unchanged, as for the framebuffers
in framebuffer.py).

This module is deliberately independent of any GPL core RTL (which lives under chromatix/verilog/
cores/ and is pulled in only by the `--with-core` build): it provides the reusable, permissively
licensed glue (video sink, and later the downscaler / ioctl ROM loader / pad mapping).

- CoreTestPattern: a self-contained 160x144 RGB555 test source (SMPTE-style color bars + a moving
  marker) with Game Boy LCD timing. It brings up the whole `--with-core` shell (new build variant,
  source selection, video pipeline, LCD and UVC) before any core RTL is integrated, and stays as a
  no-ROM smoke test on hardware and in the Verilator sim.
"""

from migen import *

from litex.gen import *

from chromatix.gateware.framebuffer import GBLCDTiming

# Helpers ------------------------------------------------------------------------------------------

def rgb555(r, g, b):
    """5:5:5 color, Game Boy LCD data layout {B[4:0], G[4:0], R[4:0]} (R in the LSBs)."""
    return (b & 0x1f) << 10 | (g & 0x1f) << 5 | (r & 0x1f)

# SMPTE-style 8 color bars (left to right), RGB555.
COLOR_BARS = [
    rgb555(31, 31, 31), # White.
    rgb555(31, 31,  0), # Yellow.
    rgb555( 0, 31, 31), # Cyan.
    rgb555( 0, 31,  0), # Green.
    rgb555(31,  0, 31), # Magenta.
    rgb555(31,  0,  0), # Red.
    rgb555( 0,  0, 31), # Blue.
    rgb555( 0,  0,  0), # Black.
]

# Core Test Pattern --------------------------------------------------------------------------------

class CoreTestPattern(LiteXModule):
    """
    160x144 test source presenting the Game Boy LCD interface ("hclk" domain), so it drives the video
    pipeline exactly like the Game Boy core or the framebuffers. Draws 8 vertical color bars (20 px
    each) with a 1 px vertical white marker that moves one pixel per frame (so a frozen core is
    obvious) and a white frame border.

    Exposes gb_clkena / gb_data[15] (RGB555) / gb_mode[2] / gb_on / gb_vsync, consumed by
    VideoPipeline (video.py) and, for the application/core builds, selected as the `lcd_source`.
    """
    def __init__(self):
        self.gb_clkena = Signal()
        self.gb_data   = Signal(15)
        self.gb_mode   = Signal(2)
        self.gb_on     = Signal()
        self.gb_vsync  = Signal()

        # # #

        # Game Boy LCD timing (160x144 visible, vsync on line 0).
        self.timing = timing = GBLCDTiming()

        x     = timing.x    # Visible pixel in line (0..159), valid on gb_clkena.
        y     = timing.line # Line in frame (visible 0..143).
        vsync = timing.gb_vsync

        # Moving marker (one column per frame), advanced on each vsync rising edge.
        vsync_r = Signal()
        marker  = Signal(8)
        self.sync.hclk += [
            vsync_r.eq(vsync),
            If(vsync & ~vsync_r,
                If(marker == (160 - 1),
                    marker.eq(0),
                ).Else(
                    marker.eq(marker + 1),
                ),
            ),
        ]

        # Pattern: border + moving marker (white) over 8 color bars (bar = x // 20).
        white = rgb555(31, 31, 31)
        bar   = Signal(15)

        # Bar selection by pixel: 8 bars of 20 px. Use a small comparator ladder (x // 20).
        bars = Array([Constant(c, 15) for c in COLOR_BARS])
        bar_index = Signal(max=8)
        self.comb += [
            If(   x <  20, bar_index.eq(0)
            ).Elif(x <  40, bar_index.eq(1)
            ).Elif(x <  60, bar_index.eq(2)
            ).Elif(x <  80, bar_index.eq(3)
            ).Elif(x < 100, bar_index.eq(4)
            ).Elif(x < 120, bar_index.eq(5)
            ).Elif(x < 140, bar_index.eq(6)
            ).Else(         bar_index.eq(7)),
            bar.eq(bars[bar_index]),
        ]

        border = Signal()
        self.comb += border.eq((x == 0) | (x == 159) | (y == 0) | (y == 143))

        self.comb += [
            self.gb_clkena.eq(timing.gb_clkena),
            self.gb_mode.eq(timing.gb_mode),
            self.gb_vsync.eq(timing.gb_vsync),
            self.gb_on.eq(1),
            If(border | (x == marker),
                self.gb_data.eq(white),
            ).Else(
                self.gb_data.eq(bar),
            ),
        ]

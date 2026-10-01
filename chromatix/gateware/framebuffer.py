#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
LCD framebuffer: a CPU-written 8-bit indexed framebuffer + 256 colors palette shown on the
Chromatic's 160x144 LCD.

As the LCD terminal, the pixels are produced as the Game Boy LCD interface (clkena/data/mode/vsync,
hClk), so the framebuffer replaces the Game Boy core at the input of the video pipeline: OSD, color
correction, LCD and UVC capture work unchanged.
"""

from migen import *
from migen.genlib.cdc import PulseSynchronizer

from litex.gen import *

from litex.soc.interconnect import wishbone
from litex.soc.interconnect.csr import *

# LCD Framebuffer ----------------------------------------------------------------------------------

class LCDFramebuffer(LiteXModule):
    """
    bus ("sys" domain, 32-bit words): pixels at word 0 (4 pixels per word, little endian: pixel x at
    byte x of the line, lines of width bytes), palette at word `PALETTE_OFFSET` (256 entries, RGB555:
    {B[4:0], G[4:0], R[4:0]}). Byte writes are supported (sel).

    Pixels ("hclk" domain): Game Boy LCD interface, same timings as the LCD terminal (4 hClk per
    pixel clock, line_dots pixel clocks per line, 160 visible from h_start, frame_lines lines, 144
    visible, vsync during line 0).

    CSR: frame (frames counter, incremented at each vsync: frame pacing/tearing avoidance).
    """
    PALETTE_OFFSET = 0x2000 # Words (byte offset 0x8000).

    def __init__(self, width=160, height=144, line_dots=456, h_start=80, frame_lines=154):
        assert width % 4 == 0
        self.bus       = bus = wishbone.Interface(data_width=32, address_width=32, addressing="word")
        # Game Boy LCD interface (hClk).
        self.gb_clkena = Signal()
        self.gb_data   = Signal(15)
        self.gb_mode   = Signal(2)
        self.gb_on     = Signal(reset=1)
        self.gb_vsync  = Signal()
        # CSRs.
        self._frame    = CSRStatus(32, description="Frames counter (incremented at each vsync).")

        # # #

        words = width*height//4
        assert words <= self.PALETTE_OFFSET

        # Memories (pixels: 4 per word, byte writes; palette).
        pixels   = Memory(32, words)
        pix_wr   = pixels.get_port(write_capable=True, we_granularity=8, clock_domain="sys")
        pix_rd   = pixels.get_port(clock_domain="hclk")
        palette  = Memory(15, 256)
        pal_wr   = palette.get_port(write_capable=True, clock_domain="sys")
        pal_rd   = palette.get_port(clock_domain="hclk")
        self.specials += pixels, pix_wr, pix_rd, palette, pal_wr, pal_rd

        # Wishbone (writes only, reads return 0) ---------------------------------------------------
        is_palette = Signal()
        self.comb += [
            is_palette.eq(bus.adr[13]),
            pix_wr.adr.eq(bus.adr[:13]),
            pix_wr.dat_w.eq(bus.dat_w),
            pal_wr.adr.eq(bus.adr[:8]),
            pal_wr.dat_w.eq(bus.dat_w[:15]),
            If(bus.cyc & bus.stb & bus.we & ~bus.ack,
                If(is_palette,
                    pal_wr.we.eq(1),
                ).Else(
                    pix_wr.we.eq(bus.sel),
                )
            ),
        ]
        self.sync += [
            bus.ack.eq(0),
            If(bus.cyc & bus.stb & ~bus.ack,
                bus.ack.eq(1),
            )
        ]

        # Reader (hclk): Game Boy LCD timing + pixel generation ------------------------------------
        phase     = Signal(2)                     # hClk cycles per pixel clock.
        dot       = Signal(max=line_dots)         # Pixel clock in line.
        line      = Signal(max=frame_lines)       # Line in frame.
        x         = Signal(max=max(width, 160))   # Visible pixel (mode 3).
        line_base = Signal(max=words + width//4)  # First word of the line.
        visible   = Signal()
        shown     = Signal()
        byte      = Signal(2)
        pixel     = Signal(15)
        self.comb += [
            visible.eq((line < 144) & (dot >= h_start) & (dot < (h_start + 160))),
            shown.eq((line < height) & (x < width)),
        ]
        self.sync.hclk += [
            phase.eq(phase + 1),
            If(phase == 3,
                If(dot == (line_dots - 1),
                    dot.eq(0),
                    If(line == (frame_lines - 1),
                        line.eq(0),
                        line_base.eq(0),
                    ).Else(
                        line.eq(line + 1),
                        line_base.eq(line_base + width//4),
                    )
                ).Else(
                    dot.eq(dot + 1),
                ),
                If(visible,
                    x.eq(x + 1),
                ).Else(
                    x.eq(0),
                )
            )
        ]
        # Pixel pipeline over the 4 hClk of a pixel clock: pixels word read (phase 0), palette read
        # (phase 1), pixel (phase 2), output with clkena (phase 3).
        self.comb += [
            pix_rd.adr.eq(line_base + x[2:]),
            pal_rd.adr.eq(Array(pix_rd.dat_r[8*i:8*(i + 1)] for i in range(4))[byte]),
        ]
        self.sync.hclk += [
            If(phase == 0, byte.eq(x[:2])),
            If(phase == 2, pixel.eq(Mux(shown, pal_rd.dat_r, 0))),
        ]
        self.comb += [
            self.gb_clkena.eq(visible & (phase == 3)),
            self.gb_data.eq(pixel),
            # Modes: 2 (OAM) / 3 (pixels) then 0 (hblank), 1 during vblank; mode[1] is used as hsync.
            If(line >= 144,
                self.gb_mode.eq(1),
            ).Elif(dot < (h_start + 160),
                self.gb_mode.eq(Mux(dot < h_start, 2, 3)),
            ).Else(
                self.gb_mode.eq(0),
            ),
            self.gb_vsync.eq(line == 0),
        ]

        # Frames counter (sys) ---------------------------------------------------------------------
        vsync_d = Signal()
        self.vsync_ps = vsync_ps = PulseSynchronizer("hclk", "sys")
        self.sync.hclk += vsync_d.eq(self.gb_vsync)
        self.comb += vsync_ps.i.eq(self.gb_vsync & ~vsync_d)
        self.sync += If(vsync_ps.o, self._frame.status.eq(self._frame.status + 1))

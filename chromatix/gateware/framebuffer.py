#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
LCD framebuffers shown on the Chromatic's 160x144 LCD:
- LCDFramebuffer: CPU-written 8-bit indexed framebuffer + 256 colors palette (BSRAM).
- LCDPSRAMFramebuffer: CPU-written RGB555 frame buffers in the PSRAM, triple buffered (tear-free).

As the LCD terminal, the pixels are produced as the Game Boy LCD interface (clkena/data/mode/vsync,
hClk), so the framebuffers replace the Game Boy core at the input of the video pipeline: OSD, color
correction, LCD and UVC capture work unchanged.
"""

from migen import *
from migen.genlib.cdc import MultiReg, PulseSynchronizer

from litex.gen import *

from litex.soc.interconnect import wishbone
from litex.soc.interconnect.csr import *

# Game Boy LCD Timing ------------------------------------------------------------------------------

class GBLCDTiming(LiteXModule):
    """
    Game Boy LCD timing ("hclk" domain): 4 hClk per pixel clock, line_dots pixel clocks per line (160
    visible from h_start), frame_lines lines per frame (144 visible), vsync during line 0 (as the
    Game Boy core: the video pipeline, LCD panel and line readers re-align on its rising edge).
    """
    def __init__(self, line_dots=456, h_start=80, frame_lines=154):
        self.phase    = phase   = Signal(2)               # hClk cycles per pixel clock.
        self.dot      = dot     = Signal(max=line_dots)   # Pixel clock in line.
        self.line     = line    = Signal(max=frame_lines) # Line in frame.
        self.x        = x       = Signal(8)               # Visible pixel (mode 3).
        self.visible  = visible = Signal()
        self.gb_clkena = Signal()
        self.gb_mode   = Signal(2)
        self.gb_vsync  = Signal()

        # # #

        self.comb += visible.eq((line < 144) & (dot >= h_start) & (dot < (h_start + 160)))
        self.sync.hclk += [
            phase.eq(phase + 1),
            If(phase == 3,
                If(dot == (line_dots - 1),
                    dot.eq(0),
                    If(line == (frame_lines - 1),
                        line.eq(0),
                    ).Else(
                        line.eq(line + 1),
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
        self.comb += [
            self.gb_clkena.eq(visible & (phase == 3)),
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
        self.timing = timing = GBLCDTiming(line_dots, h_start, frame_lines)
        shown = Signal()
        byte  = Signal(2)
        pixel = Signal(15)
        self.comb += shown.eq((timing.line < height) & (timing.x < width))
        # Pixel pipeline over the 4 hClk of a pixel clock: pixels word read (phase 0), palette read
        # (phase 1), pixel (phase 2), output with clkena (phase 3).
        self.comb += [
            pix_rd.adr.eq(timing.line*(width//4) + timing.x[2:]),
            pal_rd.adr.eq(Array(pix_rd.dat_r[8*i:8*(i + 1)] for i in range(4))[byte]),
        ]
        self.sync.hclk += [
            If(timing.phase == 0, byte.eq(timing.x[:2])),
            If(timing.phase == 2, pixel.eq(Mux(shown, pal_rd.dat_r, 0))),
        ]
        self.comb += [
            self.gb_clkena.eq(timing.gb_clkena),
            self.gb_data.eq(pixel),
            self.gb_mode.eq(timing.gb_mode),
            self.gb_vsync.eq(timing.gb_vsync),
        ]

        # Frames counter (sys) ---------------------------------------------------------------------
        vsync_d = Signal()
        self.vsync_ps = vsync_ps = PulseSynchronizer("hclk", "sys")
        self.sync.hclk += vsync_d.eq(self.gb_vsync)
        self.comb += vsync_ps.i.eq(self.gb_vsync & ~vsync_d)
        self.sync += If(vsync_ps.o, self._frame.status.eq(self._frame.status + 1))

# LCD PSRAM Framebuffer ----------------------------------------------------------------------------

class LCDPSRAMFramebuffer(LiteXModule):
    """
    LCD frame buffers in the PSRAM: nbuffers 160x144 RGB555 buffers ({B[4:0], G[4:0], R[4:0]}, lines
    of 320 bytes) at base + n*buffer_stride, triple buffered by default (tear-free, no wait): the
    CPU writes the back buffer while the front one is displayed, the front buffer changes at the
    start of a frame.

    - bus ("sys" domain, 32-bit words): nslots line buffers of 40 words (8-bit indexed pixels, 4 per
      word, little endian) at words 40*slot, and the palette (256 x RGB555) at word PALETTE_OFFSET.
      The `line` CSR copies a line buffer to a frame buffer line in the PSRAM (xClk domain: palette
      lookup, then burst write on `port`), line buffers copied in order: a line buffer can be written
      again once copied (status busy).
    - Pixels ("hclk" domain): Game Boy LCD interface (GBLCDTiming), pixel data from a PSRAM line
      reader of the displayed buffer (fb_base, fb_data: the video pipeline's frame buffer reader).
    """
    LINE_PIXELS    = 160
    LINE_WORDS     = 40    # 32-bit words of a line buffer.
    PALETTE_OFFSET = 0x400 # Words (byte offset 0x1000).

    def __init__(self, port, base=0x10000, nbuffers=3, buffer_stride=0xc000, nslots=4,
        line_dots=456, h_start=80, frame_lines=154):
        assert nbuffers <= 4
        assert nslots in [2, 4]
        assert nslots*self.LINE_WORDS <= self.PALETTE_OFFSET
        self.bus       = bus = wishbone.Interface(data_width=32, address_width=32, addressing="word")
        # Game Boy LCD interface (hClk).
        self.gb_clkena = Signal()
        self.gb_data   = Signal(15)
        self.gb_mode   = Signal(2)
        self.gb_on     = Signal(reset=1)
        self.gb_vsync  = Signal()
        # PSRAM line reader of the displayed buffer.
        self.fb_base   = Signal(23, reset=base) # Displayed buffer address (changes at vsync).
        self.fb_data   = Signal(16)             # Pixel (hClk, along with gb_clkena).
        # CSRs.
        self.control = CSRStorage(fields=[
            CSRField("front", size=2, description="Buffer to display (from the next frame)."),
        ])
        self.line = CSRStorage(fields=[
            CSRField("line",   size=8, description="Frame buffer line (write: copy of the line buffer)."),
            CSRField("buffer", size=2, description="Frame buffer."),
            CSRField("slot",   size=2, description="Line buffer."),
        ])
        self.status = CSRStatus(fields=[
            CSRField("front", size=2, description="Displayed buffer (current frame)."),
            CSRField("busy",  size=4, description="Line buffers being copied to the PSRAM (1 bit per line buffer)."),
        ])
        self._frame = CSRStatus(32, description="Frames counter (incremented at each vsync).")

        # # #

        # Memories: line buffers (8-bit pixels), palette, line staging (RGB555, burst write).
        lines    = Memory(32, nslots*self.LINE_WORDS)
        lines_wr = lines.get_port(write_capable=True, we_granularity=8, clock_domain="sys")
        lines_rd = lines.get_port(clock_domain="xclk")
        palette  = Memory(15, 256)
        pal_wr   = palette.get_port(write_capable=True, clock_domain="sys")
        pal_rd   = palette.get_port(clock_domain="xclk")
        stage    = Memory(16, self.LINE_PIXELS)
        stage_wr = stage.get_port(write_capable=True, clock_domain="xclk")
        stage_rd = stage.get_port(clock_domain="xclk")
        self.specials += lines, lines_wr, lines_rd, palette, pal_wr, pal_rd, stage, stage_wr, stage_rd

        # Wishbone (writes only, reads return 0) ---------------------------------------------------
        is_palette = Signal()
        self.comb += [
            is_palette.eq(bus.adr[log2_int(self.PALETTE_OFFSET)]), # Region offset bit (bus.adr is absolute).
            lines_wr.adr.eq(bus.adr),
            lines_wr.dat_w.eq(bus.dat_w),
            pal_wr.adr.eq(bus.adr[:8]),
            pal_wr.dat_w.eq(bus.dat_w[:15]),
            If(bus.cyc & bus.stb & bus.we & ~bus.ack,
                If(is_palette,
                    pal_wr.we.eq(1),
                ).Else(
                    lines_wr.we.eq(bus.sel),
                )
            ),
        ]
        self.sync += [
            bus.ack.eq(0),
            If(bus.cyc & bus.stb & ~bus.ack,
                bus.ack.eq(1),
            )
        ]

        # Line copies (sys -> xClk: request/acknowledge toggles per line buffer) -------------------
        req_toggle = Signal(nslots)
        ack_toggle = Signal(nslots)
        req_sync   = Signal(nslots)
        ack_sync   = Signal(nslots)
        addr       = Array(Signal(23, name=f"addr{i}") for i in range(nslots))
        self.specials += [
            MultiReg(req_toggle, req_sync, "xclk"),
            MultiReg(ack_toggle, ack_sync),
        ]
        self.comb += self.status.fields.busy.eq(req_toggle ^ ack_sync)
        self.sync += If(self.line.re,
            req_toggle.eq(req_toggle ^ (1 << self.line.fields.slot)),
            addr[self.line.fields.slot].eq(base +
                Array(n*buffer_stride for n in range(4))[self.line.fields.buffer] +
                self.line.fields.line*(2*self.LINE_PIXELS)),
        )

        # xClk: line buffers copied in order. Palette lookup of the line to the staging buffer (1
        # pixel per cycle, 2 cycles latency), then 320 bytes burst write from the staging buffer (the
        # PSRAM port data is the last popped word, popped on request/write_next: read ahead).
        slot  = Signal(max=nslots)
        pend  = Signal(nslots)
        index = Signal(max=self.LINE_PIXELS + 3) # Pixel read from the line buffer.
        byte  = Signal(2)
        valid = Signal(2)                        # Pipeline: line buffer read, palette read.
        dst   = Array(Signal(8, name=f"dst{i}") for i in range(2))
        pops  = Signal(max=self.LINE_PIXELS + 2)
        pop   = Signal()
        self.comb += [
            pend.eq(req_sync ^ ack_toggle),
            lines_rd.adr.eq(slot*self.LINE_WORDS + index[2:]),
            pal_rd.adr.eq(Array(lines_rd.dat_r[8*i:8*(i + 1)] for i in range(4))[byte]),
            stage_wr.adr.eq(dst[1]),
            stage_wr.dat_w.eq(pal_rd.dat_r),
            pop.eq(port.request | port.write_next),
            # Pixel read: the one popped this cycle, else the last popped one.
            stage_rd.adr.eq(Mux(pop, pops, pops - 1)),
            port.din.eq(stage_rd.dat_r),
            port.rnw.eq(0),
            port.burst_length.eq(2*self.LINE_PIXELS),
            port.addr.eq(Array(addr)[slot]),
        ]
        self.fsm = fsm = ClockDomainsRenamer("xclk")(FSM(reset_state="IDLE"))
        fsm.act("IDLE",
            NextValue(index, 0),
            NextValue(valid, 0),
            If(Array(pend[i] for i in range(nslots))[slot],
                NextState("LOOKUP"),
            )
        )
        fsm.act("LOOKUP",
            stage_wr.we.eq(valid[1]),
            NextValue(index, index + 1),
            NextValue(byte, index[:2]),
            NextValue(dst[0], index),
            NextValue(dst[1], dst[0]),
            NextValue(valid, Cat(index < self.LINE_PIXELS, valid[0])),
            If(valid[1] & (dst[1] == (self.LINE_PIXELS - 1)),
                NextValue(pops, 0),
                NextState("REQUEST"),
            )
        )
        fsm.act("REQUEST",
            port.request.eq(1),
            NextState("WRITE"),
        )
        fsm.act("WRITE",
            If(port.done,
                NextValue(ack_toggle, ack_toggle ^ (1 << slot)),
                NextValue(slot, slot + 1),
                NextState("IDLE"),
            )
        )
        self.sync.xclk += If(pop, pops.eq(pops + 1))

        # Display (hclk): timing, front buffer changed at the start of a frame ---------------------
        self.timing = timing = GBLCDTiming(line_dots, h_start, frame_lines)
        front   = Signal(2)
        current = Signal(2)
        vsync_d = Signal()
        self.specials += [
            MultiReg(self.control.fields.front, front, "hclk"),
            MultiReg(current, self.status.fields.front),
        ]
        self.sync.hclk += [
            vsync_d.eq(timing.gb_vsync),
            If(timing.gb_vsync & ~vsync_d,
                current.eq(front),
            ),
        ]
        self.comb += [
            self.fb_base.eq(base + Array(n*buffer_stride for n in range(4))[current]),
            self.gb_clkena.eq(timing.gb_clkena),
            self.gb_data.eq(self.fb_data),
            self.gb_mode.eq(timing.gb_mode),
            self.gb_vsync.eq(timing.gb_vsync),
        ]

        # Frames counter (sys) ---------------------------------------------------------------------
        self.vsync_ps = vsync_ps = PulseSynchronizer("hclk", "sys")
        self.comb += vsync_ps.i.eq(timing.gb_vsync & ~vsync_d)
        self.sync += If(vsync_ps.o, self._frame.status.eq(self._frame.status + 1))


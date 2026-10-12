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
licensed glue (video sink / downscaler, and later the ioctl ROM loader / pad mapping).

- CoreTestPattern: a self-contained 160x144 RGB555 test source (SMPTE-style color bars + a moving
  marker) with Game Boy LCD timing, driving the GB LCD interface directly (the Game Gear-native
  path, no scaling).
- CoreVideoTestPattern: the same pattern on the generic "core video" interface (ce/rgb/hblank/vblank)
  at an arbitrary native resolution, to exercise the downscaler.
- CoreVideoDownscaler: nearest-neighbor downscaler from the generic core video interface (any native
  WxH) to the 160x144 Game Boy LCD interface, through a BRAM frame buffer. This is how a hosted core
  wider than 160x144 (NES 256x240, SMS 256x192, ...) reaches the LCD and UVC.
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

def _color_bar(x, width):
    """Combinational RGB555 of the color bar at pixel x over `width` (8 bars), as a Signal."""
    index = Signal(max=8)
    bar   = Signal(15)
    step  = width // 8
    cases = {}
    comb  = []
    # Comparator ladder (x // step), clamped to 7.
    expr = index.eq(7)
    for i in reversed(range(8)):
        expr = If(x < (i + 1) * step, index.eq(i)).Else(expr)
    comb += [expr, bar.eq(Array([Constant(c, 15) for c in COLOR_BARS])[index])]
    return bar, comb

# Core Test Pattern (direct 160x144) ---------------------------------------------------------------

class CoreTestPattern(LiteXModule):
    """
    160x144 test source presenting the Game Boy LCD interface ("hclk" domain), so it drives the video
    pipeline exactly like the Game Boy core or the framebuffers. Draws 8 vertical color bars (20 px
    each) with a 1 px vertical white marker that moves one pixel per frame (so a frozen core is
    obvious) and a white frame border.
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

        white     = rgb555(31, 31, 31)
        bar, comb = _color_bar(x, 160)
        self.comb += comb

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

# Core Video Interface -----------------------------------------------------------------------------

class CoreVideo:
    """The generic "core video" interface a hosted core presents (its native resolution clock domain):
    a pixel clock-enable, 5:5:5 color, and horizontal/vertical blanking. Active pixel = ce & ~hblank &
    ~vblank; new line = hblank rising edge; new frame = vblank rising edge. This mirrors a MiSTer core's
    CE_PIXEL / VGA_R/G/B / H-V blanking, reduced to RGB555."""
    def __init__(self):
        self.ce     = Signal()
        self.r      = Signal(5)
        self.g      = Signal(5)
        self.b      = Signal(5)
        self.hblank = Signal()
        self.vblank = Signal()

# Core Video Test Pattern (generic interface, arbitrary resolution) --------------------------------

class CoreVideoTestPattern(LiteXModule):
    """8 color bars + moving marker + border at `width`x`height`, on the generic core video interface
    (CoreVideo), one pixel per cycle with short blanking. Used to exercise the downscaler."""
    def __init__(self, width=256, height=224, cd="hclk"):
        self.video = video = CoreVideo()

        # # #

        csync   = getattr(self.sync, cd)
        total_w = width  + 32 # Short horizontal blanking.
        total_h = height + 16 # Short vertical blanking.
        xt = Signal(max=total_w)
        yt = Signal(max=total_h)
        csync += [
            If(xt == (total_w - 1),
                xt.eq(0),
                If(yt == (total_h - 1), yt.eq(0)).Else(yt.eq(yt + 1)),
            ).Else(
                xt.eq(xt + 1),
            ),
        ]

        active_x = xt < width
        active_y = yt < height

        # Moving marker (one column per frame), advanced at the end of the frame.
        marker = Signal(max=width)
        csync += If((xt == (total_w - 1)) & (yt == (total_h - 1)),
            If(marker == (width - 1), marker.eq(0)).Else(marker.eq(marker + 1)),
        )

        white     = rgb555(31, 31, 31)
        bar, comb = _color_bar(xt, width)
        self.comb += comb

        border = Signal()
        pixel  = Signal(15)
        self.comb += [
            border.eq((xt == 0) | (xt == (width - 1)) | (yt == 0) | (yt == (height - 1))),
            If(border | (xt == marker), pixel.eq(white)).Else(pixel.eq(bar)),
            video.ce.eq(1),
            video.hblank.eq(~active_x),
            video.vblank.eq(~active_y),
            video.r.eq(pixel[0:5]),
            video.g.eq(pixel[5:10]),
            video.b.eq(pixel[10:15]),
        ]

# Core Video Downscaler ----------------------------------------------------------------------------

class CoreVideoDownscaler(LiteXModule):
    """
    Nearest-neighbor downscaler: generic core video (CoreVideo, native in_w x in_h, `write_cd` domain)
    -> Game Boy LCD interface (out_w x out_h = 160x144, hClk), through a dual-port BRAM frame buffer.

    Write side (write_cd): each active input pixel runs a horizontal Bresenham accumulator to pick
    out_w columns across in_w, and each input line runs a vertical accumulator to map in_h lines to
    out_h rows; selected pixels are written to the frame buffer at (y_out*out_w + x_out).

    Read side (hClk): GBLCDTiming scans the frame buffer out as the Game Boy LCD interface, so OSD /
    color correction / LCD / UVC work unchanged. Single buffered (the input and output frames are
    asynchronous: minor tearing, acceptable for a bring-up/first version).
    """
    def __init__(self, in_w=256, in_h=224, out_w=160, out_h=144, write_cd="hclk"):
        self.video = video = CoreVideo() # Input (write_cd domain).
        # Output: Game Boy LCD interface (hClk).
        self.gb_clkena = Signal()
        self.gb_data   = Signal(15)
        self.gb_mode   = Signal(2)
        self.gb_on     = Signal()
        self.gb_vsync  = Signal()

        # # #

        fb    = Memory(16, out_w*out_h)
        fb_wr = fb.get_port(write_capable=True, clock_domain=write_cd)
        fb_rd = fb.get_port(clock_domain="hclk")
        self.specials += fb, fb_wr, fb_rd
        self.fb    = fb    # Exposed for simulation/inspection.
        self.fb_wr = fb_wr

        # Write side (write_cd): scale-on-write ----------------------------------------------------
        hblank_d = Signal()
        vblank_d = Signal()
        x_out    = Signal(max=out_w + 1)
        y_out    = Signal(max=out_h + 1)
        row_base = Signal(max=out_w*out_h + 1) # y_out*out_w, maintained incrementally.
        hacc     = Signal(max=in_w + out_w + 1)
        vacc     = Signal(max=in_h + out_h + 1)
        active   = Signal()
        wsync    = getattr(self.sync, write_cd)
        self.comb += active.eq(video.ce & ~video.hblank & ~video.vblank)
        # Frame buffer write port driven registered (adr/dat_w/we move together, 1 cycle after the
        # pick), so the write is always self-consistent.
        wsync += [
            hblank_d.eq(video.hblank),
            vblank_d.eq(video.vblank),
            fb_wr.we.eq(0),
            # New frame (vblank rising): reset vertical and horizontal mapping.
            If(video.vblank & ~vblank_d,
                y_out.eq(0),
                row_base.eq(0),
                vacc.eq(0),
                x_out.eq(0),
                hacc.eq(in_w), # So the first pixel of the first line is written.
            ),
            # Active pixel: horizontal Bresenham pick.
            If(active,
                If(hacc >= in_w,
                    hacc.eq(hacc - in_w + out_w),
                    If((x_out < out_w) & (y_out < out_h),
                        fb_wr.we.eq(1),
                        fb_wr.adr.eq(row_base + x_out),
                        fb_wr.dat_w.eq(Cat(video.r, video.g, video.b)),
                        x_out.eq(x_out + 1),
                    ),
                ).Else(
                    hacc.eq(hacc + out_w),
                ),
            ),
            # End of line (hblank rising): vertical accumulate, reset horizontal.
            If(video.hblank & ~hblank_d,
                x_out.eq(0),
                hacc.eq(in_w), # So the first pixel of the next line is written.
                vacc.eq(vacc + out_h),
                If((vacc + out_h) >= in_h,
                    vacc.eq(vacc + out_h - in_h),
                    If(y_out < out_h,
                        y_out.eq(y_out + 1),
                        row_base.eq(row_base + out_w),
                    ),
                ),
            ),
        ]

        # Read side (hClk): scan the frame buffer out as the Game Boy LCD interface ----------------
        self.timing = timing = GBLCDTiming()
        self.comb += [
            fb_rd.adr.eq(timing.line*out_w + timing.x),
            self.gb_clkena.eq(timing.gb_clkena),
            self.gb_data.eq(fb_rd.dat_r[:15]),
            self.gb_mode.eq(timing.gb_mode),
            self.gb_vsync.eq(timing.gb_vsync),
            self.gb_on.eq(1),
        ]

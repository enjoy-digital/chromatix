#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
Chromatic video pipeline (port of vid_system_top.sv, ST7785_panel_master.v and overlay*.vhd).

Game Boy pixels (hClk) -> frame blend with previous frame (PSRAM) -> OSD/overlays -> color correction
-> line buffer -> ST7785 RGB666 panel timing (gClk, 3 dot clocks per pixel) + UVC copy.
"""

from migen import *

from litex.gen import *

from litex.build.io import DDROutput

# Overlays -----------------------------------------------------------------------------------------

BATTERY_FRONT = [
    "01111111111111110",
    "10000000000000001",
    "10000000000000001",
    "10000000000000001",
    "10000000000000001",
    "10001111111110001",
    "10001000000010001",
    "10001010000011001",
    "10001010000011001",
    "10001000000010001",
    "10001111111110001",
    "10000000000000001",
    "10000000000000001",
    "10000000000000001",
    "10000000000000001",
    "01111111111111110",
]

# 3x5 glyphs: 0-9, then (timer) ":" "." or (debug) A-F.
GLYPHS_DIGITS = [
    ["111", "101", "101", "101", "111"], # 0
    ["010", "110", "010", "010", "010"], # 1
    ["111", "001", "111", "100", "111"], # 2
    ["111", "001", "111", "001", "111"], # 3
    ["101", "101", "111", "001", "001"], # 4
    ["111", "100", "111", "001", "111"], # 5
    ["111", "100", "111", "101", "111"], # 6
    ["111", "001", "001", "001", "001"], # 7
    ["111", "101", "111", "101", "111"], # 8
    ["111", "101", "111", "001", "111"], # 9
]
GLYPHS_TIMER = GLYPHS_DIGITS + [
    ["000", "100", "000", "100", "000"], # :
    ["000", "000", "000", "000", "100"], # .
]
GLYPHS_HEX = GLYPHS_DIGITS + [
    ["111", "101", "111", "101", "101"], # A
    ["110", "101", "111", "101", "110"], # B
    ["111", "100", "100", "100", "111"], # C
    ["110", "101", "101", "101", "110"], # D
    ["111", "100", "111", "100", "111"], # E
    ["111", "100", "111", "100", "100"], # F
]

def bitmap_lookup(bitmap, x, y):
    """Combinational bitmap[y][x] lookup (bitmap as a list of strings)."""
    bits = [int(c) for row in bitmap for c in row]
    width = len(bitmap[0])
    return Array(Constant(b, 1) for b in bits)[y*width + x]

def glyph_lookup(glyphs, number, x, y):
    """Combinational glyph pixel lookup (3x5 glyphs, x < 3, y < 5)."""
    bits = []
    for glyph in glyphs:
        for row in glyph:
            bits += [int(c) for c in row]
    return Array(Constant(b, 1) for b in bits)[number*15 + y*3 + x]

# Color Correction ---------------------------------------------------------------------------------

class ColorCorrection(LiteXModule):
    """GBC LCD color correction (port of color_correction in vid_system_top.sv)."""
    def __init__(self):
        self.correct_lcd = Signal()
        self.correct_uvc = Signal()
        self.valid       = Signal()
        self.hsync       = Signal()
        self.vsync       = Signal()
        self.pixel       = Signal(18) # {B, G, R} (6-bit each).

        self.valid_out   = Signal()
        self.hsync_out   = Signal()
        self.vsync_out   = Signal()
        self.pixel_lcd   = Signal(18)
        self.pixel_uvc   = Signal(18)

        # # #

        r = self.pixel[0:6]
        g = self.pixel[6:12]
        b = self.pixel[12:18]

        # UVC correction.
        r10 = Signal(10)
        g8  = Signal(8)
        b10 = Signal(10)
        self.comb += [
            r10.eq(r*13 + g*2 + b),
            g8.eq(g*3 + b),
            b10.eq(r*3 + g*2 + b*11),
        ]

        # LCD correction.
        rlcd1 = Signal(16)
        rlcd2 = Signal(16)
        rlcd3 = Signal(16)
        glcd  = Signal(16)
        blcd  = Signal(16)
        self.comb += [
            rlcd1.eq(r[1:]*216 + g[1:]*30),
            rlcd2.eq(b[1:]*25),
            rlcd3.eq(Mux(rlcd1 < rlcd2, 0, rlcd1 - rlcd2)),
            glcd.eq(r[1:]*39 + g[1:]*137 + b[1:]*24),
            blcd.eq(r[1:]*21 + g[1:]*24  + b[1:]*125),
        ]
        def clamp(v):
            return Mux(v[13], 0x3f, v[7:13])

        self.sync += [
            self.valid_out.eq(self.valid),
            self.hsync_out.eq(self.hsync),
            self.vsync_out.eq(self.vsync),
            self.pixel_lcd.eq(Mux(self.correct_lcd, Cat(clamp(rlcd3), clamp(glcd), clamp(blcd)), Cat(r, g, b))),
            self.pixel_uvc.eq(Mux(self.correct_uvc, Cat(r10[4:10], g8[2:8], b10[4:10]), Cat(r, g, b))),
        ]

# ST7785 Panel Master ------------------------------------------------------------------------------

class ST7785PanelMaster(LiteXModule):
    """
    ST7785 RGB666 panel timing generator with Game Boy line buffer (port of ST7785_panel_master.v).

    Lines are written in hClk (160 pixels) and scanned out in gClk, each pixel over 3 dot clocks
    (B, G, R phases on the 6-bit bus). A UVC copy (18-bit) is output alongside.
    """
    H_LW, H_VALID, H_FP, H_BP = 30, 720, 129, 32
    V_LW, V_VALID, V_FP, V_BP = 2, 144, 2, 10
    OFFSET      = 41
    FINE_OFFSET = 418
    DEPTH       = 1024

    def __init__(self):
        # hClk.
        self.pixel     = Signal(18)
        self.pixel_uvc = Signal(18)
        self.valid     = Signal()
        self.hsync     = Signal()
        self.vsync     = Signal()
        self.lcd_on    = Signal()
        # gClk.
        self.nrst      = Signal()
        self.lcd_en    = Signal()
        self.de        = Signal()
        self.lcd_hsync = Signal()
        self.lcd_vsync = Signal()
        self.genlock   = Signal()
        self.db        = Signal(6)
        self.uvc_en    = Signal()
        self.uvc_db    = Signal(18)

        # # #

        pixel_for_hs = self.H_LW + self.H_VALID + self.H_FP + self.H_BP
        pixel_for_vs = self.V_VALID + self.V_FP + self.V_BP

        # Line Buffer.
        # Note: not an attribute, so the memory isn't exposed on the CSR bus by AutoCSR.
        mem     = Memory(36, self.DEPTH)
        wr_port = mem.get_port(write_capable=True, clock_domain="hclk")
        rd_port = mem.get_port(clock_domain="gclk")
        self.specials += mem, wr_port, rd_port

        # hClk: write side.
        hg_vs_r1 = Signal()
        hg_vs_r2 = Signal()
        hs_sr    = Signal(16)
        wa       = Signal(10)
        wr_count = Signal(8)
        gb_vsync = Signal()
        gb_hsync = Signal()
        self.sync.hclk += [
            hg_vs_r1.eq(self.vsync),
            hg_vs_r2.eq(hg_vs_r1),
            hs_sr.eq(Cat(self.hsync, hs_sr[:15])),
        ]
        self.comb += [
            gb_vsync.eq(hg_vs_r1 & ~hg_vs_r2),
            gb_hsync.eq(hs_sr[15] & ~hs_sr[14]),
        ]
        self.sync.hclk += [
            If(gb_vsync,
                wr_count.eq(0),
                wa.eq(0),
            ).Elif(gb_hsync,
                wr_count.eq(0),
            ).Elif(self.valid,
                If(wr_count <= 159,
                    wr_count.eq(wr_count + 1),
                    If(wa < (self.DEPTH - 1),
                        wa.eq(wa + 1),
                    ).Else(
                        wa.eq(0),
                    )
                )
            )
        ]
        self.comb += [
            wr_port.adr.eq(wa),
            wr_port.dat_w.eq(Cat(self.pixel, self.pixel_uvc)),
            wr_port.we.eq(self.valid & (wr_count <= 159)),
        ]

        # gClk: timing / read side.
        h_count     = Signal(12)
        v_count     = Signal(12)
        de_i        = Signal()
        ra          = Signal(10)
        rd_count    = Signal(8)
        phase       = Signal(2)
        hoffset     = Signal(8)
        p_vs        = Signal()
        p_vs_r1     = Signal()
        g_vs_r1     = Signal()
        g_vs_r2     = Signal()
        fine_delay  = Signal(11)
        delayed     = Signal()
        on_aligned  = Signal()
        on_aligned1 = Signal()
        on_aligned2 = Signal()
        frame_done  = Signal()
        de_r1       = Signal()
        de_r2       = Signal()
        vsync_r1    = Signal()

        self.comb += [
            de_i.eq(
                (h_count > (self.H_BP + self.H_LW)) &
                (h_count <= (self.H_VALID + self.H_BP + self.H_LW)) &
                (v_count <  (self.V_VALID + self.V_LW + self.V_FP)) &
                (v_count >= (self.V_LW + self.V_FP))
            ),
            frame_done.eq((v_count >= (self.V_VALID + self.V_LW + self.V_FP)) & ~self.lcd_hsync),
            rd_port.adr.eq(ra),
        ]
        pixel_lcd = rd_port.dat_r[0:18]
        pixel_uvc = rd_port.dat_r[18:36]

        self.sync.gclk += [
            p_vs.eq(self.vsync),
            p_vs_r1.eq(p_vs),
            g_vs_r1.eq(self.vsync),
            g_vs_r2.eq(g_vs_r1),
        ]

        # Line buffer read.
        self.sync.gclk += [
            If(p_vs & ~p_vs_r1,
                ra.eq(0),
                rd_count.eq(0),
                phase.eq(2),
            ).Elif(~self.lcd_hsync,
                phase.eq(2),
                hoffset.eq(0),
                rd_count.eq(0),
            ).Else(
                If(phase < 2,
                    phase.eq(phase + 1),
                ).Else(
                    phase.eq(0),
                ),
                If(de_i & self.lcd_vsync & (phase == 2),
                    If(hoffset < self.OFFSET,
                        hoffset.eq(hoffset + 1),
                    )
                ),
                If(de_i & (v_count >= (self.V_LW + self.V_FP)) & (phase == 1),
                    If((hoffset >= self.OFFSET) & (rd_count <= 159),
                        If(ra < (self.DEPTH - 1),
                            ra.eq(ra + 1),
                        ).Else(
                            ra.eq(0),
                        ),
                        rd_count.eq(rd_count + 1),
                    )
                ),
            )
        ]

        # LCD on/off alignment (scanout is several rows behind the emulator).
        self.sync.gclk += [
            If((g_vs_r1 & ~g_vs_r2) | (on_aligned1 & ~on_aligned2),
                fine_delay.eq(0),
            ).Elif(fine_delay != self.FINE_OFFSET,
                fine_delay.eq(fine_delay + 1),
            ),
            delayed.eq(fine_delay == (self.FINE_OFFSET - 1)),
            If((v_count == 0) | frame_done,
                on_aligned.eq(self.lcd_on),
            ),
            on_aligned1.eq(on_aligned),
            on_aligned2.eq(on_aligned1),
        ]

        # Timing counters.
        self.sync.gclk += [
            If(~self.nrst | delayed | ~on_aligned,
                v_count.eq(0),
                h_count.eq(0),
            ).Elif(h_count == pixel_for_hs,
                v_count.eq(v_count + 1),
                h_count.eq(0),
            ).Elif(v_count >= (pixel_for_vs + self.V_LW),
                v_count.eq(0),
                h_count.eq(0),
            ).Else(
                h_count.eq(h_count + 1),
            )
        ]

        # Outputs.
        self.sync.gclk += [
            de_r1.eq(de_i),
            de_r2.eq(de_r1),
            self.de.eq(de_r2),
            self.lcd_hsync.eq(h_count >= self.H_LW),
            self.lcd_vsync.eq(~(v_count < self.V_LW)),
            vsync_r1.eq(self.lcd_vsync),
            If(self.lcd_vsync & ~vsync_r1,
                self.genlock.eq(~self.genlock),
            ),
            If((hoffset >= self.OFFSET) & on_aligned,
                If(self.lcd_en,
                    Case(phase, {
                        2: self.db.eq(pixel_lcd[12:18]), # Blue.
                        1: self.db.eq(pixel_lcd[6:12]),  # Green.
                        0: self.db.eq(pixel_lcd[0:6]),   # Red.
                        "default": [],
                    }),
                    self.uvc_db.eq(pixel_uvc),
                    self.uvc_en.eq(de_r2),
                ).Else(
                    self.db.eq(0x3f),
                    self.uvc_db.eq(0x3ffff),
                    self.uvc_en.eq(de_r2),
                )
            ).Else(
                self.db.eq(0),
                self.uvc_db.eq(0),
                self.uvc_en.eq(0),
            )
        ]

# Video Pipeline -----------------------------------------------------------------------------------

class VideoPipeline(LiteXModule):
    """
    Chromatic video pipeline (port of vid_system_top.sv).

    Clock domains: hclk (Game Boy pixels), gclk (panel timing, timers).
    """
    def __init__(self, lcd_pads):
        # Game Boy LCD (hClk).
        self.gb_clkena     = Signal()
        self.gb_data       = Signal(15)
        self.gb_mode       = Signal(2)
        self.gb_on         = Signal()
        self.gb_vsync      = Signal()

        # Frame buffer (hClk).
        self.fb_new_line   = Signal()
        self.fb_address    = Signal(23)
        self.fb_write      = Signal()
        self.fb_data       = Signal(16)
        self.fb_prev       = Signal(16) # Previous frame pixel (frame blending).
        self.osd_data      = Signal(16) # OSD pixel (RGB565, 0xF81F: transparent).

        # Controls.
        self.menu_disabled = Signal()
        self.lcd_init_done = Signal()
        self.lcd_en        = Signal()
        self.frame_blend   = Signal()
        self.correct_lcd   = Signal()
        self.correct_uvc   = Signal()
        self.voltage_low   = Signal()
        self.low_batt_mode = Signal(2)
        self.show_timer    = Signal()
        self.run_timer     = Signal()
        self.reset_timer   = Signal()
        self.second        = Signal() # gClk.
        self.percent       = Signal() # gClk.
        self.debug_system  = Signal(32)
        self.debug_on      = Signal()
        self.draw_osd      = Signal()

        # UVC (gClk).
        self.uvc_en        = Signal()
        self.uvc_db        = Signal(18)

        # # #

        # Dot clock (gClk forwarded).
        self.specials += DDROutput(i1=1, i2=0, o=lcd_pads.dotclk, clk=ClockSignal("gclk"))

        # Frame buffer addressing / screen position (hClk).
        hsync    = self.gb_mode[1]
        vsync    = self.gb_vsync
        hsync_r1 = Signal()
        vsync_r1 = Signal()
        screen_x = Signal(8)
        screen_y = Signal(8)
        self.sync.hclk += [
            hsync_r1.eq(hsync),
            vsync_r1.eq(vsync),
            If(vsync & ~vsync_r1,
                self.fb_address.eq(0x10000),
            ).Elif(self.fb_new_line & ~vsync,
                self.fb_address.eq(self.fb_address + 320),
            ),
            If(hsync & ~hsync_r1,
                screen_x.eq(0),
                screen_y.eq(screen_y + 1),
            ).Elif(self.gb_clkena,
                screen_x.eq(screen_x + 1),
            ),
            If(vsync & ~vsync_r1,
                self.draw_osd.eq(~self.menu_disabled),
                screen_y.eq(0),
            ),
        ]
        self.comb += [
            self.fb_write.eq(self.gb_clkena),
            self.fb_new_line.eq(~hsync & hsync_r1),
            self.fb_data.eq(self.gb_data),
        ]

        # Frame blend (with previous frame).
        game = []
        for i in range(3):
            prev = self.fb_prev[5*i:5*(i+1)]
            cur  = self.gb_data[5*i:5*(i+1)]
            s = Signal(7)
            self.comb += s.eq(Cat(0, prev) + Cat(0, cur))
            c = Signal(6)
            self.comb += c.eq(Mux(self.frame_blend, s[1:7], Cat(0, cur)))
            game.append(c)
        pixel = Cat(*game) # {B, G, R}.

        # Timer / low battery blink (gClk).
        t_pl, t_ph, t_sl, t_sh, t_ml, t_mh, t_hl = [Signal(4) for _ in range(7)]
        lbb_state      = Signal()
        show_low_batt  = Signal()
        self.sync.gclk += [
            If(self.run_timer & self.percent,
                If(t_pl == 9,
                    t_ph.eq(t_ph + 1),
                    t_pl.eq(0),
                ).Else(
                    t_pl.eq(t_pl + 1),
                )
            ),
            If(self.run_timer & self.second,
                t_pl.eq(0),
                t_ph.eq(0),
                If(t_sl == 9,
                    t_sl.eq(0),
                    If(t_sh == 5,
                        t_sh.eq(0),
                        If(t_ml == 9,
                            t_ml.eq(0),
                            If(t_mh == 5,
                                t_mh.eq(0),
                                If(t_hl == 9,
                                    t_pl.eq(9), t_ph.eq(9), t_sl.eq(9), t_sh.eq(5),
                                    t_ml.eq(9), t_mh.eq(5), t_hl.eq(9),
                                ).Else(
                                    t_hl.eq(t_hl + 1),
                                )
                            ).Else(
                                t_mh.eq(t_mh + 1),
                            )
                        ).Else(
                            t_ml.eq(t_ml + 1),
                        )
                    ).Else(
                        t_sh.eq(t_sh + 1),
                    )
                ).Else(
                    t_sl.eq(t_sl + 1),
                )
            ),
            If(self.reset_timer,
                t_pl.eq(0), t_ph.eq(0), t_sl.eq(0), t_sh.eq(0),
                t_ml.eq(0), t_mh.eq(0), t_hl.eq(0),
            ),
            If(self.low_batt_mode == 0b01, # Blink.
                If(self.second,
                    lbb_state.eq(~lbb_state),
                ),
                show_low_batt.eq(lbb_state),
            ).Elif(self.low_batt_mode == 0b10, # Hide.
                lbb_state.eq(0),
                show_low_batt.eq(0),
            ).Else( # Show (0b00) / Reserved (0b11).
                lbb_state.eq(0),
                show_low_batt.eq(1),
            ),
        ]

        # Overlays (combinational).
        x, y = screen_x, screen_y
        battery_front = Signal()
        battery_back  = Signal()
        timer_front   = Signal()
        timer_back    = Signal()
        timer_number  = Signal()
        debug_digit   = Signal()
        self.comb += [
            If((x >= 140) & (x <= 156) & (y >= 2) & (y <= 17),
                battery_front.eq(bitmap_lookup(BATTERY_FRONT, (x - 140)[:5], (y - 2)[:4])),
            ),
            If((x >= 141) & (x <= 157) & (y >= 3) & (y <= 18),
                battery_back.eq(~(((x == 157) & ((y == 3) | (y == 18))) | ((x == 141) & (y == 18)))),
            ),
            If((x >= 1) & (x <= 39) & (y >= 2) & (y <= 17),
                timer_front.eq(((x == 1) | (x == 39) | (y == 2) | (y == 17)) &
                              ~(((x == 1) | (x == 39)) & ((y == 2) | (y == 17)))),
            ),
            If((x >= 2) & (x <= 40) & (y >= 3) & (y <= 18),
                timer_back.eq(~(((x == 40) & ((y == 3) | (y == 18))) | ((x == 2) & (y == 18)))),
            ),
        ]

        # Timer digits: [HL] ':' [MH ML] ':' [SH SL] '.' [PH PL].
        num_x   = Signal(2)
        num_y   = Signal(3)
        number  = Signal(4)
        digits  = [
            (4,  6,  t_hl), (8,  8,  10), (10, 13, t_mh), (14, 17, t_ml), (18, 18, 10),
            (20, 23, t_sh), (24, 27, t_sl), (28, 28, 11), (30, 33, t_ph), (34, 37, t_pl),
        ]
        num_cases = None
        for start, end, value in digits:
            stmt = [num_x.eq(x - start), number.eq(value)]
            cond = (x >= start) & (x <= end)
            num_cases = If(cond, *stmt) if num_cases is None else num_cases.Elif(cond, *stmt)
        self.comb += num_cases.Else(num_x.eq(3), number.eq(15))
        self.comb += [
            num_y.eq(Mux((y >= 8) & (y <= 12), y - 8, 7)),
            If((num_x < 3) & (num_y < 5) & (number < 12),
                timer_number.eq(glyph_lookup(GLYPHS_TIMER, number, num_x, num_y)),
            ),
        ]

        # Debug digits (8 hex digits of debug_system, bottom left).
        dbg_x      = Signal(2)
        dbg_y      = Signal(3)
        dbg_number = Signal(4)
        dbg_cases  = None
        for i in range(8):
            start = 4*i
            stmt  = [dbg_x.eq(x - start), dbg_number.eq(self.debug_system[4*(7-i):4*(8-i)])]
            cond  = (x >= start) & (x <= start + 3)
            dbg_cases = If(cond, *stmt) if dbg_cases is None else dbg_cases.Elif(cond, *stmt)
        dbg_cases = dbg_cases.Else(dbg_x.eq(3), dbg_number.eq(15))
        self.comb += dbg_cases
        self.comb += [
            dbg_y.eq(Mux(y >= 136, y - 136, 7)),
            If((dbg_x < 3) & (dbg_y < 5),
                debug_digit.eq(glyph_lookup(GLYPHS_HEX, dbg_number, dbg_x, dbg_y)),
            ),
        ]

        # OSD / Overlay compositing (hClk).
        osd_transparent = Signal()
        overlay_color   = Signal(18)
        overlay_active  = Signal()
        overlay_crush   = Signal()
        osd             = self.osd_data
        self.comb += osd_transparent.eq(osd == 0xf81f)
        self.sync.hclk += [
            # RGB565 -> {B, G, R} RGB666 (LSB replicated).
            overlay_color.eq(Cat(osd[11], osd[11:16], osd[6], osd[6:11], osd[0], osd[0:5])),
            overlay_active.eq(self.draw_osd & ~osd_transparent),
            overlay_crush.eq(self.draw_osd & osd_transparent),
            If(~self.draw_osd,
                If(self.debug_on & debug_digit,
                    overlay_color.eq(Cat(Constant(0, 6), Constant(0, 6), Constant(0, 6))), # Black.
                    overlay_active.eq(1),
                ).Elif(self.debug_on & (x <= 31) & (y >= 136),
                    overlay_color.eq(0x3ffff), # White.
                    overlay_active.eq(1),
                ).Elif(self.voltage_low & show_low_batt & battery_front,
                    overlay_color.eq(Cat(Constant(0x3f, 6), Constant(0, 6), Constant(0, 6))), # Red.
                    overlay_active.eq(1),
                ).Elif(self.show_timer & (timer_front | timer_number),
                    overlay_color.eq(0x3ffff), # White.
                    overlay_active.eq(1),
                ).Elif((self.voltage_low & show_low_batt & battery_back) | (self.show_timer & timer_back),
                    overlay_crush.eq(1),
                )
            ),
        ]

        # Color correction (hClk).
        self.color_correction = cc = ClockDomainsRenamer("hclk")(ColorCorrection())
        self.comb += [
            cc.correct_lcd.eq(self.correct_lcd),
            cc.correct_uvc.eq(self.correct_uvc),
            cc.valid.eq(self.gb_clkena),
            cc.hsync.eq(hsync),
            cc.vsync.eq(vsync),
            cc.pixel.eq(pixel),
        ]
        def crush(p):
            return Cat(p[2:6], Constant(0, 2), p[8:12], Constant(0, 2), p[14:18], Constant(0, 2))
        pixel_lcd = Signal(18)
        pixel_uvc = Signal(18)
        self.comb += [
            pixel_lcd.eq(Mux(overlay_crush, crush(cc.pixel_lcd), Mux(overlay_active, overlay_color, cc.pixel_lcd))),
            pixel_uvc.eq(Mux(overlay_crush, crush(cc.pixel_uvc), Mux(overlay_active, overlay_color, cc.pixel_uvc))),
        ]

        # Panel.
        self.panel = panel = ST7785PanelMaster()
        self.comb += [
            panel.pixel.eq(pixel_lcd),
            panel.pixel_uvc.eq(pixel_uvc),
            panel.valid.eq(cc.valid_out),
            panel.hsync.eq(cc.hsync_out),
            panel.vsync.eq(cc.vsync_out),
            panel.lcd_on.eq(self.gb_on),
            panel.nrst.eq(self.lcd_init_done),
            panel.lcd_en.eq(self.lcd_en),
            lcd_pads.enable.eq(panel.de),
            lcd_pads.hsync.eq(panel.lcd_hsync),
            lcd_pads.vsync.eq(panel.lcd_vsync),
            lcd_pads.db.eq(panel.db),
            self.uvc_en.eq(panel.uvc_en),
            self.uvc_db.eq(panel.uvc_db),
        ]

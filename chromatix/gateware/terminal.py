#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
LCD text terminal: shows a console byte stream (ex: the LiteX BIOS UART output) on the Chromatic's
160x144 LCD, as a 40x24 characters terminal with a 4x6 font.

The pixels are produced as the Game Boy LCD interface (clkena/data/mode/vsync, hClk), so the terminal
replaces the Game Boy core at the input of the video pipeline: frame buffer, OSD, color correction,
LCD and UVC capture work unchanged.

Supported: printable ASCII, CR, LF (+ scrolling), BS, HT (8 columns tab stops), and CSI sequences
(skipped, except SGR colors: 0/22/39 normal, 1 bold, 31/91 red, 32/92 green). As LiteX's VideoTerminal,
lines longer than the screen are clipped (not wrapped).
"""

from migen import *
from migen.genlib.cdc import MultiReg

from litex.gen import *

from litex.soc.interconnect import stream

# Font ---------------------------------------------------------------------------------------------

# "Tom Thumb" 4x6 font (Robey Pointer, MIT license): ASCII 0x20-0x7e, 6 rows of 3 pixels (MSB left),
# 1 pixel column spacing.
FONT_4X6 = [
    [0b000, 0b000, 0b000, 0b000, 0b000, 0b000], # ' '
    [0b010, 0b010, 0b010, 0b000, 0b010, 0b000], # '!'
    [0b101, 0b101, 0b000, 0b000, 0b000, 0b000], # '"'
    [0b101, 0b111, 0b101, 0b111, 0b101, 0b000], # '#'
    [0b011, 0b110, 0b011, 0b110, 0b010, 0b000], # '$'
    [0b100, 0b001, 0b010, 0b100, 0b001, 0b000], # '%'
    [0b110, 0b110, 0b111, 0b101, 0b011, 0b000], # '&'
    [0b010, 0b010, 0b000, 0b000, 0b000, 0b000], # "'"
    [0b001, 0b010, 0b010, 0b010, 0b001, 0b000], # '('
    [0b100, 0b010, 0b010, 0b010, 0b100, 0b000], # ')'
    [0b101, 0b010, 0b101, 0b000, 0b000, 0b000], # '*'
    [0b000, 0b010, 0b111, 0b010, 0b000, 0b000], # '+'
    [0b000, 0b000, 0b000, 0b010, 0b100, 0b000], # ','
    [0b000, 0b000, 0b111, 0b000, 0b000, 0b000], # '-'
    [0b000, 0b000, 0b000, 0b000, 0b010, 0b000], # '.'
    [0b001, 0b001, 0b010, 0b100, 0b100, 0b000], # '/'
    [0b011, 0b101, 0b101, 0b101, 0b110, 0b000], # '0'
    [0b010, 0b110, 0b010, 0b010, 0b010, 0b000], # '1'
    [0b110, 0b001, 0b010, 0b100, 0b111, 0b000], # '2'
    [0b110, 0b001, 0b010, 0b001, 0b110, 0b000], # '3'
    [0b101, 0b101, 0b111, 0b001, 0b001, 0b000], # '4'
    [0b111, 0b100, 0b110, 0b001, 0b110, 0b000], # '5'
    [0b011, 0b100, 0b111, 0b101, 0b111, 0b000], # '6'
    [0b111, 0b001, 0b010, 0b100, 0b100, 0b000], # '7'
    [0b111, 0b101, 0b111, 0b101, 0b111, 0b000], # '8'
    [0b111, 0b101, 0b111, 0b001, 0b110, 0b000], # '9'
    [0b000, 0b010, 0b000, 0b010, 0b000, 0b000], # ':'
    [0b000, 0b010, 0b000, 0b010, 0b100, 0b000], # ';'
    [0b001, 0b010, 0b100, 0b010, 0b001, 0b000], # '<'
    [0b000, 0b111, 0b000, 0b111, 0b000, 0b000], # '='
    [0b100, 0b010, 0b001, 0b010, 0b100, 0b000], # '>'
    [0b111, 0b001, 0b010, 0b000, 0b010, 0b000], # '?'
    [0b010, 0b101, 0b111, 0b100, 0b011, 0b000], # '@'
    [0b010, 0b101, 0b111, 0b101, 0b101, 0b000], # 'A'
    [0b110, 0b101, 0b110, 0b101, 0b110, 0b000], # 'B'
    [0b011, 0b100, 0b100, 0b100, 0b011, 0b000], # 'C'
    [0b110, 0b101, 0b101, 0b101, 0b110, 0b000], # 'D'
    [0b111, 0b100, 0b111, 0b100, 0b111, 0b000], # 'E'
    [0b111, 0b100, 0b111, 0b100, 0b100, 0b000], # 'F'
    [0b011, 0b100, 0b111, 0b101, 0b011, 0b000], # 'G'
    [0b101, 0b101, 0b111, 0b101, 0b101, 0b000], # 'H'
    [0b111, 0b010, 0b010, 0b010, 0b111, 0b000], # 'I'
    [0b001, 0b001, 0b001, 0b101, 0b010, 0b000], # 'J'
    [0b101, 0b101, 0b110, 0b101, 0b101, 0b000], # 'K'
    [0b100, 0b100, 0b100, 0b100, 0b111, 0b000], # 'L'
    [0b101, 0b111, 0b111, 0b101, 0b101, 0b000], # 'M'
    [0b101, 0b111, 0b111, 0b111, 0b101, 0b000], # 'N'
    [0b010, 0b101, 0b101, 0b101, 0b010, 0b000], # 'O'
    [0b110, 0b101, 0b110, 0b100, 0b100, 0b000], # 'P'
    [0b010, 0b101, 0b101, 0b111, 0b011, 0b000], # 'Q'
    [0b110, 0b101, 0b111, 0b110, 0b101, 0b000], # 'R'
    [0b011, 0b100, 0b010, 0b001, 0b110, 0b000], # 'S'
    [0b111, 0b010, 0b010, 0b010, 0b010, 0b000], # 'T'
    [0b101, 0b101, 0b101, 0b101, 0b011, 0b000], # 'U'
    [0b101, 0b101, 0b101, 0b010, 0b010, 0b000], # 'V'
    [0b101, 0b101, 0b111, 0b111, 0b101, 0b000], # 'W'
    [0b101, 0b101, 0b010, 0b101, 0b101, 0b000], # 'X'
    [0b101, 0b101, 0b010, 0b010, 0b010, 0b000], # 'Y'
    [0b111, 0b001, 0b010, 0b100, 0b111, 0b000], # 'Z'
    [0b111, 0b100, 0b100, 0b100, 0b111, 0b000], # '['
    [0b000, 0b100, 0b010, 0b001, 0b000, 0b000], # '\\'
    [0b111, 0b001, 0b001, 0b001, 0b111, 0b000], # ']'
    [0b010, 0b101, 0b000, 0b000, 0b000, 0b000], # '^'
    [0b000, 0b000, 0b000, 0b000, 0b111, 0b000], # '_'
    [0b100, 0b010, 0b000, 0b000, 0b000, 0b000], # '`'
    [0b000, 0b110, 0b011, 0b101, 0b111, 0b000], # 'a'
    [0b100, 0b110, 0b101, 0b101, 0b110, 0b000], # 'b'
    [0b000, 0b011, 0b100, 0b100, 0b011, 0b000], # 'c'
    [0b001, 0b011, 0b101, 0b101, 0b011, 0b000], # 'd'
    [0b000, 0b011, 0b101, 0b110, 0b011, 0b000], # 'e'
    [0b001, 0b010, 0b111, 0b010, 0b010, 0b000], # 'f'
    [0b000, 0b011, 0b101, 0b111, 0b001, 0b010], # 'g'
    [0b100, 0b110, 0b101, 0b101, 0b101, 0b000], # 'h'
    [0b010, 0b000, 0b010, 0b010, 0b010, 0b000], # 'i'
    [0b001, 0b000, 0b001, 0b001, 0b101, 0b010], # 'j'
    [0b100, 0b101, 0b110, 0b110, 0b101, 0b000], # 'k'
    [0b110, 0b010, 0b010, 0b010, 0b111, 0b000], # 'l'
    [0b000, 0b111, 0b111, 0b111, 0b101, 0b000], # 'm'
    [0b000, 0b110, 0b101, 0b101, 0b101, 0b000], # 'n'
    [0b000, 0b010, 0b101, 0b101, 0b010, 0b000], # 'o'
    [0b000, 0b110, 0b101, 0b101, 0b110, 0b100], # 'p'
    [0b000, 0b011, 0b101, 0b101, 0b011, 0b001], # 'q'
    [0b000, 0b011, 0b100, 0b100, 0b100, 0b000], # 'r'
    [0b000, 0b011, 0b110, 0b011, 0b110, 0b000], # 's'
    [0b010, 0b111, 0b010, 0b010, 0b011, 0b000], # 't'
    [0b000, 0b101, 0b101, 0b101, 0b011, 0b000], # 'u'
    [0b000, 0b101, 0b101, 0b111, 0b010, 0b000], # 'v'
    [0b000, 0b101, 0b111, 0b111, 0b111, 0b000], # 'w'
    [0b000, 0b101, 0b010, 0b010, 0b101, 0b000], # 'x'
    [0b000, 0b101, 0b101, 0b011, 0b001, 0b010], # 'y'
    [0b000, 0b111, 0b011, 0b110, 0b111, 0b000], # 'z'
    [0b011, 0b010, 0b100, 0b010, 0b011, 0b000], # '{'
    [0b010, 0b010, 0b000, 0b010, 0b010, 0b000], # '|'
    [0b110, 0b010, 0b001, 0b010, 0b110, 0b000], # '}'
    [0b011, 0b110, 0b000, 0b000, 0b000, 0b000], # '~'
]

# Terminal -----------------------------------------------------------------------------------------

TERM_COLS   = 40
TERM_LINES  = 24
FONT_WIDTH  = 4
FONT_HEIGHT = 6
LINE_STRIDE = 64 # Characters buffer stride (power of 2).

# Colors (RGB555: {B, G, R}): normal, bold, green, red.
COLORS = [
    (25, 25, 25),
    (31, 31, 31),
    (10, 31, 10),
    (31,  8,  8),
]

def rgb555(r, g, b):
    return (b << 10) | (g << 5) | r

class LCDTerminal(LiteXModule):
    """
    sink ("sys" domain): console bytes. Pixels ("hclk" domain): Game Boy LCD interface, 4 hClk per
    pixel clock, line_dots pixel clocks per line (160 visible from h_start), frame_lines lines per
    frame (144 visible), vsync during line 0 (as the Game Boy core: the video pipeline, LCD panel and
    frame buffer reader re-align on its rising edge). The defaults are the Game Boy timings.
    """
    def __init__(self, line_dots=456, h_start=80, frame_lines=154):
        self.sink      = sink = stream.Endpoint([("data", 8)])
        # Game Boy LCD interface (hClk).
        self.gb_clkena = Signal()
        self.gb_data   = Signal(15)
        self.gb_mode   = Signal(2)
        self.gb_on     = Signal(reset=1)
        self.gb_vsync  = Signal()

        # # #

        # Characters buffer: {color[1:0], char[6:0]}, TERM_LINES lines of LINE_STRIDE characters.
        chars   = Memory(9, TERM_LINES*LINE_STRIDE, init=[ord(" ")]*(TERM_LINES*LINE_STRIDE))
        wrport  = chars.get_port(write_capable=True, clock_domain="sys")
        rdport  = chars.get_port(clock_domain="hclk")
        font    = Memory(3, 128*8, init=sum([(g + [0, 0]) for g in [[0]*6]*32 + FONT_4X6 + [[0]*6]], []))
        font_rd = font.get_port(clock_domain="hclk")
        self.specials += chars, wrport, rdport, font, font_rd

        # Writer (sys) -----------------------------------------------------------------------------
        col     = Signal(max=LINE_STRIDE) # Characters after TERM_COLS are stored but not shown.
        row     = Signal(max=TERM_LINES)  # Buffer line of the cursor.
        top     = Signal(max=TERM_LINES)  # Buffer line shown at the top of the screen.
        full    = Signal()                # All screen lines used (new lines scroll).
        color   = Signal(2)
        param   = Signal(8)               # CSI parameter.
        clr_col = Signal(max=TERM_COLS + 1)
        c       = sink.data

        def sgr():
            return Case(param, {
                0:  NextValue(color, 0), 22: NextValue(color, 0), 39: NextValue(color, 0),
                1:  If(color == 0, NextValue(color, 1)),
                31: NextValue(color, 3), 91: NextValue(color, 3),
                32: NextValue(color, 2), 92: NextValue(color, 2),
                "default": [],
            })

        self.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            sink.ready.eq(1),
            If(sink.valid,
                If((c >= 0x20) & (c < 0x7f),
                    wrport.adr.eq(Cat(col, row)),
                    wrport.dat_w.eq(Cat(c[:7], color)),
                    wrport.we.eq(1),
                    If(col != (LINE_STRIDE - 1), NextValue(col, col + 1)),
                ).Elif(c == ord("\t"),
                    If(col[3:] != (LINE_STRIDE//8 - 1), NextValue(col, Cat(Constant(0, 3), col[3:] + 1))),
                ).Elif(c == ord("\r"),
                    NextValue(col, 0),
                ).Elif(c == ord("\n"),
                    NextValue(col, 0),
                    NextState("NEWLINE"),
                ).Elif(c == 0x08,
                    If(col != 0, NextValue(col, col - 1)),
                ).Elif(c == 0x1b,
                    NextState("ESC"),
                )
            )
        )
        fsm.act("NEWLINE",
            NextValue(clr_col, 0),
            If(row == (TERM_LINES - 1),
                NextValue(row, 0),
            ).Else(
                NextValue(row, row + 1),
            ),
            If(full | (row == (TERM_LINES - 1)),
                NextValue(full, 1),
            ),
            If(full | (row == (TERM_LINES - 1)),
                # Scroll: the new cursor line becomes the bottom line of the screen.
                If(top == (TERM_LINES - 1),
                    NextValue(top, 0),
                ).Else(
                    NextValue(top, top + 1),
                ),
            ),
            NextState("CLEAR"),
        )
        fsm.act("CLEAR",
            # Clear the (new) cursor line.
            wrport.adr.eq(Cat(clr_col[:6], row)),
            wrport.dat_w.eq(ord(" ")),
            wrport.we.eq(1),
            NextValue(clr_col, clr_col + 1),
            If(clr_col == (TERM_COLS - 1),
                NextState("IDLE"),
            )
        )
        fsm.act("ESC",
            sink.ready.eq(1),
            If(sink.valid,
                NextValue(param, 0),
                If(c == ord("["),
                    NextState("CSI"),
                ).Else(
                    NextState("IDLE"),
                )
            )
        )
        fsm.act("CSI",
            sink.ready.eq(1),
            If(sink.valid,
                If((c >= ord("0")) & (c <= ord("9")),
                    NextValue(param, param*10 + (c - ord("0"))),
                ).Elif(c == ord(";"),
                    sgr(),
                    NextValue(param, 0),
                ).Elif((c >= 0x40) & (c <= 0x7e),
                    If(c == ord("m"), sgr()),
                    NextState("IDLE"),
                )
            )
        )

        # Reader (hclk): Game Boy LCD timing + pixel generation ------------------------------------
        top_h = Signal(max=TERM_LINES)
        row_h = Signal(max=TERM_LINES)
        col_h = Signal(max=LINE_STRIDE)
        self.specials += [
            MultiReg(top, top_h, "hclk"),
            MultiReg(row, row_h, "hclk"),
            MultiReg(col, col_h, "hclk"),
        ]

        phase   = Signal(2)               # hClk cycles per pixel clock.
        dot     = Signal(max=line_dots)   # Pixel clock in line.
        line    = Signal(max=frame_lines) # Line in frame.
        x       = Signal(8)               # Visible pixel (mode 3).
        text_x  = Signal(max=TERM_COLS)
        font_x  = Signal(2)
        text_y  = Signal(max=TERM_LINES)
        font_y  = Signal(3)
        buf_y   = Signal(max=TERM_LINES)
        frame   = Signal(5)               # Frame counter (cursor blink).
        visible = Signal()
        color_r = Signal(2)
        cursor  = Signal()
        self.comb += visible.eq((line < 144) & (dot >= h_start) & (dot < (h_start + 160)))
        self.sync.hclk += [
            phase.eq(phase + 1),
            If(phase == 3,
                If(dot == (line_dots - 1),
                    dot.eq(0),
                    If(line == (frame_lines - 1),
                        line.eq(0),
                        frame.eq(frame + 1),
                        text_y.eq(0),
                        font_y.eq(0),
                        buf_y.eq(top_h),
                    ).Else(
                        line.eq(line + 1),
                        If(line < 144,
                            If(font_y == (FONT_HEIGHT - 1),
                                font_y.eq(0),
                                text_y.eq(text_y + 1),
                                If(buf_y == (TERM_LINES - 1), buf_y.eq(0)).Else(buf_y.eq(buf_y + 1)),
                            ).Else(
                                font_y.eq(font_y + 1),
                            )
                        )
                    )
                ).Else(
                    dot.eq(dot + 1),
                ),
                If(visible,
                    x.eq(x + 1),
                    font_x.eq(font_x + 1),
                    If(font_x == (FONT_WIDTH - 1), text_x.eq(text_x + 1)),
                ).Else(
                    x.eq(0),
                    font_x.eq(0),
                    text_x.eq(0),
                )
            )
        ]
        # Pixel pipeline over the 4 hClk of a pixel clock: character read (phase 0), font read
        # (phase 1), pixel (phase 2), output with clkena (phase 3).
        pixel = Signal(15)
        bit   = Signal()
        self.comb += [
            # Glyph pixel (MSB left, 4th column: spacing).
            bit.eq(Array([font_rd.dat_r[2], font_rd.dat_r[1], font_rd.dat_r[0], 0])[font_x]),
            rdport.adr.eq(Cat(text_x[:6], buf_y)),
            font_rd.adr.eq(Cat(font_y, rdport.dat_r[:7])),
            cursor.eq((buf_y == row_h) & (text_x == col_h) & frame[4]),
        ]
        cases = {i: pixel.eq(rgb555(*COLORS[i])) for i in range(4)}
        self.sync.hclk += [
            If(phase == 1, color_r.eq(rdport.dat_r[7:9])),
            If(phase == 2,
                If(bit ^ cursor,
                    Case(color_r, cases),
                ).Else(
                    pixel.eq(0),
                )
            ),
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

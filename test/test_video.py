#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

import os
import random
import shutil
import tempfile
import subprocess

import pytest

from migen import *

from litex.gen.sim import run_simulation

from litex.build.io import DDROutput

from chromatix.gateware.video import ColorCorrection, ST7785PanelMaster, VideoPipeline
from chromatix.gateware.video import GLYPHS_TIMER, GLYPHS_HEX, BATTERY_FRONT, glyph_lookup

from test.eqcheck import export_migen, eqcheck

# Helpers ------------------------------------------------------------------------------------------

ROOT     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
GOLD_REV = "5bd6b0f" # Last revision with the original video sources in the tree.
GOLD_DIR = "chromatix/verilog/bsp"

def fetch_gold_source(name):
    r = subprocess.run(["git", "show", f"{GOLD_REV}:{GOLD_DIR}/{name}"], capture_output=True,
        text=True, cwd=ROOT)
    if r.returncode != 0:
        pytest.skip(f"original {name} not available (git history)")
    return r.stdout

def pack(r, g, b):
    return (b << 12) | (g << 6) | r

def unpack(p):
    return (p & 0x3f, (p >> 6) & 0x3f, (p >> 12) & 0x3f)

def crush(p):
    return pack(*[c >> 2 for c in unpack(p)])

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
                yield dut.pixel.eq(pack(r, g, b))
                yield
                yield
                result = ((yield dut.pixel_lcd), (yield dut.pixel_uvc))
                if result != color_correction_model(r, g, b, correct_lcd, correct_uvc):
                    errors.append((r, g, b, correct_lcd, correct_uvc, result))

    run_simulation(dut, gen())
    assert errors == []

def test_color_correction_known_pixels():
    """Known corrected values (white is dimmed/tinted, black stays black) and 1-cycle latency."""
    dut = ColorCorrection()

    def gen():
        yield dut.correct_lcd.eq(1)
        yield dut.correct_uvc.eq(1)
        yield dut.valid.eq(1)
        yield dut.hsync.eq(1)
        yield dut.pixel.eq(pack(63, 63, 63))
        yield
        yield dut.valid.eq(0)
        yield dut.hsync.eq(0)
        yield dut.vsync.eq(1)
        yield dut.pixel.eq(pack(0, 0, 0))
        yield
        # White (registered at the previous edge) with syncs delayed by one cycle.
        assert (yield dut.valid_out) == 1
        assert (yield dut.hsync_out) == 1
        assert (yield dut.vsync_out) == 0
        assert unpack((yield dut.pixel_lcd)) == (53, 48, 41)
        assert unpack((yield dut.pixel_uvc)) == (63, 63, 63)
        yield
        assert (yield dut.valid_out) == 0
        assert (yield dut.vsync_out) == 1
        assert (yield dut.pixel_lcd) == 0
        assert (yield dut.pixel_uvc) == 0

    run_simulation(dut, gen())

@pytest.mark.skipif(shutil.which("yosys") is None, reason="Yosys not available")
def test_color_correction_eqcheck():
    """Formal equivalence (bounded, from reset) vs the original color_correction."""
    workdir = tempfile.mkdtemp(prefix="eqcheck_color_correction_")
    gold    = os.path.join(workdir, "vid_system_top.sv")
    gate    = os.path.join(workdir, "cc_gate.v")
    with open(gold, "w", encoding="utf-8") as f:
        f.write(fetch_gold_source("vid_system_top.sv"))
        f.write("""
module cc_gold(input sys_clk, input correct_lcd, input correct_uvc, input valid,
    input hsync, input vsync, input [17:0] pixel, output valid_out, output hsync_out,
    output vsync_out, output [17:0] pixel_lcd, output [17:0] pixel_uvc);
    color_correction u(.hClk(sys_clk), .hCorrectLCD(correct_lcd), .hCorrectUVC(correct_uvc),
        .hValid(valid), .hHsync(hsync), .hVsync(vsync), .hColorPixel(pixel),
        .hValidCorrected(valid_out), .hHsyncCorrected(hsync_out), .hVsyncCorrected(vsync_out),
        .hColorPixelCorrected(pixel_lcd), .hColorPixelUVCCorrected(pixel_uvc));
endmodule
""")
    m   = Module()
    m.clock_domains.cd_sys = ClockDomain(reset_less=True) # No reset in the original.
    m.submodules.cc = cc = ColorCorrection()
    ios = {cc.correct_lcd, cc.correct_uvc, cc.valid, cc.hsync, cc.vsync, cc.pixel, cc.valid_out,
        cc.hsync_out, cc.vsync_out, cc.pixel_lcd, cc.pixel_uvc, m.cd_sys.clk}
    export_migen(m, ios, "cc_gate", gate)
    ok, log = eqcheck([gold], "cc_gold", [gate], "cc_gate", depth=4, workdir=workdir)
    assert ok, log[-4000:]

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

# ST7785 Panel Master ------------------------------------------------------------------------------

class SmallFramePanel(ST7785PanelMaster):
    """Panel with a reduced number of lines (horizontal timings/offsets unchanged) for fast sims."""
    V_LW, V_VALID, V_FP, V_BP = 1, 3, 1, 1

def panel_sim(dut, cycles, lines=4, lcd_en=1, lcd_on=1):
    """Write `lines` Game Boy lines (pixel i: LCD = i*0x3f3, UVC = i) after a vsync (hClk) and
    trace the panel outputs (gClk): (hsync, vsync, de, db, uvc_en, uvc_db)."""
    trace = []

    def gclk_gen():
        yield dut.nrst.eq(1)
        yield dut.lcd_on.eq(lcd_on)
        yield dut.lcd_en.eq(lcd_en)
        for i in range(cycles):
            yield
            trace.append((
                (yield dut.lcd_hsync),
                (yield dut.lcd_vsync),
                (yield dut.de),
                (yield dut.db),
                (yield dut.uvc_en),
                (yield dut.uvc_db),
            ))

    def hclk_gen():
        yield dut.vsync.eq(1)
        for i in range(20):
            yield
        yield dut.vsync.eq(0)
        for line in range(lines):
            for i in range(160):
                n = line*160 + i
                yield dut.valid.eq(1)
                yield dut.pixel.eq((n*0x3f3) & 0x3ffff)
                yield dut.pixel_uvc.eq(n)
                yield
            yield dut.valid.eq(0)
            yield dut.hsync.eq(1)
            for i in range(20):
                yield
            yield dut.hsync.eq(0)
            for i in range(20):
                yield

    run_simulation(dut, {"gclk": gclk_gen(), "hclk": hclk_gen()}, clocks={"gclk": 10, "hclk": 10})
    return trace

def runs(values):
    """Run-length encode a sequence: [(value, length), ...]."""
    r = []
    for v in values:
        if r and r[-1][0] == v:
            r[-1][1] += 1
        else:
            r.append([v, 1])
    return [tuple(x) for x in r]

def test_panel_timing():
    """Panel hsync/vsync/DE periods and widths (real horizontal timings, reduced frame)."""
    dut   = SmallFramePanel()
    line  = dut.H_LW + dut.H_VALID + dut.H_FP + dut.H_BP + 1
    trace = panel_sim(dut, 16*line)

    # Skip the start-up/genlock (counters re-aligned FINE_OFFSET cycles after the vsync).
    start = 2*line
    hs    = runs([s[0] for s in trace[start:]])[1:-1]
    lows  = [length for v, length in hs if v == 0]
    highs = [length for v, length in hs if v == 1]
    falls = [i for i in range(start, len(trace)) if not trace[i][1] and trace[i - 1][1]]
    assert set(highs) == {line - dut.H_LW}
    assert set(lows)  == {dut.H_LW, dut.H_LW + 1}
    assert lows.count(dut.H_LW + 1) == len(falls) # Frame wrap: +1 cycle (h_count held at 0).

    # DE: H_VALID cycles per line, V_VALID lines per frame.
    de = runs([s[2] for s in trace[start:]])[1:-1]
    assert {length for v, length in de if v == 1} == {dut.H_VALID}

    # VSYNC: low for V_LW lines, frame = (V_LW + V_VALID + V_FP + V_BP) lines + 1 cycle.
    frame = (dut.V_LW + dut.V_VALID + dut.V_FP + dut.V_BP)*line + 1
    rises = [i for i in range(start, len(trace)) if trace[i][1] and not trace[i - 1][1]]
    assert len(falls) >= 2
    assert set(b - a for a, b in zip(falls, falls[1:])) == {frame}
    assert rises[0] - falls[0] == dut.V_LW*line
    de_lines = runs([s[2] for s in trace[falls[0]:falls[1]]])
    assert len([1 for v, length in de_lines if v == 1]) == dut.V_VALID

def test_panel_pixels():
    """Line buffer scanout: OFFSET black pixels, then the 160 Game Boy pixels in order, each
    over 3 dot clocks (R, G, B), UVC pixel alongside."""
    dut   = SmallFramePanel()
    line  = dut.H_LW + dut.H_VALID + dut.H_FP + dut.H_BP + 1
    trace = panel_sim(dut, 8*line)

    # First two active lines (DE windows).
    windows = []
    for i in range(1, len(trace)):
        if trace[i][2] and not trace[i - 1][2]:
            windows.append(i)
    assert len(windows) >= 2
    for n, w in enumerate(windows[:2]):
        de   = trace[w:w + dut.H_VALID]
        lead = 3*(dut.OFFSET - 1)
        assert all(s[3] == 0 and s[4] == 0 for s in de[:lead])
        data = de[lead:]
        assert all(s[4] == 1 for s in data)
        for i in range(160):
            pixel = ((n*160 + i)*0x3f3) & 0x3ffff
            rgb   = [s[3] for s in data[3*i:3*i + 3]]
            assert rgb == list(unpack(pixel)), (n, i)
            assert {s[5] for s in data[3*i:3*i + 3]} == {n*160 + i}

def test_panel_lcd_disabled():
    """lcd_en = 0: white pixels (DB = 0x3f, UVC = 0x3ffff) in the active area."""
    dut   = SmallFramePanel()
    line  = dut.H_LW + dut.H_VALID + dut.H_FP + dut.H_BP + 1
    trace = panel_sim(dut, 5*line, lines=1, lcd_en=0)
    active = [s for s in trace if s[4]]
    assert active
    assert {(s[3], s[5]) for s in active} == {(0x3f, 0x3ffff)}

def test_panel_lcd_off():
    """lcd_on = 0: timing counters held (no hsync/DE activity), black outputs."""
    dut   = SmallFramePanel()
    line  = dut.H_LW + dut.H_VALID + dut.H_FP + dut.H_BP + 1
    trace = panel_sim(dut, 3*line, lines=1, lcd_on=0)
    assert all(s[2] == 0 and s[3] == 0 and s[4] == 0 for s in trace)
    assert len(runs([s[0] for s in trace[2:]])) == 1

# Video Pipeline -----------------------------------------------------------------------------------

class SimDDROutput:
    @staticmethod
    def lower(dr):
        m = Module()
        m.comb += dr.o.eq(dr.i1)
        return m

class PipelineSim:
    """VideoPipeline in a single clock domain (hclk = gclk = sys) with a Game Boy LCD driver."""
    def __init__(self, **controls):
        self.pads = Record([("dotclk", 1), ("enable", 1), ("hsync", 1), ("vsync", 1), ("db", 6)])
        self.vp   = VideoPipeline(self.pads)
        self.dut  = ClockDomainsRenamer({"hclk": "sys", "gclk": "sys"})(self.vp)
        self.controls = controls
        self.pixels   = []  # (lcd, uvc) at the panel input, in order.
        self.fb       = []  # (fb_new_line, fb_address, fb_write, fb_data) per cycle.

    def pulse(self, signal, n=1):
        """Assert signal for n cycles (n events for the timer enables)."""
        yield signal.eq(1)
        for i in range(n):
            yield
        yield signal.eq(0)
        yield

    def monitor(self):
        vp = self.vp
        yield "passive"
        while True:
            yield
            if (yield vp.panel.valid):
                self.pixels.append(((yield vp.panel.pixel), (yield vp.panel.pixel_uvc)))
            self.fb.append(((yield vp.fb_new_line), (yield vp.fb_address), (yield vp.fb_write),
                (yield vp.fb_data)))

    def frame(self, lines):
        """Vsync then lines: {screen_y: [(gb_data, fb_prev, osd_data), ...]} (x from 0)."""
        vp = self.vp
        yield from self.pulse(vp.gb_vsync)
        for y in range(1, max(lines) + 1):
            yield vp.gb_mode.eq(0b11)
            yield
            for gb_data, fb_prev, osd_data in lines.get(y, []):
                yield vp.gb_clkena.eq(1)
                yield vp.gb_data.eq(gb_data)
                yield vp.fb_prev.eq(fb_prev)
                yield vp.osd_data.eq(osd_data)
                yield
            yield vp.gb_clkena.eq(0)
            yield vp.gb_mode.eq(0b00)
            yield
            yield
        for i in range(4):
            yield

    def run(self, *gens):
        vp = self.vp
        def main():
            for k, v in self.controls.items():
                yield getattr(vp, k).eq(v)
            yield
            for gen in gens:
                yield from gen
        run_simulation(self.dut, [main(), self.monitor()],
            special_overrides={DDROutput: SimDDROutput})

def rgb555(r, g, b):
    return (b << 10) | (g << 5) | r

def rgb555_to_666(p):
    return pack((p & 0x1f) << 1, ((p >> 5) & 0x1f) << 1, ((p >> 10) & 0x1f) << 1)

def rgb565_to_666(p):
    # Channel order as the original ({B, G, R} from RGB565 bits [4:0], [10:6], [15:11]).
    b5, g5, r5 = (p >> 0) & 0x1f, (p >> 6) & 0x1f, (p >> 11) & 0x1f
    return pack((r5 << 1) | (r5 & 1), (g5 << 1) | (g5 & 1), (b5 << 1) | (b5 & 1))

def test_pipeline_framebuffer():
    """Frame buffer address: 0x10000 on vsync, +320 per line end; write/data follow the Game Boy
    pixels."""
    sim   = PipelineSim(menu_disabled=1)
    lines = {y: [(rgb555(y, x & 31, 7), 0, 0) for x in range(8)] for y in range(1, 4)}
    sim.run(sim.frame(lines))
    new_lines = [i for i, s in enumerate(sim.fb) if s[0]]
    assert len(new_lines) == 3
    for n, i in enumerate(new_lines):
        assert sim.fb[i][1] == 0x10000 + 320*n
        assert sim.fb[i + 1][1] == 0x10000 + 320*(n + 1)
    written = [s[3] for s in sim.fb if s[2]]
    assert written == [p[0] for y in (1, 2, 3) for p in lines[y]]

@pytest.mark.parametrize("frame_blend", [0, 1])
def test_pipeline_frame_blend(frame_blend):
    """Game Boy pixel -> RGB666 (x2), or averaged with the previous frame pixel (sum)."""
    random.seed(frame_blend)
    sim    = PipelineSim(menu_disabled=1, frame_blend=frame_blend)
    pixels = [(random.randrange(2**15), random.randrange(2**15), 0) for _ in range(160)]
    sim.run(sim.frame({50: pixels}))
    assert len(sim.pixels) == 160
    for (cur, prev, _), (lcd, uvc) in zip(pixels, sim.pixels):
        c, p = unpack(rgb555_to_666(cur)), unpack(rgb555_to_666(prev))
        expected = pack(*[(a + b) >> 1 for a, b in zip(c, p)]) if frame_blend else pack(*c)
        assert lcd == expected
        assert uvc == expected

def test_pipeline_color_correction():
    """Color correction applied on the pipeline pixels (LCD and UVC paths)."""
    random.seed(2)
    sim    = PipelineSim(menu_disabled=1, correct_lcd=1, correct_uvc=1)
    pixels = [(random.randrange(2**15), 0, 0) for _ in range(160)]
    sim.run(sim.frame({50: pixels}))
    assert len(sim.pixels) == 160
    for (cur, _, _), result in zip(pixels, sim.pixels):
        assert result == color_correction_model(*unpack(rgb555_to_666(cur)), 1, 1)

def test_pipeline_osd():
    """OSD (menu enabled at vsync): opaque RGB565 pixels replace the game, 0xF81F darkens it."""
    random.seed(3)
    sim    = PipelineSim(menu_disabled=0)
    osd    = [0xf81f if random.random() < 0.3 else random.randrange(2**16) for _ in range(160)]
    pixels = [(random.randrange(2**15), 0, o) for o in osd]
    sim.run(sim.frame({20: pixels}))
    assert len(sim.pixels) == 160
    for (cur, _, o), (lcd, uvc) in zip(pixels, sim.pixels):
        expected = crush(rgb555_to_666(cur)) if o == 0xf81f else rgb565_to_666(o)
        assert lcd == expected
        assert uvc == expected

def test_pipeline_menu_disabled():
    """Menu disabled: OSD data ignored, draw_osd low."""
    sim    = PipelineSim(menu_disabled=1)
    pixels = [(rgb555(x & 31, 3, 9), 0, 0x1234) for x in range(160)]
    draw   = []
    def check():
        draw.append((yield sim.vp.draw_osd))
    sim.run(sim.frame({20: pixels}), check())
    assert draw == [0]
    assert [lcd for lcd, uvc in sim.pixels] == [rgb555_to_666(p[0]) for p in pixels]

def overlay_model(x, y, timer, debug_value, voltage_low=1, show_low_batt=1, show_timer=1,
    debug_on=1):
    """Overlay model (overlay*.vhd + vid_system_top.sv priorities): 'black', 'white', 'red',
    'crush' or None (game pixel). timer: (HL, MH, ML, SH, SL, PH, PL)."""
    def batt_front():
        return 140 <= x <= 156 and 2 <= y <= 17 and BATTERY_FRONT[y - 2][x - 140] == "1"
    def batt_back():
        if not (141 <= x <= 157 and 3 <= y <= 18):
            return False
        return not ((x == 157 and y in (3, 18)) or (x == 141 and y == 18))
    def timer_front():
        if not (1 <= x <= 39 and 2 <= y <= 17):
            return False
        return (x in (1, 39) or y in (2, 17)) and not (x in (1, 39) and y in (2, 17))
    def timer_back():
        if not (2 <= x <= 40 and 3 <= y <= 18):
            return False
        return not ((x == 40 and y in (3, 18)) or (x == 2 and y == 18))
    def timer_number():
        hl, mh, ml, sh, sl, ph, pl = timer
        layout = [(4, 6, hl), (8, 8, 10), (10, 13, mh), (14, 17, ml), (18, 18, 10), (20, 23, sh),
            (24, 27, sl), (28, 28, 11), (30, 33, ph), (34, 37, pl)]
        for start, end, n in layout:
            if start <= x <= end:
                gx, gy = x - start, y - 8
                return gx < 3 and 0 <= gy < 5 and GLYPHS_TIMER[n][gy][gx] == "1"
        return False
    def debug_digit():
        if x > 31 or y < 136 or y - 136 >= 5:
            return False
        gx, n = x % 4, (debug_value >> (4*(7 - x//4))) & 0xf
        return gx < 3 and GLYPHS_HEX[n][y - 136][gx] == "1"
    if debug_on and debug_digit():
        return "black"
    if debug_on and x <= 31 and y >= 136:
        return "white"
    if voltage_low and show_low_batt and batt_front():
        return "red"
    if show_timer and (timer_front() or timer_number()):
        return "white"
    if (voltage_low and show_low_batt and batt_back()) or (show_timer and timer_back()):
        return "crush"
    return None

def test_pipeline_overlays():
    """Timer (running across an hour, percent digits), low battery and debug overlays over the
    game, checked against a model of the original overlays."""
    debug_value = 0x0123abcd
    sim  = PipelineSim(menu_disabled=1, voltage_low=1, low_batt_mode=0b00, show_timer=1,
        run_timer=1, debug_on=1, debug_system=debug_value)
    game = rgb555(20, 21, 22)
    ys   = list(range(1, 20)) + list(range(136, 144))
    def timer():
        yield from sim.pulse(sim.vp.second, 3600 + 2*60 + 5) # 1:02:05.
        yield from sim.pulse(sim.vp.percent, 37)             # .37
    width = {y: 160 if y < 20 else 40 for y in ys} # Debug lines: overlays at x < 32 only.
    sim.run(timer(), sim.frame({y: [(game, 0, 0)]*width[y] for y in ys}))
    colors = {
        "black" : 0,
        "white" : 0x3ffff,
        "red"   : pack(0x3f, 0, 0),
        "crush" : crush(rgb555_to_666(game)),
        None    : rgb555_to_666(game),
    }
    coords   = [(x, y) for y in ys for x in range(width[y])]
    expected = [colors[overlay_model(x, y, (1, 0, 2, 0, 5, 3, 7), debug_value)] for x, y in coords]
    got      = [lcd for lcd, uvc in sim.pixels]
    assert len(got) == len(expected)
    errors = [(c, g, e) for c, g, e in zip(coords, got, expected) if g != e]
    assert errors == []
    assert [uvc for lcd, uvc in sim.pixels] == got

def test_pipeline_timer_reset():
    """reset_timer clears the timer (saturation at 9:59:59.99 not simulated: 36000 cycles)."""
    sim  = PipelineSim(menu_disabled=1, show_timer=1, run_timer=1)
    game = rgb555(0, 0, 0)
    ys   = range(8, 13)
    def timer():
        yield from sim.pulse(sim.vp.second, 59*60 + 58) # 0:59:58.
        yield from sim.pulse(sim.vp.percent, 12)        # .12
    def check(timer_value):
        start = len(sim.pixels)
        yield from sim.frame({y: [(game, 0, 0)]*40 for y in ys})
        got = [lcd for lcd, uvc in sim.pixels[start:]]
        exp = [0x3ffff if overlay_model(x, y, timer_value, 0, voltage_low=0, debug_on=0) == "white"
            else 0 for y in ys for x in range(40)] # Black game: crushed shadow stays black.
        results.append(got == exp)
    results = []
    sim.run(timer(), check((0, 5, 9, 5, 8, 1, 2)), sim.pulse(sim.vp.reset_timer),
        check((0, 0, 0, 0, 0, 0, 0)))
    assert results == [True, True]

@pytest.mark.parametrize("mode, seconds, shown", [
    (0b00, 0, True),  # Show.
    (0b01, 0, False), # Blink: hidden...
    (0b01, 1, True),  # ... then toggles every second.
    (0b01, 2, False),
    (0b10, 1, False), # Hide.
    (0b11, 0, True),  # Reserved: show.
])
def test_pipeline_low_battery_mode(mode, seconds, shown):
    """Low battery indicator display modes."""
    sim  = PipelineSim(menu_disabled=1, voltage_low=1, low_batt_mode=mode)
    game = rgb555(0, 0, 0)
    sim.run(sim.pulse(sim.vp.second, seconds), sim.frame({2: [(game, 0, 0)]*160}))
    red = [x for x, (lcd, uvc) in enumerate(sim.pixels) if lcd == pack(0x3f, 0, 0)]
    assert red == (list(range(141, 156)) if shown else [])

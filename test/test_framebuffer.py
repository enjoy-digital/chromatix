#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""LCD framebuffers (BSRAM, PSRAM): Wishbone pixel/palette writes -> 160x144 Game Boy LCD pixels."""

import random

from migen import *

from litex.gen.sim import run_simulation

from chromatix.gateware.memory      import MultiPortRAMCtrl, PSRAMPortAdapter, LineReader
from chromatix.gateware.framebuffer import LCDFramebuffer, LCDPSRAMFramebuffer

from test.test_memory import NativePSRAM

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
        # Wait for the writer, then capture a frame: 144 lines of 160 pixels from the vsync rising
        # edge.
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

# LCD PSRAM Framebuffer ----------------------------------------------------------------------------

def test_psram_framebuffer():
    """8-bit lines written in the line buffers, copied to frame buffer 1 in the PSRAM (model)
    through the palette, displayed after the front buffer change: LCD stream pixels from the PSRAM
    line reader."""
    class DUT(Module):
        def __init__(self):
            xclk = ClockDomainsRenamer("xclk")
            self.submodules.ctrl    = ctrl  = xclk(MultiPortRAMCtrl(nports=2))
            self.submodules.psram   = psram = xclk(NativePSRAM())
            self.submodules.adapter = xclk(PSRAMPortAdapter(ctrl, psram))
            # Short blankings.
            self.submodules.fb      = fb    = LCDPSRAMFramebuffer(ctrl.ports[1],
                line_dots   = 166,
                h_start     = 2,
                frame_lines = 146,
            )
            ctrl.ports[0].rnw.reset          = 1
            ctrl.ports[0].burst_length.reset = 320
            self.submodules.reader  = reader = LineReader(ctrl.ports[0], ctrl.dout, fb.fb_base, 1)
            self.comb += [
                reader.valid.eq(fb.gb_clkena),
                reader.hsync.eq(fb.gb_mode[1]),
                reader.vsync.eq(fb.gb_vsync),
                fb.fb_data.eq(reader.data),
            ]
            self.clock_domains.cd_xclk = ClockDomain()
            self.clock_domains.cd_hclk = ClockDomain()

    dut     = DUT()
    random.seed(3)
    palette = [random.randrange(1 << 15) for _ in range(256)]
    lines   = {0: [x for x in range(160)], 1: [(3*x) & 0xff for x in range(160)],
               77: [255 - x for x in range(160)], 143: [0x23]*160}
    screen  = []
    res     = {}

    # Absolute bus word addresses, as decoded by the SoC (region at 0x90000000).
    region = 0x90000000//4

    def writer():
        fb = dut.fb
        for i, color in enumerate(palette):
            yield from fb.bus.write(region + fb.PALETTE_OFFSET + i, color)
        for n, (y, pixels) in enumerate(lines.items()):
            slot = n % 4 # Line buffers used in order.
            while ((yield fb.status.fields.busy) >> slot) & 1:
                yield
            for i in range(40):
                word = sum(p << 8*j for j, p in enumerate(pixels[4*i:4*i + 4]))
                yield from fb.bus.write(region + slot*40 + i, word)
            yield from fb.line.write(y | (1 << 8) | (slot << 10))
        while (yield fb.status.fields.busy):
            yield
        yield from fb.control.write(1)
        res["written"] = True

    def reader():
        while "written" not in res:
            yield
        # Second frame start after the front buffer change (the first one latches it).
        for _ in range(2):
            while (yield dut.fb.gb_vsync):
                yield
            while not (yield dut.fb.gb_vsync):
                yield
        frame = []
        while len(frame) < 144*160:
            if (yield dut.fb.gb_clkena):
                frame.append((yield dut.fb.gb_data))
            yield
        screen.extend(frame[160*y:160*(y + 1)] for y in range(144))
        res["front"] = (yield dut.fb.status.fields.front)

    run_simulation(dut, {"sys": writer(), "hclk": reader(), "xclk": dut.psram.generator(gap=0)},
        clocks={"sys": 10, "xclk": 10, "hclk": 20})
    assert dut.psram.errors == []
    for y, pixels in lines.items():
        assert screen[y] == [palette[p] for p in pixels], y
    assert screen[2] == [0]*160 # Not written.
    assert res["front"] == 1

#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litex.gen.sim import run_simulation

from chromatix.gateware.core_shell import rgb555
from chromatix.gateware.cores.nes import NESVideoAdapter, NESMemory, NESJoypad, NES_PALETTE_555

# Palette ------------------------------------------------------------------------------------------

def test_nes_palette():
    """64 entries; the 2C02 RGB888 values reduce to RGB555 ({B,G,R}, R in the LSBs)."""
    assert len(NES_PALETTE_555) == 64
    assert NES_PALETTE_555[0]  == rgb555(0x54 >> 3, 0x54 >> 3, 0x54 >> 3) # 0x545454.
    assert NES_PALETTE_555[13] == rgb555(0, 0, 0)                        # black.
    assert NES_PALETTE_555[32] == rgb555(0xEC >> 3, 0xEE >> 3, 0xEC >> 3) # near-white.

# Video adapter ------------------------------------------------------------------------------------

def test_nes_video_adapter():
    """color/cycle/scanline -> CoreVideo: ce on raster change, hblank at cycle>=256, vblank at
    scanline>=240, rgb from the palette."""
    dut = NESVideoAdapter(cd="sys")

    def gen():
        # Visible pixel: color 1 at (cycle 10, scanline 5).
        yield dut.color.eq(1); yield dut.cycle.eq(10); yield dut.scanline.eq(5)
        yield # latch previous.
        yield # now cycle/scanline changed vs registered -> ce.
        assert (yield dut.video.hblank) == 0
        assert (yield dut.video.vblank) == 0
        pix = NES_PALETTE_555[1]
        assert (yield dut.video.r) == (pix & 0x1f)
        assert (yield dut.video.g) == ((pix >> 5) & 0x1f)
        assert (yield dut.video.b) == ((pix >> 10) & 0x1f)
        # Hold same position: no raster change -> ce low.
        yield
        assert (yield dut.video.ce) == 0
        # Horizontal blank.
        yield dut.cycle.eq(300); yield
        assert (yield dut.video.hblank) == 1
        # Vertical blank.
        yield dut.scanline.eq(245); yield
        assert (yield dut.video.vblank) == 1

    run_simulation(dut, gen())

# Memory -------------------------------------------------------------------------------------------

def test_nes_memory():
    """PRG/CHR ROM pre-loaded into BRAM: CPU reads PRG (channel B, 0..), PPU reads CHR (channel A,
    0x200000..); prg_mask mirrors a 16 KB PRG into the 32 KB CPU window."""
    prg = [(a*7 + 1) & 0xff for a in range(32*1024)]
    chr = [(a*3 + 5) & 0xff for a in range(8*1024)]
    dut = NESMemory(prg_kb=32, chr_kb=8, cd="sys", prg_init=prg, chr_init=chr)

    def read_prg(addr):
        yield dut.b_addr.eq(addr); yield; yield
        return (yield dut.b_dout)

    def read_chr(addr):
        yield dut.a_addr.eq(addr); yield; yield
        return (yield dut.a_dout)

    def gen():
        yield dut.prg_mask.eq(0x7fff)
        for a in [0, 5, 100, 0x3fff, 0x7fff]:
            assert (yield from read_prg(a)) == prg[a], a
        for a in [0, 5, 100, 0x1fff]:
            assert (yield from read_chr(0x200000 + a)) == chr[a], a
        # 16 KB mirroring: PRG address 0x4005 masks to 0x0005.
        yield dut.prg_mask.eq(0x3fff)
        assert (yield from read_prg(0x4005)) == prg[5]

    run_simulation(dut, gen())

# Joypad -------------------------------------------------------------------------------------------

def test_nes_joypad():
    """Standard controller: strobe latches, each clock shifts out A,B,Select,Start,Up,Down,Left,Right
    (1 = pressed)."""
    dut = NESJoypad(cd="sys")
    pressed = dict(a=1, b=0, select=1, start=0, up=1, down=0, left=1, right=0)
    order   = ["a", "b", "select", "start", "up", "down", "left", "right"]

    def gen():
        for name, v in pressed.items():
            yield getattr(dut, name).eq(v)
        # Latch.
        yield dut.strobe.eq(1); yield; yield
        assert (yield dut.data) == pressed["a"]
        yield dut.strobe.eq(0); yield
        for i, name in enumerate(order):
            assert (yield dut.data) == pressed[name], name
            # Clock rising edge -> next button.
            yield dut.clock.eq(1); yield
            yield dut.clock.eq(0); yield
        # After 8 reads, official controllers return 1.
        assert (yield dut.data) == 1

    run_simulation(dut, gen())

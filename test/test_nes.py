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

class _FakeCache(Module):
    """CoreROMCache stand-in: `data` = rom[addr]; `stall` while a new address is looked up, for
    `latency` cycles (1 = a hit's lookup, more = a miss being filled)."""
    def __init__(self, rom, latency):
        self.addr  = Signal(21)
        self.rd    = Signal()
        self.data  = Signal(8)
        self.stall = Signal()
        self.flush = Signal()
        self.latency = Signal(8, reset=latency)
        mem  = Memory(8, len(rom), init=rom)
        port = mem.get_port(async_read=True)
        self.specials += mem, port
        last = Signal(21, reset=2**21 - 1)
        cnt  = Signal(8)
        self.comb += [port.adr.eq(self.addr), self.data.eq(port.dat_r)]
        self.sync += If(self.rd & (self.addr != last),
            last.eq(self.addr), cnt.eq(self.latency - 1),
        ).Elif(cnt != 0, cnt.eq(cnt - 1))
        self.comb += self.stall.eq(self.rd & ((self.addr != last) | (cnt != 0)))

def test_nes_memory_prg_cache():
    """PRG through a cache: a read (cpumem_read held 4 cycles, as cart_pre) is served without freezing
    the NES on a hit; a miss freezes it (`stall`) until the data is there; the byte is right either
    way."""
    prg   = [(a*13 + 7) & 0xff for a in range(4096)]
    cache = _FakeCache(prg, latency=1)
    dut   = NESMemory(chr_kb=8, cd="sys", prg_cache=cache, with_ram=True)
    dut.submodules.cache = cache

    def cpu_read(addr):
        """One CPU PRG read: returns (data at the end of the access, cycles frozen)."""
        frozen = 0
        yield dut.b_addr.eq(addr); yield dut.b_read.eq(1)
        n = 0
        while n < 4:                      # cart_pre lasts 4 cycles -- extended while frozen.
            yield
            if (yield dut.stall):
                frozen += 1
            else:
                n += 1
        yield dut.b_read.eq(0)
        for _ in range(4):
            yield
        return (yield dut.b_dout), frozen

    def gen():
        for a in [0x10, 0x11, 0x200, 0x10]:
            data, frozen = yield from cpu_read(a)
            assert data == prg[a] and frozen == 0, (hex(a), data, frozen)    # Hits: no freeze.
        yield cache.latency.eq(30)
        data, frozen = yield from cpu_read(0x345)
        assert data == prg[0x345] and frozen > 20, (data, frozen)            # Miss: frozen.

    run_simulation(dut, gen())

def test_nes_memory_rams():
    """with_ram: CHR-RAM is the CHR space (0x200000) made PPU-writable (as the mappers address it),
    distinct from CIRAM (0x300000; it used to alias the 2 KB CIRAM); PRG-RAM (0x3C0000) and CPU RAM
    (0x380000) are distinct; all read/write."""
    cache = _FakeCache([0]*16, latency=1)
    dut   = NESMemory(chr_kb=8, cd="sys", prg_cache=cache, with_ram=True)
    dut.submodules.cache = cache

    def write(ch, addr, data):
        if ch == "a":
            yield dut.a_addr.eq(addr); yield dut.a_din.eq(data); yield dut.a_write.eq(1)
            yield
            yield dut.a_write.eq(0)
        else:
            yield dut.b_addr.eq(addr); yield dut.b_din.eq(data); yield dut.b_write.eq(1)
            yield
            yield dut.b_write.eq(0)
        yield

    def read(ch, addr):
        yield (dut.a_addr if ch == "a" else dut.b_addr).eq(addr); yield; yield
        return (yield (dut.a_dout if ch == "a" else dut.b_dout))

    def gen():
        yield from write("a", 0x300010, 0x11) # CIRAM.
        yield from write("a", 0x200010, 0x22) # CHR-RAM (same low address bits).
        yield from write("a", 0x201810, 0x33) # CHR-RAM beyond 2 KB.
        yield from write("b", 0x380005, 0x44) # CPU RAM.
        yield from write("b", 0x3C0005, 0x55) # PRG-RAM.
        assert (yield from read("a", 0x300010)) == 0x11
        assert (yield from read("a", 0x200010)) == 0x22
        assert (yield from read("a", 0x201810)) == 0x33
        assert (yield from read("b", 0x380005)) == 0x44
        assert (yield from read("b", 0x3C0005)) == 0x55

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
            # A $4016 read: nes.v holds the clock high while the CPU samples the data, which must
            # still be this button; the shift to the next one happens on the falling edge.
            yield dut.clock.eq(1); yield
            for _ in range(3):
                yield
                assert (yield dut.data) == pressed[name], (name, "during read")
            yield dut.clock.eq(0); yield; yield # Falling edge, then the registered shift.
        # After 8 reads, official controllers return 1.
        assert (yield dut.data) == 1

    run_simulation(dut, gen())

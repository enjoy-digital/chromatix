#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

import random

from migen import *

from litex.gen.sim import run_simulation, passive

from chromatix.gateware.memory import MultiPortRAMCtrl, PSRAMPortAdapter
from chromatix.gateware.vcart  import MBC, VirtualCart, MBC_NONE, MBC_MBC1, MBC_MBC2, MBC_MBC3, MBC_MBC5
from chromatix.gateware.vcart  import MBC_HUC1
from chromatix.gateware.vcart  import ROM_BASE, RAM_BASE

from test.test_memory import NativePSRAM

# MBC Reference Model ------------------------------------------------------------------------------

class MBCModel:
    def __init__(self, mbc, rom_mask, ram_mask, mbc1m=False, mbc30=False):
        self.mbc, self.rom_mask, self.ram_mask = mbc, rom_mask, ram_mask
        self.mbc1m, self.mbc30 = mbc1m, mbc30
        self.ram_enable, self.rom_bank, self.bank2, self.mode = 0, 1, 0, 0

    def write(self, a, d):
        if a >= 0x8000:
            return
        region = a >> 13
        if self.mbc == MBC_MBC1:
            if region == 0: self.ram_enable = (d & 0xf) == 0xa
            if region == 1: self.rom_bank   = (d & 0x1f) or 1
            if region == 2: self.bank2      = d & 0x3
            if region == 3: self.mode       = d & 0x1
        elif self.mbc == MBC_MBC2:
            if a < 0x4000:
                if a & 0x100: self.rom_bank   = (d & 0xf) or 1
                else:         self.ram_enable = (d & 0xf) == 0xa
        elif self.mbc == MBC_MBC3:
            if region == 0: self.ram_enable = (d & 0xf) == 0xa
            if region == 1: self.rom_bank   = (d & (0xff if self.mbc30 else 0x7f)) or 1
            if region == 2:
                self.mode = (d >> 3) & 1 # RTC registers.
                if not self.mode:
                    self.bank2 = d & 0x7
        elif self.mbc == MBC_HUC1:
            if region == 0: self.mode     = (d & 0xf) == 0xe # IR.
            if region == 1: self.rom_bank = (d & 0x3f) or 1
            if region == 2: self.bank2    = d & 0x3
        elif self.mbc == MBC_MBC5:
            region = a >> 12
            if region in [0, 1]: self.ram_enable = (d & 0xf) == 0xa
            if region == 2: self.rom_bank = (self.rom_bank & 0x100) | d
            if region == 3: self.rom_bank = (self.rom_bank & 0x0ff) | ((d & 1) << 8)
            if region in [4, 5]: self.bank2 = d & 0xf

    def rom_addr(self, a):
        if self.mbc == MBC_NONE:
            bank = a >> 14
        elif self.mbc == MBC_MBC1 and self.mbc1m:
            bank = ((self.bank2 << 4) if self.mode else 0) if a < 0x4000 else ((self.bank2 << 4) | (self.rom_bank & 0xf))
        elif self.mbc == MBC_MBC1:
            bank = ((self.bank2 << 5) if self.mode else 0) if a < 0x4000 else ((self.bank2 << 5) | self.rom_bank)
        else:
            bank = self.rom_bank if a >= 0x4000 else 0
        return ((bank & self.rom_mask) << 14) | (a & 0x3fff)

    def ram_addr(self, a):
        if self.mbc == MBC_MBC2:
            return a & 0x1ff
        if self.mbc == MBC_MBC1:
            bank = self.bank2 if self.mode else 0
        else:
            bank = self.bank2
        return ((bank & self.ram_mask) << 13) | (a & 0x1fff)

    def ram_enabled(self):
        if self.mbc == MBC_NONE:
            return False
        if self.mbc == MBC_HUC1:
            return not self.mode
        if self.mbc == MBC_MBC3:
            return self.ram_enable and not self.mode
        return self.ram_enable

# MBC ----------------------------------------------------------------------------------------------

def test_mbc():
    """MBC registers/mapping against the reference model (random register writes and addresses)."""
    for mbc, rom_mask, ram_mask, mbc1m, mbc30 in [(MBC_NONE, 1, 0, 0, 0), (MBC_MBC1, 0x3f, 0x3, 0, 0),
        (MBC_MBC1, 0x3f, 0x3, 1, 0), (MBC_MBC2, 0xf, 0, 0, 0), (MBC_MBC3, 0x7f, 0x3, 0, 0),
        (MBC_MBC3, 0xff, 0x7, 0, 1), (MBC_MBC5, 0x1ff, 0xf, 0, 0), (MBC_HUC1, 0x3f, 0x3, 0, 0)]:
        dut   = MBC()
        model = MBCModel(mbc, rom_mask, ram_mask, mbc1m, mbc30)
        rng   = random.Random(mbc*4 + mbc1m*2 + mbc30)
        errors = []

        def gen():
            yield dut.mbc.eq(mbc)
            yield dut.mbc1m.eq(mbc1m)
            yield dut.mbc30.eq(mbc30)
            yield dut.rom_mask.eq(rom_mask)
            yield dut.ram_mask.eq(ram_mask)
            yield
            for _ in range(400):
                if rng.random() < 0.3:
                    a, d = rng.randrange(0x8000), rng.randrange(256)
                    yield dut.a.eq(a)
                    yield dut.din.eq(d)
                    yield dut.wr.eq(1)
                    yield
                    yield dut.wr.eq(0)
                    model.write(a, d)
                a = rng.choice([rng.randrange(0x8000), 0xa000 + rng.randrange(0x2000)])
                yield dut.a.eq(a)
                yield
                if a < 0x8000:
                    if (yield dut.rom_addr) != model.rom_addr(a):
                        errors.append((mbc, "rom", hex(a)))
                else:
                    if (yield dut.ram_addr) != model.ram_addr(a):
                        errors.append((mbc, "ram", hex(a)))
                    if (yield dut.ram_enabled) != model.ram_enabled():
                        errors.append((mbc, "ram_enable", hex(a)))

        run_simulation(dut, gen())
        assert errors == []

# Virtual Cartridge --------------------------------------------------------------------------------

class VirtualCartDUT(Module):
    def __init__(self):
        self.submodules.ctrl    = ctrl  = ClockDomainsRenamer("xclk")(MultiPortRAMCtrl(nports=2))
        self.submodules.psram   = psram = ClockDomainsRenamer("xclk")(NativePSRAM())
        self.submodules.adapter = ClockDomainsRenamer("xclk")(PSRAMPortAdapter(ctrl, psram))
        self.submodules.vcart   = VirtualCart(ctrl.ports[1], ctrl.dout, lines=16)


def run_vcart(mbc, rom, accesses, rom_mask, ram_mask=0x3, gap=0):
    """Run Game Boy bus accesses [("r"|"w", address, data)] through the virtual cartridge (the bus
    waits while `wait`), returns the read data and the PSRAM model."""
    dut = VirtualCartDUT()
    for i in range(0, len(rom), 2):
        dut.psram.mem[(ROM_BASE + i)//2] = rom[i] | (rom[i + 1] << 8)
    res = {"read": [], "max_wait": 0}

    def main():
        yield dut.vcart.enable.eq(1)
        yield dut.vcart.mbc_type.eq(mbc)
        yield dut.vcart.rom_mask.eq(rom_mask)
        yield dut.vcart.ram_mask.eq(ram_mask)
        while (yield dut.vcart.wait): # Initial flush.
            yield
        for kind, a, d in accesses:
            yield dut.vcart.a.eq(a)
            yield dut.vcart.din.eq(d)
            yield (dut.vcart.rd if kind == "r" else dut.vcart.wr).eq(1)
            yield
            yield # Lookup (synchronous tags).
            waits = 0
            while (yield dut.vcart.wait):
                waits += 1
                yield
            res["max_wait"] = max(res["max_wait"], waits)
            yield # Data valid 1 cycle after the address (synchronous cache read).
            if kind == "r":
                res["read"].append((yield dut.vcart.data))
            yield dut.vcart.rd.eq(0)
            yield dut.vcart.wr.eq(0)
            yield
        res["misses"] = (yield dut.vcart.misses)

    run_simulation(dut, {"hclk": main(), "xclk": dut.psram.generator(gap=gap)},
        clocks={"hclk": 60, "xclk": 15, "qspi": 25})
    assert dut.psram.errors == []
    return res, dut.psram


def test_vcart_rom_reads():
    """ROM reads (sequential and random, with MBC5 bank switching) return the ROM content."""
    rng   = random.Random(1)
    rom   = bytes(rng.randrange(256) for _ in range(8*16384))
    model = MBCModel(MBC_MBC5, 7, 0)
    accesses, expected = [], []
    for n in range(300):
        if n % 50 == 0:
            bank = rng.randrange(8)
            accesses.append(("w", 0x2000, bank))
            model.write(0x2000, bank)
        a = rng.choice([rng.randrange(0x8000), 0x100 + n, 0x4000 + (n*3 % 0x4000)])
        accesses.append(("r", a, 0))
        expected.append(rom[model.rom_addr(a)])
    res, _ = run_vcart(MBC_MBC5, rom, accesses, rom_mask=7, gap=1)
    assert res["read"] == expected
    assert 0 < res["misses"] < len(expected) # Cache hits (lines of 16 bytes).


def test_vcart_cram():
    """Cartridge RAM writes are read back (cache) and written through to the PSRAM."""
    rom = bytes(32768)
    accesses = [("w", 0x0000, 0x0a)] # RAM enable.
    writes = {0xa000: 0x12, 0xa001: 0x34, 0xa123: 0x56, 0xbffe: 0x78}
    accesses += [("w", a, d) for a, d in writes.items()]
    accesses += [("r", a, 0) for a in writes]
    accesses += [("w", 0x0000, 0x00), ("r", 0xa000, 0)] # RAM disabled: $FF.
    res, psram = run_vcart(MBC_MBC1, rom, accesses, rom_mask=1)
    assert res["read"] == list(writes.values()) + [0xff]
    for a, d in writes.items():
        word = psram.mem[(RAM_BASE + a - 0xa000)//2]
        assert (word >> (8*(a & 1))) & 0xff == d


def test_vcart_save_snapshot():
    """Cartridge RAM writes set save_dirty, a block 0 snapshot request clears it and the 1KB block is
    copied to the snapshot buffer (ready with the request sequence)."""
    dut  = VirtualCartDUT()
    save = {i: (i*13 + 5) & 0xff for i in range(0x400, 0x800)} # Block 1 ($A400-$A7FF) content.
    for i in range(0, 2*1024*2, 2):
        dut.psram.mem[(RAM_BASE + i)//2] = 0xffff
    for a, d in save.items():
        word = dut.psram.mem[(RAM_BASE + (a & ~1))//2]
        dut.psram.mem[(RAM_BASE + (a & ~1))//2] = (word & ~(0xff << 8*(a & 1))) | (d << 8*(a & 1))
    res  = {}

    def main():
        vc = dut.vcart
        yield vc.enable.eq(1)
        yield vc.mbc_type.eq(MBC_MBC1)
        yield vc.rom_mask.eq(1)
        yield vc.ram_mask.eq(0x3)
        while not (yield vc.initialized):
            yield
        res["dirty0"] = (yield vc.save_dirty)
        # RAM enable, write $A000 (block 0).
        for a, d in [(0x0000, 0x0a), (0xa000, 0x42)]:
            yield vc.a.eq(a)
            yield vc.din.eq(d)
            yield vc.wr.eq(1)
            yield
            yield
            while (yield vc.wait):
                yield
            yield
            yield vc.wr.eq(0)
            yield
        res["dirty1"] = (yield vc.save_dirty)
        # Snapshot block 0 (clears dirty), then block 1.
        for block, sequence in [(0, 0x1234), (1, 0x5678)]:
            yield vc.snapshot_block.eq(block)
            yield vc.snapshot_sequence.eq(sequence)
            yield vc.snapshot_request.eq(~(yield vc.snapshot_request))
            for _ in range(4):
                yield
            while not ((yield vc.snapshot_ready) and (yield vc.snapshot_ready_sequence) == sequence):
                yield
            res[f"dirty_snap{block}"] = (yield vc.save_dirty)
            data = []
            for i in range(1024):
                yield vc.snapshot_addr.eq(i)
                yield
                yield
                data.append((yield vc.snapshot_data))
            res[f"block{block}"] = data

    run_simulation(dut, {"hclk": main(), "xclk": dut.psram.generator()},
        clocks={"hclk": 60, "xclk": 15, "qspi": 60})
    assert dut.psram.errors == []
    assert (res["dirty0"], res["dirty1"], res["dirty_snap0"]) == (0, 1, 0)
    assert res["block0"][0] == 0x42
    assert res["block1"] == [save[0x400 + i] for i in range(1024)]

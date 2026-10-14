#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

import random

from migen import *

from litex.gen.sim import run_simulation

from chromatix.gateware.memory import MultiPortRAMCtrl, PSRAMPortAdapter
from chromatix.gateware.cores.rom_cache import CoreBankCache

from test.test_memory import NativePSRAM

BASE = 0x600000

class BankCacheDUT(Module):
    """CoreBankCache (sys) + the caller's 16-bit slot BRAM, on the PSRAM model (xclk)."""
    def __init__(self, bank_bits, bank_size, slots):
        self.submodules.ctrl    = ctrl  = ClockDomainsRenamer("xclk")(MultiPortRAMCtrl(nports=2))
        self.submodules.psram   = psram = ClockDomainsRenamer("xclk")(NativePSRAM())
        self.submodules.adapter = ClockDomainsRenamer("xclk")(PSRAMPortAdapter(ctrl, psram))
        self.submodules.cache   = cache = CoreBankCache(ctrl.ports[1], ctrl.dout, base=BASE,
            bank_bits=bank_bits, bank_size=bank_size, slots=slots, cd="sys")
        mem = Memory(16, slots*bank_size//2)
        rd  = mem.get_port()
        wr  = mem.get_port(write_capable=True)
        self.specials += mem, rd, wr
        self.data = Signal(8)
        self.comb += [
            wr.adr.eq(cache.fill_adr), wr.dat_w.eq(cache.fill_data), wr.we.eq(cache.fill_we),
            rd.adr.eq(Cat(cache.addr[1:cache.offset_bits], cache.slot)),
            self.data.eq(Mux(cache.addr[0], rd.dat_r[8:16], rd.dat_r[0:8])),
        ]

def run_bank_cache(addrs, bank_bits=4, bank_size=32, slots=4):
    dut = BankCacheDUT(bank_bits, bank_size, slots)
    rom = [random.randrange(256) for _ in range(bank_size*2**bank_bits)]
    for i in range(0, len(rom), 2):
        dut.psram.mem[(BASE + i)//2] = rom[i] | (rom[i + 1] << 8)
    res = {"errors": [], "stalled": []}

    def main():
        yield dut.cache.enable.eq(1)
        while (yield dut.cache.stall): # Initial flush.
            yield
        for a in addrs:
            yield dut.cache.addr.eq(a)
            yield dut.cache.rd.eq(1)
            yield
            stalls = 0
            while (yield dut.cache.stall):
                stalls += 1
                yield
            yield # Registered BRAM read.
            if (yield dut.data) != rom[a]:
                res["errors"].append(hex(a))
            res["stalled"].append(stalls)
            yield dut.cache.rd.eq(0)
            yield
        res["misses"] = (yield dut.cache.misses)

    run_simulation(dut, {"sys": main(), "xclk": dut.psram.generator()},
        clocks={"sys": 12, "xclk": 15})
    assert dut.psram.errors == []
    return res

def test_bank_cache_reads():
    # Random reads over all the 16 banks with 4 slots: constant evictions, every byte correct.
    random.seed(1)
    addrs = [random.randrange(16*32) for _ in range(300)]
    res = run_bank_cache(addrs)
    assert res["errors"] == []
    assert res["misses"] > 16

def test_bank_cache_working_set():
    # A working set that fits (3 banks, 4 slots): 3 misses, then reads never stall.
    random.seed(2)
    addrs = [random.choice([2, 7, 11])*32 + random.randrange(32) for _ in range(200)]
    res = run_bank_cache(addrs)
    assert res["errors"] == []
    assert res["misses"] == 3
    assert all(s == 0 for s in res["stalled"][50:])

def test_bank_cache_second_chance():
    # Banks 0-3 fill the 4 slots (all referenced); bank 4 comes in (the sweep clears the reference
    # bits, evicts slot 0 = bank 0). Bank 1 is then read again (referenced): when bank 5 comes in,
    # bank 1 is spared (second chance) and bank 2 (cold) is evicted -- bank 1 never refaults.
    seq = [0, 32, 64, 96, 128, 33, 160, 34, 35]
    res = run_bank_cache(seq)
    assert res["errors"] == []
    assert res["misses"] == 6 # 4 cold + banks 4 and 5.
    assert res["stalled"][-2:] == [0, 0]

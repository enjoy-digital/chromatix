#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litex.gen import *
from litex.gen.sim import run_simulation, passive

from chromatix.gateware.psram  import MR0, MR4, MR8, PSRAMController, PSRAMBIST
from chromatix.gateware.memory import memory_port_layout

# Simulation Helpers -------------------------------------------------------------------------------

class PSRAMSimPHY(LiteXModule):
    """PSRAMGW5APHY controller-side interface without the Gowin primitives (driven by PSRAMModel)."""
    def __init__(self):
        self.reset     = Signal()
        self.cs_n      = Signal(reset=1)
        self.clk_en    = Signal()
        self.dq_o      = Signal(16)
        self.dq_oe_n   = Signal(reset=1)
        self.rwds_o    = Signal(2, reset=0b11)
        self.rwds_oe_n = Signal(reset=1)
        self.dq_i      = [Signal(8) for _ in range(4)]
        self.rwds_i    = Signal(4)


class _DFFCSim(Module):
    """Cycle-based model of the GW5A DFFC (D flip-flop with asynchronous clear)."""
    def __init__(self, d, clk, clear, q):
        q_r = Signal()
        self.comb += q.eq(q_r & ~clear)
        sync  = getattr(self.sync, clk.cd)
        sync += q_r.eq(d & ~clear)


class SimInstance:
    """
    Special override lowering Gowin primitives for simulation: DFFC gets a behavioral model, the
    PSRAM PHY primitives (OSER4/IDES4/IODELAY/IOBUF) are removed: their controller-side signals are
    then driven by PSRAMModel.
    """
    @staticmethod
    def lower(instance):
        if instance.of == "DFFC":
            ios = {item.name: item.expr for item in instance.items if isinstance(item, Instance._IO)}
            return _DFFCSim(ios["D"], ios["CLK"], ios["CLEAR"], ios["Q"])
        assert instance.of in ["OSER4", "IDES4", "IODELAY", "IOBUF"], instance.of
        return Module()


class PSRAMModel:
    """
    Behavioral OPI PSRAM seen at the PHY/controller interface (one PSRAM clock per sys cycle).

    Decodes the commands (global reset, mode register write/read, burst write/read), stores data in
    a word dictionary and returns read data from `latency` clocks after the command (with RWDS
    toggling, as seen through the IDES4). `corrupt(word_addr, data)` can alter the read data.
    """
    CMD_READ     = 0x2020
    CMD_WRITE    = 0xa0a0
    CMD_MR_READ  = 0x4040
    CMD_MR_WRITE = 0xc0c0
    CMD_RESET    = 0xffff

    def __init__(self, phy, vendor_id=0x0d, latency=10, corrupt=None):
        self.phy       = phy
        self.mem       = {}
        self.mr        = {1: vendor_id}
        self.mr_writes = []
        self.commands  = []
        self.latency   = latency
        self.corrupt   = corrupt or (lambda addr, data: data)
        self.errors    = []

    @passive
    def generator(self):
        phy   = self.phy
        cycle = 0
        cmd   = None
        addr  = 0
        ptr   = 0
        while True:
            cs_n      = (yield phy.cs_n)
            clk_en    = (yield phy.clk_en)
            dq_o      = (yield phy.dq_o)
            dq_oe_n   = (yield phy.dq_oe_n)
            rwds_oe_n = (yield phy.rwds_oe_n)
            rdata     = None
            if cs_n:
                cycle = 0
            elif clk_en:
                cycle += 1
                if cycle == 1:
                    cmd = dq_o
                elif cycle == 2:
                    addr = dq_o << 16
                elif cycle == 3:
                    addr |= dq_o
                    ptr   = addr >> 1
                    self.commands.append((cmd, addr))
                elif cycle == 4 and cmd == self.CMD_MR_WRITE:
                    self.mr[addr & 0xf] = dq_o >> 8
                    self.mr_writes.append((addr & 0xf, dq_o >> 8))
                # Write data (RWDS driven by the controller).
                if cycle > 3 and cmd == self.CMD_WRITE and not dq_oe_n and not rwds_oe_n:
                    if (ptr >> 9) != (addr >> 10):
                        self.errors.append(f"write burst crosses a 1kB row at word 0x{ptr:06x}")
                    self.mem[ptr] = (dq_o >> 8) | ((dq_o & 0xff) << 8)
                    ptr += 1
                # Read data.
                if cycle >= self.latency:
                    if cmd == self.CMD_MR_READ:
                        rdata = self.mr.get(addr & 0xf, 0)
                    elif cmd == self.CMD_READ:
                        rdata = self.corrupt(ptr, self.mem.get(ptr, 0))
                        ptr  += 1
            if rdata is None:
                yield phy.rwds_i.eq(0)
                for q in range(4):
                    yield phy.dq_i[q].eq(0)
            else:
                # First DDR beat (low byte) with RWDS high, sampled twice per beat by the IDES4.
                yield phy.rwds_i.eq(0b0011)
                for q, byte in enumerate([rdata & 0xff, rdata & 0xff, rdata >> 8, rdata >> 8]):
                    yield phy.dq_i[q].eq(byte)
            yield

# PSRAM Controller ---------------------------------------------------------------------------------

class PSRAMControllerDUT(LiteXModule):
    def __init__(self):
        self.phy  = PSRAMSimPHY()
        self.ctrl = PSRAMController(self.phy, startup_cycles=16)


def psram_wait_ready(dut, timeout=2000):
    for _ in range(timeout):
        if (yield dut.ctrl.ready):
            return
        yield
    raise AssertionError("PSRAM controller not ready")


def psram_write(dut, addr, words):
    ctrl = dut.ctrl
    yield ctrl.addr.eq(addr)
    yield ctrl.burst_length.eq(2*len(words))
    yield ctrl.din.eq(words[0])
    yield ctrl.req_write.eq(1)
    yield
    yield ctrl.req_write.eq(0)
    # Standard FIFO-like feed: din moves to the next word on the cycle following write_next.
    i = 0
    while not (yield ctrl.done):
        if (yield ctrl.write_next):
            i += 1
            yield ctrl.din.eq(words[min(i, len(words) - 1)])
        yield
    assert i == len(words) - 1


def psram_read(dut, addr, nwords):
    ctrl = dut.ctrl
    yield ctrl.addr.eq(addr)
    yield ctrl.burst_length.eq(2*nwords)
    yield ctrl.req_read.eq(1)
    yield
    yield ctrl.req_read.eq(0)
    words = []
    while True:
        if (yield ctrl.dout_valid):
            words.append((yield ctrl.dout))
        if (yield ctrl.done):
            break
        yield
    return words


def test_psram_controller_init():
    """Startup: global reset then MR0/MR4/MR8 writes, ready once the vendor ID reads 0x0D."""
    dut   = PSRAMControllerDUT()
    model = PSRAMModel(dut.phy)

    def main():
        yield from psram_wait_ready(dut)
        assert (yield dut.ctrl.vendor_id) == 0x0d

    run_simulation(dut, [main(), model.generator()])
    assert [cmd for cmd, _ in model.commands[:4]] == [PSRAMModel.CMD_RESET] + [PSRAMModel.CMD_MR_WRITE]*3
    assert model.mr_writes == [(0, MR0), (4, MR4), (8, MR8)]
    mr_reads = [addr & 0xf for cmd, addr in model.commands if cmd == PSRAMModel.CMD_MR_READ]
    assert mr_reads == [0, 1, 2, 3, 4, 8]


def test_psram_controller_bad_vendor_id():
    """ready stays low when the vendor ID read back is not 0x0D."""
    dut   = PSRAMControllerDUT()
    model = PSRAMModel(dut.phy, vendor_id=0x05)

    def main():
        for _ in range(1500):
            assert (yield dut.ctrl.ready) == 0
            yield
        assert (yield dut.ctrl.vendor_id) == 0x05

    run_simulation(dut, [main(), model.generator()])


def test_psram_controller_bursts():
    """Write/read bursts (incl. 1kB row crossing on writes) at two read latencies."""
    for latency in [10, 12]:
        dut    = PSRAMControllerDUT()
        model  = PSRAMModel(dut.phy, latency=latency)
        bursts = [
            (0x000000, [0x1000 + i for i in range(8)]),
            (0x0003f0, [0xa500 + i for i in range(24)]),  # Crosses the 0x400 row boundary.
            (0x7ffc00, [0x5a00 ^ (i*0x0101) for i in range(4)]),
            (0x001236, [0xbeef]),
        ]
        reads = {}

        def main():
            yield from psram_wait_ready(dut)
            for addr, words in bursts:
                yield from psram_write(dut, addr, words)
                yield
            for addr, words in bursts:
                reads[addr] = yield from psram_read(dut, addr, len(words))
                yield
            # Back-to-back reads without idle gap.
            reads["b2b"] = []
            for addr, words in bursts[:2]:
                reads["b2b"] += yield from psram_read(dut, addr, len(words))

        run_simulation(dut, [main(), model.generator()])
        assert model.errors == []
        for addr, words in bursts:
            assert [model.mem.get((addr >> 1) + i) for i in range(len(words))] == words
            assert reads[addr] == words, (latency, hex(addr))
        assert reads["b2b"] == bursts[0][1] + bursts[1][1]

# PSRAM BIST ---------------------------------------------------------------------------------------

class PortRAM:
    """Port-level RAM model (arbiter granted port + shared ready/dout), for BIST tests."""
    def __init__(self, port, ctrl, corrupt_word=None):
        self.port         = port
        self.ctrl         = ctrl
        self.mem          = {}
        self.corrupt_word = corrupt_word
        self.bursts       = []

    @passive
    def generator(self):
        port, ctrl = self.port, self.ctrl
        yield ctrl.ready.eq(1)
        while True:
            if (yield port.request):
                rnw    = (yield port.rnw)
                addr   = (yield port.addr) >> 1
                nwords = (yield port.burst_length) // 2
                self.bursts.append((rnw, addr, nwords))
                yield ctrl.ready.eq(0)
                # write_next is asserted in the cycle din is sampled (as the PSRAM controller does).
                yield port.write_next.eq((not rnw) and (nwords > 1))
                yield
                for i in range(nwords):
                    if rnw:
                        data = self.mem.get(addr + i, 0)
                        if addr + i == self.corrupt_word:
                            data ^= 0x0100
                        yield ctrl.dout.eq(data)
                        yield port.dout_valid.eq(1)
                        yield port.done.eq(i == nwords - 1)
                        yield
                    else:
                        self.mem[addr + i] = (yield port.din)
                        yield port.write_next.eq(i + 1 < nwords - 1)
                        yield port.done.eq(i == nwords - 1)
                        yield
                yield port.dout_valid.eq(0)
                yield port.write_next.eq(0)
                yield port.done.eq(0)
                yield ctrl.ready.eq(1)
            yield


class PSRAMBISTDUT(LiteXModule):
    def __init__(self, burst_words):
        self.port  = Record(memory_port_layout())
        self.ready = Signal()
        self.dout  = Signal(16)
        self.bist  = PSRAMBIST(self.port, self, burst_words=burst_words)


def run_bist(corrupt_word=None, stop_on_fail=False, burst_words=512):
    dut = PSRAMBISTDUT(burst_words)
    ram = PortRAM(dut.port, dut, corrupt_word=corrupt_word)
    res = {}

    def main():
        for _ in range(60000):
            if (yield dut.bist.finished):
                break
            if stop_on_fail and (yield dut.bist.failed):
                break
            yield
        res["finished"] = (yield dut.bist.finished)
        res["failed"]   = (yield dut.bist.failed)

    run_simulation(dut, [main(), ram.generator()])
    return res, ram


def test_psram_bist_pass():
    """BIST runs the 4 patterns (write + read passes) and passes on a good memory."""
    res, ram = run_bist()
    assert res == {"finished": 1, "failed": 0}
    # 4 patterns x (8 write + 8 read) bursts of 1kB, spread over the lower 4MB.
    assert len(ram.bursts) == 4*2*8
    assert [rnw for rnw, _, _ in ram.bursts[:16]] == [0]*8 + [1]*8
    assert [addr for _, addr, _ in ram.bursts[:8]] == [n*(512*512 + 1) for n in range(8)]
    assert all(nwords == 512 for _, _, nwords in ram.bursts)
    # Last pattern (zeros) written in memory.
    assert set(ram.mem.values()) == {0x0000}


def test_psram_bist_fail():
    """BIST flags a single corrupted bit."""
    res, ram = run_bist(corrupt_word=3*(512*512 + 1) + 100, stop_on_fail=True)
    assert res["failed"] == 1

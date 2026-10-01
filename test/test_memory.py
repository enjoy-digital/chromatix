#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litex.gen.sim import run_simulation, passive

from litex.soc.interconnect import stream

from litex.soc.cores.ram.opi_psram import opi_psram_cmd_layout, opi_psram_wdata_layout
from litex.soc.cores.ram.opi_psram import opi_psram_rdata_layout

from chromatix.gateware.memory import memory_port_layout, MultiPortRAMCtrl, BurstWriteFIFO
from chromatix.gateware.memory import GBBurstWrite, QSPIBurstWrite, QSPISlave, LineReader
from chromatix.gateware.memory import MemorySystem, PORT_BIST, PSRAMWishbone, PSRAMPortAdapter

# Helpers ------------------------------------------------------------------------------------------

class ClockDomainsWrapper(Module):
    def __init__(self, dut, domains):
        self.submodules.dut = dut
        for domain in domains:
            setattr(self.clock_domains, f"cd_{domain}", ClockDomain(domain))

class _DFFCSim(Module):
    """Cycle-based model of the GW5A DFFC (D flip-flop with asynchronous clear)."""
    def __init__(self, d, clk, clear, q):
        q_r = Signal()
        self.comb += q.eq(q_r & ~clear)
        sync  = getattr(self.sync, clk.cd)
        sync += q_r.eq(d & ~clear)


class SimInstance:
    """Special override lowering Gowin primitives for simulation: DFFC behavioral model."""
    @staticmethod
    def lower(instance):
        assert instance.of == "DFFC", instance.of
        ios = {item.name: item.expr for item in instance.items if isinstance(item, Instance._IO)}
        return _DFFCSim(ios["D"], ios["CLK"], ios["CLEAR"], ios["Q"])

# Native PSRAM Model -------------------------------------------------------------------------------

class NativePSRAM(Module):
    """LiteX OPIPSRAMCore native port stand-in: ``generator`` serves the accesses from ``mem``
    (16-bit words), with ``gap`` idle cycles between words."""
    def __init__(self, *args, **kwargs):
        self.cmd   = stream.Endpoint(opi_psram_cmd_layout(23))
        self.wdata = stream.Endpoint(opi_psram_wdata_layout())
        self.rdata = stream.Endpoint(opi_psram_rdata_layout())
        self.ready = Signal(reset=1)
        self.mem   = {}
        self.accesses = []
        self.errors   = []

    @passive
    def generator(self, gap=0):
        while True:
            yield self.cmd.ready.eq(1)
            yield
            if not (yield self.cmd.valid):
                continue
            yield self.cmd.ready.eq(0)
            we, addr, length = (yield self.cmd.we), (yield self.cmd.addr), (yield self.cmd.len)
            self.accesses.append((we, addr, length))
            if addr & 1:
                self.errors.append(f"Odd address 0x{addr:x}.")
            for n in range(length):
                word = addr//2 + n
                if we:
                    yield self.wdata.ready.eq(1)
                    yield
                    while not (yield self.wdata.valid):
                        self.errors.append("Write data underrun.")
                        yield
                    self.mem[word] = (yield self.wdata.data)
                    yield self.wdata.ready.eq(0)
                else:
                    yield self.rdata.valid.eq(1)
                    yield self.rdata.data.eq(self.mem.get(word, 0))
                    yield
                    yield self.rdata.valid.eq(0)
                for _ in range(gap):
                    yield

class PSRAMPortAdapterDUT(Module):
    def __init__(self, nports=2):
        self.submodules.ctrl    = ctrl  = MultiPortRAMCtrl(nports=nports)
        self.submodules.psram   = psram = NativePSRAM()
        self.submodules.adapter = PSRAMPortAdapter(ctrl, psram)

# Multi-Port RAM Controller ------------------------------------------------------------------------

def test_multiport_ram_ctrl_arbitration():
    """Latched requests are all served, one burst at a time, with per-port write_next/done routing."""
    dut    = MultiPortRAMCtrl(nports=3)
    grants = []
    beats  = {i: 0 for i in range(3)}
    dones  = {i: 0 for i in range(3)}

    def requesters():
        for i, port in enumerate(dut.ports):
            yield port.rnw.eq(0)
            yield port.addr.eq(0x100*(i + 1))
            yield port.burst_length.eq(8)
        # Requests from ports 2 and 0 at once, port 1 slightly later.
        yield dut.ports[2].request.eq(1)
        yield dut.ports[0].request.eq(1)
        yield
        yield dut.ports[2].request.eq(0)
        yield dut.ports[0].request.eq(0)
        yield
        yield dut.ports[1].request.eq(1)
        yield
        yield dut.ports[1].request.eq(0)
        for _ in range(300):
            for i, port in enumerate(dut.ports):
                beats[i] += (yield port.write_next)
                dones[i] += (yield port.done)
            yield

    @passive
    def psram_controller():
        # Fake PSRAM controller: 4 write_next beats then done for each write burst.
        yield dut.ready.eq(1)
        while True:
            if (yield dut.req_write):
                grants.append((yield dut.addr))
                yield dut.ready.eq(0)
                for _ in range(4):
                    yield dut.write_next.eq(1)
                    yield
                    yield dut.write_next.eq(0)
                    yield
                yield dut.done.eq(1)
                yield
                yield dut.done.eq(0)
                yield dut.ready.eq(1)
            yield

    run_simulation(dut, [requesters(), psram_controller()])
    assert sorted(grants) == [0x100, 0x200, 0x300]
    assert beats == {0: 4, 1: 4, 2: 4}
    assert dones == {0: 1, 1: 1, 2: 1}

# Burst Write FIFO ---------------------------------------------------------------------------------

def test_burst_write_fifo_standard_read():
    """q is updated with the next word on the cycle following each read enable (non-FWFT)."""
    fifo  = BurstWriteFIFO("write", "read", depth=16)
    dut   = ClockDomainsWrapper(fifo, ["write", "read"])
    words = [0x1111*(i + 1) for i in range(8)]
    reads = []

    def writer():
        for word in words:
            yield fifo.we.eq(1)
            yield fifo.data.eq(word)
            yield
        yield fifo.we.eq(0)

    def reader():
        # Wait for the FIFO to fill through the CDC.
        for _ in range(64):
            yield
        assert (yield fifo.empty) == 0
        for _ in words:
            yield fifo.re.eq(1)
            yield
            yield fifo.re.eq(0)
            yield
            reads.append((yield fifo.q))
        assert (yield fifo.empty) == 1

    run_simulation(dut, {"write": writer(), "read": reader()}, clocks={"write": 10, "read": 7})
    assert reads == words

# GB Burst Write -----------------------------------------------------------------------------------

def burst_write_pop(port, n):
    """Arbiter/controller side of a burst write: pop n words (request, then write_next)."""
    words = []
    yield port.write_next.eq(1)
    yield
    for i in range(n):
        if i == n - 2:
            yield port.write_next.eq(0)
        words.append((yield port.din))
        yield
    yield port.write_next.eq(0)
    return words


def test_gb_burst_write():
    """Pixels (once RAM is ready at new line) are burst on the next line, FIFO flushed on VSync."""
    port   = Record(memory_port_layout())
    gbw    = GBBurstWrite(port)
    dut    = ClockDomainsWrapper(gbw, ["hclk", "xclk"])
    words  = [0x0100*(i + 1) + i for i in range(8)]
    bursts = []

    def hclk_gen():
        def pulse(sig):
            yield sig.eq(1)
            yield
            yield sig.eq(0)
            yield

        def write(data):
            for d in data:
                yield gbw.write.eq(1)
                yield gbw.data.eq(d)
                yield
            yield gbw.write.eq(0)
            for _ in range(16):
                yield

        yield gbw.address.eq(0x012340)
        # RAM not ready at new line: pixels dropped.
        yield from pulse(gbw.new_line)
        yield from write([0xdead]*4)
        # RAM ready at new line: FIFO empty, no request.
        yield gbw.ram_ready.eq(1)
        yield from pulse(gbw.new_line)
        yield from write([0xbeef]*3)
        # VSync rising edge: FIFO flushed.
        yield from pulse(gbw.vsync)
        for _ in range(16):
            yield
        yield from write(words)
        yield from pulse(gbw.new_line)
        for _ in range(64):
            yield

    @passive
    def xclk_gen():
        while True:
            if (yield port.request):
                addr = (yield port.addr)
                bursts.append((addr, (yield from burst_write_pop(port, len(words)))))
                assert (yield gbw.fifo.empty) == 1
            yield

    run_simulation(dut, {"hclk": hclk_gen(), "xclk": xclk_gen()}, clocks={"hclk": 13, "xclk": 10})
    assert bursts == [(0x012340, words)]

# QSPI Burst Write ---------------------------------------------------------------------------------

def test_qspi_burst_write():
    """Words written during a QSPI transfer are burst to the PSRAM on CS rising (if RAM is ready)."""
    port   = Record(memory_port_layout())
    qbw    = QSPIBurstWrite(port)
    dut    = ClockDomainsWrapper(qbw, ["qspi_n", "xclk"])
    words  = [0x1111*(i + 1) for i in range(6)]
    bursts = []

    def transfer(data, address):
        yield qbw.address.eq(address)
        yield qbw.cs.eq(0)
        for d in data:
            yield qbw.data_valid.eq(1)
            yield qbw.data.eq(d)
            yield
        yield qbw.data_valid.eq(0)
        yield
        yield qbw.cs.eq(1)
        for _ in range(32):
            yield

    def qspi_gen():
        yield qbw.cs.eq(1)
        yield
        yield from transfer(words, 0xff345678)
        yield from transfer(words[:2], 0x00000002)
        yield qbw.ram_ready.eq(0)
        yield from transfer(words[:2], 0x00000004)

    @passive
    def xclk_gen():
        # RAM ready once past the startup CS edge (cs is 0 at reset, as the QSPI pads are sampled).
        for _ in range(16):
            yield
        yield qbw.ram_ready.eq(1)
        while True:
            if (yield port.request):
                addr = (yield port.addr)
                n    = 2 if len(bursts) else len(words)
                bursts.append((addr, (yield from burst_write_pop(port, n))))
            yield

    run_simulation(dut, {"qspi_n": qspi_gen(), "xclk": xclk_gen()}, clocks={"qspi_n": 17, "xclk": 10})
    assert bursts == [(0x345678, words), (0x000002, words[:2])]

# QSPI Slave ---------------------------------------------------------------------------------------

def qspi_transfer(pads, address, data, command=0):
    """ESP32 QSPI transfer: command, length and address on MOSI, 3 dummy clocks, data nibbles on 4 lines."""
    bits  = [command] + [(len(data) >> (9 - i)) & 1 for i in range(10)]
    bits += [(address >> (31 - i)) & 1 for i in range(32)]
    bits += [0]*3
    yield pads.cs_n.eq(0)
    for bit in bits:
        yield pads.mosi.eq(bit)
        yield
    for byte in data:
        for nibble in [byte >> 4, byte & 0xf]:
            yield pads.mosi.eq((nibble >> 0) & 1)
            yield pads.miso.eq((nibble >> 1) & 1)
            yield pads.wp_n.eq((nibble >> 2) & 1)
            yield pads.hd.eq(  (nibble >> 3) & 1)
            yield
    yield pads.cs_n.eq(1)
    for _ in range(4):
        yield


def test_qspi_slave():
    """Address/data decoding, 16-bit word assembly, CS async reset and menu init (2nd transfer to 0)."""
    pads  = Record([("cs_n", 1), ("mosi", 1), ("miso", 1), ("wp_n", 1), ("hd", 1)])
    slave = QSPISlave(pads)
    dut   = ClockDomainsWrapper(slave, ["qspi", "qspi_n"])
    data  = [0x12, 0x34, 0x56, 0x78, 0x9a, 0xbc]
    log   = {"words": [], "addresses": [], "menu_init": []}

    def gen():
        yield pads.cs_n.eq(1)
        yield
        for address in [0x00c0ffee, 0x00000000, 0x00000000]:
            yield from qspi_transfer(pads, address, data)
            log["addresses"].append((yield slave.address))
            log["menu_init"].append((yield slave.menu_init))

    @passive
    def monitor():
        while True:
            if (yield slave.data_valid):
                log["words"].append((yield slave.data))
            yield

    # SPI mode 0: master drives on QSPI_CLK falling edges, the slave samples on rising edges.
    run_simulation(dut, {"qspi_n": [gen(), monitor()]}, clocks={"qspi": 10, "qspi_n": (10, 5)},
        special_overrides={Instance: SimInstance})
    assert log["addresses"] == [0x00c0ffee, 0x00000000, 0x00000000]
    assert log["menu_init"] == [0, 0, 1]
    assert log["words"] == [0x3412, 0x7856, 0xbc9a]*3

# Line Reader --------------------------------------------------------------------------------------

def test_line_reader():
    """Burst read requests at start of frame/end of line, line buffer streamed out with the pixels."""
    port     = Record(memory_port_layout())
    ram_dout = Signal(16)
    reader   = LineReader(port, ram_dout, base=0x10000, ram_ready=1)
    dut      = ClockDomainsWrapper(reader, ["hclk", "xclk"])
    lines    = [[(n << 12) + i for i in range(LineReader.LINE_DEPTH)] for n in range(2)]
    requests = []
    stream   = []

    def hclk_gen():
        # Start of frame.
        yield reader.vsync.eq(1)
        for _ in range(8):
            yield
        yield reader.vsync.eq(0)
        for _ in range(400):
            yield
        # Line: HSync pulse then valid pixels.
        yield reader.hsync.eq(1)
        yield
        for i in range(LineReader.LINE_DEPTH):
            yield reader.valid.eq(1)
            yield
            if i:
                stream.append((yield reader.data))
        yield reader.valid.eq(0)
        yield
        stream.append((yield reader.data))
        # End of line: HSync falling edge.
        yield reader.hsync.eq(0)
        for _ in range(400):
            yield

    @passive
    def xclk_gen():
        while True:
            if (yield port.request):
                requests.append((yield port.addr))
                yield
                for word in lines[len(requests) - 1]:
                    yield ram_dout.eq(word)
                    yield port.dout_valid.eq(1)
                    yield
                yield port.dout_valid.eq(0)
                yield port.done.eq(1)
                yield
                yield port.done.eq(0)
            yield

    run_simulation(dut, {"hclk": hclk_gen(), "xclk": xclk_gen()}, clocks={"hclk": 13, "xclk": 10})
    assert requests == [0x10000, 0x10000 + 2*LineReader.LINE_DEPTH]
    assert stream == lines[0]

# PSRAM Port Adapter -------------------------------------------------------------------------------

def test_psram_port_adapter():
    """Arbiter bursts -> native accesses: write_next N-1 times, done after the last written word or
    with the last read word, data in order."""
    for gap in [0, 3]:
        dut  = PSRAMPortAdapterDUT()
        port = dut.ctrl.ports[0]
        res  = {"write_next": 0, "done": [], "read": []}
        words = [0x1000 + i for i in range(6)]

        def main():
            # Write burst (12 bytes at 0x200).
            yield port.rnw.eq(0)
            yield port.addr.eq(0x200)
            yield port.burst_length.eq(12)
            yield port.din.eq(words[0])
            yield port.request.eq(1)
            yield
            yield port.request.eq(0)
            index = 0
            while True:
                if (yield port.write_next):
                    res["write_next"] += 1
                    index += 1
                    yield port.din.eq(words[index])
                if (yield port.done):
                    res["done"].append(("w", (yield port.write_next)))
                    break
                yield
            yield
            # Read burst.
            yield port.rnw.eq(1)
            yield port.request.eq(1)
            yield
            yield port.request.eq(0)
            while True:
                if (yield port.dout_valid):
                    res["read"].append((yield dut.ctrl.dout))
                if (yield port.done):
                    res["done"].append(("r", (yield port.dout_valid)))
                    break
                yield

        run_simulation(dut, [main(), dut.psram.generator(gap=gap)])
        assert res["write_next"] == len(words) - 1
        assert res["done"] == [("w", 0), ("r", 1)]
        assert res["read"] == words
        assert dut.psram.accesses == [(1, 0x200, 6), (0, 0x200, 6)]
        assert dut.psram.errors == []

# PSRAM Wishbone -----------------------------------------------------------------------------------

class PSRAMWishboneDUT(Module):
    def __init__(self, base, data_width, synchronous=False, fetch=1, buffers=0):
        self.submodules.ctrl    = ctrl  = ClockDomainsRenamer("xclk")(MultiPortRAMCtrl(nports=2))
        self.submodules.psram   = psram = ClockDomainsRenamer("xclk")(NativePSRAM())
        self.submodules.adapter = ClockDomainsRenamer("xclk")(PSRAMPortAdapter(ctrl, psram))
        self.submodules.bridge  = bridge = PSRAMWishbone(ctrl.ports[1], ctrl.dout, base=base,
            data_width=data_width, synchronous=synchronous, fetch=fetch, buffers=buffers)


def run_psram_wishbone(data_width, lines, base=0x400000, synchronous=False):
    dut = PSRAMWishboneDUT(base, data_width=data_width, synchronous=synchronous)
    res = {}

    def main():
        bus = dut.bridge.bus
        for adr, data in lines.items():
            yield from bus.write(adr, data)
        res["read"] = {}
        for adr in reversed(list(lines)):
            res["read"][adr] = (yield from bus.read(adr))

    # Synchronous: sys = xClk/2 from the same PLL (as pClk/xClk), else unrelated clocks.
    run_simulation(dut, {"sys": main(), "xclk": dut.psram.generator(gap=1)},
        clocks={"sys": 20 if synchronous else 80, "xclk": 10})
    assert res["read"] == lines
    assert dut.psram.errors == []
    return dut


def test_psram_wishbone():
    """128-bit Wishbone lines (sys) written/read back through the arbiter + PSRAM (xClk)."""
    base  = 0x400000
    lines = {0x000: 0x00112233445566778899aabbccddeeff, 0x001: 0x0123456789abcdef0f1e2d3c4b5a6978,
             0x3ff: 0xdeadbeefcafef00d5555aaaa12345678}
    dut   = run_psram_wishbone(128, lines, base)
    # Line n at PSRAM byte address base + 16*n (8 words, 16-bit word 0 = data[15:0]).
    for adr, data in lines.items():
        words = [dut.psram.mem[(base + 16*adr)//2 + i] for i in range(8)]
        assert words == [(data >> 16*i) & 0xffff for i in range(8)]


def test_psram_wishbone_64():
    """64-bit Wishbone lines (as used behind the SoC L2 cache)."""
    run_psram_wishbone(64, {0x000: 0x0011223344556677, 0x001: 0x8899aabbccddeeff, 0x7ff: 0xdeadbeefcafef00d})

def test_psram_wishbone_64_synchronous():
    """64-bit Wishbone lines, synchronous bridge (sys = xClk/2, CPU builds)."""
    run_psram_wishbone(64, {0x000: 0x0011223344556677, 0x001: 0x8899aabbccddeeff, 0x7ff: 0xdeadbeefcafef00d},
        synchronous=True)

# Memory System ------------------------------------------------------------------------------------

def test_memory_system_bist(monkeypatch):
    """PSRAM BIST through the arbiter and the PSRAM adapter (native PSRAM model), all patterns."""
    import chromatix.gateware.memory as memory
    monkeypatch.setattr(memory, "OPIPSRAMPHY",  lambda pads: Module())
    monkeypatch.setattr(memory, "OPIPSRAMCore", NativePSRAM)
    qspi_pads  = Record([("clk", 1), ("cs_n", 1), ("mosi", 1), ("miso", 1), ("wp_n", 1), ("hd", 1)])
    psram_pads = Record([("ce_n", 1), ("clk", 1), ("dq", 8), ("dqs", 1)])
    dut        = MemorySystem(qspi_pads, psram_pads)
    res        = {"reads": 0}

    def main():
        yield qspi_pads.cs_n.eq(1)
        for _ in range(200000):
            res["reads"] += (yield dut.ctrl.ports[PORT_BIST].dout_valid)
            if (yield dut.bist_done) or (yield dut.bist_failed):
                break
            yield
        res["done"]   = (yield dut.bist_done)
        res["failed"] = (yield dut.bist_failed)

    # Video domain (unused here) clocked slowly to speed up the simulation.
    run_simulation(dut, {"psram": [main(), dut.psram.generator()]},
        clocks={"psram": 10, "xclk": 10, "fclk": 5, "hclk": 100000}, special_overrides={Instance: SimInstance})
    assert res["done"]   == 1
    assert res["failed"] == 0
    assert res["reads"]  == 4*8*512 # 4 patterns, 8 read bursts of 512 words, checked by the BIST.
    assert dut.psram.errors == []
    # First access: 512-word write burst at 0, then bursts every 512*512 + 1 words.
    assert dut.psram.accesses[:2] == [(1, 0, 512), (1, 2*(512*512 + 1), 512)]

# Memory Counters ----------------------------------------------------------------------------------

def test_memory_counters():
    """Accesses/requests/busy cycles counting, longest request, clear."""
    from chromatix.gateware.memory import MemoryCounters
    access  = Signal()
    request = Signal()
    pending = Signal()
    dut     = MemoryCounters(access, request, pending)

    def gen():
        for i in range(3):
            yield access.eq(1)
            yield request.eq(1)
            yield pending.eq(1)
            yield
            yield access.eq(0)
            yield request.eq(0)
            for _ in range(4 + i):
                yield
            yield pending.eq(0)
            yield
        yield
        assert (yield dut.accesses.status) == 3
        assert (yield dut.requests.status) == 3
        assert (yield dut.busy.status) == (5 + 6 + 7)
        assert (yield dut.latency.status) == 7
        yield from dut.control.write(1)
        yield
        assert (yield dut.requests.status) == 0

    run_simulation(dut, gen())

def test_psram_wishbone_fetch_buffers():
    """Block fetch (4 x 64-bit) + 2 buffers: random reads/writes checked against a reference, buffer
    hits (fewer PSRAM requests than reads), writes updating the buffered copy."""
    import random
    random.seed(2)
    base = 0x080000
    dut  = PSRAMWishboneDUT(base, data_width=64, synchronous=True, fetch=4, buffers=2)
    ref  = {}
    res  = {"errors": [], "requests": 0, "reads": 0}

    @passive
    def count():
        while True:
            res["requests"] += (yield dut.bridge.request)
            yield

    def main():
        bus = dut.bridge.bus
        # Initial content.
        for adr in range(64):
            ref[adr] = random.getrandbits(64)
            yield from bus.write(adr, ref[adr])
        res["requests"] = 0
        # Sequential reads (hits after each block fetch).
        for adr in range(64):
            v = yield from bus.read(adr)
            res["reads"] += 1
            if v != ref[adr]: res["errors"].append(("seq", adr, v, ref[adr]))
        res["seq_requests"] = res["requests"]
        # Random reads/writes (buffered copies updated by the writes).
        for _ in range(300):
            adr = random.randrange(64)
            if random.random() < 0.3:
                ref[adr] = random.getrandbits(64)
                yield from bus.write(adr, ref[adr])
            else:
                v = yield from bus.read(adr)
                if v != ref[adr]: res["errors"].append(("rnd", adr, v, ref[adr]))

    run_simulation(dut, {"sys": [main(), count()], "xclk": dut.psram.generator(gap=1)},
        clocks={"sys": 10, "xclk": 10})
    assert res["errors"] == []
    assert res["seq_requests"] == 64//4 # One PSRAM read per 4-word block.


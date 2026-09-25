#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litex.gen.sim import run_simulation

from chromatix.gateware.psram  import PSRAMController
from chromatix.gateware.memory import memory_port_layout, MultiPortRAMCtrl, BurstWriteFIFO
from chromatix.gateware.memory import GBBurstWrite, QSPIBurstWrite, QSPISlave, LineReader
from chromatix.gateware.memory import MemorySystem, PORT_BIST

from test.test_psram import SimInstance, PSRAMModel

# Helpers ------------------------------------------------------------------------------------------

class ClockDomainsWrapper(Module):
    def __init__(self, dut, domains):
        self.submodules.dut = dut
        for domain in domains:
            setattr(self.clock_domains, f"cd_{domain}", ClockDomain(domain))

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

# Memory System ------------------------------------------------------------------------------------

def run_memory_system_bist(monkeypatch):
    """Run the PSRAM BIST of MemorySystem against PSRAMModel, up to the end of its first pattern."""
    # Short PSRAM startup delay for simulation.
    import chromatix.gateware.memory as memory
    monkeypatch.setattr(memory, "PSRAMController",
        lambda phy: PSRAMController(phy, startup_cycles=16))
    qspi_pads  = Record([("clk", 1), ("cs_n", 1), ("mosi", 1), ("miso", 1), ("wp_n", 1), ("hd", 1)])
    psram_pads = Record([("ce_n", 1), ("clk", 1), ("dq", 8), ("dqs", 1)])
    dut        = MemorySystem(qspi_pads, psram_pads)
    model      = PSRAMModel(dut.phy)
    res        = {"reads": 0}

    def main():
        yield qspi_pads.cs_n.eq(1)
        for _ in range(20000):
            res["reads"] += (yield dut.ctrl.ports[PORT_BIST].dout_valid)
            if (yield dut.bist_failed):
                break
            # First pattern written and read back: the BIST starts writing the second pattern.
            cmds = [cmd for cmd, _ in model.commands if cmd in [PSRAMModel.CMD_READ, PSRAMModel.CMD_WRITE]]
            if cmds[-1:] == [PSRAMModel.CMD_WRITE] and cmds.count(PSRAMModel.CMD_READ) == 8:
                break
            yield
        res["failed"] = (yield dut.bist_failed)
        res["cmds"]   = cmds

    # Video domain (unused here) clocked slowly to speed up the simulation.
    run_simulation(dut, {"xclk": [main(), model.generator()]}, clocks={"xclk": 10, "hclk": 100000},
        special_overrides={Instance: SimInstance})
    return res, model


def test_memory_system_bist(monkeypatch):
    """PSRAM BIST through the arbiter, the PSRAM controller and a behavioral PSRAM (first pattern)."""
    res, model = run_memory_system_bist(monkeypatch)
    assert res["failed"] == 0
    assert res["reads"]  == 8*512 # Read data checked by the BIST.
    # 8 write bursts (bursts 1-7 cross a 1kB row: 2 write commands), 8 read bursts.
    assert res["cmds"] == [PSRAMModel.CMD_WRITE]*15 + [PSRAMModel.CMD_READ]*8 + [PSRAMModel.CMD_WRITE]
    assert model.errors == []
    # First pattern (alternating 0x55aa/0xaa55) written to the 8 x 1kB bursts, split at 1kB rows.
    for n in range(8):
        base = n*(512*512 + 1)
        assert [model.mem[base + i] for i in range(4)] == [0x55aa, 0xaa55]*2
        assert model.mem[base + 511] == 0xaa55


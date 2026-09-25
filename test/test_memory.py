#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litex.gen.sim import run_simulation

from chromatix.gateware.memory import MultiPortRAMCtrl, BurstWriteFIFO

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

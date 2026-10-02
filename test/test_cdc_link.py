#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""CDCLink: debug bridge / application (UART + DMA) switch of the CDC byte stream, host touch."""

from migen import *

from litex.gen import *
from litex.gen.sim import run_simulation, passive

from litex.soc.interconnect import wishbone

from chromatix.gateware.cdc_link import CDCLink

MEM = [0x03020100 + 0x04040404*i for i in range(16)] # Bytes 0, 1, 2... in little endian words.

class DUT(LiteXModule):
    def __init__(self):
        self.link = CDCLink(clk_freq=16, release_time=1)
        self.mem  = wishbone.SRAM(4*len(MEM), init=MEM)
        self.comb += self.link.bus.connect(self.mem.bus)

@passive
def collect(endpoint, out):
    """Generator: always ready, collect the bytes of an endpoint."""
    while True:
        yield endpoint.ready.eq(1)
        if (yield endpoint.valid):
            out.append((yield endpoint.data))
        yield

def send(endpoint, data):
    for d in data:
        yield endpoint.valid.eq(1)
        yield endpoint.data.eq(d)
        yield
        while not (yield endpoint.ready):
            yield
    yield endpoint.valid.eq(0)

def test_cdc_link():
    dut   = DUT()
    link  = dut.link
    host  = []
    debug = []

    def gen():
        # Debug bridge (default): both directions.
        yield from send(link.source, [0x11, 0x22])
        yield link.debug_sink.valid.eq(1)
        yield link.debug_sink.data.eq(0x33)
        yield
        yield link.debug_sink.valid.eq(0)
        for _ in range(4):
            yield
        assert debug == [0x11, 0x22]
        assert host  == [0x33]

        # Application: host bytes to the UART RX, UART TX to the host.
        yield from link.control.write(1)
        yield from send(link.source, [0x44])
        for _ in range(4):
            yield
        assert (yield link.uart._rxempty.status) == 0
        assert (yield link.uart._rxtx.w) == 0x44
        yield from link.uart._rxtx.write(0x55)
        for _ in range(8):
            yield
        assert host[-1] == 0x55
        assert debug == [0x11, 0x22]

        # DMA: memory bytes in order (little endian).
        del host[:]
        assert (yield link.status.fields.dma_idle) == 1
        yield from link.dma._base.write(0)
        yield from link.dma._length.write(4*len(MEM))
        yield from link.dma._enable.write(1)
        for _ in range(400):
            yield
        assert host == list(range(4*len(MEM)))
        assert (yield link.status.fields.dma_idle) == 1
        yield from link.dma._enable.write(0)

        # Host 1200 baud touch: back to the debug bridge (debug session: DTR asserted).
        yield link.dtr.eq(1)
        yield link.touch.eq(1)
        yield
        yield
        assert (yield link.status.fields.touch) == 1
        yield from send(link.source, [0x66])
        for _ in range(4):
            yield
        assert debug == [0x11, 0x22, 0x66]
        # Debug session ends (DTR released): back to the application.
        yield link.dtr.eq(0)
        for _ in range(20):
            yield
        assert (yield link.status.fields.touch) == 0
        yield from send(link.source, [0x88])
        for _ in range(4):
            yield
        assert debug == [0x11, 0x22, 0x66]
        # New touch, then the application selects the link again.
        yield link.touch.eq(0)
        yield
        yield link.dtr.eq(1)
        yield link.touch.eq(1)
        yield
        yield
        assert (yield link.status.fields.touch) == 1
        yield from link.control.write(1)
        yield
        assert (yield link.status.fields.touch) == 0
        yield from send(link.source, [0x77])
        for _ in range(4):
            yield
        assert debug == [0x11, 0x22, 0x66]

    run_simulation(dut, [gen(), collect(link.sink, host), collect(link.debug_source, debug)])

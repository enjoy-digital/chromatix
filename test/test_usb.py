#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

import random

from migen import *

from litex.gen.sim import run_simulation

from chromatix.gateware.usb import ColorSpaceConvertor, VideoFIFO
from chromatix.gateware.usb import CSC_COEFFICIENTS, CSC_FRAC_BITS, CSC_LATENCY

# Color Space Convertor ----------------------------------------------------------------------------

def csc_model(r, g, b):
    out = []
    for a, b_, c, s in CSC_COEFFICIENTS:
        coefs = [round(k*(2**CSC_FRAC_BITS)) for k in (a, b_, c)]
        v = (coefs[0]*r + coefs[1]*g + coefs[2]*b + (s << CSC_FRAC_BITS) + (1 << (CSC_FRAC_BITS - 1))) >> CSC_FRAC_BITS
        out.append(min(max(v, 0), 255))
    return out

def test_csc_conversion():
    """RGB -> YCbCr matches the BT.601 model, with CSC_LATENCY cycles latency."""
    dut    = ColorSpaceConvertor()
    pixels = [(0, 0, 0), (255, 255, 255), (255, 0, 0), (0, 255, 0), (0, 0, 255)]
    pixels += [tuple(random.randrange(256) for _ in range(3)) for _ in range(32)]
    outs   = []

    def gen():
        yield dut.I_rst_n.eq(1)
        for r, g, b in pixels + [(0, 0, 0)]*(CSC_LATENCY + 2):
            yield dut.I_din0.eq(r)
            yield dut.I_din1.eq(g)
            yield dut.I_din2.eq(b)
            yield dut.I_dinvalid.eq(1)
            yield
            if (yield dut.O_doutvalid):
                outs.append([(yield dut.O_dout0), (yield dut.O_dout1), (yield dut.O_dout2)])

    run_simulation(dut, gen())
    assert outs[:len(pixels)] == [csc_model(*p) for p in pixels]
    # Reference points (limited range BT.601): black/white Y = 16/235, neutral chroma.
    assert outs[0] == [16, 128, 128]
    assert outs[1] == [235, 128, 128]

# Video FIFO ---------------------------------------------------------------------------------------

def test_video_fifo_level_and_fwft():
    """FWFT data order and read-side level (Rnum) / Almost_Full."""
    fifo  = VideoFIFO(depth=64)
    dut   = fifo # write/read domains clocked directly by the simulator.
    data  = list(range(1, 41))
    reads = []

    def writer():
        for _ in range(8):
            yield
        for d in data:
            yield fifo.Data.eq(d)
            yield fifo.WrEn.eq(1)
            yield
        yield fifo.WrEn.eq(0)

    def reader():
        yield fifo.AlmostFullTh.eq(32)
        for _ in range(200):
            yield
        assert (yield fifo.Rnum) == len(data)
        assert (yield fifo.Almost_Full) == 1
        for _ in data:
            reads.append((yield fifo.Q))
            yield fifo.RdEn.eq(1)
            yield
            yield fifo.RdEn.eq(0)
            yield
        for _ in range(8):
            yield
        assert (yield fifo.Rnum) == 0
        assert (yield fifo.Almost_Full) == 0

    run_simulation(dut, {"write": writer(), "read": reader()}, clocks={"write": 20, "read": 7})
    assert reads == data

def test_video_fifo_design_domains_reset():
    """clock_pins=False (as integrated): Almost_Empty and Reset (resynchronized in both domains)."""
    fifo = VideoFIFO(depth=64, clock_pins=False)

    def read_level(n):
        for _ in range(n):
            yield
        return ((yield fifo.Rnum), (yield fifo.Almost_Empty), (yield fifo.Empty))

    def writer():
        for _ in range(8):
            yield
        for d in [0x11, 0x22, 0x33]:
            yield fifo.Data.eq(d)
            yield fifo.WrEn.eq(1)
            yield
        yield fifo.WrEn.eq(0)
        for _ in range(64):
            yield
        yield fifo.Reset.eq(1)
        for _ in range(8):
            yield
        yield fifo.Reset.eq(0)
        for _ in range(64):
            yield
        yield fifo.Data.eq(0x44)
        yield fifo.WrEn.eq(1)
        yield
        yield fifo.WrEn.eq(0)

    def reader():
        assert (yield from read_level(40)) == (3, 0, 0)
        assert (yield fifo.Q) == 0x11
        yield fifo.RdEn.eq(1)
        yield
        yield
        yield fifo.RdEn.eq(0)
        assert (yield from read_level(4)) == (1, 1, 0)
        assert (yield fifo.Q) == 0x33
        # Reset (FIFO flushed, level cleared).
        assert (yield from read_level(60)) == (0, 1, 1)
        # Operational again after reset.
        assert (yield from read_level(80)) == (1, 1, 0)
        assert (yield fifo.Q) == 0x44

    run_simulation(fifo, {"write": writer(), "read": reader()}, clocks={"write": 10, "read": 10})

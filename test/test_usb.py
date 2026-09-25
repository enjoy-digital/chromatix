#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

import random

from migen import *

from litex.gen.sim import run_simulation

from chromatix.gateware.usb import ColorSpaceConvertor, VideoFIFO, FixedPointDivider
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

# Fixed Point Divider ------------------------------------------------------------------------------

def test_fixed_point_divider():
    """quotient_out = floor(dividend*8/divisor), used as CDC UART baudrate divider."""
    dut = FixedPointDivider()
    cases = [(60_000_000*4, 115200), (60_000_000*4, 1_000_000), (60_000_000*4, 9600), (12345, 7)]
    results = []

    def gen():
        for dividend, divisor in cases:
            yield dut.dividend.eq(dividend)
            yield dut.divisor.eq(divisor)
            yield dut.start.eq(1)
            # Wait for a full computation with these inputs.
            completes = 0
            while completes < 2:
                yield
                completes += (yield dut.complete)
            yield
            results.append((yield dut.quotient_out))

    run_simulation(dut, gen())
    assert results == [((dividend*8)//divisor) & 0xffffffff for dividend, divisor in cases]
    # 60MHz / 115200 = 520.83 -> divider_value = 520, fraction = 3 (quarters).
    assert (results[0] >> 5) == 520

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

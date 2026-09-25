#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
UVC video helpers (replacements for the Gowin CSC and FIFO IPs used by the original design):

- ColorSpaceConvertor : SDTV Computer RGB -> YCbCr (Gowin CSC color model 1).
- VideoFIFO           : 2048x8 async FWFT FIFO with read-side level and flags.
"""

from migen import *
from migen.genlib.fifo import AsyncFIFOBuffered
from migen.genlib.cdc  import MultiReg, GrayCounter

from litex.gen import *

# Color Space Convertor ----------------------------------------------------------------------------

# SDTV Computer RGB to YCbCr (BT.601), 10-bit fractional coefficients (Gowin CSC Color Model 1).
CSC_COEFFICIENTS = [
    # A (R)   B (G)   C (B)   S (Offset)
    ( 0.257,  0.504,  0.098,  16), # Y.
    (-0.148, -0.291,  0.439, 128), # Cb.
    ( 0.439, -0.368, -0.071, 128), # Cr.
]
CSC_FRAC_BITS = 10
CSC_LATENCY   = 6

class ColorSpaceConvertor(LiteXModule):
    """RGB888 -> YCbCr (8-bit), CSC_LATENCY cycles latency on data and valid."""
    def __init__(self, clock_pins=True):
        self.I_rst_n     = Signal()
        self.I_clk       = Signal()
        self.I_din0      = Signal(8) # R.
        self.I_din1      = Signal(8) # G.
        self.I_din2      = Signal(8) # B.
        self.I_dinvalid  = Signal()
        self.O_dout0     = Signal(8) # Y.
        self.O_dout1     = Signal(8) # Cb.
        self.O_dout2     = Signal(8) # Cr.
        self.O_doutvalid = Signal()

        # # #

        # Clock domain from the I_clk pin (standalone module) or "sys" from the design.
        if clock_pins:
            self.cd_sys = ClockDomain(reset_less=True)
            self.comb += self.cd_sys.clk.eq(self.I_clk)

        din   = [self.I_din0, self.I_din1, self.I_din2]
        douts = [self.O_dout0, self.O_dout1, self.O_dout2]
        for (a, b, c, s), dout in zip(CSC_COEFFICIENTS, douts):
            coefs  = [round(k*(2**CSC_FRAC_BITS)) for k in (a, b, c)]
            offset = (s << CSC_FRAC_BITS) + (1 << (CSC_FRAC_BITS - 1)) # Offset + rounding.
            # Stage 1: products.
            products = [Signal((20, True)) for _ in range(3)]
            self.sync += [p.eq(Cat(d, 0) * coef) for p, d, coef in zip(products, din, coefs)]
            # Stage 2: sum.
            total = Signal((22, True))
            self.sync += total.eq(products[0] + products[1] + products[2] + offset)
            # Stage 3: scale/saturate.
            result = Signal(8)
            value  = total >> CSC_FRAC_BITS
            self.sync += [
                If(value < 0,
                    result.eq(0),
                ).Elif(value > 255,
                    result.eq(255),
                ).Else(
                    result.eq(value),
                )
            ]
            # Stages 4-6: delay to match the Gowin CSC latency.
            pipe = result
            for _ in range(CSC_LATENCY - 3):
                _pipe = Signal(8)
                self.sync += _pipe.eq(pipe)
                pipe = _pipe
            self.comb += dout.eq(pipe)

        # Valid.
        valid = self.I_dinvalid
        for _ in range(CSC_LATENCY):
            _valid = Signal()
            self.sync += _valid.eq(valid & self.I_rst_n)
            valid = _valid
        self.comb += self.O_doutvalid.eq(valid)

    def get_ios(self):
        return {self.I_rst_n, self.I_clk, self.I_din0, self.I_din1, self.I_din2, self.I_dinvalid,
            self.O_dout0, self.O_dout1, self.O_dout2, self.O_doutvalid}

# Video FIFO ---------------------------------------------------------------------------------------

class VideoFIFO(LiteXModule):
    """
    2048x8 asynchronous FWFT FIFO with read-side level (Rnum) and almost empty/full flags.

    Almost_Full/Almost_Empty are computed from the read-side level (only used in the read domain).
    """
    def __init__(self, depth=2048, clock_pins=True):
        self.Data         = Signal(8)
        self.Reset        = Signal()
        self.WrClk        = Signal()
        self.RdClk        = Signal()
        self.WrEn         = Signal()
        self.RdEn         = Signal()
        self.Rnum         = Signal(13)
        self.Almost_Empty = Signal()
        self.Almost_Full  = Signal()
        self.AlmostFullTh = Signal(12)
        self.Q            = Signal(8)
        self.Empty        = Signal()
        self.Full         = Signal()

        # # #

        # Clock domains (write/read) from the WrClk/RdClk pins (standalone module) or from the design,
        # reset from Reset: resynchronized in the write domain, then in the read domain from the write
        # reset, so that the read side is released after the write pointer has been reset (otherwise
        # the read side can load a stale word from the synchronized pointer of the previous frame).
        rst_write = Signal()
        rst_read  = Signal()
        if clock_pins:
            self.cd_write = ClockDomain()
            self.cd_read  = ClockDomain()
            self.comb += [
                self.cd_write.clk.eq(self.WrClk),
                self.cd_read.clk.eq(self.RdClk),
                self.cd_write.rst.eq(rst_write),
                self.cd_read.rst.eq(rst_read),
            ]
        self.specials += [
            MultiReg(self.Reset, rst_write, odomain="write", reset=1),
            MultiReg(rst_write,  rst_read,  odomain="read",  reset=1),
        ]

        # FIFO.
        fifo = AsyncFIFOBuffered(8, depth)
        if not clock_pins:
            fifo = ResetInserter(["write", "read"])(fifo)
            self.comb += [fifo.reset_write.eq(rst_write), fifo.reset_read.eq(rst_read)]
        self.fifo = fifo
        self.comb += [
            fifo.din.eq(self.Data),
            fifo.we.eq(self.WrEn),
            self.Full.eq(~fifo.writable),
            self.Q.eq(fifo.dout),
            fifo.re.eq(self.RdEn),
            self.Empty.eq(~fifo.readable),
        ]

        # Read-side level: gray-coded write count resynchronized to the read domain.
        cnt_bits  = log2_int(depth) + 1
        wr_count  = ClockDomainsRenamer("write")(ResetInserter()(GrayCounter(cnt_bits)))
        self.submodules += wr_count
        self.comb += [
            wr_count.ce.eq(fifo.we & fifo.writable),
            wr_count.reset.eq(rst_write),
        ]
        wr_gray_r = Signal(cnt_bits)
        self.specials += MultiReg(wr_count.q, wr_gray_r, "read")
        wr_bin_r  = Signal(cnt_bits)
        bits      = [wr_gray_r[-1]] # Gray -> binary (MSB first), one Signal per bit.
        for i in reversed(range(cnt_bits - 1)):
            bit = Signal()
            self.comb += bit.eq(bits[-1] ^ wr_gray_r[i])
            bits.append(bit)
        self.comb += wr_bin_r.eq(Cat(*reversed(bits)))
        rd_count  = Signal(cnt_bits)
        self.sync.read += If(rst_read, rd_count.eq(0)).Elif(fifo.re & fifo.readable, rd_count.eq(rd_count + 1))
        level = Signal(cnt_bits)
        self.comb += level.eq(wr_bin_r - rd_count)
        self.sync.read += [
            self.Rnum.eq(level),
            self.Almost_Full.eq(level >= self.AlmostFullTh),
            self.Almost_Empty.eq(level <= 1),
        ]

    def get_ios(self):
        return {self.Data, self.Reset, self.WrClk, self.RdClk, self.WrEn, self.RdEn, self.Rnum,
            self.Almost_Empty, self.Almost_Full, self.AlmostFullTh, self.Q, self.Empty, self.Full}

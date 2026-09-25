#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
LiteX/Migen replacements for the Gowin IPs used by the legacy USB (UVC/UAC/CDC) subsystem.

Each core is generated as a standalone Verilog module with the exact name/ports of the Gowin IP it
replaces, so usbuvcuart_top.v is used unchanged:

- Color_Space_Convertor_Top : Gowin CSC (encrypted), SDTV Computer RGB -> YCbCr.
- fifo_video                : Gowin FIFO (encrypted), 2048x8 async FWFT FIFO with read level.
- Fixed_Point_Divider_Top   : Gowin divider (encrypted), CDC UART baudrate divider.
- Gowin_PLL_UVC             : Gowin PLL wrapper, 24MHz -> 960MHz/60MHz USB PHY clocks.
"""

import os

from migen import *
from migen.genlib.fifo import AsyncFIFOBuffered
from migen.genlib.cdc  import MultiReg, PulseSynchronizer, GrayCounter

from litex.gen import *

from litex.soc.cores.clock.gowin_gw5a import GW5APLL

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
    def __init__(self):
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
    def __init__(self, depth=2048):
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

        # Clock domains (write/read), reset from Reset (resynchronized in each domain).
        self.cd_write = ClockDomain()
        self.cd_read  = ClockDomain()
        self.comb += [
            self.cd_write.clk.eq(self.WrClk),
            self.cd_read.clk.eq(self.RdClk),
        ]
        self.specials += [
            MultiReg(self.Reset, self.cd_write.rst, odomain="write", reset=1),
            MultiReg(self.Reset, self.cd_read.rst,  odomain="read",  reset=1),
        ]

        # FIFO.
        self.fifo = fifo = AsyncFIFOBuffered(8, depth)
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
        wr_count  = ClockDomainsRenamer("write")(GrayCounter(cnt_bits))
        self.submodules += wr_count
        self.comb += wr_count.ce.eq(fifo.we & fifo.writable)
        wr_gray_r = Signal(cnt_bits)
        self.specials += MultiReg(wr_count.q, wr_gray_r, "read")
        wr_bin_r  = Signal(cnt_bits)
        self.comb += wr_bin_r[-1].eq(wr_gray_r[-1])
        for i in reversed(range(cnt_bits - 1)):
            self.comb += wr_bin_r[i].eq(wr_bin_r[i + 1] ^ wr_gray_r[i])
        rd_count  = Signal(cnt_bits)
        self.sync.read += If(fifo.re & fifo.readable, rd_count.eq(rd_count + 1))
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

# Fixed Point Divider ------------------------------------------------------------------------------

class FixedPointDivider(LiteXModule):
    """
    Sequential divider: quotient_out = floor(dividend * 2**frac_bits / divisor) (32-bit).

    Continuously restarts while start is high; complete pulses when quotient_out is updated.
    """
    def __init__(self, frac_bits=3):
        self.clk          = Signal()
        self.dividend     = Signal(32)
        self.divisor      = Signal(32)
        self.start        = Signal()
        self.quotient_out = Signal(32)
        self.complete     = Signal()

        # # #

        self.cd_sys = ClockDomain(reset_less=True)
        self.comb += self.cd_sys.clk.eq(self.clk)

        nbits     = 32 + frac_bits
        remainder = Signal(33)
        quotient  = Signal(nbits)
        numerator = Signal(nbits)
        divisor   = Signal(32)
        count     = Signal(max=nbits + 1)
        busy      = Signal()
        diff      = Signal(34)
        shifted   = Signal(33)

        self.comb += [
            shifted.eq(Cat(numerator[-1], remainder[:32])),
            diff.eq(shifted - divisor),
        ]
        self.sync += [
            self.complete.eq(0),
            If(~busy,
                If(self.start,
                    busy.eq(1),
                    count.eq(nbits),
                    numerator.eq(self.dividend << frac_bits),
                    divisor.eq(self.divisor),
                    remainder.eq(0),
                    quotient.eq(0),
                )
            ).Else(
                numerator.eq(numerator << 1),
                If(diff[33], # Negative: keep shifted remainder.
                    remainder.eq(shifted),
                    quotient.eq(Cat(0, quotient[:-1])),
                ).Else(
                    remainder.eq(diff),
                    quotient.eq(Cat(1, quotient[:-1])),
                ),
                count.eq(count - 1),
                If(count == 1,
                    busy.eq(0),
                    self.complete.eq(1),
                ),
            ),
            If(self.complete, self.quotient_out.eq(quotient[:32])),
        ]

    def get_ios(self):
        return {self.clk, self.dividend, self.divisor, self.start, self.quotient_out, self.complete}

# USB PLL ------------------------------------------------------------------------------------------

class USBPLL(LiteXModule):
    """24MHz -> 960MHz (clkout0, USB PHY oversampling) / 60MHz (clkout1, UTMI) using GW5APLL."""
    def __init__(self, platform):
        self.clkin   = Signal()
        self.reset   = Signal()
        self.lock    = Signal()
        self.clkout0 = Signal()
        self.clkout1 = Signal()

        # # #

        self.cd_usb_960 = ClockDomain(reset_less=True)
        self.cd_usb_60  = ClockDomain(reset_less=True)
        self.pll = pll = GW5APLL(devicename=platform.devicename, device=platform.device)
        pll.register_clkin(self.clkin, 24e6)
        pll.create_clkout(self.cd_usb_960, 960e6, with_reset=False)
        pll.create_clkout(self.cd_usb_60,   60e6, with_reset=False)
        self.comb += [
            pll.reset.eq(self.reset),
            self.lock.eq(pll.locked),
            self.clkout0.eq(self.cd_usb_960.clk),
            self.clkout1.eq(self.cd_usb_60.clk),
        ]

    def get_ios(self):
        return {self.clkin, self.reset, self.lock, self.clkout0, self.clkout1}

# Generation ---------------------------------------------------------------------------------------

def add_usb_ip_sources(platform, output_dir):
    """Generate the LiteX USB IP replacements as standalone Verilog modules and add them."""
    os.makedirs(output_dir, exist_ok=True)
    for name, module in [
        ("Color_Space_Convertor_Top", ColorSpaceConvertor()),
        ("fifo_video",                VideoFIFO()),
        ("Fixed_Point_Divider_Top",   FixedPointDivider()),
        ("Gowin_PLL_UVC",             USBPLL(platform)),
    ]:
        filename = os.path.join(output_dir, f"{name}.v")
        platform.get_verilog(module, ios=module.get_ios(), name=name).write(filename)
        platform.add_source(filename)

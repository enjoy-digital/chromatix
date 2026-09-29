#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litex.gen import *

from litex.soc.cores.clock.gowin_gw5a import GW5APLL

# CRG (Clock Reset Generator) ----------------------------------------------------------------------

class CRG(LiteXModule):
    """
    Clock Reset Generator for the Chromatic FPGA.

    Generates five clock domains from the 33.55432 MHz board crystal via a GW5A PLL:
    fClk (~134 MHz), pClk (~33.5 MHz), hClk (~16.8 MHz), gClk (~8.4 MHz), xClk (~67 MHz).

    The LiteX "sys" domain (CSR bus, debug bridge, CPU) is an alias of gClk (or of `sys_clk`), reset
    until the PLL locks.

    Parameters:
    - platform : GowinPlatform instance providing devicename, device, and clk_fpga pad.
    - sys_clk  : Clock domain aliased by "sys" ("gclk" or "pclk").
    """
    def __init__(self, platform, sys_clk="gclk"):
        assert sys_clk in ["gclk", "pclk"]
        self.cd_fclk = ClockDomain("fclk", reset_less=True)
        self.cd_pclk = ClockDomain("pclk", reset_less=True)
        self.cd_hclk = ClockDomain("hclk", reset_less=True)
        self.cd_gclk = ClockDomain("gclk", reset_less=True)
        self.cd_xclk = ClockDomain("xclk", reset_less=True)
        self.cd_sys  = ClockDomain("sys")

        # # #

        # Main PLL: 33.55432MHz -> fClk/pClk/hClk/gClk/xClk.
        clk_fpga = platform.request("clk_fpga")
        self.pll = pll = GW5APLL(
            devicename = platform.devicename,
            device     = platform.device,
        )
        self.comb += pll.reset.eq(0)
        pll.register_clkin(clk_fpga, 33.55432e6)
        # Exact integer ratios (CLKOUT0..4 order is used by the SDC generated clocks).
        pll.create_clkout(self.cd_fclk, 33.55432e6 * 4, with_reset=False)  # ~134.22 MHz (CLKOUT0).
        pll.create_clkout(self.cd_pclk, 33.55432e6,     with_reset=False)  # ~33.55 MHz  (CLKOUT1).
        pll.create_clkout(self.cd_hclk, 33.55432e6 / 2, with_reset=False)  # ~16.78 MHz  (CLKOUT2).
        pll.create_clkout(self.cd_gclk, 33.55432e6 / 4, with_reset=False)  # ~8.39 MHz   (CLKOUT3).
        pll.create_clkout(self.cd_xclk, 33.55432e6 * 2, with_reset=False)  # ~67.11 MHz  (CLKOUT4).

        # Sys: alias of gClk/pClk.
        self.comb += [
            self.cd_sys.clk.eq(getattr(self, f"cd_{sys_clk}").clk),
            self.cd_sys.rst.eq(~pll.locked),
        ]

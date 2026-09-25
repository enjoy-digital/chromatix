#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

import re

from migen import *

from litex.gen import *

from chromatix import Platform
from chromatix.gateware.crg import CRG

# CRG ----------------------------------------------------------------------------------------------

CLK_FPGA_FREQ = 33.55432e6

def test_crg_pll_config():
    """Exact integer ratios from the 33.55432MHz crystal on CLKOUT0..4 (order used by the SDC)."""
    platform = Platform()
    crg      = CRG(platform)
    config   = crg.pll.compute_config()
    vco_min, vco_max = crg.pll.vco_freq_range
    assert vco_min <= config["vco"] <= vco_max
    # fClk (x4), pClk (x1), hClk (/2), gClk (/4), xClk (x2).
    ratios = [4, 1, 1/2, 1/4, 2]
    for n, ratio in enumerate(ratios):
        assert config[f"diff{n}"] == 0
        assert config["vco"]/config[f"odiv{n}"] == CLK_FPGA_FREQ*ratio

def test_crg_elaboration():
    """The CRG elaborates: PLL instance, clock domains, sys = gClk held in reset until PLL lock."""
    class Top(LiteXModule):
        def __init__(self, platform):
            self.crg = CRG(platform)

    platform = Platform()
    top      = Top(platform)
    verilog  = str(platform.get_verilog(top))
    for name in ["fclk", "pclk", "hclk", "gclk", "xclk", "sys"]:
        assert f"{name}_clk" in verilog
    assert "PLLA" in verilog
    # CLKOUT0..4 order (referenced by the SDC generated clocks).
    for n, name in enumerate(["fclk", "pclk", "hclk", "gclk", "xclk"]):
        assert re.search(rf"\.CLKOUT{n}\s*\((\w+)\)", verilog).group(1) == \
               re.search(rf"assign\s+{name}_clk\s*=\s*(\w+);", verilog).group(1)
    assert re.search(r"assign\s+sys_clk\s*=\s*gclk_clk;", verilog)
    assert re.search(r"assign\s+sys_rst\s*=\s*\(~\w*locked\w*\);", verilog)

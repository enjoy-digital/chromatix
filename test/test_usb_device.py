#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
USB composite device elaboration (Gowin IPs/primitives can't be simulated): Verilog conversion with
the LiteX PHY, instances and UTMI/pad connections.
"""

import re

from migen import *

from chromatix import Platform
from chromatix.gateware.usb_device import USBDevice

# Helpers ------------------------------------------------------------------------------------------

class _Top(Module):
    def __init__(self, platform):
        self.clock_domains.cd_gclk = ClockDomain() # Video/audio samples clock (from the SoC).
        clk_24 = platform.request("clk_24")
        pads   = platform.request("usb")
        self.submodules.usb = USBDevice(platform, clk_24, pads)

def elaborate():
    platform = Platform()
    top      = _Top(platform)
    return str(platform.get_verilog(top, name="usb_device"))

def instance(verilog, module, name=r"\w+"):
    """Returns the port connections {port: net} of an instance."""
    m = re.search(rf"^{module}\s*(#\(.*?\)\s*)?{name}\s*\((.*?)\n\);", verilog, re.S | re.M)
    assert m is not None, f"{module} {name} not found"
    return dict(re.findall(r"\.(\w+)\s*\(([^()]*(?:\([^()]*\))?[^()]*)\)", m.group(2)))

# Tests --------------------------------------------------------------------------------------------

def test_usb_device_elaboration():
    """Controller + PLL + LiteX USB2PHY instantiated, UTMI connected between controller and PHY."""
    verilog = elaborate()
    ctrl    = instance(verilog, "USB_Device_Controller_Top", "u_usb_device_controller_top")
    assert ctrl["clk_i"].strip() == "phy_clk"
    assert "PLLA" in verilog and "usb_pll" in verilog

    utmi = {port: ctrl[f"utmi_{port}_{d}"].strip() for port, d in [("dataout", "o"), ("txvalid", "o"),
        ("txready", "i"), ("datain", "i"), ("linestate", "i"), ("xcvrselect", "o")]}
    for net in utmi.values():
        assert net and not re.fullmatch(r"\d+'d\d+", net) # Connected, not tied.

    # LiteX USB2PHY: GW5A SerDes primitives (lowered generic specials), SerDes clock net used by the
    # timing constraints.
    for primitive in ["DHCE", "CLKDIV", "IDES16", "OSER16", "ELVDS_IOBUF"]:
        assert re.search(rf"^{primitive}\b", verilog, re.M), primitive
    assert re.search(r"\busb_hs_clk\b", verilog)
    iobuf = instance(verilog, "ELVDS_IOBUF", "gw5a_elvds_iobuf")
    assert iobuf["IO"].strip() == "usb_d_p" and iobuf["IOB"].strip() == "usb_d_n"

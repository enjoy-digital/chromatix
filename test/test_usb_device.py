#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
USB composite device elaboration (Gowin primitives can't be simulated): Verilog conversion with the
LUNA device core (Amaranth -> Verilog), the LiteX PHY and the UTMI/pad connections.
"""

import os
import re

import pytest

from migen import *

from chromatix import Platform

pytest.importorskip("luna")

from chromatix.gateware.usb_device import USBDevice

# Helpers ------------------------------------------------------------------------------------------

class _Top(Module):
    def __init__(self, platform):
        self.clock_domains.cd_gclk = ClockDomain() # Video/audio samples clock (from the SoC).
        clk_24 = platform.request("clk_24")
        pads   = platform.request("usb")
        self.submodules.usb = USBDevice(platform, clk_24, pads)

def elaborate(output_dir):
    platform = Platform()
    platform.output_dir = str(output_dir)
    top      = _Top(platform)
    verilog  = str(platform.get_verilog(top, name="usb_device"))
    return platform, verilog

def instance(verilog, module, name=r"\w+"):
    """Returns the port connections {port: net} of an instance."""
    m = re.search(rf"^{module}\s*(#\(.*?\)\s*)?{name}\s*\((.*?)\n\);", verilog, re.S | re.M)
    assert m is not None, f"{module} {name} not found"
    return dict(re.findall(r"\.(\w+)\s*\(([^()]*(?:\([^()]*\))?[^()]*)\)", m.group(2)))

# Tests --------------------------------------------------------------------------------------------

def test_usb_device_elaboration(tmp_path):
    """LUNA core + PLL + LiteX USB2PHY instantiated, UTMI connected between the core and the PHY."""
    platform, verilog = elaborate(tmp_path)
    core = instance(verilog, "luna_usb_device")
    assert core["usb_clk"].strip() == "phy_clk"
    assert "PLLA" in verilog and "usb_pll" in verilog
    # Converted LUNA core registered as a source, no Gowin USB IP.
    sources = [os.path.basename(path) for path, language, library in platform.sources]
    assert "luna_usb_device.v" in sources
    assert "USB_Device_Controller_Top" not in verilog

    utmi = {port: core[f"utmi_{port}"].strip() for port in ["tx_data", "tx_valid", "tx_ready",
        "rx_data", "line_state", "xcvr_select"]}
    for net in utmi.values():
        assert net and not re.fullmatch(r"\d+'d\d+", net) # Connected, not tied.

    # LiteX USB2PHY: GW5A SerDes primitives (lowered generic specials), SerDes clock net used by the
    # timing constraints.
    for primitive in ["DHCE", "CLKDIV", "IDES16", "OSER16", "ELVDS_IOBUF"]:
        assert re.search(rf"^{primitive}\b", verilog, re.M), primitive
    assert re.search(r"\busb_hs_clk\b", verilog)
    iobuf = instance(verilog, "ELVDS_IOBUF", "gw5a_elvds_iobuf")
    assert iobuf["IO"].strip() == "usb_d_p" and iobuf["IOB"].strip() == "usb_d_n"

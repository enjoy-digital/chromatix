#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

import os
import importlib.util

import pytest

from litex.soc.integration.builder import Builder

from chromatix import Platform
from chromatix.gateware.sources import add_verilog_sources
from chromatix.gateware.usb     import add_usb_ip_sources

# Helpers ------------------------------------------------------------------------------------------

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

def load_target():
    spec   = importlib.util.spec_from_file_location("chromatix_target", os.path.join(ROOT, "chromatix.py"))
    target = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(target)
    return target

# SoC Elaboration ----------------------------------------------------------------------------------

@pytest.mark.parametrize("with_debug_bridge", [False, True])
def test_soc_elaboration(tmp_path, with_debug_bridge):
    """The SoC elaborates (Verilog/constraints/CSR map) with and without the debug bridge."""
    target   = load_target()
    platform = Platform()
    add_verilog_sources(platform)
    target.add_timing_constraints(platform)
    soc      = target.BaseSoC(platform, with_debug_bridge=with_debug_bridge)
    csr_csv  = os.path.join(tmp_path, "csr.csv")
    builder  = Builder(soc, output_dir=str(tmp_path), csr_csv=csr_csv)
    add_usb_ip_sources(platform, output_dir=os.path.join(tmp_path, "usb_ip"))
    builder.build(build_name="chromatic", run=False)
    verilog  = open(os.path.join(builder.gateware_dir, "chromatic.v")).read()
    assert "debug_ctrl_buttons" in open(csr_csv).read()
    assert ("uartbone" in verilog) == with_debug_bridge

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
from chromatix.gateware.sources import VERILOG_PATH, VERILOG_SOURCES
from chromatix.gateware.sources import add_verilog_sources

# Helpers ------------------------------------------------------------------------------------------

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

def load_target():
    spec   = importlib.util.spec_from_file_location("chromatix_target", os.path.join(ROOT, "chromatix.py"))
    target = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(target)
    return target

# Verilog Sources ----------------------------------------------------------------------------------

def test_verilog_sources():
    """All the Verilog/VHDL sources exist (MiSTer submodule initialized) and get the right language."""
    for source in VERILOG_SOURCES:
        assert os.path.exists(os.path.join(VERILOG_PATH, source)), source
    platform = Platform()
    add_verilog_sources(platform)
    sources  = {os.path.relpath(path, VERILOG_PATH): language for path, language, library in platform.sources}
    assert sorted(sources) == sorted(VERILOG_SOURCES)
    for source, language in sources.items():
        if source.endswith(".vhd"):
            assert language == "vhdl", source
        else:
            assert language in ["verilog", "systemverilog"], source

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
    builder.build(build_name="chromatic", run=False)
    with open(os.path.join(builder.gateware_dir, "chromatic.v"), encoding="utf-8") as f:
        verilog = f.read()
    with open(csr_csv, encoding="utf-8") as f:
        csrs = f.read()
    assert ("uartbone" in verilog) == with_debug_bridge
    # CSRs used by scripts/chromatic.py.
    for name in ["debug_ctrl_buttons", "debug_ctrl_status", "debug_ctrl_system_control", "debug_ctrl_volt",
        "debug_ctrl_adc_value", "debug_ctrl_volume", "debug_ctrl_pmic_sys_status"]:
        assert f"csr_register,{name}," in csrs, name
    assert "csr_base,identifier_mem," in csrs

def test_soc_bios_elaboration(tmp_path):
    """LiteX BIOS demo: VexRiscv + UART (USB CDC) + LCD terminal in place of the Game Boy core and a PSRAM
    main RAM (the BIOS software is not compiled here)."""
    target   = load_target()
    platform = Platform()
    target.add_timing_constraints(platform)
    soc      = target.BaseSoC(platform, with_bios=True)
    csr_csv  = os.path.join(tmp_path, "csr.csv")
    builder  = Builder(soc, output_dir=str(tmp_path), csr_csv=csr_csv, compile_software=False)
    builder.build(build_name="chromatic", run=False)
    with open(os.path.join(builder.gateware_dir, "chromatic.v"), encoding="utf-8") as f:
        verilog = f.read()
    with open(csr_csv, encoding="utf-8") as f:
        csrs = f.read()
    assert "VexRiscv" in verilog
    assert "emu_system_top" not in verilog
    assert "terminal" in verilog
    assert "csr_base,uart," in csrs
    assert "memory_region,main_ram,0x40000000,4194304,cached" in csrs
    assert platform.sources == [] or all("emu" not in path for path, _, _ in platform.sources)

# IO Constraints -----------------------------------------------------------------------------------

def test_soc_io_constraints(tmp_path):
    """Single-ended _p/_n pins (HDMI debug) are constrained individually, the USB pair as expected."""
    target   = load_target()
    platform = Platform()
    add_verilog_sources(platform)
    target.add_timing_constraints(platform)
    soc      = target.BaseSoC(platform)
    builder  = Builder(soc, output_dir=str(tmp_path), csr_csv=os.path.join(tmp_path, "csr.csv"))
    builder.build(build_name="chromatic", run=False)
    with open(os.path.join(builder.gateware_dir, "chromatic.cst"), encoding="utf-8") as f:
        cst = f.read()
    for name, pin in [("hdmi_clk_p", "C15"), ("hdmi_clk_n", "C16"), ("hdmi_d_p[0]", "D16"), ("hdmi_d_n[0]", "D15")]:
        assert f'IO_LOC "{name}" {pin};' in cst
        assert f'IO_PORT "{name}" IO_TYPE=LVCMOS33' in cst
    assert 'IO_LOC "usb_d_p" B11,A11;' in cst
    assert 'IO_PORT "usb_d_p" IO_TYPE=LVCMOS33D' in cst
    assert 'IO_PORT "usb_d_n"' not in cst
    # ESP32 IO0 open-drain with pull-up (driven high, it prevents the ESP32 SD card init).
    assert 'IO_PORT "esp32_ctrl_io0" IO_TYPE=LVCMOS33 DRIVE=8 PULL_MODE=UP PULL_STRENGTH=STRONG OPEN_DRAIN=ON;' in cst

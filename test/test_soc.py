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

def test_soc_app_elaboration(tmp_path):
    """CPU application: VexRiscv (I/D caches, no ROM: reset in the PSRAM main RAM) + PSRAM
    framebuffer/PCM audio in place of the Game Boy core, UARTBone + crossover UART on the USB CDC
    (switchable to an application UART + DMA), CPU UART to the ESP32."""
    target   = load_target()
    platform = Platform()
    target.add_timing_constraints(platform)
    soc      = target.BaseSoC(platform, with_app=True)
    csr_csv  = os.path.join(tmp_path, "csr.csv")
    builder  = Builder(soc, output_dir=str(tmp_path), csr_csv=csr_csv, compile_software=False)
    builder.build(build_name="chromatic", run=False)
    with open(os.path.join(builder.gateware_dir, "chromatic.v"), encoding="utf-8") as f:
        verilog = f.read()
    with open(csr_csv, encoding="utf-8") as f:
        csrs = f.read()
    assert "VexRiscv" in verilog and "VexRiscv_Lite" not in verilog
    assert "emu_system_top" not in verilog
    assert "uartbone" in verilog
    for name in ["uart", "esp32_uart", "usb_link"]:
        assert f"csr_base,{name}," in csrs, name
    for name in ["ctrl_reset", "framebuffer_line", "framebuffer_status", "pcm_data",
        "demo_buttons_status", "memory_counters_requests", "esp32_uart_rxtx", "usb_link_control",
        "usb_link_status", "usb_link_uart_rxtx", "usb_link_dma_base", "usb_link_dma_enable"]:
        assert f"csr_register,{name}," in csrs, name
    assert "constant,pcm_interrupt," in csrs
    assert f"memory_region,main_ram,0x40000000,{target.APP_RAM_SIZE},cached" in csrs
    assert "memory_region,framebuffer,0x90000000,65536,io" in csrs
    assert "memory_region,rom," not in csrs
    assert "constant,config_cpu_reset_addr,1073741824" in csrs

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

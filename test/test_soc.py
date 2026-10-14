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

def test_soc_core_elaboration(tmp_path):
    """MiSTer-core shell: the Game Boy core is replaced by the core shell (bring-up test pattern),
    not a CPU, and keeps the common peripherals (no debug bridge by default)."""
    target   = load_target()
    platform = Platform()
    target.add_timing_constraints(platform)
    soc      = target.BaseSoC(platform, with_core=True)
    csr_csv  = os.path.join(tmp_path, "csr.csv")
    builder  = Builder(soc, output_dir=str(tmp_path), csr_csv=csr_csv, compile_software=False)
    builder.build(build_name="chromatic", run=False)
    with open(os.path.join(builder.gateware_dir, "chromatic.v"), encoding="utf-8") as f:
        verilog = f.read()
    with open(csr_csv, encoding="utf-8") as f:
        csrs = f.read()
    assert "core_src_marker" in verilog    # The core shell test pattern source is present.
    assert "23040-words" in verilog        # The downscaler frame buffer (160x144) is present.
    assert "emu_system_top" not in verilog # Game Boy core replaced.
    assert "VexRiscv" not in verilog        # Not a CPU build.
    assert "uartbone" not in verilog        # No debug bridge by default.
    assert "csr_register,debug_ctrl_status," in csrs # Common peripherals kept.

def test_soc_core_nes_loader_elaboration(tmp_path):
    """--core nes --with-debug-bridge: the NES runtime ROM loader (NESLoaderCSR) is present, the Game
    Boy virtual cartridge is NOT (no GB core), and -- the key regression guard -- the PRG/CHR ROM
    BRAMs are not auto-mapped onto the CSR bus (which would add a third port and defeat BSRAM
    inference, overflowing the device with registers)."""
    from chromatix.gateware.cores.nes import NES_RTL_PATH
    if not os.path.exists(os.path.join(NES_RTL_PATH, "nes.v")):
        pytest.skip("NES core RTL not present (chromatix/verilog/cores/nes/, gitignored GPL drop-in).")
    target   = load_target()
    platform = Platform()
    target.add_timing_constraints(platform)
    soc      = target.BaseSoC(platform, with_core=True, core="nes", with_debug_bridge=True)
    assert soc.usb.uvc_frames == [(256, 240)] # UVC streams the NES native raster (no downscale).
    csr_csv  = os.path.join(tmp_path, "csr.csv")
    builder  = Builder(soc, output_dir=str(tmp_path), csr_csv=csr_csv, compile_software=False)
    builder.build(build_name="chromatic", run=False)
    with open(os.path.join(builder.gateware_dir, "chromatic.v"), encoding="utf-8") as f:
        verilog = f.read()
    with open(csr_csv, encoding="utf-8") as f:
        csrs = f.read()
    assert "uartbone" in verilog                         # Debug bridge present.
    assert "csr_register,nes_loader_control," in csrs    # Runtime ROM loader CSRs present.
    assert "csr_register,nes_loader_data," in csrs
    assert "vcart" not in csrs                            # No Game Boy virtual cartridge.
    import re
    assert not re.search(r"mem_(prg|chr)\[sram\d+_adr\]", verilog) # ROM BRAMs not CSR-mapped.

def test_soc_core_sms_elaboration(tmp_path):
    """--core sms: the SMS/Game Gear core glue (SMSCore + the `system`/`video` instances) elaborates.
    Skips if the gitignored SMS RTL is absent. (A fitting build additionally needs the cartridge in
    PSRAM behind a cache -- see doc/CORES.md -- so this only checks the structural wiring.)"""
    from chromatix.gateware.cores.sms import SMS_RTL_PATH
    if not os.path.exists(os.path.join(SMS_RTL_PATH, "system.v")):
        pytest.skip("SMS core RTL not present (chromatix/verilog/cores/sms/, gitignored GPL drop-in).")
    target   = load_target()
    platform = Platform()
    target.add_timing_constraints(platform)
    soc      = target.BaseSoC(platform, with_core=True, core="sms")
    assert soc.usb.uvc_frames == [(256, 192)] # UVC streams the SMS native raster (no downscale).
    builder  = Builder(soc, output_dir=str(tmp_path), csr_csv=os.path.join(tmp_path, "csr.csv"),
                       compile_software=False)
    builder.build(build_name="chromatic", run=False)
    with open(os.path.join(builder.gateware_dir, "chromatic.v"), encoding="utf-8") as f:
        verilog = f.read()
    assert "system " in verilog            # SMS DUT instantiated.
    assert "emu_system_top" not in verilog # Game Boy core replaced.

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

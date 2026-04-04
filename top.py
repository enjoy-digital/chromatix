#!/usr/bin/env python3

#
# This file is part of Chromatic FPGA LiteX Build.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

import os
import argparse

from migen import *

from litex.build.gowin.platform import GowinPlatform
from litex.build.generic_platform import *

# Platform -----------------------------------------------------------------------------------------

class Platform(GowinPlatform):
    device      = "GW5A-EV25UG256CC1/I0"
    devicename  = "GW5A-25"

    def __init__(self, toolchain="gowin"):
        GowinPlatform.__init__(self,
            device     = self.device,
            io         = [],
            connectors = [],
            toolchain  = toolchain,
            devicename = self.devicename,
        )

        # Gowin Build Options (from original build_evt1_x2.tcl).
        self.toolchain.options["top_module"]             = "top"
        self.toolchain.options["verilog_std"]             = "sysv2017"
        self.toolchain.options["vhdl_std"]                = "vhd2008"
        self.toolchain.options["rw_check_on_ram"]         = 1
        self.toolchain.options["use_sspi_as_gpio"]        = 1
        self.toolchain.options["power_on_reset_monitor"]  = 1
        self.toolchain.options["use_i2c_as_gpio"]         = 1
        self.toolchain.options["use_cpu_as_gpio"]         = 1
        self.toolchain.options["multi_boot"]              = 0
        self.toolchain.options["bit_format"]              = "bin"
        self.toolchain.options["bg_programming"]          = "jtag_sspi_qsspi"
        self.toolchain.options["use_mspi_as_gpio"]        = 1

    def finalize(self, fragment, *args, **kwargs):
        # Skip default clock domain handling (original top.v manages its own clocks).
        if not self.finalized:
            self.do_finalize(fragment, *args, **kwargs)
            self.finalized = True

    def do_finalize(self, fragment, *args, **kwargs):
        pass

# Source Files -------------------------------------------------------------------------------------

def add_sources(platform, base_path):
    """Add all RTL source files (matching the .gprj project file)."""
    src = os.path.join(base_path, "src")

    def add(path, language=None):
        full = os.path.join(src, path)
        if not os.path.exists(full):
            print(f"WARNING: Source file not found: {full}")
            return
        platform.add_source(full, language=language)

    # Gowin IP.
    add("fifo1k/fifo1k.v")
    add("gowin_adc/gowin_adc.v")
    add("gowin_pll_preevt/gowin_pll.v")

    # Top.
    add("top.v")

    # BSP - Board Support Package.
    add("rtl/BSP/ST7785_init.v")
    add("rtl/BSP/ST7785_panel_master.v")
    add("rtl/BSP/adc_wrap.v")
    add("rtl/BSP/aud_system_top.v")
    add("rtl/BSP/button_debounce.v")
    add("rtl/BSP/gb_burst_write.v")
    add("rtl/BSP/i2c_master.sv")
    add("rtl/BSP/mem_system_top.sv")
    add("rtl/BSP/mm_burst_read_to_stream.v")
    add("rtl/BSP/mm_burst_write.v")
    add("rtl/BSP/mpmc.v")
    add("rtl/BSP/polling_master.v")
    add("rtl/BSP/qspi_slave.v")
    add("rtl/BSP/system_monitor.sv")
    add("rtl/BSP/system_monitor_arbiter.sv")
    add("rtl/BSP/tlv320_init.v")
    add("rtl/BSP/vid_system_top.sv")
    add("rtl/BSP/vid_tpg.v")

    # BSP - UART.
    add("rtl/BSP/uart/fixed_point_divider/fixed_point_divider.v")
    add("rtl/BSP/uart/uart.v")
    add("rtl/BSP/uart_packet_wrapper_rx.sv")
    add("rtl/BSP/uart_packet_wrapper_tx.sv")

    # BSP - VHDL.
    add("rtl/BSP/MultiPortRamCtrl.vhd",    language="vhdl")
    add("rtl/BSP/PSRAMBIST_Burst.vhd",     language="vhdl")
    add("rtl/BSP/PSRAMController.vhd",      language="vhdl")
    add("rtl/BSP/overlayBatteryBack.vhd",   language="vhdl")
    add("rtl/BSP/overlayBatteryFront.vhd",  language="vhdl")
    add("rtl/BSP/overlayDebug.vhd",         language="vhdl")
    add("rtl/BSP/overlayTimerBack.vhd",     language="vhdl")
    add("rtl/BSP/overlayTimerFront.vhd",    language="vhdl")
    add("rtl/BSP/overlayTimerNumber.vhd",   language="vhdl")
    add("rtl/BSP/uart/uart_rx.vhd",         language="vhdl")
    add("rtl/BSP/uart/uart_tx.vhd",         language="vhdl")

    # BSP - Other.
    add("rtl/BSP/tlv320regs.hex")

    # EMU - Emulation Core.
    add("rtl/EMU/audio_filter.v")
    add("rtl/EMU/cart.v")
    add("rtl/EMU/emu_system_top.v")
    add("rtl/EMU/iir_filter.sv")

    # EMU - Game Boy Core (modified MiSTer).
    add("rtl/EMU/CORE/cheatcodes.sv")
    add("rtl/EMU/CORE/dpramV.v")
    add("rtl/EMU/CORE/dpram_difV.v")
    add("rtl/EMU/CORE/gb.v")
    add("rtl/EMU/CORE/link.v")
    add("rtl/EMU/CORE/sprites.v")
    add("rtl/EMU/CORE/video.v")
    add("rtl/EMU/CORE/videoBypass.v")

    # EMU - Game Boy Core VHDL.
    add("rtl/EMU/CORE/T80/GBse.vhd",           language="vhdl")
    add("rtl/EMU/CORE/T80/T80.vhd",             language="vhdl")
    add("rtl/EMU/CORE/T80/T80_MCode.vhd",       language="vhdl")
    add("rtl/EMU/CORE/bus_savestates.vhd",       language="vhdl")
    add("rtl/EMU/CORE/gbc_snd.vhd",             language="vhdl")
    add("rtl/EMU/CORE/speedcontrol.vhd",         language="vhdl")

    # EMU - MiSTer submodule VHDL.
    add("Gameboy_MiSTer/rtl/T80/T80_ALU.vhd",    language="vhdl")
    add("Gameboy_MiSTer/rtl/T80/T80_Pack.vhd",   language="vhdl")
    add("Gameboy_MiSTer/rtl/T80/T80_Reg.vhd",    language="vhdl")
    add("Gameboy_MiSTer/rtl/gb_savestates.vhd",   language="vhdl")
    add("Gameboy_MiSTer/rtl/gb_statemanager.vhd", language="vhdl")
    add("Gameboy_MiSTer/rtl/reg_savestates.vhd",  language="vhdl")

    # EMU - MiSTer submodule Verilog.
    add("Gameboy_MiSTer/rtl/hdma.v")
    add("Gameboy_MiSTer/rtl/timer.v")

    # USB Subsystem.
    add("rtl/USB/USBUVCUART/usbuvcuart_top.v")
    add("rtl/USB/USBUVCUART/Gowin_PLL_UVC/Gowin_PLL_UVC.v")
    add("rtl/USB/USBUVCUART/color_space_convertor/color_space_convertor.v")
    add("rtl/USB/USBUVCUART/fifo_video/fifo_video.v")
    add("rtl/USB/USBUVCUART/usb_video/usb_defs.v")
    add("rtl/USB/USBUVCUART/usb_video/usb_descriptor_video.v")
    add("rtl/USB/USBUVCUART/usb_video/uvc_defs.v")
    add("rtl/USB/USBUVCUART/usb_device_controller/usb_device_controller.v")
    add("rtl/USB/USBUVCUART/usb2_0_softphy/usb2_0_softphy_top.v")
    add("rtl/USB/USBUVCUART/usb2_0_softphy/usb2_0_softphy_name.v")
    add("rtl/USB/USBUVCUART/usb2_0_softphy/usb2_0_softphy_encryption.v")
    add("rtl/USB/USBUVCUART/usb2_0_softphy/static_macro_define.v")
    add("rtl/USB/USBUVCUART/uart/uart.v")
    add("rtl/USB/USBUVCUART/sync_fifo/usb_fifo.v")
    add("rtl/USB/USBUVCUART/sync_fifo/sync_rx_pkt_fifo.v")
    add("rtl/USB/USBUVCUART/sync_fifo/sync_tx_pkt_fifo.v")

# Constraints --------------------------------------------------------------------------------------

def add_constraints(platform, base_path):
    """Add original CST and SDC constraint files."""
    src       = os.path.join(base_path, "src")
    cst_path  = os.path.join(src, "board", "evt1_x2", "evt1_x2.cst")
    sdc_path  = os.path.join(src, "board", "evt1_x2", "evt1_x2.sdc")

    # Inject original CST content into LiteX-generated .cst file.
    with open(cst_path) as f:
        for line in f:
            line = line.strip()
            if line:
                platform.toolchain.additional_cst_commands.append(line)

    # Add original SDC as additional source.
    platform.toolchain.additional_tcl_commands.insert(0,
        f"add_file {sdc_path}"
    )

# Build --------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Chromatic FPGA LiteX Build")
    parser.add_argument("--build",      action="store_true", help="Build bitstream.")
    parser.add_argument("--no-compile", action="store_true", help="Generate build files without running toolchain.")
    parser.add_argument("--toolchain",  default="gowin",     help="FPGA toolchain (gowin or apicula).")
    args = parser.parse_args()

    # Create Platform.
    platform  = Platform(toolchain=args.toolchain)
    base_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "esp32t")

    # Add Sources/Constraints.
    add_sources(platform, base_path)
    add_constraints(platform, base_path)

    # Build.
    if args.build:
        platform.build(Module(),
            build_name = "chromatic",
            run        = not args.no_compile,
        )

if __name__ == "__main__":
    main()

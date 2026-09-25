#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

import os

# Verilog/VHDL Sources -----------------------------------------------------------------------------

VERILOG_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "verilog"))

# Subsystems still integrated as Verilog/VHDL Instances (see README migration table).
VERILOG_SOURCES = [
    # Gowin IP.
    "ip/gowin_adc/gowin_adc.v",

    # BSP: Video, Memory, System Monitor, Battery ADC.
    "bsp/ST7785_panel_master.v",
    "bsp/adc_wrap.v",
    "bsp/mm_burst_read_to_stream.v",
    "bsp/qspi_slave.v",
    "bsp/system_monitor.sv",
    "bsp/vid_system_top.sv",
    "bsp/PSRAMBIST_Burst.vhd",
    "bsp/PSRAMController.vhd",
    "bsp/overlayBatteryBack.vhd",
    "bsp/overlayBatteryFront.vhd",
    "bsp/overlayDebug.vhd",
    "bsp/overlayTimerBack.vhd",
    "bsp/overlayTimerFront.vhd",
    "bsp/overlayTimerNumber.vhd",
    "bsp/uart/uart_rx.vhd",
    "bsp/uart/uart_tx.vhd",

    # EMU: Emulation system.
    "emu/audio_filter.v",
    "emu/cart.v",
    "emu/emu_system_top.v",
    "emu/iir_filter.sv",

    # EMU: Game Boy Core (modified MiSTer).
    "emu/CORE/cheatcodes.sv",
    "emu/CORE/dpramV.v",
    "emu/CORE/dpram_difV.v",
    "emu/CORE/gb.v",
    "emu/CORE/link.v",
    "emu/CORE/sprites.v",
    "emu/CORE/video.v",
    "emu/CORE/videoBypass.v",
    "emu/CORE/T80/GBse.vhd",
    "emu/CORE/T80/T80.vhd",
    "emu/CORE/T80/T80_MCode.vhd",
    "emu/CORE/bus_savestates.vhd",
    "emu/CORE/gbc_snd.vhd",
    "emu/CORE/speedcontrol.vhd",

    # EMU: Game Boy Core (unmodified MiSTer submodule).
    "Gameboy_MiSTer/rtl/T80/T80_ALU.vhd",
    "Gameboy_MiSTer/rtl/T80/T80_Pack.vhd",
    "Gameboy_MiSTer/rtl/T80/T80_Reg.vhd",
    "Gameboy_MiSTer/rtl/gb_savestates.vhd",
    "Gameboy_MiSTer/rtl/gb_statemanager.vhd",
    "Gameboy_MiSTer/rtl/reg_savestates.vhd",
    "Gameboy_MiSTer/rtl/hdma.v",
    "Gameboy_MiSTer/rtl/timer.v",

    # USB: UVC + UAC + CDC (Gowin USB device controller + soft PHY).
    "usb/usbuvcuart_top.v",
    "usb/usb_video/usb_defs.v",
    "usb/usb_video/usb_descriptor_video.v",
    "usb/usb_video/uvc_defs.v",
    "usb/usb_device_controller/usb_device_controller_top.v",
    "usb/usb_device_controller/usb_device_controller.v",
    "usb/usb2_0_softphy/usb2_0_softphy_top.v",
    "usb/usb2_0_softphy/usb2_0_softphy_name.v",
    "usb/usb2_0_softphy/usb2_0_softphy.v",
    "usb/usb2_0_softphy/static_macro_define.v",
    "usb/uart/uart.v",
    "usb/sync_fifo/usb_fifo.v",
    "usb/sync_fifo/sync_rx_pkt_fifo.v",
    "usb/sync_fifo/sync_tx_pkt_fifo.v",
]

def add_verilog_sources(platform):
    for source in VERILOG_SOURCES:
        path = os.path.join(VERILOG_PATH, source)
        if not os.path.exists(path):
            raise FileNotFoundError(f"{path} not found (git submodule update --init?).")
        language = "vhdl" if source.endswith(".vhd") else None
        platform.add_source(path, language=language)

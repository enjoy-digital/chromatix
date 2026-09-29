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
]

def add_verilog_sources(platform):
    for source in VERILOG_SOURCES:
        path = os.path.join(VERILOG_PATH, source)
        if not os.path.exists(path):
            raise FileNotFoundError(f"{path} not found (git submodule update --init?).")
        language = "vhdl" if source.endswith(".vhd") else None
        platform.add_source(path, language=language)

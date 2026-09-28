#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
Simulation support (Verilator, see chromatix_sim.py): VHDL -> Verilog conversion of the Game Boy
core VHDL parts (GHDL), cartridge model and scripted buttons (sim-only Verilog helpers in verilog/sim).
"""

import os
import re
import shutil
import hashlib
import subprocess

from functools import reduce
from operator import or_

from migen import *

from litex.gen import *

from chromatix.gateware.sources import VERILOG_PATH, VERILOG_SOURCES

# VHDL -> Verilog (GHDL) ---------------------------------------------------------------------------

# VHDL entities instantiated from the Verilog sources (with their generics).
VHDL_TOPS = [
    ("GBse",            {}),
    ("gbc_snd",         {}),
    ("speedcontrol",    {}),
    ("gb_savestates",   {}),
    ("gb_statemanager", {"Softmap_SaveState_ADDR": 58720256, "Softmap_Rewind_ADDR": 33554432}),
]

# VHDL files in analysis order (packages first).
VHDL_ORDER = [
    "emu/CORE/bus_savestates.vhd",
    "Gameboy_MiSTer/rtl/reg_savestates.vhd",
    "Gameboy_MiSTer/rtl/T80/T80_Pack.vhd",
]

# Simulation-only Verilog sources.
SIM_VERILOG_PATH = os.path.join(VERILOG_PATH, "sim")

def vhdl_sources():
    vhdl = [s for s in VERILOG_SOURCES if s.endswith(".vhd")]
    return VHDL_ORDER + [s for s in vhdl if s not in VHDL_ORDER]

def verilog_sources():
    return [os.path.join(VERILOG_PATH, s) for s in VERILOG_SOURCES if not s.endswith(".vhd")]

def convert_vhdl(output_dir, ghdl="ghdl"):
    """
    Convert the Game Boy core VHDL entities to a single Verilog file (GHDL synthesis), cached on the
    VHDL sources content. Returns the Verilog file path.
    """
    sources = vhdl_sources()
    digest  = hashlib.sha1()
    for source in sources:
        with open(os.path.join(VERILOG_PATH, source), "rb") as f:
            digest.update(f.read())
    digest.update(repr(VHDL_TOPS).encode())
    os.makedirs(output_dir, exist_ok=True)
    verilog = os.path.join(output_dir, "gb_vhdl.v")
    stamp   = os.path.join(output_dir, "gb_vhdl.sha1")
    if os.path.exists(verilog) and os.path.exists(stamp) and open(stamp).read() == digest.hexdigest():
        return verilog
    if shutil.which(ghdl) is None:
        raise OSError("GHDL is required to convert the Game Boy core VHDL sources for simulation.")

    # Copy the sources: GHDL gives the ALU instance outputs of T80 ("alu" label) the names of the
    # ALU_* signals (duplicate Verilog declarations), rename the label.
    work_dir = os.path.join(output_dir, "ghdl")
    shutil.rmtree(work_dir, ignore_errors=True)
    os.makedirs(work_dir)
    files = []
    for source in sources:
        with open(os.path.join(VERILOG_PATH, source), encoding="latin-1") as f:
            vhdl = f.read()
        if source.endswith("T80/T80.vhd"):
            vhdl = re.sub(r"^(\s*)alu : T80_ALU", r"\1u_alu : T80_ALU", vhdl, flags=re.M)
        path = os.path.join(work_dir, source.replace("/", "_"))
        with open(path, "w", encoding="latin-1") as f:
            f.write(vhdl)
        files.append(path)
    ghdl_args = ["--std=08", "-fsynopsys", f"--workdir={work_dir}"]
    subprocess.run([ghdl, "-a", *ghdl_args, *files], check=True, stderr=subprocess.DEVNULL)

    # Synthesize each top, merge (dropping modules already emitted by a previous top).
    modules = {}
    for top, generics in VHDL_TOPS:
        cmd = [ghdl, "--synth", *ghdl_args, "--out=verilog"]
        cmd += [f"-g{k}={v}" for k, v in generics.items()]
        cmd += [top]
        out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
        for body, name in re.findall(r"(module\s+(\S+).*?endmodule\n)", out, re.S):
            modules.setdefault(name, body)
        # Generics are resolved by GHDL: accept (and ignore) the Verilog parameters.
        if generics:
            params = ", ".join(f"parameter {k} = {v}" for k, v in generics.items())
            modules[top] = modules[top].replace(f"module {top}\n", f"module {top} #({params})\n", 1)
    verilog_code = "\n".join(modules.values())
    verilog_code = re.sub(r"\bdo\b", "do_", verilog_code) # SystemVerilog keyword.
    with open(verilog, "w") as f:
        f.write(verilog_code)
    with open(stamp, "w") as f:
        f.write(digest.hexdigest())
    return verilog

# Cartridge ----------------------------------------------------------------------------------------

# Cartridge types (header 0x147): ROM only, MBC1, MBC5.
CART_TYPES_MBC1 = [0x01, 0x02, 0x03]
CART_TYPES_MBC5 = [0x19, 0x1a, 0x1b, 0x1c, 0x1d, 0x1e]
CART_TYPES      = [0x00, 0x08, 0x09] + CART_TYPES_MBC1 + CART_TYPES_MBC5

CART_ROM_SIZE   = 8*1024*1024

def check_rom(rom):
    if len(rom) < 0x150 or len(rom) > CART_ROM_SIZE:
        raise ValueError(f"Invalid ROM size ({len(rom)} bytes).")
    if rom[0x147] not in CART_TYPES:
        raise ValueError(f"Unsupported cartridge type 0x{rom[0x147]:02x} (ROM only, MBC1, MBC5).")

def rom_init(rom):
    """ROM padded to the cartridge ROM size, mirrored (as the unused upper bank bits of a real ROM)."""
    check_rom(rom)
    size = max(0x8000, 1 << (len(rom) - 1).bit_length())
    rom  = bytes(rom) + bytes([0xff]*(size - len(rom)))
    return rom*(CART_ROM_SIZE//size)

def write_rom_init(filename, rom):
    """Rewrite the cartridge ROM $readmemh file (to run another ROM without rebuilding)."""
    with open(filename, "w") as f:
        f.write("\n".join(f"{b:02x}" for b in rom_init(rom)) + "\n")

class SimCartridge(LiteXModule):
    """
    Game Boy cartridge model on the Chromatic cartridge bus (pClk domain): 8MB ROM (the ROM content is
    loaded at runtime from the "cart_rom" $readmemh file) with ROM only/MBC1/MBC5 banking, selected
    from the ROM header (0x147), and 128KB external RAM (16 banks for MBC5, bank 0 for MBC1).

    Reads are asynchronous (as a real cartridge, data on CART_D while RD is low), writes are taken
    while WR is low (MBC registers, RAM).
    """
    def __init__(self, rom, a, d, rd, wr, cs):
        # # #

        # ROM.
        self.specials.cart_rom = rom_mem = Memory(8, CART_ROM_SIZE, init=list(rom_init(rom)), name="cart_rom")
        rom_port = rom_mem.get_port(async_read=True)
        hdr_port = rom_mem.get_port(async_read=True)
        self.specials += rom_port, hdr_port

        # MBC type (header).
        mbc1 = Signal()
        mbc5 = Signal()
        self.comb += [
            hdr_port.adr.eq(0x147),
            mbc1.eq(reduce(or_, [hdr_port.dat_r == t for t in CART_TYPES_MBC1])),
            mbc5.eq(reduce(or_, [hdr_port.dat_r == t for t in CART_TYPES_MBC5])),
        ]

        # MBC registers.
        rom_bank = Signal(9, reset=1)
        ram_bank = Signal(4)
        ram_en   = Signal()
        self.sync += If(~wr & ~a[15],
            Case(a[13:15], {
                # 0x0000-0x1fff: RAM enable.
                0: ram_en.eq(d.i[0:4] == 0xa),
                # 0x2000-0x3fff: ROM bank (MBC5: 0x3000-0x3fff: bit 8).
                1: If(mbc5,
                        If(a[12], rom_bank[8].eq(d.i[0])).Else(rom_bank[0:8].eq(d.i)),
                    ).Elif(mbc1,
                        rom_bank[0:5].eq(Mux(d.i[0:5] == 0, 1, d.i[0:5])),
                    ),
                # 0x4000-0x5fff: RAM bank (MBC5), ROM bank bits 5-6 (MBC1).
                2: If(mbc5,
                        ram_bank.eq(d.i[0:4]),
                    ).Elif(mbc1,
                        rom_bank[5:7].eq(d.i[0:2]),
                    ),
            })
        )
        self.comb += If(a[14],
            rom_port.adr.eq(Cat(a[0:14], rom_bank)),
        ).Else(
            rom_port.adr.eq(a[0:14]),
        )

        # RAM (0xa000-0xbfff, CS low).
        self.specials.cart_ram = ram_mem = Memory(8, 16*8192, name="cart_ram")
        ram_port = ram_mem.get_port(write_capable=True, async_read=True)
        self.specials += ram_port
        ram_sel = Signal()
        self.comb += [
            ram_sel.eq(~cs & (a[13:16] == 0b101) & (ram_en | ~(mbc1 | mbc5))),
            ram_port.adr.eq(Cat(a[0:13], Mux(mbc5, ram_bank, 0))),
            ram_port.dat_w.eq(d.i),
            ram_port.we.eq(ram_sel & ~wr),
        ]

        # Data bus.
        self.comb += [
            d.oe.eq(~rd & wr & (~a[15] | ram_sel)),
            d.o.eq(Mux(a[15], ram_port.dat_r, rom_port.dat_r)),
        ]

# Buttons ------------------------------------------------------------------------------------------

# gb_buttons.v bit order.
BUTTONS = ["a", "b", "sel", "start", "dpad_up", "dpad_down", "dpad_left", "dpad_right"]

def parse_button_sequence(sequence):
    """"start@120+10,a@200" -> [("start", 120, 10), ("a", 200, 5)] (button@frame[+frames])."""
    presses = []
    for item in filter(None, (sequence or "").split(",")):
        m = re.fullmatch(r"(\w+)@(\d+)(?:\+(\d+))?", item.strip())
        if m is None or m.group(1) not in BUTTONS:
            raise ValueError(f"Invalid button press {item!r} (button@frame[+frames], buttons: {BUTTONS}).")
        presses.append((m.group(1), int(m.group(2)), int(m.group(3) or 5)))
    return presses

def button_events(presses):
    """Button presses -> sorted (frame, buttons mask) events (as loaded by gb_buttons.v)."""
    frames = sorted({f for _, start, length in presses for f in (start, start + length)})
    events = []
    for frame in frames:
        mask = 0
        for button, start, length in presses:
            if start <= frame < start + length:
                mask |= 1 << BUTTONS.index(button)
        events.append((frame, mask))
    return events

def write_button_events(filename, presses):
    with open(filename, "w") as f:
        for frame, mask in button_events(presses):
            f.write(f"{(frame << 8) | mask:08x}\n")
        f.write("ffffffff\n")

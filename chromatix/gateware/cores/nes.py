#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
NES core integration glue for the `--with-core nes` build (the worked example of the core-porting
methodology, doc/CORES.md).

This module is the *permissively licensed* LiteX glue only: it adapts a hosted NES core's native
interface to the Chromatic shell (core_shell.py). The NES core RTL itself (nand2mario's `nestang`,
GPL, derived from Ludvig Strigeus' fpganes) is **not** vendored here; it is dropped under
`chromatix/verilog/cores/nes/` and wired by `NESCore` (see the integration note at the bottom). So:
- NESVideoAdapter : NES `color[5:0]`/`cycle`/`scanline` -> the generic CoreVideo interface, through
  the public 2C02 palette (reduced to RGB555). Feeds CoreVideoDownscaler (256x240 -> 160x144).
- NESMemory       : PRG + CHR in BRAM (NROM-class, single-cycle: no PSRAM latency), routed by the
  core's 22-bit address (PRG at 0, CHR at 0x200000), with the loader write path and PRG mirroring.
- NESJoypad       : the console buttons -> the NES standard controller serial protocol.

These three are self-contained and unit-tested (test/test_nes.py); NESCore assembles them with the
NES RTL once it is present.
"""

import os

from migen import *
from migen.fhdl.specials import NO_CHANGE
from migen.genlib.cdc     import MultiReg

from litex.gen import *

from litex.soc.interconnect.csr      import CSRStorage, CSRField
from litex.soc.cores.clock.gowin_gw5a import GW5APLL

from chromatix.gateware.core_shell import CoreVideo, rgb555, add_core_clock_constraints
from chromatix.gateware.framebuffer import GBLCDTiming
from chromatix.gateware.cores.rom_cache import CoreROMCache

NES_RTL_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "verilog", "cores", "nes")
NES_PRG_BASE = 0x400000 # PRG-ROM placed here in the PSRAM (host upload, PSRAM-cartridge builds).
NES_MAPPERS  = [0, 1, 2, 3, 4, 7, 66] # Mapper subset of the PSRAM-cartridge builds: NROM, MMC1, UxROM,
                                      # CNROM, MMC3, AxROM, GxROM (all mappers do not fit the GW5A-25).
NES_CHR_KB   = 32 # CHR-ROM window (BRAM) of the PSRAM-cartridge builds (CNROM, small MMC1/MMC3).

# NES 2C02 palette (public hardware data), RGB888 -> reduced to RGB555 at import ------------------

NES_PALETTE_RGB24 = [
    0x545454, 0x001E74, 0x081090, 0x300088, 0x440064, 0x5C0030, 0x540400, 0x3C1800,
    0x202A00, 0x083A00, 0x004000, 0x003C00, 0x00323C, 0x000000, 0x000000, 0x000000,
    0x989698, 0x084CC4, 0x3032EC, 0x5C1EE4, 0x8814B0, 0xA01464, 0x982220, 0x783C00,
    0x545A00, 0x287200, 0x087C00, 0x007628, 0x006678, 0x000000, 0x000000, 0x000000,
    0xECEEEC, 0x4C9AEC, 0x787CEC, 0xB062EC, 0xE454EC, 0xEC58B4, 0xEC6A64, 0xD48820,
    0xA0AA00, 0x74C400, 0x4CD020, 0x38CC6C, 0x38B4CC, 0x3C3C3C, 0x000000, 0x000000,
    0xECEEEC, 0xA8CCEC, 0xBCBCEC, 0xD4B2EC, 0xECAEEC, 0xECAED4, 0xECB4B0, 0xE4C490,
    0xCCD278, 0xB4DE78, 0xA8E290, 0x98E2B4, 0xA0D6E4, 0xA0A2A0, 0x000000, 0x000000,
]

def _rgb24_to_555(c):
    return rgb555((c >> 19) & 0x1f, (c >> 11) & 0x1f, (c >> 3) & 0x1f)

NES_PALETTE_555 = [_rgb24_to_555(c) for c in NES_PALETTE_RGB24]

# NES Video Adapter --------------------------------------------------------------------------------

class NESVideoAdapter(LiteXModule):
    """
    NES core video (`color[5:0]` palette index, `cycle[8:0]`, `scanline[8:0]`, in the `cd` domain) ->
    the generic CoreVideo interface (ce / rgb555 / hblank / vblank): a 2C02 palette lookup and
    raster-edge detection (one ce per visible pixel, hblank/vblank from the raster position). The NES
    renders 256x240 visible (cycle 0..255, scanline 0..239); CoreVideoDownscaler scales it to 160x144.
    """
    def __init__(self, cd="nes"):
        self.color    = Signal(6)
        self.cycle    = Signal(9)
        self.scanline = Signal(9)
        self.video    = video = CoreVideo()

        # # #

        sync    = getattr(self.sync, cd)
        cycle_d = Signal(9)
        scan_d  = Signal(9)
        changed = Signal()
        sync += [cycle_d.eq(self.cycle), scan_d.eq(self.scanline)]
        self.comb += changed.eq((self.cycle != cycle_d) | (self.scanline != scan_d))

        pixel = Signal(15)
        self.comb += pixel.eq(Array([Constant(c, 15) for c in NES_PALETTE_555])[self.color])
        self.comb += [
            video.ce.eq(changed),
            video.hblank.eq(self.cycle >= 256),
            video.vblank.eq(self.scanline >= 240),
            video.r.eq(pixel[0:5]),
            video.g.eq(pixel[5:10]),
            video.b.eq(pixel[10:15]),
        ]

# NES Memory (PRG/CHR ROM + CPU RAM + nametable CIRAM, in BRAM) ------------------------------------

class NESMemory(LiteXModule):
    """
    The NES core's external memory for a hosted cartridge. The core maps everything into a unified
    22-bit space (see cart.sv / the mappers):
    - PRG-ROM at 0x000000: in BRAM (prg_init / the loader, NROM-size), or -- with `prg_cache` (a
      CoreROMCache on a PSRAM port) -- served from the PSRAM, for PRG up to 2 MB (MMC1/UxROM/...),
    - CHR-ROM at 0x200000 (read-only BRAM, chr_kb),
    - the 2 KB CPU RAM (incl. the stack $0100-$01FF) at 0x380000 (read/WRITE) -- cart.sv routes CPU
      $0000-$1FFF here; without a writable RAM the stack is lost and the first RTS crashes the CPU,
    - the 2 KB nametable CIRAM at 0x300000 (read/WRITE) -- cart.sv routes PPU VRAM here,
    - with `with_ram`: the 8 KB PRG-RAM/WRAM at 0x3C0000 ($6000-$7FFF, ex MMC1 saves, read/WRITE),
      and CHR-RAM: the mappers of the subset (MMC1, Mapper28) keep CHR-RAM in the CHR space
      (0x200000) and just allow PPU writes (chr_allow = flags[15], nes.v gates ppumem_write with it),
      so the CHR BRAM becomes PPU-writable. Its single write port is shared with the loader, which
      then runs in the `cd` domain (load_cd = cd).
    Channel B = CPU (PRG + CPU RAM + PRG-RAM), channel A = PPU (CHR + CIRAM + CHR-RAM). BRAM reads are
    registered on `read_cd` (a faster clock gives fresh data within one core cycle); writes on `cd`.

    PRG through the cache: nes.v issues a PRG read (cpumem_read, during the cart_pre PPU tick) ~4
    master clocks before the CPU samples the data. A read is latched and looked up; a hit is served
    in 2 cycles (no wait); only a read still unserved 2 cycles in asserts `stall` (wired to the
    patched nes.v `stall` input: the whole console freezes) until the line is filled from the PSRAM.
    So hits cost nothing and a miss pauses the NES coherently (the video path is decoupled).
    """
    def __init__(self, prg_kb=32, chr_kb=8, cd="nes", read_cd=None, prg_init=None, chr_init=None,
                 with_load=False, load_cd="sys", prg_cache=None, with_ram=False):
        read_cd = read_cd or cd
        # Channel B: CPU (PRG-ROM reads, CPU-RAM/PRG-RAM reads/writes).
        self.b_addr  = Signal(22)
        self.b_read  = Signal()
        self.b_write = Signal()
        self.b_din   = Signal(8) # Data from the CPU (writes).
        self.b_dout  = Signal(8) # Data to the CPU (reads).
        # Channel A: PPU (CHR-ROM reads, CIRAM/CHR-RAM reads/writes).
        self.a_addr  = Signal(22)
        self.a_read  = Signal()
        self.a_write = Signal()
        self.a_din   = Signal(8)
        self.a_dout  = Signal(8)
        # PRG size mask (BRAM PRG: 0x3FFF for 16 KB, 0x7FFF for 32 KB), from the mapper flags.
        self.prg_mask = Signal(15, reset=2**15 - 1)
        # Freeze the NES (PRG cache miss being served); 0 without prg_cache.
        self.stall    = Signal()
        # Runtime loader write path (host -> PRG/CHR BRAM, in the `load_cd` domain). Used only when
        # `with_load`: a second, write-only port on each ROM BRAM (so PRG/CHR become SDP BRAM: the NES
        # read port and the load write port are independent, no read-before-write hazard). The NES is
        # held in reset while loading, so the write (load_cd) and read (read_cd) never race. With
        # prg_cache, PRG goes to the PSRAM instead (host write_psram) and only CHR is loaded here.
        self.load_addr = Signal(15) # Byte address within PRG (sel=0) or CHR (sel=1).
        self.load_data = Signal(8)
        self.load_we   = Signal()
        self.load_sel  = Signal()   # 0 = PRG, 1 = CHR.

        # # #

        sync   = getattr(self.sync, cd)
        chr    = Memory(8, chr_kb*1024, init=chr_init)
        cpuram = Memory(8, 2048) # CPU RAM (0x380000), read/write.
        ciram  = Memory(8, 2048) # Nametable CIRAM (0x300000), read/write.
        chr_rd    = chr.get_port(clock_domain=read_cd)
        cpuram_rd = cpuram.get_port(clock_domain=read_cd)
        cpuram_wr = cpuram.get_port(write_capable=True, clock_domain=cd)
        ciram_rd  = ciram.get_port(clock_domain=read_cd)
        ciram_wr  = ciram.get_port(write_capable=True, clock_domain=cd)
        self.specials += chr, cpuram, ciram
        self.specials += chr_rd, cpuram_rd, cpuram_wr, ciram_rd, ciram_wr
        # NB: the Memories are kept in `specials` only, never as module attributes -- a Memory
        # exposed as `self.<name>` is auto-mapped onto the CSR bus by LiteX when a bus is present
        # (--with-debug-bridge), which adds a third port and defeats BSRAM inference (falls back to
        # registers: "DFF exceeds the resource limit").

        a_is_chr = (self.a_addr[20:22] == 0b10) # 0x200000: CHR (ROM, or RAM if the mapper allows).
        if with_ram:
            # One write port (cd) shared by the loader (core held in reset) and the PPU (CHR-RAM).
            assert load_cd == cd
            chr_wr = chr.get_port(write_capable=True, clock_domain=cd)
            self.specials += chr_wr
            ld_chr = self.load_we & self.load_sel
            self.comb += [
                chr_wr.adr.eq(Mux(ld_chr, self.load_addr, self.a_addr)[:len(chr_wr.adr)]),
                chr_wr.dat_w.eq(Mux(ld_chr, self.load_data, self.a_din)),
                chr_wr.we.eq(ld_chr | (self.a_write & a_is_chr)),
            ]
        elif with_load:
            chr_ld = chr.get_port(write_capable=True, clock_domain=load_cd)
            self.specials += chr_ld
            self.comb += [
                chr_ld.adr.eq(self.load_addr[:len(chr_ld.adr)]), chr_ld.dat_w.eq(self.load_data),
                chr_ld.we.eq(self.load_we & self.load_sel),
            ]

        # Region decode.
        if with_ram:
            b_is_ram    = (self.b_addr[18:22] == 0b1110)    # 0x380000: CPU RAM.
            b_is_prgram = (self.b_addr[18:22] == 0b1111)    # 0x3C0000: PRG-RAM.
            a_is_ciram  = (self.a_addr[17:22] == 0b11000)   # 0x300000: CIRAM.
        else:
            b_is_ram    = self.b_addr[21] & self.b_addr[20] & self.b_addr[19]
            b_is_prgram = 0
            a_is_ciram  = self.a_addr[20]

        # PRG-ROM.
        prg_data = Signal(8)
        if prg_cache is None:
            prg    = Memory(8, prg_kb*1024, init=prg_init)
            prg_rd = prg.get_port(clock_domain=read_cd)
            self.specials += prg, prg_rd
            if with_load:
                prg_ld = prg.get_port(write_capable=True, clock_domain=load_cd)
                self.specials += prg_ld
                self.comb += [
                    prg_ld.adr.eq(self.load_addr[:len(prg_ld.adr)]), prg_ld.dat_w.eq(self.load_data),
                    prg_ld.we.eq(self.load_we & ~self.load_sel),
                ]
            self.comb += [
                prg_rd.adr.eq(self.b_addr[:15] & self.prg_mask),
                prg_data.eq(prg_rd.dat_r),
            ]
        else:
            c       = prg_cache
            rd_prg  = Signal()
            rd_d    = Signal()
            pending = Signal()
            age     = Signal(2)
            addr_l  = Signal(21)
            self.comb += [
                rd_prg.eq(self.b_read & ~self.b_addr[21]),
                c.addr.eq(addr_l),
                c.rd.eq(pending),
                # Freeze only a read that is still unserved 2 cycles in (a hit is served by then).
                self.stall.eq(pending & age[1]),
            ]
            sync += [
                rd_d.eq(rd_prg),
                If(rd_prg & ~rd_d,
                    # New PRG read: latch its address, look it up.
                    addr_l.eq(self.b_addr[:21]),
                    pending.eq(1),
                    age.eq(0),
                ).Elif(pending,
                    If(~age[1], age.eq(age + 1)),
                    If(~c.stall,
                        # Hit (lookup stable / line filled): capture the byte, release.
                        prg_data.eq(c.data),
                        pending.eq(0),
                    ),
                ),
            ]

        # CPU reads, PPU reads, RAM writes.
        b_read_mux = Mux(b_is_ram, cpuram_rd.dat_r, prg_data)
        a_read_mux = Mux(a_is_ciram, ciram_rd.dat_r, chr_rd.dat_r)
        self.comb += [
            chr_rd.adr.eq(self.a_addr[:len(chr_rd.adr)]),
            cpuram_rd.adr.eq(self.b_addr[:11]),
            ciram_rd.adr.eq(self.a_addr[:11]),
            cpuram_wr.adr.eq(self.b_addr[:11]),
            cpuram_wr.dat_w.eq(self.b_din),
            cpuram_wr.we.eq(self.b_write & b_is_ram),
            ciram_wr.adr.eq(self.a_addr[:11]),
            ciram_wr.dat_w.eq(self.a_din),
            ciram_wr.we.eq(self.a_write & a_is_ciram),
        ]
        if with_ram:
            prgram = Memory(8, 8192) # PRG-RAM/WRAM (0x3C0000), read/write.
            prgram_rd = prgram.get_port(clock_domain=read_cd)
            prgram_wr = prgram.get_port(write_capable=True, clock_domain=cd)
            self.specials += prgram, prgram_rd, prgram_wr
            self.comb += [
                prgram_rd.adr.eq(self.b_addr[:13]),
                prgram_wr.adr.eq(self.b_addr[:13]),
                prgram_wr.dat_w.eq(self.b_din),
                prgram_wr.we.eq(self.b_write & b_is_prgram),
            ]
            b_read_mux = Mux(b_is_prgram, prgram_rd.dat_r, b_read_mux)
        self.comb += [
            self.b_dout.eq(b_read_mux),
            self.a_dout.eq(a_read_mux),
        ]

# NES Joypad (standard controller serial protocol) ------------------------------------------------

class NESJoypad(LiteXModule):
    """
    The console buttons -> the NES standard controller serial protocol. While `strobe` (joypad_out[0])
    is high, the button state is (re)latched; each `clock` (joypad_clock[0]) falling edge (end of the
    $4016 read) then shifts out the next button. Report order: A, B, Select, Start, Up, Down, Left, Right. `data` is
    joypad1_data[0] (1 = pressed, the MiSTer NES convention).
    """
    def __init__(self, cd="nes"):
        # Buttons (1 = pressed), in report order.
        self.a      = Signal()
        self.b      = Signal()
        self.select = Signal()
        self.start  = Signal()
        self.up     = Signal()
        self.down   = Signal()
        self.left   = Signal()
        self.right  = Signal()
        # NES controller lines.
        self.strobe = Signal() # joypad_out[0].
        self.clock  = Signal() # joypad_clock[0].
        self.data   = Signal() # joypad1_data[0].

        # # #

        sync    = getattr(self.sync, cd)
        buttons = Cat(self.a, self.b, self.select, self.start, self.up, self.down, self.left, self.right)
        shift   = Signal(8, reset=1)
        clock_d = Signal()
        sync += [
            clock_d.eq(self.clock),
            If(self.strobe,
                shift.eq(buttons),
            # Shift on the clock FALLING edge: nes.v holds joypad_clock high for the whole $4016 read
            # and the CPU samples the data during it -- shifting on the rising edge made each read
            # return the next button (Start seen as Select...). Same as nestang's top.
            ).Elif(~self.clock & clock_d,
                shift.eq(Cat(shift[1:], 1)), # Shift; official controllers return 1 after 8 reads.
            ),
        ]
        self.comb += self.data.eq(Mux(self.strobe, self.a, shift[0]))

# NES RTL sources ----------------------------------------------------------------------------------

def _nes_lower_rtl(dst_dir, mappers=None):
    """Apply the ChromatiX patches to a NES RTL tree already copied to dst_dir, in place (repo RTL
    untouched):
    - nes.v: an `input stall` freezing the whole console (the CPU/PPU/cart clock-enables and the
      divider/de-jitter state), so a cartridge served from the PSRAM through a cache can hold the
      core on a miss (nes.v has no wait input: it assumes fixed-latency SDRAM).
    - cart.sv (if `mappers`): the one-hot mapper decode is ANDed with a constant mask of the listed
      mapper numbers, so a runtime mapper number still lets synthesis prune every other mapper (all
      of them do not fit the GW5A-25)."""
    import re
    nesv = os.path.join(dst_dir, "nes.v")
    with open(nesv, encoding="utf-8") as f:
        text = f.read()
    text, n = re.subn(r"(module NES\(\s*\n\s*input\s+clk,)",
        r"\1\n\tinput         stall,          // ChromatiX: freeze the console (cartridge cache miss).",
        text, count=1)
    assert n == 1, "nes.v: NES port list not found"
    for ce, div in [("cpu_ce ", "cpu"), ("ppu_ce ", "ppu")]:
        old = f"wire {ce} = (div_{div} == div_{div}_n);"
        assert old in text, f"nes.v: {old} not found"
        text = text.replace(old, f"wire {ce} = (div_{div} == div_{div}_n) & ~stall;")
    old = "always @(posedge clk) begin\n\tif (~freeze_clocks | ~(div_ppu == (div_ppu_n - 1'b1))) begin"
    assert old in text, "nes.v: divider block not found"
    text = text.replace(old, "always @(posedge clk) if (~stall) begin\n"
        "\tif (~freeze_clocks | ~(div_ppu == (div_ppu_n - 1'b1))) begin")
    with open(nesv, "w", encoding="utf-8") as f:
        f.write(text)

    if mappers is not None:
        cartsv = os.path.join(dst_dir, "cart.sv")
        with open(cartsv, encoding="utf-8") as f:
            text = f.read()
        mask = sum(1 << m for m in mappers)
        old  = "reg [1023:0] me;"
        assert old in text, "cart.sv: mapper decode not found"
        text = text.replace(old, f"localparam [1023:0] CHROMATIX_MAPPER_MASK = 1024'h{mask:x};\n{old}")
        old  = "\tme[{flags[18:17],flags[7:0]}] = 1'b1;"
        assert old in text, "cart.sv: mapper one-hot not found"
        text = text.replace(old, old + "\n\tme = me & CHROMATIX_MAPPER_MASK; // ChromatiX: mapper subset.")
        with open(cartsv, "w", encoding="utf-8") as f:
            f.write(text)

def nes_rtl_lowered(dst_dir, src_dir=None, mappers=None):
    """Copy the NES RTL to dst_dir and apply the ChromatiX patches (see _nes_lower_rtl). Returns
    dst_dir. add_nes_sources builds from this so every build (hardware and simulation) gets them."""
    import shutil
    src_dir = src_dir or NES_RTL_PATH
    shutil.rmtree(dst_dir, ignore_errors=True)
    shutil.copytree(src_dir, dst_dir)
    _nes_lower_rtl(dst_dir, mappers=mappers)
    return dst_dir

def add_nes_sources(platform, mappers=None):
    """Add the vendored NES core RTL (chromatix/verilog/cores/nes/, GPL) to the build, through the
    ChromatiX patches (nes_rtl_lowered: stall input, optional mapper-subset mask)."""
    rtl_path = nes_rtl_lowered(os.path.join("build", "nes_src"), mappers=mappers)
    for sub, files in [
        ("",        ["nes.v", "ppu.v", "apu.v", "compat.v", "dpram.v", "game_loader.v",
                     "cart.sv", "EEPROM_24C0x.sv"]),
        ("t65",     ["T65_Pack.v", "T65_ALU.v", "T65_MCode.v", "T65.v"]),
        # Mapper set matching nestang's Primer 25K build (same GW5A-25). VRC/FDS (expansion-audio,
        # huge) are excluded; cart.sv tolerates their absence (the *_audio nets default to 0).
        ("mappers", ["generic.sv", "iir_filter.v", "misc.sv", "MMC1.sv", "MMC2.sv", "MMC3.sv",
                     "MMC5.sv", "Namco.sv", "Sachen.sv", "Sunsoft.sv", "JYCompany.sv"]),
    ]:
        for f in files:
            path     = os.path.join(rtl_path, sub, f)
            language = "systemverilog" if f.endswith(".sv") else "verilog"
            platform.add_source(path, language=language)

def _read_ines(path):
    """Read an iNES ROM into a list of byte values: a binary .nes file, or a whitespace-separated
    hex dump (.hex, as the bundled roms)."""
    if path.endswith(".hex"):
        with open(path) as f:
            return [int(tok, 16) for tok in f.read().split()]
    with open(path, "rb") as f:
        return list(f.read())

def _size_code(pages):
    """iNES page count -> mapper_flags size field (0:min .. 7), as in game_loader.v."""
    for i, n in enumerate([1, 2, 4, 8, 16, 32, 64]):
        if pages <= n:
            return i
    return 7

def parse_ines(path):
    """Parse an iNES ROM into (prg bytes, chr bytes, mapper_flags) for a direct BRAM load with a
    constant mapper_flags (so the hosted core's unused mappers are pruned at synthesis)."""
    data   = _read_ines(path)
    hdr    = data[:16]
    assert hdr[:4] == [0x4e, 0x45, 0x53, 0x1a], "not an iNES ROM"
    prg_n, chr_n = hdr[4], hdr[5]
    prg    = data[16:16 + prg_n*16384]
    chr    = data[16 + prg_n*16384:16 + prg_n*16384 + chr_n*8192]
    mapper = (hdr[6] >> 4) | (hdr[7] & 0xf0)
    flags  = (mapper
        | (_size_code(prg_n) << 8)
        | (_size_code(chr_n) << 11)
        | ((hdr[6] & 1)      << 14)   # mirroring.
        | ((1 if chr_n == 0 else 0) << 15)) # CHR RAM.
    return prg, chr, flags

# NES Video Downscaler (indexed frame buffer, palette on read) -------------------------------------

class NESVideoDownscaler(LiteXModule):
    """
    NES core video (color/cycle/scanline, `cd` domain) -> 160x144 Game Boy LCD interface (hClk),
    folding the adapter and downscaler: raster-edge detection, nearest-neighbor downscale of the
    256x240 raster, and a frame buffer that stores the **6-bit palette index** (not RGB) so it is
    half the BRAM; the 2C02 palette (RGB555) is applied on read. Single buffered (minor tearing).
    """
    def __init__(self, cd="nes", in_w=256, in_h=240, out_w=160, out_h=144):
        self.color     = Signal(6)
        self.cycle     = Signal(9)
        self.scanline  = Signal(9)
        self.gb_clkena = Signal()
        self.gb_data   = Signal(15)
        self.gb_mode   = Signal(2)
        self.gb_on     = Signal()
        self.gb_vsync  = Signal()

        # # #

        fb    = Memory(8, out_w*out_h)
        fb_wr = fb.get_port(write_capable=True, clock_domain=cd)
        fb_rd = fb.get_port(clock_domain="hclk")
        self.specials += fb, fb_wr, fb_rd

        # Raster -> active pixel / blanking (cd domain).
        wsync   = getattr(self.sync, cd)
        cycle_d = Signal(9)
        scan_d  = Signal(9)
        hblank  = Signal()
        vblank  = Signal()
        active  = Signal()
        hbl_d   = Signal()
        vbl_d   = Signal()
        self.comb += [
            hblank.eq(self.cycle >= in_w),
            vblank.eq(self.scanline >= in_h),
            active.eq(((self.cycle != cycle_d) | (self.scanline != scan_d)) & ~hblank & ~vblank),
        ]

        # Nearest-neighbor downscale (Bresenham on write), storing the palette index.
        x_out    = Signal(max=out_w + 1)
        y_out    = Signal(max=out_h + 1)
        row_base = Signal(max=out_w*out_h + 1)
        hacc     = Signal(max=in_w + out_w + 1)
        vacc     = Signal(max=in_h + out_h + 1)
        wsync += [
            cycle_d.eq(self.cycle), scan_d.eq(self.scanline),
            hbl_d.eq(hblank), vbl_d.eq(vblank),
            fb_wr.we.eq(0),
            If(vblank & ~vbl_d, # New frame.
                y_out.eq(0), row_base.eq(0), vacc.eq(0), x_out.eq(0), hacc.eq(in_w),
            ),
            If(active,
                If(hacc >= in_w,
                    hacc.eq(hacc - in_w + out_w),
                    If((x_out < out_w) & (y_out < out_h),
                        fb_wr.we.eq(1),
                        fb_wr.adr.eq(row_base + x_out),
                        fb_wr.dat_w.eq(self.color),
                        x_out.eq(x_out + 1),
                    ),
                ).Else(hacc.eq(hacc + out_w)),
            ),
            If(hblank & ~hbl_d, # End of line.
                x_out.eq(0), hacc.eq(in_w),
                # Only visible lines advance the output row (hblank also toggles on the vblank lines,
                # which would otherwise push the picture down by ~vblank*out_h/in_h rows).
                If(~vblank,
                    vacc.eq(vacc + out_h),
                    If((vacc + out_h) >= in_h,
                        vacc.eq(vacc + out_h - in_h),
                        If(y_out < out_h, y_out.eq(y_out + 1), row_base.eq(row_base + out_w)),
                    ),
                ),
            ),
        ]

        # Read side (hClk): scan out, palette index -> RGB555.
        self.timing = timing = GBLCDTiming()
        self.comb += [
            fb_rd.adr.eq(timing.line*out_w + timing.x),
            self.gb_clkena.eq(timing.gb_clkena),
            self.gb_data.eq(Array([Constant(c, 15) for c in NES_PALETTE_555])[fb_rd.dat_r[:6]]),
            self.gb_mode.eq(timing.gb_mode),
            self.gb_vsync.eq(timing.gb_vsync),
            self.gb_on.eq(1),
        ]

# Built-in ROM (replaces nestang's GameData, no $readmemh) -----------------------------------------

class NESBuiltinROM(LiteXModule):
    """Streams a built-in iNES ROM to GameLoader (indata/indata_clk), asserting `downloading` while
    streaming — the LiteX equivalent of nestang's GameData, initialized from a .hex at build time."""
    def __init__(self, hex_path, cd="nes"):
        data = _read_ines(hex_path)
        self.downloading = Signal(reset=1)
        self.odata       = Signal(8)
        self.odata_clk   = Signal()

        # # #

        mem    = Memory(8, len(data), init=data)
        port   = mem.get_port(clock_domain=cd)
        self.specials += mem, port
        sync   = getattr(self.sync, cd)
        addr   = Signal(max=len(data) + 1)
        phase  = Signal() # 2 cycles per byte.
        self.comb += [port.adr.eq(addr), self.odata.eq(port.dat_r)]
        sync += [
            self.odata_clk.eq(0),
            If(self.downloading,
                phase.eq(~phase),
                If(phase,
                    self.odata_clk.eq(1), # Byte valid (1 cycle after addr), strobe it.
                    If(addr == (len(data) - 1),
                        self.downloading.eq(0),
                    ).Else(
                        addr.eq(addr + 1),
                    ),
                ),
            ),
        ]

# NES Loader CSR (host -> NESCore runtime loader, over the debug bridge) ---------------------------

class NESLoaderCSR(LiteXModule):
    """SoC-CSR front end to NESCore's runtime loader (needs --with-debug-bridge), driven by
    scripts/chromatic.py nes-load. Two layouts:
    - PSRAM cartridge (core.prg_cache): the host holds the core, writes PRG to the PSRAM
      (NES_PRG_BASE), streams CHR (sel=1) as 32-bit words, sets `flags` (mapper_flags[31:0], as
      game_loader.v), flushes the PRG cache and releases -- any game of the synthesized mapper subset.
    - BRAM cartridge (NROM only): PRG then CHR streamed to BRAM, config bits instead of flags.
    Reset defaults match the seed ROM, so the core boots it with no host interaction."""
    def __init__(self, core):
        if getattr(core, "prg_cache", None) is not None:
            self.control = CSRStorage(fields=[
                CSRField("hold",     size=1, offset=0,             description="Hold the NES in reset while loading."),
                CSRField("sel",      size=1, offset=1,             description="Load target: 1 = CHR (PRG is in the PSRAM)."),
                CSRField("addr_rst", size=1, offset=5, pulse=True, description="Reset the load address to 0 (before each section)."),
                CSRField("flush",    size=1, offset=6, pulse=True, description="Invalidate the PRG cache."),
            ])
            self.flags = CSRStorage(32, reset=core.ld_default_flags, description="mapper_flags[31:0] (iNES header, as game_loader.v).")
            self.comb += [
                core.cfg_flags.eq(self.flags.storage),
                # As the SMS loader: the sys (gClk) pulse spans 2+ nes cycles, seen by the cache.
                core.cart_flush.eq(self.control.fields.flush),
            ]
        else:
            self.control = CSRStorage(fields=[
                CSRField("hold",     size=1, offset=0,             description="Hold the NES in reset while loading."),
                CSRField("sel",      size=1, offset=1,             description="Load target: 0 = PRG, 1 = CHR."),
                CSRField("prg32k",   size=1, offset=2, reset=core.ld_default_prg32k,  description="PRG size: 0 = 16 KB, 1 = 32 KB."),
                CSRField("mirror_v", size=1, offset=3, reset=core.ld_default_mirror_v, description="Mirroring: 0 = horizontal, 1 = vertical."),
                CSRField("chr_ram",  size=1, offset=4, reset=core.ld_default_chr_ram,  description="CHR is RAM (iNES CHR page count 0)."),
                CSRField("addr_rst", size=1, offset=5, pulse=True, description="Reset the load address to 0 (before each section)."),
            ])
            self.comb += [
                core.cfg_prg32k.eq(self.control.fields.prg32k),
                core.cfg_mirror_v.eq(self.control.fields.mirror_v),
                core.cfg_chr_ram.eq(self.control.fields.chr_ram),
            ]
        self.data = CSRStorage(32, description="Write 4 bytes to the current target at the current address, then address += 4.")

        # # #

        self.comb += [
            core.ld_hold.eq(self.control.fields.hold),
            core.ld_sel.eq(self.control.fields.sel),
            core.ld_addr_rst.eq(self.control.fields.addr_rst),
            core.ld_word.eq(self.data.storage),
            core.ld_word_we.eq(self.data.re),
        ]

# NES Core -----------------------------------------------------------------------------------------

class NESCore(LiteXModule):
    """
    A hosted NES core on the Chromatic shell: a ~21.48 MHz clock (own PLL), the ROM loaded directly
    into PRG/CHR BRAM at build time with a constant mapper_flags (so the hosted core's ~50 unused
    mappers are pruned at synthesis), and the glue (indexed downscaler -> Game Boy LCD interface,
    APU -> shared audio bus, console buttons -> joypad). `buttons` has .a/.b/.select/.start/.up/.down/
    .left/.right (1 = pressed). Exposes the Game Boy LCD interface and left/right audio.

    Cartridge: NROM in BRAM (built-in ROM, or the NROM runtime loader); or -- with the loader and a
    PSRAM port (cart_port) -- PRG from the PSRAM through a cache (the console freezes on a miss) and
    a runtime mapper among NES_MAPPERS (the build's subset: all mappers do not fit the GW5A-25), CHR
    up to NES_CHR_KB in BRAM, plus CHR-RAM and PRG-RAM (see NESMemory).
    """
    def __init__(self, platform, clk, clk_freq, buttons, rom="nes15.hex", with_pll=True,
                 with_loader=False, cart_port=None, cart_port_dout=None, cart_ready=None):
        self.gb_clkena = Signal()
        self.gb_data   = Signal(15)
        self.gb_mode   = Signal(2)
        self.gb_on     = Signal()
        self.gb_vsync  = Signal()
        self.left      = Signal(16)
        self.right     = Signal(16)
        # Runtime loader interface (driven by SoC CSRs in the sys domain; see chromatix.py). Any
        # NROM game can be uploaded over the debug bridge without a rebuild: hold the core, stream
        # PRG then CHR, set the config bits, release. Unused (tied off) when with_loader=False.
        self.ld_hold     = Signal() # Hold the NES in reset while loading.
        self.ld_sel      = Signal() # Load target: 0 = PRG, 1 = CHR.
        self.ld_addr_rst = Signal() # Pulse: reset the load address to 0 (before each section).
        self.ld_word     = Signal(32)
        self.ld_word_we  = Signal() # Pulse: write the 4 bytes of ld_word at the current address.
        self.cfg_prg32k  = Signal() # 0 = 16 KB PRG (mirrored), 1 = 32 KB PRG.
        self.cfg_mirror_v = Signal() # 0 = horizontal, 1 = vertical nametable mirroring.
        self.cfg_chr_ram = Signal() # 1 = CHR is RAM (iNES CHR page count 0).
        # PSRAM-cartridge loader (cart_port): mapper_flags[31:0] and the PRG cache flush (pulse).
        self.cfg_flags   = Signal(32)
        self.cart_flush  = Signal()
        self.prg_cache   = None

        # # #

        psram_cart = with_loader and (cart_port is not None)
        add_nes_sources(platform, mappers=NES_MAPPERS if psram_cart else None)
        # `rom` is a path (any .nes / .hex iNES ROM) or a bare name under cores/nes/roms/.
        rom_path = rom if os.path.exists(rom) else os.path.join(NES_RTL_PATH, "roms", rom)
        prg, chr, mapper_flags = parse_ines(rom_path)
        prg_size = (mapper_flags >> 8) & 0x7
        prg_mask = 0x3fff if prg_size == 0 else 0x7fff
        # Default config (matches the seed ROM, so --nes-loader boots it with no host interaction).
        self.ld_default_prg32k  = 1 if prg_size else 0
        self.ld_default_mirror_v = (mapper_flags >> 14) & 1
        self.ld_default_chr_ram = (mapper_flags >> 15) & 1
        self.ld_default_flags   = mapper_flags & 0xffffffff

        # NES clock (~21.477 MHz) + a 4x clock (~85.9 MHz) for the ROM BRAM reads, so cpumem/ppumem
        # data is fresh within one NES cycle (the core samples it the next NES edge), as nestang's
        # SDRAM runs at 4x the core clock.
        if with_pll:
            self.cd_nes   = ClockDomain()
            self.cd_nes4x = ClockDomain()
            self.pll      = pll = GW5APLL(devicename=platform.devicename, device=platform.device, name="nes_pll")
            pll.register_clkin(clk, clk_freq)
            pll.create_clkout(self.cd_nes4x, 21.477272e6*4)
            pll.create_clkout(self.cd_nes,   21.477272e6)
            locked = pll.locked
            add_core_clock_constraints(platform, pll, 0, "nes4x_clk")
            add_core_clock_constraints(platform, pll, 1, "nes_clk")
            # nes -> nes4x (NESMemory BRAM reads: address from the core, registered on nes4x): the
            # address is held for a whole nes cycle (4 nes4x cycles) and the data is sampled at the
            # next nes edge, so the address only has to settle by the 3rd nes4x edge.
            # nes4x -> nes (the read data back to the core): with the address stable for the whole nes
            # cycle, the BRAM output is stable from the 1st nes4x edge, sampled at the next nes edge.
            getattr(platform, "extra_timing_constraints", []).extend([
                "set_multicycle_path -from [get_clocks {nes_clk}] -to [get_clocks {nes4x_clk}] -setup -end 3",
                "set_multicycle_path -from [get_clocks {nes_clk}] -to [get_clocks {nes4x_clk}] -hold -end 2",
                "set_multicycle_path -from [get_clocks {nes4x_clk}] -to [get_clocks {nes_clk}] -setup -start 3",
                "set_multicycle_path -from [get_clocks {nes4x_clk}] -to [get_clocks {nes_clk}] -hold -start 2",
            ])
        else:
            # Simulation: the nes/nes4x clock domains are provided externally, always "locked".
            locked = Signal(reset=1)

        # Reset: held for 256 NES cycles after the PLL locks (the core needs settle time, as nestang's
        # reset_cnt); cold_reset tied low (as nestang). Count UP from the power-on 0 so it does not
        # depend on a domain reset (the nes clock only runs once locked).
        reset_cnt = Signal(8)
        reset_nes = Signal()
        hold_nes  = Signal() # ld_hold (sys) synchronized into the nes domain.
        self.specials += MultiReg(self.ld_hold, hold_nes, "nes")
        self.sync.nes += [
            If(hold_nes,                              # Held in reset while loading: restart the
                reset_cnt.eq(0),                      # settle counter so the core starts fresh on
            ).Elif(locked & (reset_cnt != 255),       # the new ROM when hold is released.
                reset_cnt.eq(reset_cnt + 1),
            ),
        ]
        # With the PSRAM cartridge: also held until the PSRAM is ready (BIST done) -- the first PRG
        # fetch must not reach the PSRAM before (as the SMS core).
        ready_nes = Signal(reset=1)
        if cart_ready is not None:
            self.specials += MultiReg(cart_ready, ready_nes, "nes")
        self.comb += reset_nes.eq(~locked | ~ready_nes | (reset_cnt != 255))

        # PRG/CHR ROM in BRAM. Without the loader: sized to the ROM, direct-init (read-only ROM, which
        # Gowin infers as BSRAM). With the loader: fixed to the max NROM window (32 KB PRG + 8 KB CHR),
        # NOT init (Gowin cannot infer BSRAM for an initialized *and* written memory -- it falls back
        # to registers), uploaded by the host at runtime; any NROM game loads without a rebuild.
        # PSRAM cartridge: PRG from the PSRAM (the host uploads it at NES_PRG_BASE) through a cache in
        # the nes domain; CHR (NES_CHR_KB) + CHR-RAM + PRG-RAM in BRAM.
        if psram_cart:
            self.prg_cache = cache = CoreROMCache(cart_port, cart_port_dout, base=NES_PRG_BASE,
                addr_width=21, lines=128, core_cd="nes")
            self.comb += cache.flush.eq(self.cart_flush)
            self.mem = mem = NESMemory(chr_kb=NES_CHR_KB, cd="nes", read_cd="nes4x",
                with_load=True, load_cd="nes", prg_cache=cache, with_ram=True)
        else:
            if with_loader:
                prg_kb, chr_kb = 32, 8
                prg_init, chr_init = None, None
            else:
                prg_kb = max(1, len(prg)//1024)
                chr_kb = max(1, len(chr)//1024)
                prg_init, chr_init = prg, chr
            self.mem = mem = NESMemory(prg_kb=prg_kb, chr_kb=chr_kb, cd="nes", read_cd="nes4x",
                prg_init=prg_init, chr_init=chr_init, with_load=with_loader, load_cd="sys")
        if psram_cart:
            pass # PRG size handled by the mapper (cart.sv masks the PRG banks by the size code).
        elif with_loader:
            self.comb += mem.prg_mask.eq(Mux(self.cfg_prg32k, 0x7fff, 0x3fff))
        else:
            self.comb += mem.prg_mask.eq(prg_mask)

        # Loader byte-spreader: a 32-bit word from the host is written as 4 consecutive bytes over 4
        # cycles, with an auto-incrementing address; ld_addr_rst reloads 0 (per section). In the sys
        # domain for the BRAM cartridge; in the nes domain for the PSRAM cartridge (the CHR BRAM write
        # port is shared with the PPU's CHR-RAM writes there): the word/sel are static CSRs, only the
        # write/reset strobes cross, as toggles.
        if with_loader:
            ld_addr   = Signal(15)
            ld_busy   = Signal()
            ld_cnt    = Signal(2)
            ld_word_r = Signal(32)
            if psram_cart:
                we_tog, rst_tog       = Signal(), Signal()
                we_tog_n, rst_tog_n   = Signal(), Signal()
                we_tog_d, rst_tog_d   = Signal(), Signal()
                self.sync += [
                    If(self.ld_word_we,  we_tog.eq(~we_tog)),
                    If(self.ld_addr_rst, rst_tog.eq(~rst_tog)),
                ]
                self.specials += [
                    MultiReg(we_tog,  we_tog_n,  "nes"),
                    MultiReg(rst_tog, rst_tog_n, "nes"),
                ]
                ld_sync    = self.sync.nes
                word_we    = Signal()
                addr_rst   = Signal()
                ld_sync   += [we_tog_d.eq(we_tog_n), rst_tog_d.eq(rst_tog_n)]
                self.comb += [word_we.eq(we_tog_n != we_tog_d), addr_rst.eq(rst_tog_n != rst_tog_d)]
            else:
                ld_sync  = self.sync
                word_we  = self.ld_word_we
                addr_rst = self.ld_addr_rst
            ld_sync += [
                mem.load_we.eq(0),
                If(addr_rst, ld_addr.eq(0)),
                If(word_we & ~ld_busy,
                    ld_word_r.eq(self.ld_word), ld_busy.eq(1), ld_cnt.eq(0),
                ),
                If(ld_busy,
                    mem.load_we.eq(1),
                    mem.load_sel.eq(self.ld_sel),
                    mem.load_addr.eq(ld_addr),
                    mem.load_data.eq(Array([ld_word_r[0:8], ld_word_r[8:16],
                                            ld_word_r[16:24], ld_word_r[24:32]])[ld_cnt]),
                    ld_addr.eq(ld_addr + 1),
                    ld_cnt.eq(ld_cnt + 1),
                    If(ld_cnt == 3, ld_busy.eq(0)),
                ),
            ]

        # Joypad. joypad_out/clock are 3/2-bit buses; bit 0 drives the glue, serial data on
        # joypad1_data[0].
        self.joy     = joy = NESJoypad(cd="nes")
        joypad_out   = Signal(3)
        joypad_clock = Signal(2)
        joypad1_data = Signal(5)
        self.comb += [
            joy.a.eq(buttons.a), joy.b.eq(buttons.b), joy.select.eq(buttons.select),
            joy.start.eq(buttons.start), joy.up.eq(buttons.up), joy.down.eq(buttons.down),
            joy.left.eq(buttons.left), joy.right.eq(buttons.right),
            joy.strobe.eq(joypad_out[0]),
            joy.clock.eq(joypad_clock[0]),
            joypad1_data.eq(joy.data),
        ]

        # Video: raster -> indexed downscale -> Game Boy LCD interface.
        self.video = video = NESVideoDownscaler(cd="nes")
        self.comb += [
            self.gb_clkena.eq(video.gb_clkena),
            self.gb_data.eq(video.gb_data),
            self.gb_mode.eq(video.gb_mode),
            self.gb_on.eq(video.gb_on),
            self.gb_vsync.eq(video.gb_vsync),
        ]
        # Native 256x240 raster (CoreVideo, "nes" domain), ex for a native-resolution UVC stream
        # (pruned by synthesis when unused).
        self.native = native = NESVideoAdapter(cd="nes")
        self.native_video, self.native_cd, self.native_size = native.video, "nes", (256, 240)
        self.comb += [
            native.color.eq(video.color),
            native.cycle.eq(video.cycle),
            native.scanline.eq(video.scanline),
        ]

        # NES core (constant mapper number prunes the unused mappers). With the loader, only the
        # size/mirror/CHR-RAM fields are made runtime; the mapper number (bits 7:0) stays a constant 0
        # (NROM), so synthesis still prunes the ~50 other mappers (they would not fit otherwise).
        if psram_cart:
            # Runtime mapper_flags from the host (the build's mapper-subset mask keeps pruning).
            mapper_flags_in = Signal(64)
            self.comb += mapper_flags_in.eq(Cat(self.cfg_flags, Constant(0, 32)))
        elif with_loader:
            mapper_flags_in = Signal(64)
            self.comb += mapper_flags_in.eq(Cat(
                Constant(0, 8),       # [7:0]   mapper = 0 (NROM), constant.
                self.cfg_prg32k,      # [8]     PRG size (0 = 16 KB code 0, 1 = 32 KB code 1).
                Constant(0, 2),       # [10:9]  PRG size high bits.
                Constant(0, 3),       # [13:11] CHR size (code 0 = 8 KB).
                self.cfg_mirror_v,    # [14]    mirroring.
                self.cfg_chr_ram,     # [15]    CHR-RAM.
                Constant(0, 48),      # [63:16].
            ))
        else:
            mapper_flags_in = Constant(mapper_flags, 64)

        sample = Signal(16)
        self.specials += Instance("NES",
            i_clk            = ClockSignal("nes"),
            i_stall          = mem.stall, # ChromatiX patch (nes_rtl_lowered): PRG cache miss -> freeze.
            i_reset_nes      = reset_nes,
            i_cold_reset     = 0,
            i_sys_type       = 0,
            i_mapper_flags   = mapper_flags_in,
            o_sample         = sample,
            o_color          = video.color,
            o_joypad_out     = joypad_out,
            o_joypad_clock   = joypad_clock,
            i_joypad1_data   = joypad1_data,
            i_joypad2_data   = 0,
            i_fds_busy       = 0,
            i_fds_eject      = 0,
            i_diskside       = 0,
            i_audio_channels = 0b11111,
            o_cpumem_addr    = mem.b_addr,
            o_cpumem_read    = mem.b_read,
            o_cpumem_write   = mem.b_write,
            o_cpumem_dout    = mem.b_din,
            i_cpumem_din     = mem.b_dout,
            o_ppumem_addr    = mem.a_addr,
            o_ppumem_read    = mem.a_read,
            o_ppumem_write   = mem.a_write,
            o_ppumem_dout    = mem.a_din,
            i_ppumem_din     = mem.a_dout,
            i_bram_din       = 0,
            o_cycle          = video.cycle,
            o_scanline       = video.scanline,
            i_int_audio      = 1,
            i_ext_audio      = 0,
            i_gg             = 0,
            i_gg_code        = 0,
            i_gg_reset       = 0,
        )
        # APU sample: unsigned (MiSTer NES: AUDIO_S = 0) -> signed for the shared bus (it was fed as
        # signed: every crossing of mid-scale wrapped -- loud distortion), halved for headroom (as
        # nestang's sample >> 1): (sample ^ 0x8000) >>> 1.
        audio = Signal(16)
        self.comb += [
            audio.eq(Cat(sample[1:15], ~sample[15], ~sample[15])),
            self.left.eq(audio),
            self.right.eq(audio),
        ]

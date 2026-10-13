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

from litex.gen import *

from litex.soc.cores.clock.gowin_gw5a import GW5APLL

from chromatix.gateware.core_shell import CoreVideo, rgb555
from chromatix.gateware.framebuffer import GBLCDTiming

NES_RTL_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "verilog", "cores", "nes")

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

# NES Memory (PRG + CHR in BRAM) -------------------------------------------------------------------

class NESMemory(LiteXModule):
    """
    PRG + CHR ROM in BRAM for a hosted NROM-class NES core (single-cycle, so the core's "pre-fetch a
    few cycles ahead" is trivially met, unlike PSRAM), pre-loaded at build time (prg_init/chr_init).
    The NES presents a unified 22-bit space: PRG at 0x000000.., CHR at 0x200000.. (bit 21). Channel B
    = CPU reads PRG, channel A = PPU reads CHR. `prg_mask` mirrors a 16 KB PRG into the 32 KB CPU
    window. Read-only (NROM PRG-ROM + CHR-ROM): Gowin BRAM cannot do read-before-write on a read+write
    port, and a CHR-RAM / runtime-loader variant would use separate write ports.

    All ports are in the `cd` (NES) domain; reads are registered (1 cycle). The write-side signals are
    kept on the interface (ignored) so the NES core's cpumem/ppumem write ports can connect.
    """
    def __init__(self, prg_kb=32, chr_kb=8, cd="nes", read_cd=None, prg_init=None, chr_init=None):
        read_cd = read_cd or cd # Read clock: a faster domain gives fresh data within one core cycle
                                # (the core samples cpumem_din the next core edge), as nestang's SDRAM.
        # Channel B: CPU (reads PRG).
        self.b_addr  = Signal(22)
        self.b_read  = Signal()
        self.b_write = Signal() # Ignored (read-only ROM).
        self.b_din   = Signal(8)
        self.b_dout  = Signal(8)
        # Channel A: PPU (reads CHR).
        self.a_addr  = Signal(22)
        self.a_read  = Signal()
        self.a_write = Signal() # Ignored (read-only ROM).
        self.a_din   = Signal(8)
        self.a_dout  = Signal(8)
        # PRG size mask (0x3FFF for 16 KB, 0x7FFF for 32 KB), from the mapper flags.
        self.prg_mask = Signal(15, reset=2**15 - 1)

        # # #

        prg = Memory(8, prg_kb*1024, init=prg_init)
        chr = Memory(8, chr_kb*1024, init=chr_init)
        prg_rd = prg.get_port(clock_domain=read_cd) # Read-only, fast clock.
        chr_rd = chr.get_port(clock_domain=read_cd) # Read-only, fast clock.
        self.specials += prg, prg_rd, chr, chr_rd
        self.prg = prg # Exposed for simulation.
        self.chr = chr

        self.comb += [
            prg_rd.adr.eq(self.b_addr[:15] & self.prg_mask), # PRG, mirrored.
            self.b_dout.eq(prg_rd.dat_r),
            chr_rd.adr.eq(self.a_addr[:13]),                 # CHR.
            self.a_dout.eq(chr_rd.dat_r),
        ]

# NES Joypad (standard controller serial protocol) ------------------------------------------------

class NESJoypad(LiteXModule):
    """
    The console buttons -> the NES standard controller serial protocol. While `strobe` (joypad_out[0])
    is high, the button state is (re)latched; each `clock` (joypad_clock[0]) rising edge then shifts
    out the next button. Report order: A, B, Select, Start, Up, Down, Left, Right. `data` is
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
            ).Elif(self.clock & ~clock_d, # Clock rising edge.
                shift.eq(Cat(shift[1:], 1)), # Shift; official controllers return 1 after 8 reads.
            ),
        ]
        self.comb += self.data.eq(Mux(self.strobe, self.a, shift[0]))

# NES RTL sources ----------------------------------------------------------------------------------

def add_nes_sources(platform):
    """Add the vendored NES core RTL (chromatix/verilog/cores/nes/, GPL) to the build."""
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
            path     = os.path.join(NES_RTL_PATH, sub, f)
            language = "systemverilog" if f.endswith(".sv") else "verilog"
            platform.add_source(path, language=language)

def _read_ines(path):
    """Parse a whitespace-separated hex iNES file into a list of byte values."""
    with open(path) as f:
        return [int(tok, 16) for tok in f.read().split()]

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
                vacc.eq(vacc + out_h),
                If((vacc + out_h) >= in_h,
                    vacc.eq(vacc + out_h - in_h),
                    If(y_out < out_h, y_out.eq(y_out + 1), row_base.eq(row_base + out_w)),
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

# NES Core -----------------------------------------------------------------------------------------

class NESCore(LiteXModule):
    """
    A hosted NES core on the Chromatic shell: a ~21.48 MHz clock (own PLL), the ROM loaded directly
    into PRG/CHR BRAM at build time with a constant mapper_flags (so the hosted core's ~50 unused
    mappers are pruned at synthesis), and the glue (indexed downscaler -> Game Boy LCD interface,
    APU -> shared audio bus, console buttons -> joypad). `buttons` has .a/.b/.select/.start/.up/.down/
    .left/.right (1 = pressed). Exposes the Game Boy LCD interface and left/right audio.

    First target: NROM (mapper 0) games such as the bundled nes15. A general runtime loader + full
    mapper set needs more logic than the GW5A-25 has with all mappers enabled (see doc/CORES.md).
    """
    def __init__(self, platform, clk, clk_freq, buttons, rom="nes15.hex", with_pll=True):
        self.gb_clkena = Signal()
        self.gb_data   = Signal(15)
        self.gb_mode   = Signal(2)
        self.gb_on     = Signal()
        self.gb_vsync  = Signal()
        self.left      = Signal(16)
        self.right     = Signal(16)

        # # #

        add_nes_sources(platform)
        prg, chr, mapper_flags = parse_ines(os.path.join(NES_RTL_PATH, "roms", rom))
        prg_size = (mapper_flags >> 8) & 0x7
        prg_mask = 0x3fff if prg_size == 0 else 0x7fff

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
        else:
            # Simulation: the nes/nes4x clock domains are provided externally, always "locked".
            locked = Signal(reset=1)

        # Reset: held for 256 NES cycles after the PLL locks (the core needs settle time, as nestang's
        # reset_cnt); cold_reset tied low (as nestang). Count UP from the power-on 0 so it does not
        # depend on a domain reset (the nes clock only runs once locked).
        reset_cnt = Signal(8)
        reset_nes = Signal()
        self.sync.nes += If(locked & (reset_cnt != 255), reset_cnt.eq(reset_cnt + 1))
        self.comb += reset_nes.eq(~locked | (reset_cnt != 255))

        # PRG/CHR ROM pre-loaded into BRAM (direct, no runtime loader), sized to the ROM.
        prg_kb = max(1, len(prg)//1024)
        chr_kb = max(1, len(chr)//1024)
        self.mem = mem = NESMemory(prg_kb=prg_kb, chr_kb=chr_kb, cd="nes", read_cd="nes4x",
            prg_init=prg, chr_init=chr)
        self.comb += mem.prg_mask.eq(prg_mask)

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

        # NES core (constant mapper_flags prunes the unused mappers).
        sample = Signal(16)
        self.specials += Instance("NES",
            i_clk            = ClockSignal("nes"),
            i_reset_nes      = reset_nes,
            i_cold_reset     = 0,
            i_sys_type       = 0,
            i_mapper_flags   = Constant(mapper_flags, 64),
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
        self.comb += [self.left.eq(sample), self.right.eq(sample)]

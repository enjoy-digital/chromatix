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

from migen import *

from litex.gen import *

from chromatix.gateware.core_shell import CoreVideo, rgb555

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
    PRG + CHR ROM/RAM in BRAM for a hosted NROM-class NES core (single-cycle, so the core's "pre-fetch
    a few cycles ahead" is trivially met, unlike PSRAM). The NES presents a unified 22-bit space:
    PRG at 0x000000.., CHR at 0x200000.. (bit 21). Two channels: A = PPU (CHR), B = CPU, and the ROM
    loader also writes through channel B (bit 21 selects PRG/CHR). `prg_mask` mirrors a 16 KB PRG into
    the 32 KB CPU window.

    All ports are in the `cd` (NES) domain; reads are registered (1 cycle).
    """
    def __init__(self, prg_kb=32, chr_kb=8, cd="nes"):
        # Channel B: CPU (reads PRG) and, during loading, the ROM loader (writes PRG and CHR).
        self.b_addr  = Signal(22)
        self.b_read  = Signal()
        self.b_write = Signal()
        self.b_din   = Signal(8) # To memory (loader / CPU write).
        self.b_dout  = Signal(8) # From memory (CPU read).
        # Channel A: PPU (reads/writes CHR).
        self.a_addr  = Signal(22)
        self.a_read  = Signal()
        self.a_write = Signal()
        self.a_din   = Signal(8)
        self.a_dout  = Signal(8)
        # PRG size mask (e.g. 0x3FFF for 16 KB, 0x7FFF for 32 KB), set from the loader's mapper flags.
        self.prg_mask = Signal(15, reset=2**15 - 1)

        # # #

        prg = Memory(8, prg_kb*1024)
        chr = Memory(8, chr_kb*1024)
        # PRG: one R/W port on channel B (loader writes when bit21=0, else CPU reads).
        prg_port = prg.get_port(write_capable=True, clock_domain=cd)
        # CHR: port A = PPU (R/W), port B = loader write (bit21=1).
        chr_a = chr.get_port(write_capable=True, clock_domain=cd)
        chr_b = chr.get_port(write_capable=True, clock_domain=cd)
        self.specials += prg, prg_port, chr, chr_a, chr_b
        self.prg = prg # Exposed for simulation.
        self.chr = chr

        b_is_chr = self.b_addr[21]
        self.comb += [
            # PRG (channel B): CPU read address masked for mirroring; loader write when bit21=0.
            prg_port.adr.eq(Mux(self.b_write & ~b_is_chr, self.b_addr[:15], self.b_addr[:15] & self.prg_mask)),
            prg_port.dat_w.eq(self.b_din),
            prg_port.we.eq(self.b_write & ~b_is_chr),
            self.b_dout.eq(prg_port.dat_r),
            # CHR port A: PPU.
            chr_a.adr.eq(self.a_addr[:13]),
            chr_a.dat_w.eq(self.a_din),
            chr_a.we.eq(self.a_write),
            self.a_dout.eq(chr_a.dat_r),
            # CHR port B: loader CHR write (channel B, bit21=1).
            chr_b.adr.eq(self.b_addr[:13]),
            chr_b.dat_w.eq(self.b_din),
            chr_b.we.eq(self.b_write & b_is_chr),
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

# NESCore integration note -------------------------------------------------------------------------
#
# NESCore assembles the glue above with the NES RTL once it is vendored under
# chromatix/verilog/cores/nes/ (nand2mario `nestang` core: nes.v, ppu.v, apu.v, cart.sv, t65/,
# mappers/, compat.v, dpram.v, game_loader.v, EEPROM_24C0x.sv — GPL). Importing third-party RTL into
# the repo is gated (untrusted-code integration); do it explicitly, then the remaining wiring is:
#
#   pll = GW5APLL(...)                      # ~21.48 MHz "nes" clock from clk_fpga/clk_24.
#   Instance("GameLoader", ... )            # iNES parse; indata/indata_clk from the ROM source
#                                           # (host debug bridge or a PSRAM/BRAM ROM buffer) ->
#                                           # mem_addr/mem_data/mem_write + mapper_flags.
#   Instance("NES", i_clk=ClockSignal("nes"),
#       o_color=adapter.color, o_cycle=adapter.cycle, o_scanline=adapter.scanline, o_sample=sample,
#       o_cpumem_addr=mem.b_addr, o_cpumem_read=mem.b_read, i_cpumem_din=mem.b_dout,  # via load mux
#       o_ppumem_addr=mem.a_addr, o_ppumem_read=mem.a_read, i_ppumem_din=mem.a_dout,
#       o_joypad_out=joy.strobe, o_joypad_clock=joy.clock, i_joypad1_data=joy.data, ...)
#   NESVideoAdapter -> CoreVideoDownscaler(256, 240, write_cd="nes") -> gb_lcd_* (the shell).
#   sample -> the shared left/right audio bus.  CoreInput(console buttons) -> NESJoypad.
#
# During loading, channel B (mem.b_*) is driven by GameLoader; after loading, by the NES CPU.

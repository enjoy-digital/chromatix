#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
SMS / Game Gear core integration glue for the `--with-core sms` build (the 2nd worked example of the
core-porting methodology, doc/CORES.md -- genericity: a different console family than the NES).

Like nes.py, this is the *permissively licensed* LiteX glue only; the core RTL (nand2mario's
`smstang`, a Gowin port of SMS-MiSTer, GPL) is dropped under `chromatix/verilog/cores/sms/` (see its
README). `SMSCore` adapts the `system` DUT to the Chromatic shell:
- clock: one 53.69 MHz `clk_sys` (own PLL) + the clock-enables ce_cpu(/15)/ce_vdp(/5)/ce_pix(/10)/
  ce_sp (cadence copied from smstang_top.sv),
- video: the `video` raster drives x,y into `system` and reads color[11:0] (BGR 4:4:4 -> RGB555);
  Game Gear is 160x144 native, SMS is 256x192 -> CoreVideoDownscaler -> the Game Boy LCD interface,
- memory: system RAM (16 KB) in BRAM; the cartridge ROM via rom_rd/rom_a/rom_do -- here a BRAM
  placeholder (NROM-style, small ROMs). **A fitting SMS build needs the cartridge in PSRAM behind a
  cache** (the ROM is up to 512 KB; with a BRAM cartridge + the 16 KB RAM + 16 KB VRAM + boot ROM +
  the frame buffer the design overflows the 56 BSRAM) -- the next step, reusing VirtualCart's cache.
- audio: audioL/R -> the shared bus; input: the console buttons -> j1_* (active low).

STATUS: first-cut glue -- it elaborates (structural wiring of the 2nd core). Fit needs the PSRAM
cartridge cache; rendering needs the color/raster pipeline aligned on hardware. See doc/CORES.md.
"""

import os

from migen import *
from migen.genlib.cdc import MultiReg

from litex.gen import *

from litex.soc.cores.clock.gowin_gw5a import GW5APLL

from chromatix.gateware.core_shell import CoreVideo, CoreVideoDownscaler, rgb555

SMS_RTL_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "verilog", "cores", "sms")

SMS_CLK_FREQ = 53.693175e6 # clk_sys (= 15x the ~3.58 MHz Z80 ce_cpu).

# SMS RTL sources ----------------------------------------------------------------------------------

def add_sms_sources(platform):
    """Add the vendored SMS core RTL (chromatix/verilog/cores/sms/, GPL) + the ChromatiX FM stub."""
    files = [
        "system.v", "video.v",
        "vdp.v", "vdp_main.v", "vdp_background.v", "vdp_sprites.v", "vdp_sprite_shifter.v",
        "vdp_cram.v", "io.v", "audiomix.v", "MC8123.v", "SEGASYS1_PRGDEC.v", "parts.v",
        "spram.v", "sprom.v", "dpram.v",
        "opll_stub.v", # ChromatiX BSD FM stub (replaces the VHDL vm2413 opll for first bring-up).
        "t80/t80.v", "t80/t80_alu.v", "t80/t80_mcode.v", "t80/t80_reg.v", "t80/t80pa.v", "t80/t80s.v",
        "jt89/jt89.v", "jt89/jt89_mixer.v", "jt89/jt89_noise.v", "jt89/jt89_tone.v", "jt89/jt89_vol.v",
    ]
    for f in files:
        platform.add_source(os.path.join(SMS_RTL_PATH, f), language="verilog")

# SMS Core -----------------------------------------------------------------------------------------

class SMSCore(LiteXModule):
    """
    A hosted SMS / Game Gear core on the Chromatic shell. `buttons` has .a/.b/.select/.start/.up/
    .down/.left/.right (1 = pressed). Exposes the Game Boy LCD interface and left/right audio.
    `game_gear` selects the 160x144 native path (no downscale); otherwise SMS 256x192 is downscaled.
    """
    def __init__(self, platform, clk, clk_freq, buttons, rom=None, game_gear=True, with_pll=True):
        self.gb_clkena = Signal()
        self.gb_data   = Signal(15)
        self.gb_mode   = Signal(2)
        self.gb_on     = Signal()
        self.gb_vsync  = Signal()
        self.left      = Signal(16)
        self.right     = Signal(16)

        # # #

        add_sms_sources(platform)

        # Clock: one 53.69 MHz clk_sys (own PLL), the SMS "sms" clock domain.
        if with_pll:
            self.cd_sms = ClockDomain()
            self.pll    = pll = GW5APLL(devicename=platform.devicename, device=platform.device, name="sms_pll")
            pll.register_clkin(clk, clk_freq)
            pll.create_clkout(self.cd_sms, SMS_CLK_FREQ)
            locked = pll.locked
        else:
            locked = Signal(reset=1)

        # Clock-enables (cadence from smstang_top.sv): clkd counts 0..29, ce_cpu at 9/24 (/15),
        # ce_vdp every 5, ce_pix at 9/19/29 (/10), ce_sp every other cycle.
        clkd   = Signal(5)
        ce_cpu = Signal()
        ce_vdp = Signal()
        ce_pix = Signal()
        ce_sp  = Signal()
        self.sync.sms += [
            ce_cpu.eq(0), ce_vdp.eq(0), ce_pix.eq(0),
            ce_sp.eq(clkd[0]),
            clkd.eq(clkd + 1),
            If(clkd == 29, clkd.eq(0), ce_vdp.eq(1), ce_pix.eq(1),
            ).Elif(clkd == 24, ce_cpu.eq(1), ce_vdp.eq(1),
            ).Elif(clkd == 19, ce_vdp.eq(1), ce_pix.eq(1),
            ).Elif(clkd == 14, ce_vdp.eq(1),
            ).Elif(clkd ==  9, ce_cpu.eq(1), ce_vdp.eq(1), ce_pix.eq(1),
            ).Elif(clkd ==  4, ce_vdp.eq(1),
            ),
        ]

        # Reset: held ~256 ce_cpu cycles after the PLL locks (count up from power-on 0).
        reset_cnt = Signal(8)
        reset     = Signal()
        self.sync.sms += If(locked & ce_cpu & (reset_cnt != 255), reset_cnt.eq(reset_cnt + 1))
        self.comb += reset.eq(~locked | (reset_cnt != 255))

        # Cartridge ROM (placeholder BRAM; PSRAM+cache is the next step -- see the module docstring).
        rom_init = None
        if rom is not None:
            path = rom if os.path.exists(rom) else os.path.join(SMS_RTL_PATH, "roms", rom)
            with open(path, "rb") as f:
                rom_init = list(f.read())
        cart     = Memory(8, 2**16, init=rom_init) # 64 KB window (unbanked small ROMs).
        cart_rd  = cart.get_port(clock_domain="sms")
        self.specials += cart, cart_rd
        rom_a    = Signal(22)
        rom_do   = Signal(8)
        self.comb += [cart_rd.adr.eq(rom_a[:16]), rom_do.eq(cart_rd.dat_r)]

        # System RAM (16 KB) in BRAM.
        ram      = Memory(8, 2**14)
        ram_rdwr = ram.get_port(write_capable=True, clock_domain="sms")
        self.specials += ram, ram_rdwr
        ram_a    = Signal(14)
        ram_d    = Signal(8)
        ram_we   = Signal()
        ram_q    = Signal(8)
        self.comb += [
            ram_rdwr.adr.eq(ram_a), ram_rdwr.dat_w.eq(ram_d), ram_rdwr.we.eq(ram_we),
            ram_q.eq(ram_rdwr.dat_r),
        ]

        # Video raster (drives x,y into `system`, produces blanking); smode_* come from the VDP.
        x        = Signal(9)
        y        = Signal(9)
        color    = Signal(12)
        hblank   = Signal()
        vblank   = Signal()
        smode_M1 = Signal()
        smode_M2 = Signal()
        smode_M3 = Signal()
        ggres    = 1 if game_gear else 0
        self.specials += Instance("video",
            i_clk         = ClockSignal("sms"),
            i_ce_pix      = ce_pix,
            i_pal         = 0,
            i_border      = 0,
            i_ggres       = ggres,
            i_mask_column = 0,
            i_cut_mask    = 0,
            i_smode_M1    = smode_M1,
            i_smode_M3    = smode_M3,
            o_x           = x,
            o_y           = y,
            o_hblank      = hblank,
            o_vblank      = vblank,
        )

        # SMS system (the DUT). Most configuration tied to a plain NTSC SMS/Game Gear.
        audio_l = Signal(16)
        audio_r = Signal(16)
        self.specials += Instance("system",
            p_MAX_SPPL        = 63,
            i_clk_sys         = ClockSignal("sms"),
            i_ce_cpu          = ce_cpu,
            i_ce_vdp          = ce_vdp,
            i_ce_pix          = ce_pix,
            i_ce_sp           = ce_sp,
            i_turbo           = 0,
            i_gg              = 1 if game_gear else 0,
            i_ggres           = ggres,
            i_systeme         = 0,
            i_bios_en         = 0,
            i_GG_EN           = 0,
            i_GG_CODE         = 0,
            i_GG_RESET        = 0,
            i_RESET_n         = ~reset,
            # Cartridge ROM.
            o_rom_rd          = Signal(),
            o_rom_a           = rom_a,
            i_rom_do          = rom_do,
            # Controller 1 (active low): console buttons -> the SMS pad.
            i_j1_up           = ~buttons.up,
            i_j1_down         = ~buttons.down,
            i_j1_left         = ~buttons.left,
            i_j1_right        = ~buttons.right,
            i_j1_tl           = ~buttons.a,     # Button 1.
            i_j1_tr           = ~buttons.b,     # Button 2.
            i_j1_th           = 1,
            i_j1_start        = ~buttons.start, # Game Gear Start / SMS pause.
            i_j1_coin         = 1,
            i_j1_a3           = 1,
            # Controller 2 (idle, active low).
            i_j2_up = 1, i_j2_down = 1, i_j2_left = 1, i_j2_right = 1, i_j2_tl = 1, i_j2_tr = 1,
            i_j2_th = 1, i_j2_start = 1, i_j2_coin = 1, i_j2_a3 = 1,
            i_pause           = 0,
            # Peripherals / DIP (inactive).
            i_E0Type = 0, i_E1Use = 0, i_E2Use = 0, i_E0 = 0xff, i_F2 = 0xff, i_F3 = 0xff,
            i_has_paddle = 0, i_has_pedal = 0, i_paddle = 0, i_paddle2 = 0, i_pedal = 0,
            # Video.
            i_x               = x,
            i_y               = y,
            o_color           = color,
            i_palettemode     = 1 if game_gear else 0,
            o_smode_M1        = smode_M1,
            o_smode_M2        = smode_M2,
            o_smode_M3        = smode_M3,
            i_ysj_quirk       = 0,
            i_pal             = 0,
            i_region          = 0,
            i_mapper_lock     = 0,
            i_vdp_enables     = 0,
            i_psg_enables     = 0,
            # Audio.
            i_fm_ena          = 0,
            o_audioL          = audio_l,
            o_audioR          = audio_r,
            i_dbr             = 0,
            i_sp64            = 0,
            # System RAM.
            o_ram_a           = ram_a,
            o_ram_d           = ram_d,
            o_ram_we          = ram_we,
            i_ram_q           = ram_q,
            # Cartridge backup RAM (unused for now).
            o_nvram_a         = Signal(15),
            o_nvram_d         = Signal(8),
            o_nvram_we        = Signal(),
            i_nvram_q         = 0,
            # MC8123 key (unused).
            i_encrypt         = 0,
            o_key_a           = Signal(13),
            i_key_d           = 0,
            # ioctl ROM download (unused -- cartridge served from BRAM).
            i_ROMCL           = ClockSignal("sms"),
            i_ROMAD           = 0,
            i_ROMDT           = 0,
            i_ROMEN           = 0,
        )
        self.comb += [self.left.eq(audio_l), self.right.eq(audio_r)]

        # Video: raster color -> CoreVideo stream -> downscaler (160x144 identity for GG, 256x192 for
        # SMS) -> Game Boy LCD interface. color is BGR 4:4:4 -> RGB555 (top bits replicated).
        in_w, in_h = (160, 144) if game_gear else (256, 192)
        self.video = ds = CoreVideoDownscaler(in_w=in_w, in_h=in_h, write_cd="sms")
        r4, g4, b4 = color[0:4], color[4:8], color[8:12]
        self.comb += [
            ds.video.ce.eq(ce_pix),
            ds.video.hblank.eq(hblank),
            ds.video.vblank.eq(vblank),
            ds.video.r.eq(Cat(r4[3], r4)),  # 4-bit -> 5-bit (replicate MSB).
            ds.video.g.eq(Cat(g4[3], g4)),
            ds.video.b.eq(Cat(b4[3], b4)),
            self.gb_clkena.eq(ds.gb_clkena),
            self.gb_data.eq(ds.gb_data),
            self.gb_mode.eq(ds.gb_mode),
            self.gb_on.eq(ds.gb_on),
            self.gb_vsync.eq(ds.gb_vsync),
        ]

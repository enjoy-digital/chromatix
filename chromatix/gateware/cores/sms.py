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

from litex.soc.interconnect.csr      import CSRStorage, CSRField, CSRStatus
from litex.soc.cores.clock.gowin_gw5a import GW5APLL

from chromatix.gateware.core_shell     import CoreVideo, CoreVideoDownscaler, rgb555
from chromatix.gateware.cores.rom_cache import CoreROMCache

SMS_RTL_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "verilog", "cores", "sms")

SMS_CLK_FREQ = 53.693175e6 # clk_sys (= 15x the ~3.58 MHz Z80 ce_cpu).
SMS_ROM_BASE = 0x400000    # Cartridge ROM placed here in the PSRAM (host upload); upper half, clear
                           # of the framebuffers/OSD/BIST in the low PSRAM.

# SMS RTL sources ----------------------------------------------------------------------------------

def sms_rtl_with_io_debug(dst_dir, src_dir=None):
    """Copy the SMS RTL to dst_dir and add a 21-bit I/O-debug output to system.v (the Z80 <-> VDP
    register-write handshake: {D_in, vdp_WR_n, M1_n, WR_n, RD_n, IORQ_n, A[7:0]}), for hardware
    introspection. Repo RTL untouched. Returns dst_dir."""
    import shutil, re
    src_dir = src_dir or SMS_RTL_PATH
    shutil.rmtree(dst_dir, ignore_errors=True)
    shutil.copytree(src_dir, dst_dir)
    sysv = os.path.join(dst_dir, "system.v")
    with open(sysv, encoding="utf-8") as f:
        text = f.read()
    text = text.replace("ROMEN);", "ROMEN, dbg_io);", 1)
    text = re.sub(r"\nendmodule\s*$",
        "\n   output [20:0] dbg_io;\n"
        "   assign dbg_io = {D_in, vdp_WR_n, M1_n, WR_n, RD_n, IORQ_n, A[7:0]};\nendmodule\n",
        text, count=1)
    with open(sysv, "w", encoding="utf-8") as f:
        f.write(text)
    return dst_dir

def add_sms_sources(platform, rtl_path=None):
    """Add the vendored SMS core RTL (chromatix/verilog/cores/sms/, GPL) + the ChromatiX FM stub."""
    rtl_path = rtl_path or SMS_RTL_PATH
    files = [
        "system.v", "video.v",
        "vdp.v", "vdp_main.v", "vdp_background.v", "vdp_sprites.v", "vdp_sprite_shifter.v",
        "vdp_cram.v", "io.v", "audiomix.v", "cheatcodes.sv", "MC8123.v", "SEGASYS1_PRGDEC.v", "parts.v",
        "spram.v", "sprom.v", "dpram.v",
        "opll_stub.v", # ChromatiX BSD FM stub (replaces the VHDL vm2413 opll for first bring-up).
        "t80/t80.v", "t80/t80_alu.v", "t80/t80_mcode.v", "t80/t80_reg.v", "t80/t80pa.v", "t80/t80s.v",
        "jt89/jt89.v", "jt89/jt89_mixer.v", "jt89/jt89_noise.v", "jt89/jt89_tone.v", "jt89/jt89_vol.v",
    ]
    for f in files:
        language = "systemverilog" if f.endswith(".sv") else "verilog"
        platform.add_source(os.path.join(rtl_path, f), language=language)

# SMS Loader CSR (host -> SMSCore cartridge, over the debug bridge) --------------------------------

class SMSLoaderCSR(LiteXModule):
    """SoC-CSR front end to SMSCore's cartridge loader (needs --with-debug-bridge). The host
    (scripts/chromatic.py sms-load) holds the core, uploads the ROM into the PSRAM at SMS_ROM_BASE
    over the debug bridge, flushes the cache, then releases the hold so the core boots the ROM."""
    def __init__(self, core, with_debug=False):
        self.control = CSRStorage(fields=[
            CSRField("hold",  size=1, offset=0,             description="Hold the SMS in reset while loading."),
            CSRField("flush", size=1, offset=1, pulse=True, description="Invalidate the cartridge cache."),
        ])

        # # #

        from migen.genlib.cdc import MultiReg
        self.comb += [
            core.ld_hold.eq(self.control.fields.hold),
            core.cart_flush.eq(self.control.fields.flush),
        ]
        # Cache debug/stats CSRs: off by default -- the design fills the GW5A-25's BSRAM (56/56) and
        # is routing-bound, so the extra CSR nets fail to route. Enable only for bring-up debugging
        # (and free some BSRAM first). These pinpointed the BIST-race FILL stall (see cart_ready).
        if with_debug:
            self.misses = CSRStatus(32, description="Cartridge cache misses.")
            self.reads  = CSRStatus(32, description="Cartridge reads.")
            self.debug  = CSRStatus(32, description="Debug: VDP color+raster, or the VDP I/O handshake (hw_debug).")
            self.specials += MultiReg(core.dbg, self.debug.status)
            if hasattr(core, "cart"):
                self.specials += [
                    MultiReg(core.cart.misses, self.misses.status),
                    MultiReg(core.cart.reads,  self.reads.status),
                ]

# SMS Core -----------------------------------------------------------------------------------------

class SMSCore(LiteXModule):
    """
    A hosted SMS / Game Gear core on the Chromatic shell. `buttons` has .a/.b/.select/.start/.up/
    .down/.left/.right (1 = pressed). Exposes the Game Boy LCD interface and left/right audio.
    `game_gear` selects the 160x144 native path (no downscale); otherwise SMS 256x192 is downscaled.
    """
    def __init__(self, platform, clk, clk_freq, buttons, rom=None, game_gear=True, with_pll=True,
                 cart_port=None, cart_port_dout=None, cart_ready=None, bios_en=False, cart_bram_kb=64,
                 hw_debug=False):
        self.gb_clkena = Signal()
        self.gb_data   = Signal(15)
        self.gb_mode   = Signal(2)
        self.gb_on     = Signal()
        self.gb_vsync  = Signal()
        self.left      = Signal(16)
        self.right     = Signal(16)
        # Runtime loader interface (sys domain, driven by SoC CSRs; see chromatix.py): hold the core
        # in reset while the host uploads the ROM into the PSRAM, and flush the cache afterwards.
        self.ld_hold    = Signal()
        self.cart_flush = Signal()
        self.dbg        = Signal(32) # Debug CSR payload (color+raster, or the VDP I/O handshake when hw_debug).

        # # #

        # hw_debug: build against an RTL copy with an I/O-debug output added to system.v (the Z80 <->
        # VDP register-write handshake), for hardware introspection; otherwise the repo RTL as-is.
        if hw_debug:
            add_sms_sources(platform, rtl_path=sms_rtl_with_io_debug(os.path.join("build", "sms_dbg_src")))
        else:
            add_sms_sources(platform)

        rom_a   = Signal(22) # Cartridge address / read (from the core).
        rom_rd  = Signal()
        rom_do  = Signal(8)  # Cartridge byte (to the core).
        stall   = Signal()   # Cartridge cache stall (freezes the clock-enables on a PSRAM miss).
        dbg_io  = Signal(21) # system.v I/O-debug bus (hw_debug): {D_in,vdp_WR_n,M1_n,WR_n,RD_n,IORQ_n,A[7:0]}.

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
        # `stall` (cartridge cache miss) freezes the cadence: hold clkd + emit no enables, so the
        # whole core pauses until the PSRAM line is fetched, then resumes exactly where it left off.
        self.sync.sms += [
            ce_cpu.eq(0), ce_vdp.eq(0), ce_pix.eq(0), ce_sp.eq(0),
            If(~stall,
                ce_sp.eq(clkd[0]),
                clkd.eq(clkd + 1),
                If(clkd == 29, clkd.eq(0), ce_vdp.eq(1), ce_pix.eq(1),
                ).Elif(clkd == 24, ce_cpu.eq(1), ce_vdp.eq(1),
                ).Elif(clkd == 19, ce_vdp.eq(1), ce_pix.eq(1),
                ).Elif(clkd == 14, ce_vdp.eq(1),
                ).Elif(clkd ==  9, ce_cpu.eq(1), ce_vdp.eq(1), ce_pix.eq(1),
                ).Elif(clkd ==  4, ce_vdp.eq(1),
                ),
            ),
        ]

        # Reset: held ~256 ce_cpu cycles after the PLL locks AND the cartridge PSRAM is ready (BIST
        # done) -- otherwise the core's first cartridge fetch issues a PSRAM read before the PSRAM is
        # initialised, the one-cycle request is dropped and the cache hangs forever in its FILL state
        # (observed). ld_hold (sys -> sms) also restarts it, so the core boots fresh on a freshly
        # uploaded ROM. The settle counter runs on the raw sms clock (not ce) so it advances even
        # while the cache stalls.
        hold_sms  = Signal()
        ready_sms = Signal() # PSRAM ready (BIST done), synchronized into the sms domain.
        self.specials += MultiReg(self.ld_hold, hold_sms, "sms")
        if cart_ready is not None:
            self.specials += MultiReg(cart_ready, ready_sms, "sms")
        else:
            self.comb += ready_sms.eq(1) # No cartridge port (sim/elaboration): always ready.
        reset_cnt = Signal(8)
        reset     = Signal()
        self.sync.sms += If(hold_sms | ~ready_sms, reset_cnt.eq(0)
            ).Elif(locked & (reset_cnt != 255), reset_cnt.eq(reset_cnt + 1))
        self.comb += reset.eq(~locked | (reset_cnt != 255))

        # Cartridge ROM. With a PSRAM port (cart_port): a direct-mapped cache (CoreROMCache) serves
        # the ROM from the PSRAM (uploaded by the host) and `stall`s the core on a miss -- this is
        # what lets the design fit (the ROM is up to 512 KB, far too big for BRAM). Without a port
        # (sim/elaboration): a small BRAM placeholder, no stall.
        if cart_port is not None:
            self.cart = cart = CoreROMCache(cart_port, cart_port_dout, base=SMS_ROM_BASE,
                addr_width=22, lines=128, core_cd="sms")
            self.comb += [
                cart.addr.eq(rom_a),
                cart.rd.eq(rom_rd & ~reset), # Only fault in lines for real cartridge reads.
                cart.flush.eq(self.cart_flush),
                rom_do.eq(cart.data),
                stall.eq(cart.stall),
            ]
        else:
            rom_init = None
            if rom is not None:
                path = rom if os.path.exists(rom) else os.path.join(SMS_RTL_PATH, "roms", rom)
                with open(path, "rb") as f:
                    rom_init = list(f.read())
            bits     = log2_int(cart_bram_kb*1024)
            cart     = Memory(8, cart_bram_kb*1024, init=rom_init) # unbanked small ROMs.
            cart_rd  = cart.get_port(clock_domain="sms")
            self.specials += cart, cart_rd
            self.comb += [cart_rd.adr.eq(rom_a[:bits]), rom_do.eq(cart_rd.dat_r)]

        # System RAM (16 KB window). (An 8 KB BRAM is enough for the SMS's 8 KB, but on this part it
        # did not reduce the BSRAM count and placed worse; the 16 KB layout routes at 56/56.)
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
            i_bios_en         = 1 if bios_en else 0, # Boot the SMS BIOS (mboot.hex) if enabled.
            i_GG_EN           = 0,
            i_GG_CODE         = 0,
            i_GG_RESET        = 0,
            i_RESET_n         = ~reset,
            # Cartridge ROM (served by the PSRAM cache or the BRAM placeholder).
            o_rom_rd          = rom_rd,
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
            **({"o_dbg_io": dbg_io} if hw_debug else {}),
        )
        self.comb += [self.left.eq(audio_l), self.right.eq(audio_r)]

        # Video: raster color -> CoreVideo stream -> downscaler (160x144 identity for GG, 256x192 for
        # SMS) -> Game Boy LCD interface. color is BGR 4:4:4 -> RGB555 (top bits replicated).
        in_w, in_h = (160, 144) if game_gear else (256, 192)
        # color_bits=3 (RGB333 frame buffer, ~12 BSRAM vs ~20 for RGB555): the SMS fills the
        # GW5A-25's BSRAM, and a smaller frame buffer is what gives the design routing headroom.
        self.video = ds = CoreVideoDownscaler(in_w=in_w, in_h=in_h, write_cd="sms", color_bits=3)
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
            # Debug CSR: the VDP I/O handshake (hw_debug) or the VDP color output + raster x.
            self.dbg.eq(dbg_io if hw_debug else Cat(color, x[:4])),
        ]

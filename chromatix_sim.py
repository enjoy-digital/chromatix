#!/usr/bin/env python3

#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
ChromatiX simulation (Verilator): the Game Boy core (emu_system_top, VHDL parts converted with
GHDL) with a cartridge model, scripted buttons and the LCD output captured as PNG frames.

    ./chromatix_sim.py --rom game.gb --frames 300 --every 30 --buttons start@200+10
    ./chromatix_sim.py --rom other.gb --frames 100 --no-compile # Reuse the build.

The ROM, frames and buttons are runtime inputs: --no-compile runs any ROM on the previous build.
"""

import os
import re
import json
import glob
import time
import shutil
import argparse
import subprocess

from migen import *

from litex.gen import *

from litex.build.generic_platform import Pins, Subsignal
from litex.build.sim              import SimPlatform
from litex.build.sim.config       import SimConfig

from chromatix.gateware.sources import VERILOG_PATH
from chromatix.gateware.sim     import SIM_VERILOG_PATH, convert_vhdl, verilog_sources
from chromatix.gateware.sim     import SimCartridge, write_rom_init, check_rom
from chromatix.gateware.sim     import SimPSRAMPort, SimVirtualCartConfig, SimNativePSRAM
from chromatix.gateware.sim     import parse_button_sequence, write_button_events
from chromatix.gateware.memory  import memory_port_layout, MemorySystem
from chromatix.gateware.video   import VideoPipeline
from chromatix.gateware.vcart   import VirtualCart

# IOs ----------------------------------------------------------------------------------------------

PCLK_FREQ = 33.55432e6
GB_FPS    = 4194304/70224 # Game Boy frame rate (59.73 fps).

_io = [
    ("sys_clk", 0, Pins(1)),
    ("sys_rst", 0, Pins(1)),

    # Window (gbwindow simulation module): Game Boy LCD, keyboard/gamepad buttons, finish.
    ("gb_lcd", 0,
        Subsignal("clk",    Pins(1)),
        Subsignal("clkena", Pins(1)),
        Subsignal("data",   Pins(15)),
        Subsignal("vsync",  Pins(1)),
    ),
    ("sim_ctrl", 0,
        Subsignal("keys",   Pins(8)),
        Subsignal("finish", Pins(1)),
    ),
]

# Simulation modules (LiteX simulation external modules).
SIM_MODULES_PATH = os.path.join(SIM_VERILOG_PATH, "modules")

# Simulation Top -----------------------------------------------------------------------------------

class SimTop(LiteXModule):
    def __init__(self, platform, rom,
        vcart         = False,
        vcart_latency = 16,
        vcart_hold    = 0,
        video         = False,
        frame_blend   = False,
        correct       = False,
        dual_clock    = False,
        window        = False):
        # Clocks: hClk from the simulation clocker and pClk = hClk (single clock: the pClk logic,
        # cartridge address latch and button debouncers, runs at the hClk rate, halving the model
        # evaluations), or pClk from the clocker and hClk = pClk/2 (dual_clock, as the PLL outputs).
        # With the video pipeline: xClk (= fClk) from the clocker, pClk/hClk/gClk = xClk/2, /4, /8.
        self.cd_pclk = ClockDomain("pclk", reset_less=True)
        self.cd_hclk = ClockDomain("hclk", reset_less=True)
        hclk = Signal()
        if video:
            self.cd_xclk = ClockDomain("xclk", reset_less=True)
            self.cd_fclk = ClockDomain("fclk", reset_less=True)
            self.cd_gclk = ClockDomain("gclk", reset_less=True)
            div = Signal(3)
            self.comb += [
                self.cd_xclk.clk.eq(platform.request("sys_clk")),
                self.cd_fclk.clk.eq(self.cd_xclk.clk),
                self.cd_pclk.clk.eq(div[0]),
                self.cd_hclk.clk.eq(div[1]),
                self.cd_gclk.clk.eq(div[2]),
            ]
            self.sync.xclk += div.eq(div + 1)
        elif dual_clock:
            self.comb += self.cd_pclk.clk.eq(platform.request("sys_clk"))
            self.sync.pclk += hclk.eq(~hclk)
            self.comb += self.cd_hclk.clk.eq(hclk)
        else:
            self.comb += [
                self.cd_hclk.clk.eq(platform.request("sys_clk")),
                self.cd_pclk.clk.eq(self.cd_hclk.clk),
            ]

        # Reset: released after a few hClk cycles.
        reset_n = Signal()
        count   = Signal(8)
        self.sync.hclk += If(count != 0xff, count.eq(count + 1)).Else(reset_n.eq(1))

        # Game Boy LCD.
        gb_lcd_clkena = Signal()
        gb_lcd_data   = Signal(15)
        gb_lcd_mode   = Signal(2)
        gb_lcd_vsync  = Signal()
        gb_lcd_on     = Signal()

        # Cartridge.
        cart_a   = Signal(16)
        cart_d   = Signal(8)
        cart_rd  = Signal()
        cart_wr  = Signal()
        cart_cs  = Signal()
        cart_rst = Signal()
        d        = TSTriple(8)
        self.specials += d.get_tristate(cart_d)
        # Virtual cartridge hold (as the host loader: Game Boy held in reset, then released).
        vcart_hold_n = Signal(reset=int(vcart_hold > 0))
        vcart_hold_c = Signal(max=max(vcart_hold, 1) + 1)
        self.sync.hclk += If(vcart_hold_c != vcart_hold,
            vcart_hold_c.eq(vcart_hold_c + 1),
        ).Else(
            vcart_hold_n.eq(0),
        )
        if vcart:
            # Virtual cartridge (PSRAM port model in pClk as xClk), no cartridge.
            port = Record(memory_port_layout())
            dout = Signal(16)
            self.vcart     = ClockDomainsRenamer({"xclk": "pclk"})(VirtualCart(port, dout))
            self.psram     = ClockDomainsRenamer("pclk")(SimPSRAMPort(port, dout, rom, latency=vcart_latency))
            self.vcart_cfg = SimVirtualCartConfig(self.vcart, self.psram.cart_rom)
        else:
            self.cartridge = ClockDomainsRenamer("pclk")(SimCartridge(rom,
                a  = cart_a,
                d  = d,
                rd = cart_rd,
                wr = cart_wr,
                cs = cart_cs,
            ))
        self.comb += cart_rst.eq(1) # Cartridge RST pulled up.

        # Buttons ({right, left, down, up, start, select, b, a}, from buttons.hex).
        buttons  = Signal(8)
        scripted = Signal(8)
        self.specials += Instance("gb_buttons",
            i_clk     = ClockSignal("hclk"),
            i_vsync   = gb_lcd_vsync,
            o_buttons = scripted,
        )
        self.comb += buttons.eq(scripted)

        # Window: LCD to the gbwindow module, keyboard/gamepad buttons (with the scripted ones),
        # simulation end when the window is closed.
        if window:
            lcd  = platform.request("gb_lcd")
            ctrl = platform.request("sim_ctrl")
            self.comb += [
                lcd.clk.eq(ClockSignal("hclk")),
                lcd.clkena.eq(gb_lcd_clkena),
                lcd.data.eq(gb_lcd_data),
                lcd.vsync.eq(gb_lcd_vsync),
                buttons.eq(scripted | ctrl.keys),
            ]
            self.sync.hclk += If(ctrl.finish, Finish())

        # Game Boy core.
        self.specials += Instance("emu_system_top",
            i_hclk              = ClockSignal("hclk"),
            i_pclk              = ClockSignal("pclk"),
            i_reset_n           = reset_n,
            i_POWER_GOOD        = 1,
            i_customPaletteEna  = 0,
            i_paletteOff        = 0,
            i_paletteBGIn       = 0,
            i_paletteOBJ0In     = 0,
            i_paletteOBJ1In     = 0,
            i_BTN_NODIAGONAL    = 0,
            i_BTN_A             = buttons[0],
            i_BTN_B             = buttons[1],
            i_BTN_SEL           = buttons[2],
            i_BTN_START         = buttons[3],
            i_BTN_DPAD_UP       = buttons[4],
            i_BTN_DPAD_DOWN     = buttons[5],
            i_BTN_DPAD_LEFT     = buttons[6],
            i_BTN_DPAD_RIGHT    = buttons[7],
            i_BTN_MENU          = 0, # Not pressed.
            i_MENU_CLOSED       = 1,
            o_CART_A            = cart_a,
            io_CART_D           = cart_d,
            o_CART_RD           = cart_rd,
            io_CART_RST         = cart_rst,
            o_CART_WR           = cart_wr,
            o_CART_CS           = cart_cs,
            i_IR_RX             = 1,
            i_LINK_IN           = 1,
            i_LCD_INIT_DONE     = 1,
            o_gb_lcd_clkena     = gb_lcd_clkena,
            o_gb_lcd_data       = gb_lcd_data,
            o_gb_lcd_mode       = gb_lcd_mode,
            o_gb_lcd_vsync      = gb_lcd_vsync,
            o_gb_lcd_on         = gb_lcd_on,
            # Virtual cartridge.
            **(dict(
                i_VCART_EN      = self.vcart.enable,
                i_VCART_HOLD    = vcart_hold_n,
                i_VCART_WAIT    = self.vcart.wait,
                i_VCART_DATA    = self.vcart.data,
                o_VCART_A       = self.vcart.a,
                o_VCART_RD      = self.vcart.rd,
                o_VCART_WR      = self.vcart.wr,
                o_VCART_DOUT    = self.vcart.din,
                o_VCART_DMA     = self.vcart.dma,
                o_VCART_RESET   = self.vcart.reset,
            ) if vcart else dict(
                i_VCART_EN      = 0,
                i_VCART_HOLD    = 0,
                i_VCART_WAIT    = 0,
                i_VCART_DATA    = 0,
            )),
        )

        # Video pipeline (frame buffer/blend, OSD, color correction) on a PSRAM model.
        if video:
            qspi_pads  = Record([("clk", 1), ("cs_n", 1), ("mosi", 1), ("miso", 1), ("wp_n", 1), ("hd", 1)])
            psram_pads = Record([("ce_n", 1), ("clk", 1), ("dq", 8), ("dqs", 1)])
            lcd_pads   = Record([("dotclk", 1), ("db", 6), ("enable", 1), ("hsync", 1), ("vsync", 1)])
            self.comb += qspi_pads.cs_n.eq(1)
            self.memory = memory = MemorySystem(qspi_pads, psram_pads, psram_factory=SimNativePSRAM)
            self.video  = video  = VideoPipeline(lcd_pads)
            mem_reset = Signal(reset=1)
            mem_count = Signal(8)
            self.sync.xclk += If(mem_count != 0xff, mem_count.eq(mem_count + 1)).Else(mem_reset.eq(0))
            self.comb += [
                memory.reset.eq(mem_reset),
                # Game Boy LCD -> video pipeline.
                video.gb_clkena.eq(gb_lcd_clkena),
                video.gb_data.eq(gb_lcd_data),
                video.gb_mode.eq(gb_lcd_mode),
                video.gb_on.eq(gb_lcd_on),
                video.gb_vsync.eq(gb_lcd_vsync),
                # Frame buffer / OSD (PSRAM).
                memory.gb_new_line.eq(video.fb_new_line),
                memory.gb_address.eq(video.fb_address),
                memory.gb_write.eq(video.fb_write),
                memory.gb_data.eq(video.fb_data),
                memory.h_valid.eq(gb_lcd_clkena),
                memory.h_hsync.eq(gb_lcd_mode[1]),
                memory.h_vsync.eq(gb_lcd_vsync),
                video.fb_prev.eq(memory.fb_data),
                video.osd_data.eq(memory.osd_data),
                # Controls (no OSD: the menu is drawn by the ESP32).
                video.menu_disabled.eq(1),
                video.lcd_init_done.eq(1),
                video.lcd_en.eq(1),
                video.frame_blend.eq(int(frame_blend)),
                video.correct_lcd.eq(int(correct)),
                video.correct_uvc.eq(int(correct)),
            ]
            self.specials += Instance("uvc_capture",
                i_clk   = ClockSignal("gclk"),
                i_hsync = lcd_pads.hsync,
                i_vsync = lcd_pads.vsync,
                i_en    = video.uvc_en,
                i_db    = video.uvc_db,
            )

        # LCD capture (PPM frames, ends the simulation).
        self.specials += Instance("gb_lcd_capture",
            i_clk    = ClockSignal("hclk"),
            i_clkena = gb_lcd_clkena,
            i_data   = gb_lcd_data,
            i_vsync  = gb_lcd_vsync,
        )

# Build --------------------------------------------------------------------------------------------

def build_sim(gateware_dir, rom,
    threads     = 1,
    trace       = False,
    vcart       = False,
    vcart_hold  = 0,
    video       = False,
    frame_blend = False,
    correct     = False,
    dual_clock  = False,
    window      = False):
    platform = SimPlatform("SIM", _io)
    for source in verilog_sources():
        platform.add_source(source)
    platform.add_source(convert_vhdl(os.path.join(os.path.dirname(gateware_dir), "vhdl")))
    for source in ["ereg_savestatev.v", "gb_buttons.v", "gb_lcd_capture.v", "uvc_capture.v", "dffc.v"]:
        platform.add_source(os.path.join(SIM_VERILOG_PATH, source))
    # LiteX simulation DDR output model (LCD dot clock).
    import litex.build.sim
    platform.add_source(os.path.join(os.path.dirname(litex.build.sim.__file__), "verilog", "oddr_verilog.v"))
    sim_config = SimConfig()
    # xClk (video): period rounded to an even number of ps (simulation timebase).
    xclk_freq = 1e12/(2*round(1e12/(2*PCLK_FREQ)/2))
    sim_config.add_clocker("sys_clk", freq_hz=xclk_freq if video else PCLK_FREQ if dual_clock else PCLK_FREQ/2)
    top = SimTop(platform, rom,
        vcart       = vcart,
        vcart_hold  = vcart_hold,
        video       = video,
        frame_blend = frame_blend,
        correct     = correct,
        dual_clock  = dual_clock,
        window      = window,
    )
    extra_mods = {}
    if window:
        # Module sources copied to the build directory (LiteX writes its variables.mak there).
        modules_dir = os.path.join(gateware_dir, "sim_modules")
        shutil.rmtree(modules_dir, ignore_errors=True)
        shutil.copytree(os.path.join(SIM_MODULES_PATH, "gbwindow"), os.path.join(modules_dir, "gbwindow"))
        sim_config.add_module("gbwindow", ["gb_lcd", "sim_ctrl"], args={"scale": 4, "realtime": False})
        extra_mods = dict(extra_mods=["gbwindow"], extra_mods_path=modules_dir)
    platform.build(top,
        build_dir  = gateware_dir,
        sim_config = sim_config,
        opt_level  = "O3",
        threads    = threads,
        trace      = trace,
        run        = False,
        **extra_mods,
    )
    # Compile (LiteX only compiles when also running the simulation).
    compile_sim(gateware_dir)

def compile_sim(gateware_dir, cflags="", ldflags=""):
    """
    Compile the Verilator model with the LiteX simulation Makefile, tuned for speed: generated code
    not split in small functions (--output-split*: +70% here), optional extra C/LD flags (PGO).
    """
    import litex.build.sim
    litex_makefile = os.path.join(os.path.dirname(litex.build.sim.__file__), "core", "Makefile")
    with open(litex_makefile, encoding="utf-8") as f:
        makefile = f.read()
    makefile = re.sub(r"--output-split(-\w+)? \d+", lambda m: f"--output-split{m.group(1) or ''} 1000000", makefile)
    makefile = makefile.replace("-LDFLAGS \"$(LDFLAGS)\"", "-LDFLAGS \"$(LDFLAGS) $(EXTRA_LDFLAGS)\"")
    with open(os.path.join(gateware_dir, "Makefile.sim"), "w", encoding="utf-8") as f:
        f.write(makefile)
    with open(os.path.join(gateware_dir, "build_sim.sh"), encoding="utf-8") as f:
        script = f.read()
    script = script.replace(f"-f {litex_makefile}", "-f Makefile.sim")
    script = re.sub(r"OPT_LEVEL=\S+", f"OPT_LEVEL=\"O3 {cflags}\" EXTRA_LDFLAGS=\"{ldflags}\" JOBS={os.cpu_count()}", script)
    with open(os.path.join(gateware_dir, "build_sim_fast.sh"), "w", encoding="utf-8") as f:
        f.write(script)
    subprocess.run(["bash", "build_sim_fast.sh"], cwd=gateware_dir, check=True, stdout=subprocess.DEVNULL)

def pgo_sim(gateware_dir, rom, frames=20):
    """Profile-guided optimization: instrumented build, run on the ROM, optimized build."""
    pgo_dir = os.path.join(gateware_dir, "pgo")
    shutil.rmtree(pgo_dir, ignore_errors=True)
    compile_sim(gateware_dir, cflags=f"-fprofile-generate -fprofile-dir={pgo_dir}", ldflags="-fprofile-generate")
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy") # Training run without window.
    run_sim(gateware_dir, rom, frames=frames, every=frames + 1)
    if os.environ["SDL_VIDEODRIVER"] == "dummy":
        del os.environ["SDL_VIDEODRIVER"]
    compile_sim(gateware_dir, cflags=f"-fprofile-use -fprofile-dir={pgo_dir} -fprofile-partial-training "
        "-Wno-missing-profile", ldflags="-fprofile-use")

def run_sim(gateware_dir, rom, frames=60, every=1, presses=[]):
    """Run the simulation (ROM/buttons files + plusargs), returns the captured PPM frames."""
    # Boot ROM ($readmemh relative path, from the simulation directory).
    bootroms = os.path.join(gateware_dir, "BootROMs")
    if not os.path.exists(bootroms):
        os.symlink(os.path.join(VERILOG_PATH, "emu", "CORE", "BootROMs"), bootroms)
    for ppm in glob.glob(os.path.join(gateware_dir, "*_*.ppm")):
        os.remove(ppm)
    write_rom_init(os.path.join(gateware_dir, "sim_cart_rom.init"), rom)
    write_button_events(os.path.join(gateware_dir, "buttons.hex"), presses)
    subprocess.run(["obj_dir/Vsim", f"+frames={frames}", f"+every={every}"], cwd=gateware_dir, check=True)
    return sorted(glob.glob(os.path.join(gateware_dir, "frame_*.ppm")))

def run_window(gateware_dir, rom, presses=[], scale=4, realtime=False, frames=2**31 - 1,
    screenshot_frame=-1):
    """Run the simulation in a window (gbwindow module) until it is closed (or frames)."""
    config_file = os.path.join(gateware_dir, "sim_config.js")
    with open(config_file, encoding="utf-8") as f:
        config = json.load(f)
    modules = [m for m in config if m.get("module") == "gbwindow"]
    if not modules:
        raise ValueError("Simulation not built with --window.")
    modules[0]["args"] = {"scale": scale, "realtime": realtime, "screenshot_frame": screenshot_frame}
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)
    for bmp in glob.glob(os.path.join(gateware_dir, "screenshot_*.bmp")):
        os.remove(bmp)
    run_sim(gateware_dir, rom, frames=frames, every=2**31 - 1, presses=presses)

def uvc_frames(gateware_dir):
    """Video pipeline output frames (--video) of the last run."""
    return sorted(glob.glob(os.path.join(gateware_dir, "uvc_*.ppm")))

def ppm_to_png(ppm, png, scale=1):
    from PIL import Image
    image = Image.open(ppm)
    if scale > 1:
        image = image.resize((image.width*scale, image.height*scale), Image.NEAREST)
    image.save(png)

def main():
    parser = argparse.ArgumentParser(description="ChromatiX simulation (Verilator): Game Boy core + cartridge + LCD capture.")
    parser.add_argument("--rom",         required=True,         help="Game Boy ROM (ROM only, MBC1 or MBC5).")
    parser.add_argument("--frames",      default=60,  type=int, help="Frames to simulate.")
    parser.add_argument("--every",       default=1,   type=int, help="Capture one frame every N frames.")
    parser.add_argument("--buttons",     default="",            help="Button presses: button@frame[+frames],... (ex: start@200+10,a@300).")
    parser.add_argument("--output-dir",  default="build/sim",   help="Build/output directory.")
    parser.add_argument("--scale",       default=2,   type=int, help="PNG scale factor.")
    parser.add_argument("--threads",     default=1,   type=int, help="Verilator threads.")
    parser.add_argument("--trace",       action="store_true",   help="Enable waveform tracing (VCD).")
    parser.add_argument("--no-compile",  action="store_true",   help="Run on the previous build (any ROM/frames/buttons).")
    parser.add_argument("--vcart",       action="store_true",   help="Virtual cartridge (ROM served from a PSRAM model).")
    parser.add_argument("--vcart-hold",  default=0,   type=int, help="Virtual cartridge: Game Boy held in reset for N hClk cycles.")
    parser.add_argument("--video",       action="store_true",   help="Video pipeline on a PSRAM model (uvc_* frames: LCD panel/UVC output).")
    parser.add_argument("--frame-blend", action="store_true",   help="Video: frame blending.")
    parser.add_argument("--correct",     action="store_true",   help="Video: LCD/UVC color correction.")
    parser.add_argument("--dual-clock",  action="store_true",   help="Exact clocking: pClk and hClk = pClk/2 (default: single clock, pClk = hClk, faster).")
    parser.add_argument("--pgo",         action="store_true",   help="Profile-guided optimization of the build (trained on --rom, ~+20% speed).")
    parser.add_argument("--window",      action="store_true",   help="Play in a window (keyboard/gamepad, until closed): arrows, X: A, Z: B, Enter: Start, Backspace: Select, P: pause, F12: screenshot.")
    parser.add_argument("--window-scale", default=4,  type=int, help="Window scale.")
    parser.add_argument("--realtime",    action="store_true",   help="Window: never faster than the Game Boy.")
    parser.add_argument("--bench",       action="store_true",   help="Benchmark: run --frames frames (none written), print the simulation speed.")
    args = parser.parse_args()

    with open(args.rom, "rb") as f:
        rom = f.read()
    check_rom(rom)
    presses = parse_button_sequence(args.buttons)

    output_dir   = os.path.abspath(args.output_dir)
    gateware_dir = os.path.join(output_dir, "gateware")
    frames_dir   = os.path.join(output_dir, "frames")

    # Build / Run.
    if not args.no_compile:
        build_sim(gateware_dir, rom,
            threads     = args.threads,
            trace       = args.trace,
            vcart       = args.vcart,
            vcart_hold  = args.vcart_hold,
            video       = args.video,
            frame_blend = args.frame_blend,
            correct     = args.correct,
            dual_clock  = args.dual_clock,
            window      = args.window,
        )
        if args.pgo:
            pgo_sim(gateware_dir, rom)
    if args.bench:
        if args.window:
            os.environ["SDL_VIDEODRIVER"] = "dummy" # Benchmark without window display.
        start = time.time()
        run_sim(gateware_dir, rom, frames=args.frames, every=args.frames + 1, presses=presses)
        elapsed = time.time() - start
        fps     = args.frames/elapsed
        print(f"[bench] {args.frames} frames in {elapsed:.1f}s: {fps:.2f} fps, {fps/GB_FPS:.3f}x realtime.")
        return
    if args.window:
        run_window(gateware_dir, rom, presses=presses, scale=args.window_scale, realtime=args.realtime)
        return
    ppms = run_sim(gateware_dir, rom, frames=args.frames, every=args.every, presses=presses)

    # Frames -> PNG.
    os.makedirs(frames_dir, exist_ok=True)
    for png in glob.glob(os.path.join(frames_dir, "*_*.png")):
        os.remove(png)
    for ppm in ppms + uvc_frames(gateware_dir):
        ppm_to_png(ppm, os.path.join(frames_dir, os.path.basename(ppm)[:-4] + ".png"), args.scale)
    print(f"{len(ppms)} frames written to {frames_dir}.")

if __name__ == "__main__":
    main()

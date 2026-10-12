# Hosting FPGA cores on the Chromatic (`--with-core`)

A reusable LiteX "shell" to host a third-party FPGA core (a MiSTer / Tang-lineage console core: NES,
Sega Master System / Game Gear, ...) on the Chromatic (Gowin **GW5A-25**), in place of the Game Boy
core, reusing the video pipeline, PSRAM, audio and buttons unchanged.

Status (incremental): the shell and the downscaler are implemented and **verified on hardware** (a
color-bar test source, native and downscaled, shown on the LCD and over UVC). A worked core port is
in progress; this document is the methodology to port one.

## Why this is a "MiST-class", not a "MiSTer-class", target

MiSTer targets the DE10-Nano (Cyclone V, ~110K LE, 5.5 Mb BRAM, 112 DSP, a low-latency SDRAM module,
and an ARM/Linux HPS). The GW5A-25 is the same part as the Tang Primer 25K: **23,040 LUT4, 56 BSRAM
(~1 Mb), 28 DSP, 6 PLL**, with **8 MB octal PSRAM** (16-bit @ ~67 MHz, single arbiter, ~3.4 µs
random-read latency — *not* SRAM-like) and no host CPU. So:

- **Fits** (proven on this exact FPGA by nand2mario/TangCore and Harbaum/MiSTeryNano): NES, SMS/Game
  Gear, SNES (tight), Atari ST, C64, Amiga.
- **Does not fit** (needs a 60K/138K-class part): Genesis/Mega Drive, GBA, 486, and everything
  32-bit+ (N64, PSX, Saturn). "Reuse for N64" means reusing *this methodology and tooling* on bigger
  silicon, not this bitstream.
- The **binding resource is BSRAM**, and the **memory remap** (the core's SDRAM onto octal PSRAM /
  BRAM) is the gating difficulty, smallest for cores whose work/video RAM fits in BRAM (NES, SMS).

## Architecture

```
          hosted core (its own clock)                    reused ChromatiX pipeline (hClk/gClk)
  ┌───────────────────────────────────┐        ┌───────────────────────────────────────────────┐
  │  core RTL (CPU/PPU/APU, mappers)   │        │ VideoPipeline → ST7785 panel (LCD)             │
  │   video  ─ ce/rgb/hblank/vblank ───┼──┐     │               → UVC (USB)                      │
  │   audio  ─ left/right ─────────────┼─ │ ──→ │ shared audio bus → TLV320 I2S codec            │
  │   input  ← joystick ───────────────┼─ │ ←── │ console buttons (Buttons/ButtonsCSR)           │
  │   ROM    ← ioctl / memory port ────┼─ │ ←── │ PSRAM / BRAM (ROM loaded from the PC bridge)    │
  └───────────────────────────────────┘  │     └───────────────────────────────────────────────┘
                                          │
                 CoreVideoDownscaler ─────┘  (nearest-neighbor any WxH → 160x144 GB LCD interface)
```

**The one video contract.** Every source on the panel presents the *Game Boy LCD interface*
(`chromatix/gateware/core_shell.py`, matching `framebuffer.py`): in the `hclk` (~16.78 MHz) domain,
`gb_clkena` (1 strobe/pixel, 160/line), `gb_data[14:0]` RGB555 `{B,G,R}`, `gb_mode[1:0]`
(`mode[1]`=hsync), `gb_on`, `gb_vsync`; 160×144 visible, ~59.7 Hz. **160×144 is hard-wired** through
the panel line buffer, PSRAM stride and UVC geometry; there is no scaler/cropper in the pipeline, and
UVC only *integer-upscales* 160×144.

- A **160×144-native** core (Game Gear) drives this interface directly (see `CoreTestPattern`).
- A **larger** core (NES 256×240, SMS 256×192) goes through `CoreVideoDownscaler`: a nearest-neighbor
  (Bresenham-on-write) downscaler from the generic `CoreVideo` interface (`ce`/`rgb555`/`hblank`/
  `vblank`, the MiSTer `CE_PIXEL`/`VGA_*`/blanking shape) into a frame buffer, scanned out as the GB
  LCD interface. (First version: BRAM frame buffer, single-buffered — minor tearing on fast motion;
  PSRAM triple-buffering is the scalable follow-up and frees ~30 BSRAM for the core.)

## The per-core port loop (the portable methodology)

1. **Inventory** the core's RTL. Keep the *inner* core module (the DUT that MiSTer's `emu` wraps);
   **drop** `emu`, `sys_top.v`, and the whole `sys/` framework (OSD, scaler, HPS bridge).
2. **Classify** each file: portable logic vs. vendor-primitive-dependent.
3. **Lower** each vendor primitive to a LiteX/portable equivalent, and prove equivalence per block
   with `test/eqcheck.py` (Yosys), the flow in `doc/MIGRATION.md`:
   | MiSTer / vendor primitive | ChromatiX / LiteX replacement |
   |---|---|
   | `altpll` / Gowin PLL IP | `GW5APLL` (litex) + a new `ClockDomain` + SDC in `TIMING_CONSTRAINTS` |
   | `altsyncram` / `dpram` | inferred BSRAM (Migen `Memory`) — vendor-neutral |
   | hard multipliers | inferred DSP (28 available) |
   | `sys/sdram.sv` | ROM/large RAM in PSRAM behind a cache (the `VirtualCart` cache), or in BRAM for small cores; the LiteX OPI PSRAM core, never a hand-written SDRAM controller |
   | `scandoubler` / `video_mixer` | `CoreVideoDownscaler` → GB LCD interface |
   | Xilinx carry (`MUXCY`/`XORCY`, fpganes-lineage NES) | a small shim or inference |
4. **Rebuild the top** as a LiteX wrapper (the shell): clock, video sink (downscaler), audio to the
   shared `left/right` bus, buttons → the core's joystick, and ROM via an ioctl-style loader / memory
   port.
5. **Test**: Migen unit tests + the Verilator frame-capture sim (`chromatix_sim.py`) + `eqcheck`,
   then on hardware.

## Memory strategy (the hard part)

The octal PSRAM is *not* SRAM: 16-bit @ 67 MHz, one master at a time, ~0.3 µs/burst, ~3.4 µs random
read. So:

- Console **work/video RAM** (NES 2 KB+2 KB, SMS 8 KB+16 KB) → **BRAM** (single-cycle, what the core
  expects). Fits easily once the BRAM frame buffer is moved to PSRAM.
- **ROM** → **BRAM** for a small game (NROM ≤ 40 KB fits once the frame buffer is on PSRAM), or
  **PSRAM behind a cache** (reuse `VirtualCart`'s direct-mapped 16-byte-line cache, freezing the core
  on a miss) for larger ROMs/mappers.
- The **frame buffer** → **PSRAM** (triple-buffered, the `LCDPSRAMFramebuffer` reader), to keep BRAM
  for the core and remove tearing.

## Loading a ROM

Bring-up uses the PC debug bridge (no ESP32 firmware change): build `--with-core --with-debug-bridge`,
and `scripts/chromatic.py` writes the ROM into the PSRAM/BRAM ROM region over UARTBone (as it does
for the Game Boy virtual cartridge). The productized path (reuse the ChroMagic ESP32/SD QSPI 1 KB
upload) is a later step.

## Input and audio

- **Input**: the console buttons (`Buttons`/`ButtonsCSR`, D-pad + A/B + Start/Select, plus the ESP32
  `mcu_buttons`) map to the core's joystick (`CoreInput`).
- **Audio**: the core's 16-bit samples drive the shared `left/right` bus → `CodecI2S` → TLV320 (as
  the Game Boy audio does); no PWM.

## Build, flash, test

```bash
# Elaborate / test (no toolchain):
python3 chromatix.py --with-core --build --no-compile
python3 -m pytest test/test_core_shell.py test/test_soc.py -q

# Bitstream (headless: unset DISPLAY so Gowin's gw_sh does not try a GL context):
env -u DISPLAY python3 chromatix.py --with-core --gowin-path ~/tools/gowin_1.9.12.04/IDE --build

# Flash (auto-backs-up the factory image to ~/chromatix-backups; `restore` reverts):
python3 scripts/chromatix_flash.py flash build/gateware/chromatic.fs

# Verify over UVC (no debug bridge needed):
python3 scripts/chromatic.py capture core.png --size 320x288
```

## Worked example: NES (in progress — core runs, game not yet rendering)

The NES is the first worked port (NROM-class, the bundled homebrew `nes15`, 16 KB PRG + 8 KB CHR),
hosting nand2mario's `nestang` core (same GW5A-25 as the Tang Primer 25K). Build it with
`--with-core nes` after dropping the RTL in (see `chromatix/verilog/cores/nes/README.md`).

**What works (verified on hardware):**
- The whole thing **synthesizes and fits**: logic 54%, BSRAM 35/56, DSP 15/28, 3/6 PLLs, timing met
  (only the pre-existing benign PSRAM reset-recovery paths). The ~50 mappers are pruned by feeding a
  **constant `mapper_flags`** (NROM); VRC/FDS excluded.
- The glue (`chromatix/gateware/cores/nes.py`, BSD, unit-tested in `test/test_nes.py`): `NESCore`
  (its own ~21.48 MHz PLL + a 4× clock for the ROM BRAM), `NESVideoDownscaler` (NES
  `color`/`cycle`/`scanline` → 2C02 palette → 256×240 nearest-neighbor downscale → 160×144 Game Boy
  LCD interface, storing the 6-bit index so the frame buffer is ~12 BSRAM), `NESMemory` (PRG at 0.. +
  CHR at 0x200000.. in BRAM, read-only, direct-loaded from the parsed iNES at build time, 16 KB PRG
  mirroring), `NESJoypad` (buttons → the standard controller serial).
- On hardware the **NES core clocks and its PPU produces video** (confirmed: it overwrites a
  pre-initialised frame buffer), and the **display path is proven** (a palette ramp shows correctly
  over UVC).

**What does not work yet:** the game renders **black** — the CPU is clocking but never enables PPU
rendering (it is not reaching the game's draw code). Ruled out by inspection/experiment: clock source
(clk_24, proven live), reset sequence (robust up-counter), ROM parse and reset/NMI/IRQ vectors
(verified), PRG/CHR address mapping (`cpumem_addr`=PRG, `ppumem_addr`=CHR, confirmed), CPU/PPU
dividers and `sys_type` (match nestang), and memory read latency (added a 4× BRAM read clock). The
remaining fault is in CPU execution and is best found in **simulation** (a Verilator testbench of the
core + glue, or nestang's own sim with `nes15`) — hardware build/flash/capture cycles (~8 min) are
too slow to probe it. This is the documented next step.

**BRAM budget** (resolved): PRG/CHR BRAM sized to the ROM, and the frame buffer stores the 6-bit
palette index (palette applied on read) → ~12 BSRAM, giving 35/56 overall. A PSRAM-backed
triple-buffered frame buffer remains the scalable follow-up for larger cores (and removes tearing).

## Prior art (mine, don't reinvent)

- **nand2mario / TangCore**: `nestang` (NES, `game_loader.v`/`dpram.v`/`compat.v`), `snestang`,
  `mdtang`, `gbatang` — MiSTer cores on Gowin, same GW5A-25. Memory/timing lessons: run SDRAM at an
  integer multiple of the core clock, assert "ready" a cycle early for sequential bursts, put slow
  sub-CPUs (Z80) on a clock-enable, and rebuild cycle-counting cores as low-clock replicas (Gowin
  Fmax is modest).
- **Harbaum / MiSTle**: `MiSTeryNano` (has a GW5A-25 + LCD build), `FPGA-Companion` (an MCU — incl.
  ESP32 — replaces the MiSTer HPS over SPI: `mcu_spi`/`sysctrl`/`hid`/`sd_card`/`osd_u8g2`). The
  reference for the later ESP32-as-host step.
- MiSTer developer docs: the `emu` and `hps_io` signal contracts.

## Licensing

MiSTer cores are GPLv2/3; a bitstream containing one is a GPL derivative. ChromatiX is dual GPL/BSD:
the **shell stays BSD** (`core_shell.py` and the downscaler contain no core RTL), core RTL lives under
`chromatix/verilog/cores/<core>/` with its own license, and the combined `--with-core <core>` build
is **GPL**.

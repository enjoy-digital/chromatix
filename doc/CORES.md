# Hosting FPGA cores on the Chromatic (`--with-core`)

A reusable LiteX "shell" to host a third-party FPGA core (a MiSTer / Tang-lineage console core: NES,
Sega Master System / Game Gear, ...) on the Chromatic (Gowin **GW5A-25**), in place of the Game Boy
core, reusing the video pipeline, PSRAM, audio and buttons unchanged.

Status (incremental): the shell, the downscaler and a first worked core (**NES**) are implemented and
**verified on hardware** — a NES game (the bundled `nes15`) renders and is playable on the Chromatic,
downscaled to 160×144 and shown over UVC. This document is the methodology to port another.

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

**The gotcha that cost the most (read this):** a MiSTer core often puts its CPU/work RAM and its
video RAM in the *same external memory space* as the cartridge ROM, not in internal registers — so
when you replace SDRAM with your own memory you must provide **writable** RAM for those regions, not
just ROM. The NES core (`cart.sv`) maps the 2 KB CPU RAM (including the 6502 stack `$0100-$01FF`) to
`cpumem` 0x380000 and the 2 KB nametable CIRAM to `ppumem` 0x300000. Serving the whole space read-only
looks fine (the CPU fetches correct code) until the first `JSR`/`RTS`: the stack write is dropped and
`RTS` reads ROM bytes as the return address, jumping the CPU into garbage. Decode the core's address
map (grep the cart/mapper for the RAM/CIRAM base constants) and route **read/write BRAM** for every
RAM region; see `NESMemory`'s region split.

## Loading a ROM

Bring-up uses the PC debug bridge (no ESP32 firmware change): build `--with-core --with-debug-bridge`,
and `scripts/chromatic.py` writes the ROM into the ROM region over UARTBone (as it does for the Game
Boy virtual cartridge). The NES does this today — `nes-load` streams PRG/CHR into BRAM and the game
runs with no rebuild (see the worked example below). The productized path (reuse the ChroMagic
ESP32/SD QSPI 1 KB upload) is a later step.

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

## Worked example: NES — runs a game on hardware

The NES is the first worked port (NROM-class, the bundled homebrew `nes15`, 16 KB PRG + 8 KB CHR),
hosting nand2mario's `nestang` core (same GW5A-25 as the Tang Primer 25K). Build it with
`--core nes` after dropping the RTL in (see `chromatix/verilog/cores/nes/README.md`). **The game
renders and is playable on the Chromatic** (verified over UVC), downscaled 256×240 → 160×144.

**The glue** (`chromatix/gateware/cores/nes.py`, BSD, unit-tested in `test/test_nes.py`):
- `NESCore` — its own ~21.48 MHz PLL (from `clk_24`) + a 4× clock so the ROM/RAM BRAM is fresh within
  one core cycle; a constant `mapper_flags` prunes the ~50 mappers at synthesis (NROM; VRC/FDS
  excluded) so the core fits (else ~58K LUT > 23K).
- `NESVideoDownscaler` — NES `color`/`cycle`/`scanline` → 2C02 palette → 256×240 nearest-neighbor
  downscale → the 160×144 Game Boy LCD interface, storing the **6-bit palette index** (palette applied
  on read) so the frame buffer is ~12 BSRAM.
- `NESMemory` — the core's unified 22-bit space in BRAM: read-only PRG (0x000000) / CHR (0x200000)
  ROM (direct-loaded from the parsed iNES at build time, 16 KB PRG mirrored), and **read/write** CPU
  RAM (0x380000) + nametable CIRAM (0x300000). See the memory-strategy gotcha above.
- `NESJoypad` — console buttons (+ ESP32 `mcu_buttons`) → the standard NES controller serial.

Fit: logic 54%, **BSRAM 37/56**, DSP 15/28, timing met (0 setup/hold violations; only benign
async-clock warnings).

**How the black screen was fixed.** The core ran, the PPU raster + vblank worked, but the game stayed
black. A Verilator sim of the core + glue (`chromatix_sim.py --core nes`, see below) reproduced it
deterministically and the CPU PC trace showed the CPU crashing on its **first `RTS`** into
uninitialised `$00`/BRK memory — because `NESMemory` served the CPU RAM/stack as read-only ROM (the
gotcha above). Making the CPU RAM + CIRAM writable fixed it. (An early "CPU/PPU vblank-flag phase"
theory was a trace-measurement artifact — the `$2002` read tap was a *level*, not a per-read pulse.)

**Debugging workflow (reusable for the next core):**
```bash
# Build the Verilator sim of the core (single-threaded is ~100x faster here than multi-threaded):
python3 -c "import chromatix_sim as cs; cs.build_sim_nes('build/sim_nes', threads=1, trace_cpu=True)"
# Run N NES cycles; nes_trace.v logs the CPU PRG bus (windowed via +rd_lo/+rd_hi) and a periodic PC
# snapshot, flushed, self-terminating at +nes_ticks:
cd build/sim_nes && ./obj_dir/Vsim +frames=1000 +nes_ticks=2500000
# Frames are frame_*.ppm; a stuck/looping PC in nes_trace.log -> disassemble the ROM at that PC.
```
`sanitize_nes_rtl()` rewrites a Verilator-5.x-strict construct the vendored mappers use (a bare ANSI
`output` port redeclared as a body `wire`) into a build-dir copy, leaving the repo RTL untouched.

### Runtime ROM loader (verified on hardware)

`--core nes --with-debug-bridge` adds a **runtime ROM loader**: any NROM game is uploaded over the
debug bridge and runs **without a rebuild or reflash**. Verified on hardware — the same bitstream
boots empty, then `nes15` (the "15" puzzle) and `helloworld` ("HELLO WORLD") each load in ~1 s and
render, swapping live:

```bash
env -u DISPLAY python3 chromatix.py --core nes --with-debug-bridge --gowin-path ~/tools/.../IDE --build
python3 scripts/chromatix_flash.py flash build/gateware/chromatic.fs
python3 scripts/chromatic.py --serial /dev/ttyACM0 nes-load path/to/game.nes   # .nes or .hex, NROM
python3 scripts/chromatic.py capture game.png --size 320x288
```

How it works (`NESCore(with_loader=True)` + `NESLoaderCSR`, `scripts/chromatic.py nes_load`): the
PRG/CHR BRAMs are fixed to the max NROM window (32 KB/8 KB) and made host-writable (a second,
write-only BRAM port); the host holds the core in reset, sets the config (PRG size, mirroring,
CHR-RAM), streams PRG then CHR as 32-bit words (a 4-byte spreader auto-increments the address), and
releases the hold so the core boots fresh on the new ROM. The **mapper number stays a constant 0**
(only the size/mirror/CHR-RAM `mapper_flags` bits are runtime), so synthesis still prunes the ~50
other mappers. Fit: **BSRAM 47/56**, DSP 13/28, timing met.

> **Gotcha (BSRAM inference):** keep a loadable ROM `Memory` in `specials` only, never as a module
> attribute (`self.<name>`). With a bus present (`--with-debug-bridge`), LiteX auto-maps an
> attribute `Memory` onto the CSR bus, adding a third port — Gowin then cannot infer BSRAM and falls
> back to registers ("DFF … exceeds the resource limit"). Also: Gowin will not infer BSRAM for a
> memory that is *both* `init`-ialised *and* written, so the loader's PRG/CHR are uninitialised
> (the host uploads the ROM); the non-loader build keeps the read-only `init` ROM.

**Loading a different game with a dedicated build** (no debug bridge) also works: `--nes-rom
game.nes` bakes any NROM ROM into the bitstream (verified: `helloworld` renders).

**Next for the NES:** a PSRAM-backed frame buffer (frees ~30 BSRAM, double-buffered — headroom and
no tearing for larger cores); ESP32/SD loading for standalone play.

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

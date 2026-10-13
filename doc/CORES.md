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

**Next for the NES:** ESP32/SD loading for standalone play. (A PSRAM-backed *downscaler* frame
buffer was considered and **deliberately deferred** — see the note below: the NES display is already
PSRAM-backed and the only BRAM frame buffer is the downscaler's small indexed one, so the payoff for
the NES is low; it matters for a BSRAM-starved core.)

### Note: the display is already PSRAM-backed; a PSRAM downscaler FB is a core-specific option

Every `lcd_source` (Game Boy core, CPU framebuffer, or `NESCore`) drives the **GB-LCD interface**
(`gb_clkena`/`gb_data`/…), which `VideoPipeline` writes into the **PSRAM** framebuffer (`memory.gb_*`)
and reads back for the panel + UVC (with frame blending). So a hosted core's shown frame already lives
in PSRAM. The only on-chip frame buffer is `NESVideoDownscaler`'s internal **6-bit-indexed 160×144**
store (~12 BSRAM), which decouples the core's 256×240 raster (core clock) from the 160×144 GB-LCD
scanout (hClk). Moving *that* to PSRAM (core writes the downscaled RGB555 to `memory.gb_*` directly +
generates its own GB-LCD read timing from `fb_data`, bypassing its BRAM) saves ~12 BSRAM and allows
double-buffering (no tearing), but adds a PSRAM write+read stream. The NES fits comfortably
(loader build 47/56), so this is deferred until a core is BSRAM-starved (e.g. a maxed SMS) — the
mechanism (`memory.gb_*` write + `LCDPSRAMFramebuffer`-style read) is the one to reuse then.

## Worked example: SMS / Game Gear — the 2nd core (builds & fits)

The 2nd core proves the methodology generalises to a different console family. Target:
nand2mario's [`smstang`](https://github.com/nand2mario/smstang) (SMS-MiSTer ported to Gowin; same
GW5A-25). **Status: `--core sms --with-debug-bridge` builds and FITS** — BSRAM **56/56**, 0 timing
violations, logic ~72%. The GPL RTL drops under `chromatix/verilog/cores/sms/` (gitignored; see its
README); `chromatix/gateware/cores/sms.py` (`SMSCore`, BSD) + `--core sms` wire the `system` DUT to
the shell: clock-enables, the `video` raster, system RAM in BRAM, audio, input, the **PSRAM
cartridge cache**, and the SMS BIOS.

The fit blocker was memory: with a BRAM cartridge the frame buffer + 16 KB system RAM + 16 KB VRAM +
the 16 KB boot ROM + the cartridge overflow the 56 BSRAM, so the cartridge ROM (up to 512 KB) goes in
**PSRAM behind a cache** — `chromatix/gateware/cores/rom_cache.py` (`CoreROMCache`, the generalisation
of `vcart.py`'s Game-Boy cache): a direct-mapped 16-byte-line cache in the core domain in front of
one PSRAM arbiter port, which `stall`s the core (gating its clock-enables, so the whole core pauses
in cadence and resumes) on a miss while the line is fetched. This is the one hard piece the NES
avoided (NROM fit in BRAM). The host uploads the ROM over the debug bridge (`scripts/chromatic.py
sms-load`) into the PSRAM at `SMS_ROM_BASE`; the `--core sms` branch boots the SMS **BIOS**
(`mboot.hex`) in SMS mode (256×192 → downscaler) so it renders a screen with no cartridge — the first
hardware validation of the video path — then boots the uploaded cartridge.

The FM (`opll`) is stubbed (`opll_stub.v`, BSD) to skip the VHDL VM2413 and save area — fine for the
vast majority of SMS games and all Game Gear games; drop in the real `vm2413/` for FM titles.

**Render bring-up (in progress).** On hardware the core froze right after its first cartridge fetch.
A cache debug CSR (`SMSLoaderCSR(with_debug=True)` — FSM state / stall / reset / `rom_do`) pinpointed
it: the cache sat in **FILL forever** because the core left reset and issued its first PSRAM read
**before the PSRAM BIST/init finished**, so the one-cycle arbiter request was dropped and the line
fill never completed. Fixed by holding the SMS in reset until the PSRAM is ready
(`cart_ready = bist_done`).

**The routing wall (the real blocker).** The SMS core is a tight fit on the GW5A-25 (smstang itself
targets the Tang Console **60K/138K**, not the 25K): ~16.4 K LUT (71%), near-full BSRAM, and four
clock domains (sms 53.69 MHz, hClk, xClk, sys) with several CDC crossings. P&R **places** fine but
**routing** is at the edge — one configuration routed cleanly (0 unrouted) and *any* change (the
reset-gate fix, a debug CSR) tips it to tens-to-hundreds of unrouted nets.

Freeing BSRAM was tried first and is **not** the lever: the reduced-depth frame buffer (RGB333) took
the build from 56/56 to **45/56 BSRAM**, yet routing got *worse* (463 unrouted) — the odd-width BRAM
placed poorly and, more importantly, the bottleneck is interconnect/logic, not BSRAM. So a reliably
routable (and therefore renderable) build needs to **reduce logic/interconnect** or route with more
effort — in order of value: **prune the SMS core's unused blocks** (it instantiates `CODES`
cheat-codes, `MC8123` decryption, `SEGASYS1` arcade decode, lightgun — all tied off here; removing
their instances in `system.v` is the biggest LUT win), **raise Gowin place/route effort / replicate
high-fanout nets**, a **PSRAM-backed frame buffer** (moves the FB out of BRAM entirely and simplifies
the sms↔hClk path), or ultimately a **larger part**. This is the clear next step for the render.

**Also remaining** (after a routable build): video color/raster alignment (the `color[11:0]` output
lags `x`/`y` by the VDP pipeline depth — expect a shifted image first, like the NES bring-up); the
Game Gear 160×144-native path (no downscaler); FM audio (the real VM2413).

Why SMS/GG is a good second target and what is *new* vs. the NES:
- **Clean clock-enable DUT.** `system.v` runs off one 53.69 MHz `clk_sys` with enables `ce_cpu` (÷15),
  `ce_vdp` (÷5), `ce_pix` (÷10), `ce_sp` — exactly the shape Gowin likes. Map to a `GW5APLL` + a
  divider (cadence from `smstang_top.sv`). No cycle-exact gymnastics.
- **Random-access video.** Unlike the NES's streamed `color`, the SMS DUT is scanned: drive
  `x[8:0]`/`y[8:0]`, read `color[11:0]` (BGR 4-4-4 → RGB555). **Game Gear is 160×144 native → drive
  the GB-LCD interface directly, no downscaler/FB (do GG first).** SMS is 256×192 → reuse the
  downscaler.

Why SMS/GG is a good second target and what is *new* vs. the NES:
- **Clean clock-enable DUT.** `system.v` runs off one 53.69 MHz `clk_sys` with enables `ce_cpu` (÷15),
  `ce_vdp` (÷5), `ce_pix` (÷10), `ce_sp` — exactly the shape Gowin likes. Map to a `GW5APLL` + a
  divider (cadence from `smstang_top.sv`). No cycle-exact gymnastics.
- **Random-access video.** Unlike the NES's streamed `color`, the SMS DUT is scanned: drive
  `x[8:0]`/`y[8:0]`, read `color[11:0]` (BGR 4-4-4 → RGB555). **Game Gear is 160×144 native → drive
  the GB-LCD interface directly, no downscaler/FB (do GG first).** SMS is 256×192 → reuse the
  downscaler.
- **ROM from PSRAM behind a cache — the new hard part.** `rom_rd`/`rom_a[21:0]`/`rom_do[7:0]`
  (+`rom_do_valid`, ~2 cycles after `ce_cpu`) is served from SDRAM upstream; here it becomes a
  PSRAM-cache port (reuse `VirtualCart`'s direct-mapped line cache; freeze the core via the clock
  enables on a miss). The NES avoided this (NROM fit BRAM). *First-milestone shortcut*: bring the
  core up on a small (≤48 KB) ROM in BRAM, add the PSRAM cache after.
- **Work RAM** `ram_*` (8 KB) → inferred BRAM; VDP VRAM (16 KB) is internal. **Audio** `audioL/R`
  (jt89 PSG + optional VM2413 FM — disable FM to save area) → the shared bus. **Input** `j1_*` ←
  console buttons.

The combined build is GPL; the shell/glue stays BSD (upstreamable).

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

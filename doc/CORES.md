# Hosting FPGA cores on the Chromatic (`--with-core`)

A reusable LiteX "shell" to host a third-party FPGA core (a MiSTer / Tang-lineage console core: NES,
Sega Master System / Game Gear, ...) on the Chromatic (Gowin **GW5A-25**), in place of the Game Boy
core, reusing the video pipeline, PSRAM, audio and buttons unchanged.

Status: two cores run **commercial games on hardware**, loaded from the PC at runtime (no rebuild):
- **NES** (`--core nes --with-debug-bridge`, `nes-load`): NROM / mapper-0 games (32 KB PRG + 8 KB
  CHR window). Super Mario Bros. is playable (Start → World 1-1, run/jump/scroll); Donkey Kong,
  Galaga, Excitebike, Ice Climber, Pac-Man, Balloon Fight, Xevious render their title/attract modes.
  Downscaled 256×240 → 160×144.
- **Game Gear** (`--core sms --sms-game-gear --with-debug-bridge`, `sms-load`): Sega-mapper ROMs
  from the PSRAM cartridge cache. Sonic the Hedgehog is playable (title → Green Hill, rings,
  enemies); Columns, Aleste, Fantasy Zone Gear, Bubble Bobble boot. Native 160×144, 1:1.
- **Master System** (`--core sms --with-debug-bridge`, `sms-load`): Sonic the Hedgehog is playable;
  Alex Kidd in Miracle World, Fantasy Zone, Castle of Illusion, OutRun, R-Type (512 KB), Shinobi,
  Wonder Boy boot to title/attract. Downscaled 256×192 → 160×144.

Scripted play-testing works end-to-end: `scripts/chromatic.py sequence "press:start@0.2 wait:3
buttons:dpad_right+a wait:1 capture:shot.png"` (virtual buttons are OR'ed with the console's; hold
everything in one `sequence` — the script releases all buttons on exit). This document is the
methodology to port another core.

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

## Worked example: SMS / Game Gear — the 2nd core (runs on hardware; video bring-up)

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
sms-load`) into the PSRAM at `SMS_ROM_BASE`, and the core boots it. (`--sms-bram` bakes a small ROM
into BRAM instead, no cache; `bios_en` boots the `mboot.hex` BIOS.)

The FM (`opll`) is stubbed (`opll_stub.v`, BSD) to skip the VHDL VM2413 and save area — fine for the
vast majority of SMS games and all Game Gear games; drop in the real `vm2413/` for FM titles.

**The core runs on hardware.** The cache debug CSR (`SMSLoaderCSR(with_debug=True)`) confirms the
Z80 executes an uploaded cartridge ROM from the PSRAM cache: cache IDLE, `stall=0`, `reads` climbing,
only 4 line misses for a 40-byte ROM. Getting here took two fixes: (1) the cache froze in **FILL
forever** on the first fetch because the core issued its first PSRAM read before the PSRAM BIST
finished and the one-cycle arbiter request was dropped — fixed by holding reset until
`cart_ready = bist_done`; (2) that fix (and anything else) would not **route** at 56/56 BSRAM, fixed
by the RGB333 frame buffer (45/56) **and** raising the Gowin place/route effort
(`place_option/route_option 2`, `replicate_resources`).

**The port is proven correct in simulation.** A Verilator sim of `SMSCore` (`chromatix_sim.py`
`build_sim_sms`, both the BRAM cartridge and the `with_cache` PSRAM-cache path via `SimSMSPSRAMPort`)
**renders the test ROM's backdrop blue** and its injected I/O trace shows the Z80's `OUT ($BF)`
register writes reaching the VDP (`vdp_wr_n=0`, correct data). So the whole SMS integration — Z80,
VDP, video/color path, downscaler, cache — is functionally correct.

**Resolved — the backdrop renders blue on hardware.** It was two stacked bugs, not the one first
guessed ("the VDP does not latch its registers"). A `vdp` hardware debug probe
(`sms_rtl_with_io_debug(probe="vdp")`, `--sms-hw-debug vdp`) that latches a center-pixel VDP snapshot
to the debug CSR disproved that guess outright: on hardware the VDP is **fully configured** —
`mode_M4=1`, `display_on=1`, register writes latch — but `cram_vdp_D=0xFFF` at `cram_vdp_A=0`, i.e.
the color RAM returns its initial value and the CPU's palette writes never took.

1. **CRAM init+write (the white).** `vdp_cram.v` is a 32x12 palette RAM both **initialized** (to
   `0xFFF` white) and **CPU-written**. Gowin cannot infer a both-init'd-and-written array as BSRAM —
   the init wins, the writes drop, every entry reads back `0xFFF` → whole screen white. Verilator
   honours init+write and rendered correctly, hiding it through the entire sim bring-up (the same
   class of problem as the NES ROM loader). Fix (`_sms_lower_rtl_for_gowin`, applied by
   `sms_rtl_lowered`/`add_sms_sources` to every hardware build and by `sanitize_sms_rtl` to the sim,
   so both test identical RTL): drop the `initial` block so it is a clean write+read RAM that infers
   and works on hardware. (`syn_ramstyle = "registers"`, both the comment and `(* *)` attribute
   forms, is **not** honoured by this `gw_sh`, so the init removal is what does it.) With this alone
   the screen went white → **black**: CRAM now reads `0` instead of the init, but the writes still
   were not landing — exposing the second bug.
2. **SMS vs Game Gear mode (the black).** The sim ran the core as SMS (`game_gear=False`, `gg=0`), the
   hardware ran it as Game Gear (`game_gear=True`, `gg=1`) — a sim/hardware config mismatch. The test
   ROM writes CRAM the SMS way (a single byte to `$BE` at an even address); in Game Gear mode
   `cram_cpu_WE` needs the two-byte protocol (`xram_cpu_A[0]==1`), so the single-byte write is
   suppressed and CRAM stays `0` (black). The sim reproduces it exactly: `gg=0` → center `(0,0,255)`
   blue, `gg=1` → `(0,0,0)` black. Fix: `game_gear` is now a build option (`--sms-game-gear`),
   **default SMS mode (`gg=0`)** to match SMS ROMs and the sim; Game Gear ROMs set the flag.

With both fixes the `--sms-bram` SMS test ROM renders a uniform `(0,0,246)` blue frame over UVC on
hardware, matching the sim. The earlier dead-ends are retired: the cartridge cache, the sms-clock
timing (the 48 MHz drop is kept for margin but was never the cause — the writes latch fine), the
writes-not-arriving theory, and the `ce_cpu`/`ce_vdp` coincidence. The RTL is correct; the two defects
were the CRAM memory inference and the mode mismatch.

**Full BG pipeline on hardware (PSRAM cache, runtime ROM load).** A tile test ROM
(`test/sms_test_rom.py tiles.sms`: four solid-colour tiles in the pattern table, a 4-colour palette, a
nametable of diagonal colour blocks) loaded over the debug bridge with `scripts/chromatic.py sms-load`
into the plain `--core sms` (PSRAM-cache) build — no rebuild per ROM — renders the diagonal
black/red/green/blue block pattern over UVC, **pixel-identical to the sim** (`build_sim_sms` now takes
`rom_bytes=`). This exercises the whole path: Z80, VRAM pattern + nametable writes, VDP background
render, CRAM palette, the 256x192→160x144 downscaler, the PSRAM framebuffer and UVC. (One cosmetic
detail: a solid band at the very top is backdrop/uninitialised-VRAM dependent — black in sim, blue on
hardware — while the defined pattern below is identical; worth revisiting with a full-screen game.)

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

Bugs found running real games (all fixed; worth checking in any new port):
- **Downscaler row advance on blanked lines.** hblank keeps toggling during vertical blanking; a
  downscaler that advances its output row on every hblank pushes the picture down by
  `vblank_lines*out_h/in_h` (Game Gear 1:1: ~118 rows, only the top of the game visible at the
  bottom; SMS ~52; NES ~13). Only lines that carried active pixels may advance the row.
- **Game Gear Start is `pause`, not `j1_start`.** Port `$00` bit 7 reads system.v's active-low
  `pause` (also the SMS Pause/NMI). Tied to 0 it is "Start held": games skip the title, then stay
  paused.
- **Cartridge address mask.** The SMS mapper's bank registers are 8-bit and system.v doesn't mask
  them; MiSTer/smstang AND the cartridge address with the ROM size. Without it, games that write
  out-of-range banks (Alex Kidd, Fantasy Zone) read past the ROM and hang. `sms-load` now writes
  `sms_loader_mask` (ROM size rounded up to a power of 2, minus 1).
- **NES joypad shift edge.** nes.v holds `joypad_clock` high for the whole `$4016` read; shift on
  its *falling* edge or every read returns the next button (Start seen as Select).

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

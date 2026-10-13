# SMS / Game Gear core RTL (third-party, GPL) — drop-in

The `--with-core sms` build needs the Sega Master System / Game Gear core RTL here. It is **not**
vendored into this repo (third-party GPL; kept out of history by `.gitignore`). You must drop it in
yourself — this repo's tooling cannot fetch or copy it for you.

## Source

[nand2mario/smstang](https://github.com/nand2mario/smstang) — a port of the
[SMS-MiSTer](https://github.com/MiSTer-devel/SMS_MiSTer) core to Gowin FPGAs, part of
[TangCore](https://github.com/nand2mario/tangcore). Same GW5A-25 class as the Tang Primer 25K, so
the core proper already uses Gowin-friendly inferred RAM/clock-enables. **GPL** — keep the original
copyright/license headers.

```bash
git clone https://github.com/nand2mario/smstang
```

## What to copy

Copy the **core** RTL (the `system` DUT and its dependencies), not the Tang board top. `smstang`'s
`build.tcl` lists its source files — take the ones the `system` module needs and drop the Tang
Console integration:

- **Keep**: `system.v` (the DUT), the VDP (`vdp*.v`, `vdp_cram.v`, …), the Z80 (`t80/`), the PSG
  (`jt89/`), the FM (`VM2413/`, `vm2413_verilog/` — optional, large; can be disabled), on-chip RAM
  (`spram.v`, `sprom.v`, `dpram.v`), and the small support files (`MC8123.v`, `SEGASYS1_PRGDEC.v`,
  `parts.v`, `io.v`, cheat/lightgun/compressor as needed).
- **Drop** (replaced by the ChromatiX LiteX shell): `smstang_top.sv`, `sdram.v` (→ PSRAM + cache),
  the PLL wrappers (`pll/`, `plla/` → `GW5APLL`), `sms2hdmi.sv`/`hdmi2/`/`video.v`'s HDMI side
  (→ the downscaler / GB-LCD path), `usb_hid_host*` + `iosys/` (→ the console buttons + debug
  bridge), `hybrid_pwm_sd.v` (→ the I2S codec).

Place the kept files under `chromatix/verilog/cores/sms/` (mirroring `smstang/src/`'s layout for the
subdirectories). The BSD glue (`chromatix/gateware/cores/sms.py`, with `add_sms_sources()` + the
`SMSCore`/video/memory/input adapters) is written following `nes.py`'s pattern once the RTL is in and
can be iterated against on hardware; align the source list there with the files you drop here.

## Port notes (the methodology — see doc/CORES.md "Worked example: SMS/GG")

The `system` DUT is a clean clock-enable design (ideal for Gowin — no gymnastics needed):

- **Clocks**: one 53.69 MHz `clk_sys` + the clock-enables `ce_cpu` (÷15, ~3.58 MHz Z80), `ce_vdp`
  (÷5), `ce_pix` (÷10, ~5.37 MHz dot clock), `ce_sp` (sprite). Generate them from a `GW5APLL` +
  a small divider (copy the cadence from `smstang_top.sv`).
- **Video**: *random-access* — drive `x[8:0]`/`y[8:0]` in a raster and read `color[11:0]` (BGR 4-4-4,
  → RGB555). SMS is 256×192 visible → the existing `CoreVideoDownscaler`/`NESVideoDownscaler`-style
  256×192→160×144 path. **Game Gear is 160×144 native** → drive the GB-LCD interface directly, no
  downscaler, no frame buffer (do GG first).
- **ROM**: `rom_rd`/`rom_a[21:0]`/`rom_do[7:0]` (+ `rom_do_valid`, with reads starting ~2 cycles
  after `ce_cpu`). SMS/GG ROMs are 32 KB–512 KB — too big for BRAM, so this is served from **PSRAM
  behind a cache** (the gating new work vs. the NES, whose NROM fit BRAM). *First milestone shortcut*:
  use a small (≤48 KB) homebrew/SG-1000 ROM in BRAM to bring the core up before adding the cache.
- **Work RAM**: `ram_*` (8 KB system RAM) → inferred BRAM. VDP VRAM (16 KB) is internal to the VDP.
- **Audio**: `audioL`/`audioR` (jt89 PSG + optional VM2413 FM) → the shared `left/right` bus → codec.
- **Input**: `j1_*`/`j2_*` ← the console buttons (+ ESP32 `mcu_buttons`).

The combined `--with-core sms` build is GPL; the ChromatiX glue (`sms.py`, the shell) is BSD.

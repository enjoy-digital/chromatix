# ChromatiX

[![ChromatiX: the ModRetro Chromatic FPGA rebuilt with LiteX](doc/images/chromatix.jpg)](https://github.com/enjoy-digital/chromatix/releases/download/media/chromatix.mp4)

<sub>▶ Click the image for the [promo video](https://github.com/enjoy-digital/chromatix/releases/download/media/chromatix.mp4) (rendered with three.js from [`doc/illustration`](doc/illustration); video and hi-res stills in the [media release](https://github.com/enjoy-digital/chromatix/releases/tag/media)).</sub>

The [ModRetro Chromatic](https://modretro.com/products/chromatic) FPGA design (Gowin GW5A-25), freed
and rebuilt with [LiteX](https://github.com/enjoy-digital/litex) (ChromatiX: Chromatic + LiteX):

- **One Python script** (`chromatix.py`) replaces the Gowin TCL project.
- **No encrypted/vendor IP**: PLLs, FIFOs, CSC, PSRAM, video, system monitor and USB (PHY + device)
  are open LiteX/Migen cores or [LUNA](https://github.com/greatscottgadgets/luna); only the MiSTer
  Game Boy core stays in Verilog.
- **Upstream**: the USB 2.0 soft PHY and the OPI PSRAM controller written for this design are part of
  LiteX, and the Chromatic is a [litex-boards](https://github.com/litex-hub/litex-boards) target.
- **Same features as the original**, plus UVC capture at 320x288 (exact 2x2), a debug bridge (virtual
  buttons + UVC video) for automated/agentic testing, a **virtual cartridge** (Game Boy ROMs loaded
  from the PC, no cartridge needed) and a LiteX BIOS/RISC-V firmware mode.

## Architecture

```
               ┌─────────────── GW5A-25 FPGA · LiteX SoC (chromatix.py) ───────────────┐
               │                                                                           │
 Cartridge ◄──►│ ┌─────────────────┐ frames ┌──────────────────┐   ┌────────────────────┐  │
               │ │ Game Boy core   ├───────►│ Memory system    ├──►│ Video pipeline     ├──┼──► LCD
               │ │ MiSTer (Verilog)│        │ PSRAM ctrl + PHY │   │ blend · OSD ·      │  │
               │ └────────┬────────┘        │ arbiter · BIST   │   │ color correction   │  │
               │          │ audio           └────────▲─────────┘   └─────────┬──────────┘  │
               │          │                          │ OSD                   │ video       │
 ESP32 ◄──────►│          │                 ┌────────┴─────────┐   ┌─────────▼──────────┐  │
 (QSPI · UART) │          │                 │ QSPI slave ·     │   │ USB device         │  │
               │          │                 │ system monitor   │   │ UVC · UAC · CDC    │  │
               │          │                 └──────────────────┘   │ LUNA core          ├──┼──► USB 2.0
               │          ├─────────────── audio ─────────────────►│ LiteX UTMI PHY     │  │
               │ ┌────────▼────────┐                               └────────────────────┘  │
 Codec ◄───────│ │ I2S · LiteI2C   │                                                       │
               │ └─────────────────┘                                                       │
               │ CRG (2x GW5APLL) · buttons · battery ADC · CSRs · UARTBone debug bridge   │
               │                                                                           │
               └───────────────────────────────────────────────────────────────────────────┘
```

- USB: LiteX UTMI PHY (GW5A SerDes, HS + FS) + LUNA USB 2.0 device core (Amaranth, converted to
  Verilog at build time) + Migen class logic: UVC (320x288/160x144), UAC (44.1kHz), CDC-ACM bridged
  to the ESP32 UART.
- Step-by-step migration, clock domains and ported blocks: [doc/MIGRATION.md](doc/MIGRATION.md);
  next steps: [doc/ROADMAP.md](doc/ROADMAP.md).

## Install on your Chromatic (prebuilt bitstreams)

No toolchain needed: download the bitstreams and the flasher from the
[releases](https://github.com/enjoy-digital/chromatix/releases). The Chromatic's USB-C port includes a
Gowin GWU2X JTAG bridge, so the FPGA flash is written over the USB cable with
[openFPGALoader](https://github.com/trabucayre/openFPGALoader) (Linux: distribution package + its udev
rules, macOS: `brew install openfpgaloader`, Windows: MSYS2 package).

| Bitstream                | Description                                                                          |
|--------------------------|--------------------------------------------------------------------------------------|
| `chromatix-standard.fs`  | Drop-in replacement of the official design: cartridge games, ESP32 menu, USB UVC/UAC capture, USB CDC bridged to the ESP32. |
| `chromatix-vcart.fs`     | Standard + debug bridge: **virtual cartridge** (Game Boy ROMs loaded from the PC with `scripts/chromatic.py load-rom`), virtual buttons, automation (USB CDC is the debug bridge instead of the ESP32 bridge). |
| `chromatix-bios.fs`      | LiteX BIOS demo: VexRiscv RISC-V SoC, console on USB CDC and LCD, PSRAM main RAM, firmware over serialboot. |

```bash
./chromatix_flash.py info                          # Check the connection (console on, USB-C connected).
./chromatix_flash.py flash chromatix-standard.fs   # Saves the original flash image first, then installs ChromatiX.
./chromatix_flash.py restore                       # Restores the original image (first backup).
```

Backups are saved in `~/chromatix-backups` (keep them: they hold the official image). The flash is
written and verified over JTAG, which stays available whatever the flash content, so a failed or
interrupted write can simply be retried. Use at your own risk.

## Build & Flash

Requires [LiteX](https://github.com/enjoy-digital/litex) (`litex_setup.py`, recent master: USB 2.0 PHY and OPI PSRAM cores), Gowin EDA and [openFPGALoader](https://github.com/trabucayre/openFPGALoader) (with GWU2X support).

```bash
git submodule update --init --recursive   # MiSTer Game Boy core.
pip3 install --user -e .                  # ChromatiX + pinned Amaranth/LUNA.

./chromatix.py --build --no-compile                               # Generate only.
./chromatix.py --gowin-path ~/tools/gowin_1.9.12.04/IDE --build   # Full build.
./chromatix.py --flash                                            # Flash (openFPGALoader, --cable gwu2x).
python3 -m pytest -n auto test                                        # Tests.
```

Notes:
- Use **Gowin V1.9.12.04** (V1.9.10 builds don't enumerate on USB, V1.9.9 fails timing).
- USB only enumerates when the FPGA boots from **flash** (`--flash`, not `--load`); the console must be on.
- Back up the official image first: `./scripts/chromatix_flash.py backup` (restore with
  `./scripts/chromatix_flash.py restore`).
- Release bitstreams: `./scripts/build_release.sh` (`dist/`).

## Debug / Automation

`--with-debug-bridge` turns the USB CDC port into a LiteX UARTBone (instead of the ESP32 UART bridge),
directly on the USB stream (High-Speed rate, the baudrate is ignored): virtual buttons, status, USB
debug registers and the whole PSRAM from the host. With the UVC video, this allows fully automated
(or agentic) tests:

```bash
./chromatix.py --gowin-path ~/tools/gowin_1.9.12.04/IDE --with-debug-bridge --build --flash
litex_server --uart --uart-port /dev/ttyACM0 &
./scripts/chromatic.py press start --duration 0.2
./scripts/chromatic.py capture frame.png --size 320x288
./scripts/chromatic.py sequence "press:start wait:1.5 press:a wait:1.5 capture:menu.png"
```

### Virtual Cartridge (ROMs loaded from the PC)

With the debug bridge, Game Boy/Game Boy Color ROMs can also be run without a cartridge: the ROM
(up to 3.5MB) and the cartridge RAM are served from the PSRAM through a cache (MBC1/2/3/5, ROM only;
the core is briefly frozen on cache misses), and the physical cartridge bus is kept idle:

```bash
./scripts/chromatic.py load-rom game.gb                  # Loaded in ~0.1s/256KB, runs immediately.
./scripts/chromatic.py load-rom game.gb --save game.sav  # With a cartridge RAM (save) content.
./scripts/chromatic.py save game.gb game.sav             # Cartridge RAM (save) to the PC.
./scripts/chromatic.py unload                            # Back to the physical cartridge.
```

## LiteX BIOS Demo

`--with-bios` turns the Chromatic into a small LiteX dev board: a VexRiscv SoC (33MHz) runs the LiteX
BIOS in place of the Game Boy core. Its console is on the USB CDC port (`litex_term /dev/ttyACM0`) and on the
LCD (40x24 terminal, 4x6 font), so it is also streamed over UVC. The upper 4MB of the PSRAM are the
SoC main RAM (behind a L2 cache), so firmware can be loaded over the USB CDC port and run:

```bash
./chromatix.py --gowin-path ~/tools/gowin_1.9.12.04/IDE --with-bios --build --flash
litex_term /dev/ttyACM0
./scripts/chromatic.py capture bios.png --size 320x288

# Firmware demo (buttons: notes on the tone generator, shown on the console/LCD), loaded with the BIOS
# serialboot command.
make -C firmware/demo
litex_term /dev/ttyACM0 --kernel firmware/demo/demo.bin
```

Firmware peripherals: buttons (`demo_buttons_status`), square wave tone generator (`tone_period`,
`tone_volume`: speaker/headphones and USB audio), timer, LCD console.

<img src="doc/images/litex_bios.png" width="320" alt="LiteX BIOS on the Chromatic LCD, captured over UVC">

## Simulation

`chromatix_sim.py` simulates the Game Boy core with Verilator (VHDL parts converted to Verilog
with GHDL): cartridge model (ROM only, MBC1, MBC2, MBC3 without RTC, MBC5 + RAM) or virtual
cartridge with a PSRAM model (`--vcart`), scripted buttons and the LCD output captured as PNG
frames. `--video` adds the video pipeline (frame buffer in a PSRAM model, frame blend
`--frame-blend`, color correction `--correct`, ST7785 panel scan): its UVC copy is captured as
`uvc_*` frames. The ROM, frames and buttons are runtime inputs, so `--no-compile` runs another ROM
or scenario on the same build.

```bash
./test/gb_test_rom.py stripes.gb   # Minimal test ROM (8-pixel stripes, A inverts the palette).
./chromatix_sim.py --rom stripes.gb --frames 120 --every 30 --buttons a@60+5
./chromatix_sim.py --rom game.gb --frames 600 --every 60 --buttons start@300+10 --no-compile
ls build/sim/frames
```

### Window (play in the simulation)

`--window` shows the LCD in an SDL window (SDL2 required) and takes the keyboard or a gamepad until
the window is closed; the save file (`<rom>.sav`, or `--save`) is loaded and written back at the end:

```bash
./chromatix_sim.py --rom game.gb --window --pgo            # Build (PGO trained on the ROM) and play.
./chromatix_sim.py --rom other.gb --window --no-compile    # Another ROM, same build.
./chromatix_sim.py --rom game.gb --window --no-compile --wav game.wav  # Record the audio.
```

| Key                      | Game Boy |
|--------------------------|----------|
| Arrows                   | D-pad    |
| X / Z                    | A / B    |
| Enter                    | Start    |
| Backspace / Right Shift  | Select   |
| P / F12 / Escape         | Pause / screenshot (BMP) / quit |

The simulation is cycle accurate (the whole Game Boy core RTL): ~2 fps (0.04x realtime) on a recent
desktop CPU with `--pgo` (`--bench` measures it), so games play in slow motion; `--audio` plays the
sound live (only meaningful near realtime), `--wav` records it in simulation time.

## Credits

- [ModRetro](https://modretro.com/): the [Chromatic](https://modretro.com/products/chromatic) and its
  open-source FPGA design, [oss-chromatic-console-fpga](https://github.com/ModRetro/oss-chromatic-console-fpga)
  (GPL-3.0), which this project starts from (history preserved). ModRetro's product and mainboard
  pictures were the reference for the illustration/video.
- The [MiSTer Game Boy core](https://github.com/MiSTer-devel/Gameboy_MiSTer) contributors.
- The 260+ [LiteX](https://github.com/enjoy-digital/litex) contributors who, over 10+ years (building
  on [Migen](https://github.com/m-labs/migen) from M-Labs), made a port like this possible with ease.
- [LUNA](https://github.com/greatscottgadgets/luna) (Great Scott Gadgets) and
  [Amaranth](https://github.com/amaranth-lang/amaranth): the USB 2.0 device core.
- [openFPGALoader](https://github.com/trabucayre/openFPGALoader), [Yosys](https://github.com/YosysHQ/yosys),
  [Verilator](https://github.com/verilator/verilator) and [three.js](https://threejs.org/).
- [ESP-SDR](https://espargos.net/espsdr/) by [ESPARGOS](https://espargos.net/) (Florian Euchner,
  [esp-sdr](https://github.com/ESPARGOS/esp-sdr), GPL-3.0): raw I/Q captures from the ESP32
  radio, the base of the Chromatic SDR (`sdr` branch: its ESP32 firmware is ESP-SDR with a
  Chromatic transport and tuning, the console/PC tools speak its protocol).
- [h0m3us3r](https://github.com/h0m3us3r)'s [eSpDR](https://github.com/h0m3us3r/eSpDR): the 5/6 LO
  conversion mode (qualified on the original ESP32 by ESP-SDR) extending the SDR down to 1.79 GHz.
- [germaneguise](https://github.com/germaneguise): the 320x288 USB capture idea
  ([#10](https://github.com/ModRetro/oss-chromatic-console-fpga/pull/10)).

## License

ChromatiX is dual-licensed per file (see [LICENSE](LICENSE)): the ports of ModRetro's original
design (GPL-3.0) stay under the **GPL-3.0**, our own work (LiteX adaptation, new modules, tools,
tests) is under the **BSD 2-Clause** license. The Game Boy emulation Verilog is GPL, so built
bitstreams fall under the GPL.

<sub>ModRetro and Chromatic are trademarks of ModRetro; Tetris® is a trademark of The Tetris Company.
This project is not affiliated with or endorsed by ModRetro.</sub>

# ChromatiX

The [ModRetro Chromatic](https://modretro.com/products/chromatic) FPGA design (Gowin GW5A-25), rebuilt
with [LiteX](https://github.com/enjoy-digital/litex):

- **One Python script** (`chromatix.py`) replaces the Gowin TCL project.
- **No encrypted/vendor IP**: PLLs, FIFOs, CSC, PSRAM, video, system monitor and USB (PHY + device)
  are open LiteX/Migen cores or [LUNA](https://github.com/greatscottgadgets/luna); only the MiSTer
  Game Boy core stays in Verilog.
- **Same features as the original**, plus UVC capture at 320x288 (exact 2x2) and a debug bridge
  (virtual buttons + UVC video) for automated/agentic testing.

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

## Build & Flash

Requires [LiteX](https://github.com/enjoy-digital/litex) (`litex_setup.py`), Gowin EDA and [openFPGALoader](https://github.com/trabucayre/openFPGALoader) (with GWU2X support).

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
- Back up the official image first: `openFPGALoader --cable gwu2x --dump-flash --file-size 1048576 official.bin`
  (restore with `--write-flash --file-type bin --reset official.bin` or the ModRetro updater).

## Debug / Automation

`--with-debug-bridge` turns the USB CDC port into a LiteX UARTBone (instead of the ESP32 UART bridge):
virtual buttons, status and USB debug registers (UVC counters, UTMI packet monitor) from the host.
With the UVC video, this allows fully automated (or agentic) tests:

```bash
./chromatix.py --gowin-path ~/tools/gowin_1.9.12.04/IDE --with-debug-bridge --build --flash
litex_server --uart --uart-port /dev/ttyACM0 --uart-baudrate 115200 &
./scripts/chromatic.py press start --duration 0.2
./scripts/chromatic.py capture frame.png --size 320x288
./scripts/chromatic.py sequence "press:start wait:1.5 press:a wait:1.5 capture:menu.png"
```

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
- [germaneguise](https://github.com/germaneguise): the 320x288 USB capture idea
  ([#10](https://github.com/ModRetro/oss-chromatic-console-fpga/pull/10)).

## License

ChromatiX is released under the [BSD 2-Clause License](LICENSE), like the other LiteX projects.
Third-party parts keep their own licenses (see [LICENSE](LICENSE)): the Game Boy emulation Verilog
(`chromatix/verilog`, GPL from ModRetro/MiSTer) is GPL, so built bitstreams fall under the GPL.

<sub>ModRetro and Chromatic are trademarks of ModRetro; Tetris® is a trademark of The Tetris Company.
This project is not affiliated with or endorsed by ModRetro.</sub>

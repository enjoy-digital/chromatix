# ChromatiX Roadmap

The migration is done ([MIGRATION.md](MIGRATION.md)): no encrypted/vendor IP is left and only the
MiSTer Game Boy core stays in Verilog, and the USB device core stays on LUNA (Amaranth, converted to
Verilog at build time). Next steps:

## LiteX dev board demos

- **LiteX BIOS demo** (done: `--with-bios`): a VexRiscv SoC running the LiteX BIOS, with its console
  on the LCD (and so over UVC) and on the USB CDC port, and 4MB of PSRAM as main RAM to run firmware
  (serialboot). Next: firmware using the buttons/audio, faster CPU clock.
- Other cores on the Chromatic (retro cores, RISC-V SoCs, accelerators) reusing the platform, video
  pipeline, USB (UVC/UAC/CDC) and the debug/automation loop.

## Upstreaming

- LiteX: GW5A I/O primitives (OSER4/IDES4/IODELAY, SerDes), x8 OPI PSRAM PHY/controller.
- litex-boards: a `modretro_chromatic` target (PSRAM, LCD, codec, battery).

## Tooling

- Verilator simulation (done: `chromatix_sim.py`, Game Boy core + cartridge + buttons + LCD
  capture). Next: add the video pipeline (frame blend, OSD, color correction) with a PSRAM model, and
  audio capture.
- CI: re-enable once the GitHub Actions account billing issue is solved.

# ChromatiX Roadmap

The migration is done ([MIGRATION.md](MIGRATION.md)): no encrypted/vendor IP is left and only the
MiSTer Game Boy core stays in Verilog. Next steps, following the same process (isolate, port, prove
equivalent, check on hardware, then delete):

## USB: native Migen device core

Port LUNA's USB 2.0 device layers (reset/HS chirp, packet, transaction, control, isochronous/bulk
endpoints) to Migen, one layer at a time, each checked against LUNA in co-simulation (same UTMI
stimuli, same UTMI responses). This removes the Amaranth dependency.

## LiteX dev board demos

- **LiteX BIOS demo**: a VexRiscv SoC running the LiteX BIOS, with its console shown on the LCD and
  streamed over UVC, and reachable over the USB CDC port.
- Other cores on the Chromatic (retro cores, RISC-V SoCs, accelerators) reusing the platform, video
  pipeline, USB (UVC/UAC/CDC) and the debug/automation loop.

## Upstreaming

- LiteX: GW5A I/O primitives (OSER4/IDES4/IODELAY, SerDes), x8 OPI PSRAM PHY/controller.
- litex-boards: a `modretro_chromatic` target (PSRAM, LCD, codec, battery).

## Tooling

- A Verilator `litex_sim` target (GB core + video) for testing without hardware.
- CI: re-enable once the GitHub Actions account billing issue is solved.

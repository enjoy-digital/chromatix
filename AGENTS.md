# AGENTS.md: ChromatiX

The ModRetro Chromatic handheld FPGA design (Gowin GW5A-25), rebuilt with LiteX: open cores in place of
encrypted vendor IP, Game Boy core, USB (soft PHY), OPI PSRAM, video, audio, virtual cartridge,
debug bridge, hosted NES/SMS cores and games.

## Where things are

- `chromatix.py` (SoC/target), `chromatix_platform.py`, `chromatix_sim.py` (simulation); `chromatix/` (gateware cores).
- `firmware/`, `scripts/`, `doc/` (`ROADMAP.md`, `MIGRATION.md`, `GAME_LIBRARY.md`, `SIM_PERFORMANCE.md`); `test/`.

## Verify a change

```sh
python3 chromatix.py --build --no-compile                           # elaborate (CI)
python3 -m pytest -v -rs -n auto test                               # what CI runs
```

- Toolchain: Gowin V1.9.12.04 only (USB does not enumerate with V1.9.10, timing fails with V1.9.9);
  USB enumerates only when the FPGA boots from flash, not after an SRAM/JTAG load.
- Never name a Migen `Instance` of a Gowin primitive like the primitive itself; Gowin timing
  summaries miss cross-clock violations (read the Setup/Hold path tables).
- Never write the device flash unless the user asked (stock image backups stay outside the repo).

## LiteX

This project is built with LiteX: read https://github.com/enjoy-digital/litex/wiki/LiteX-for-AI-Agents (framework map, conventions,
how to verify a change, contribute back the generic fixes to LiteX and its cores).

## Debug on hardware

Method: https://github.com/enjoy-digital/litex/wiki/Debug-A-SoC-On-Hardware (check the link and the SoC identity first, registers
before traces, narrow LiteScope captures, read the generated Verilog, turn findings into tests).
`litex_mcp` gives an agent the same loop as MCP tools through `litex_server` (CSRs by name, LiteScope
captures as data, console, builds with parsed timing, simulation; writes and `--load`/`--flash`
only with `--allow-write`): https://github.com/enjoy-digital/litex/wiki/LiteX-for-AI-Agents#drive-a-soc-through-mcp.

## Rules

- LiteX style: https://github.com/enjoy-digital/litex/blob/master/doc/coding_style.md.
- Never edit generated files (`build/`, `csr.h`, `csr.csv`, generated Verilog): change the Python.
- Never flash a board or write its SPI flash unless the user asked; `--load` (volatile) for experiments.
- Never commit flash dumps, stock firmware or vendor bitstreams.
- Pull requests: one topic each, with what was tested and on which hardware; say that an agent wrote
  it and who supervised.

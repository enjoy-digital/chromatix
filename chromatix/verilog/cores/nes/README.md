# NES core RTL (third-party, GPL) — drop-in

The `--with-core nes` build needs the NES core RTL here. It is **not** vendored into this repo
(third-party GPL; kept out of history by `.gitignore`). Drop it in:

```bash
git clone https://github.com/nand2mario/nestang
cd nestang/src
D=../../<path-to-chromatix>/chromatix/verilog/cores/nes
cp nes.v ppu.v apu.v compat.v dpram.v game_loader.v cart.sv EEPROM_24C0x.sv $D/
cp t65/*.v $D/t65/
cp generic.sv iir_filter.v misc.sv MMC1.sv MMC2.sv MMC3.sv MMC5.sv Namco.sv \
   Sachen.sv Sunsoft.sv JYCompany.sv $D/mappers/          # nestang's Primer 25K (GW5A-25) mapper set
cp roms/nes15.hex $D/roms/                                # bundled homebrew NROM test ROM
```

- Source: [nand2mario/nestang](https://github.com/nand2mario/nestang) (NES core from Ludvig Strigeus'
  *fpganes*), **GPL**. Keep the original copyright/license headers.
- `add_nes_sources()` (`chromatix/gateware/cores/nes.py`) adds exactly these files. VRC/FDS are
  excluded (huge expansion-audio mappers nestang's Primer 25K build also omits); `nes15` is NROM.
- The permissive LiteX glue that wraps the core (clock, video/memory/input adapters) is in
  `chromatix/gateware/cores/nes.py`. A built `--with-core nes` bitstream is a GPL derivative
  (ChromatiX is dual GPL/BSD; the shell/glue stay BSD). See `doc/CORES.md`.

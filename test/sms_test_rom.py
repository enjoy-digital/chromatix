#!/usr/bin/env python3

#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
Minimal Master System / Game Gear test ROMs (hand-assembled Z80), to validate the hosted SMS core
(chromatix.py --core sms) video path on hardware and in the Verilator sim.

- tiles (default, SMS mode / gg=0): loads four solid-colour tiles into the pattern table, a 4-colour
  background palette, and a nametable of diagonal colour blocks, then displays them. Exercises the
  whole background pipeline: VRAM pattern + nametable writes, VDP render, CRAM palette, downscaler.
      ./test/sms_test_rom.py tiles.sms
  Load it over the PSRAM cache at runtime (no rebuild): scripts/chromatic.py sms-load tiles.sms
  (on a plain `--core sms` build), or bake it in with `--core sms --sms-bram --sms-rom tiles.sms`.

- backdrop: a flat blue screen (background tile 0 over empty VRAM -> CRAM[0]). SMS CRAM is 1 byte
  (%00BBGGRR); Game Gear CRAM is 12-bit, written two bytes per entry -- pass --gg to emit the Game
  Gear form (for a `--sms-game-gear` build).
      ./test/sms_test_rom.py blue.sms --backdrop [--gg]
"""

import sys

ROM_SIZE  = 0x4000
PAT_ADDR  = 0x1000   # pattern-table source in ROM (tiles)
NT_ADDR   = 0x1200   # nametable source in ROM (tiles)
CRAM_ADDR = 0x1900   # CRAM source in ROM (tiles)


def _reg(r, v):            # VDP register r = v
    return bytes([0x3E, v, 0xD3, 0xBF, 0x3E, 0x80 | r, 0xD3, 0xBF])

def _set_vram(addr):       # set the VRAM write address
    return bytes([0x3E, addr & 0xFF, 0xD3, 0xBF, 0x3E, 0x40 | ((addr >> 8) & 0x3F), 0xD3, 0xBF])

def _copy16(src, count):   # copy `count` bytes from `src` -> data port ($BE), 16-bit counter
    return bytes([0x21, src & 0xFF, src >> 8,          # ld hl,src
                  0x01, count & 0xFF, count >> 8,      # ld bc,count
                  0x7E, 0xD3, 0xBE, 0x23, 0x0B, 0x78, 0xB1, 0x20, 0xF7])  # loop


def tile_rom():
    """SMS mode-4 ROM drawing a diagonal pattern of 4-colour blocks."""
    code  = bytes([0xF3, 0xED, 0x56, 0x31, 0xF0, 0xDF])                 # di; im 1; ld sp,$DFF0
    code += _reg(0, 0x04) + _reg(1, 0x40) + _reg(2, 0xFF) + _reg(7, 0x00)  # mode4, display on, NT=0x3800
    code += _set_vram(0x0000) + _copy16(PAT_ADDR, 128)                  # pattern table (4 tiles x 32 B)
    code += _set_vram(0x3800) + _copy16(NT_ADDR, 32 * 28 * 2)           # nametable
    code += bytes([0x3E, 0x00, 0xD3, 0xBF, 0x3E, 0xC0, 0xD3, 0xBF,      # CRAM write address 0
                   0x21, CRAM_ADDR & 0xFF, CRAM_ADDR >> 8, 0x06, 0x04,  # ld hl,cram; ld b,4
                   0x7E, 0xD3, 0xBE, 0x23, 0x10, 0xFA])                 # loop: djnz
    code += bytes([0x18, 0xFE])                                         # jr $
    assert len(code) <= PAT_ADDR, f"code too big: {len(code):#x}"

    # Four solid tiles (colour index 0..3): per row, plane j = 0xFF when bit j of the index is set.
    pat = bytearray()
    for c in range(4):
        for _row in range(8):
            pat += bytes([0xFF if (c >> j) & 1 else 0x00 for j in range(4)])
    # Nametable 32x28 (tile index + flags), diagonal 4-colour blocks.
    nt = bytearray()
    for row in range(28):
        for col in range(32):
            nt += bytes([((col >> 2) + (row >> 2)) & 3, 0x00])
    cram = bytes([0x00, 0x03, 0x0C, 0x30])  # black, red, green, blue (SMS %00BBGGRR)

    rom = bytearray(ROM_SIZE)
    rom[0:len(code)]                  = code
    rom[PAT_ADDR:PAT_ADDR+len(pat)]   = pat
    rom[NT_ADDR:NT_ADDR+len(nt)]      = nt
    rom[CRAM_ADDR:CRAM_ADDR+len(cram)]= cram
    return bytes(rom)


def backdrop_rom(gg=False):
    """Flat blue screen: background tile 0 over empty VRAM -> CRAM[0] = blue."""
    code  = bytes([0xF3, 0xED, 0x56, 0x31, 0xF0, 0xDF])
    code += _reg(0, 0x04) + _reg(1, 0x40) + _reg(7, 0x00)   # mode 4, display on, backdrop index 0
    code += bytes([0x3E, 0x00, 0xD3, 0xBF, 0x3E, 0xC0, 0xD3, 0xBF])  # CRAM write address 0
    if gg:
        # Game Gear: 12-bit entry over two bytes. color 0xF00 (R=0,G=0,B=F) = low 0x00, high 0x0F.
        code += bytes([0x3E, 0x00, 0xD3, 0xBE, 0x3E, 0x0F, 0xD3, 0xBE])
    else:
        code += bytes([0x3E, 0x30, 0xD3, 0xBE])             # SMS: %00_11_00_00 = blue
    code += bytes([0x18, 0xFE])                             # jr $
    return code + bytes(ROM_SIZE - len(code))


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__); sys.exit(1)
    path = args[0]
    gg   = "--gg" in args
    rom  = backdrop_rom(gg=gg) if "--backdrop" in args else tile_rom()
    with open(path, "wb") as f:
        f.write(rom)
    kind = ("Game Gear backdrop" if gg else "SMS backdrop") if "--backdrop" in args else "SMS tiles"
    print(f"wrote {path} ({len(rom)} bytes, {kind})")


if __name__ == "__main__":
    main()

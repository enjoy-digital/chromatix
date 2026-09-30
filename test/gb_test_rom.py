#!/usr/bin/env python3

#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
Minimal Game Boy test ROM (no Nintendo logo): turns the LCD off in VBlank, sets tile 0 to color 0 and
tile 1 to color 3, fills the BG map with alternating tiles (8-pixel vertical stripes), sets the BG
palette, turns the LCD on, then waits for the A button to invert the BG palette (0x1b) and loops.

- ROM only (32KB): BG palette 0xe4 (white/black stripes, starting with white).
- MBC1 (64KB): BG palette read from ROM bank 2 (0x1b: inverted stripes, starting with black), to
  check the MBC banking.
- Save (MBC1+RAM+battery, 8KB RAM): BG palette read from the cartridge RAM ($A000) and $A001
  incremented, to check the save load/write back.

    ./test/gb_test_rom.py stripes.gb [--mbc1|--save]
"""

import sys

# SM83 code at 0x150.
PROGRAM = [
    0xf3,             # 0150: di
    0x31, 0xfe, 0xff, # 0151: ld   sp, $fffe
    0xf0, 0x44,       # 0154: ldh  a, ($44)       ; LY
    0xfe, 0x90,       # 0156: cp   144
    0x38, 0xfa,       # 0158: jr   c, $0154       ; Wait VBlank.
    0xaf,             # 015a: xor  a
    0xe0, 0x40,       # 015b: ldh  ($40), a       ; LCD off.
    0x21, 0x10, 0x80, # 015d: ld   hl, $8010      ; Tile 1: color 3.
    0x06, 0x10,       # 0160: ld   b, 16
    0x3e, 0xff,       # 0162: ld   a, $ff
    0x22,             # 0164: ld   (hl+), a
    0x05,             # 0165: dec  b
    0x20, 0xfc,       # 0166: jr   nz, $0164
    0x21, 0x00, 0x80, # 0168: ld   hl, $8000      ; Tile 0: color 0.
    0x06, 0x10,       # 016b: ld   b, 16
    0xaf,             # 016d: xor  a
    0x22,             # 016e: ld   (hl+), a
    0x05,             # 016f: dec  b
    0x20, 0xfc,       # 0170: jr   nz, $016e
    0x21, 0x00, 0x98, # 0172: ld   hl, $9800      ; BG map: tile = column & 1.
    0x7d,             # 0175: ld   a, l
    0xe6, 0x01,       # 0176: and  1
    0x22,             # 0178: ld   (hl+), a
    0x7c,             # 0179: ld   a, h
    0xfe, 0x9c,       # 017a: cp   $9c
    0x20, 0xf7,       # 017c: jr   nz, $0175
]

# BG palette load (at 0x17e).
PALETTE = [
    0x3e, 0xe4,       # ld   a, $e4
]
PALETTE_MBC1 = [
    0x3e, 0x02,       # ld   a, 2
    0xea, 0x00, 0x20, # ld   ($2000), a      ; ROM bank 2.
    0xfa, 0x00, 0x40, # ld   a, ($4000)
]

PALETTE_SAVE = [
    0x3e, 0x0a,       # ld   a, $0a
    0xea, 0x00, 0x00, # ld   ($0000), a      ; RAM enable.
    0x21, 0x01, 0xa0, # ld   hl, $a001
    0x34,             # inc  (hl)            ; Save counter.
    0xfa, 0x00, 0xa0, # ld   a, ($a000)      ; Palette from the save.
]

EPILOGUE = [
    0xe0, 0x47,       # ldh  ($47), a        ; BGP.
    0xaf,             # xor  a
    0xe0, 0x42,       # ldh  ($42), a        ; SCY = 0.
    0xe0, 0x43,       # ldh  ($43), a        ; SCX = 0.
    0x3e, 0x91,       # ld   a, $91
    0xe0, 0x40,       # ldh  ($40), a        ; LCD on, BG tiles at $8000, BG on.
    0x3e, 0x10,       # ld   a, $10          ; Wait for A: select the action buttons.
    0xe0, 0x00,       # ldh  ($00), a
    0xf0, 0x00,       # ldh  a, ($00)
    0xf0, 0x00,       # ldh  a, ($00)
    0xe6, 0x01,       # and  1               ; A (0: pressed).
    0x20, 0xf4,       # jr   nz, -12
    0x3e, 0x1b,       # ld   a, $1b          ; A pressed: inverted palette.
    0xe0, 0x47,       # ldh  ($47), a
    0x18, 0xfe,       # jr   $               ; Loop.
]

def make_test_rom(mbc1=False, save=False):
    rom = bytearray([0xff]*(0x10000 if mbc1 else 0x8000))
    rom[0x100:0x104] = bytes([0x00, 0xc3, 0x50, 0x01]) # nop; jp $0150.
    rom[0x104:0x134] = bytes(0x30)                      # No logo.
    rom[0x134:0x143] = b"CHROMATIX".ljust(15, b"\x00") # Title.
    rom[0x143] = 0x00                                   # DMG.
    rom[0x147] = 0x03 if save else 0x01 if mbc1 else 0x00 # MBC1+RAM+battery / MBC1 / ROM only.
    rom[0x148] = 0x01 if mbc1 else 0x00                 # 64KB / 32KB.
    rom[0x149] = 0x02 if save else 0x00                 # 8KB RAM / No RAM.
    rom[0x14d] = (-sum(rom[0x134:0x14d]) - 25) & 0xff   # Header checksum.
    program = PROGRAM + (PALETTE_SAVE if save else PALETTE_MBC1 if mbc1 else PALETTE) + EPILOGUE
    rom[0x150:0x150 + len(program)] = bytes(program)
    if mbc1:
        rom[0x4000] = 0xe4 # Bank 1 (not selected).
        rom[0x8000] = 0x1b # Bank 2: inverted palette.
    return bytes(rom)

if __name__ == "__main__":
    with open(sys.argv[1], "wb") as f:
        f.write(make_test_rom(mbc1="--mbc1" in sys.argv, save="--save" in sys.argv))

#!/usr/bin/env python3

#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
ChromatiX control/test utility (requires a --with-debug-bridge build, or a --with-doom build for run).

Start the LiteX server on the Chromatic USB CDC port first:
    litex_server --uart --uart-port /dev/ttyACM0 --uart-baudrate 115200

Then:
    ./chromatic.py ident
    ./chromatic.py status
    ./chromatic.py press start --duration 0.2
    ./chromatic.py capture frame.png
    ./chromatic.py sequence "press:start wait:2 capture:game.png"
"""

import os
import sys
import time
import zlib
import argparse
import subprocess

from litex import RemoteClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from chromatix.gateware.debug import BUTTONS

# Constants ----------------------------------------------------------------------------------------

UVC_DEVICE = "/dev/video0" # Default, when the Chromatic UVC device is not found by name.
UVC_WIDTH  = 160
UVC_HEIGHT = 144

# Virtual cartridge (see chromatix/gateware/vcart.py).
PSRAM_BASE    = 0x40000000
VCART_ROM     = 0x400000
VCART_RAM     = 0x780000
VCART_ROM_MAX = VCART_RAM - VCART_ROM
VCART_MBC     = {
    **{t: 0 for t in [0x00]},                         # ROM only.
    **{t: 1 for t in [0x01, 0x02, 0x03]},             # MBC1.
    **{t: 2 for t in [0x05, 0x06]},                   # MBC2.
    **{t: 3 for t in [0x0f, 0x10, 0x11, 0x12, 0x13]}, # MBC3 (RTC not supported).
    **{t: 5 for t in range(0x19, 0x1f)},              # MBC5.
}
VCART_RAM_SIZES = {0: 0, 1: 2048, 2: 8192, 3: 32768, 4: 131072, 5: 65536} # Header code -> bytes.

# Doom build: CPU main RAM (PSRAM_BASE = main RAM start), WAD offset (see firmware/doom).
DOOM_WAD_OFFSET = 0x370000

STATUS_FIELDS = ["bist_done", "bist_failed", "lcd_init_done", "menu_disabled", "low_battery", "bat_is_li", "headphones"]

# Chromatic ----------------------------------------------------------------------------------------

class Chromatic:
    def __init__(self, host="localhost", port=1234, csr_csv=None):
        csr_csv = csr_csv or os.path.join(os.path.dirname(os.path.abspath(__file__)), "csr.csv")
        self.bus = RemoteClient(host=host, port=port, csr_csv=csr_csv)
        self.bus.open()

    def close(self):
        self.bus.close()

    # Identifier.
    def ident(self):
        chars = []
        for i in range(256):
            c = self.bus.read(self.bus.bases.identifier_mem + 4*i) & 0xff
            if c == 0:
                break
            chars.append(chr(c))
        return "".join(chars)

    # Status.
    def status(self):
        status = self.bus.regs.debug_ctrl_status.read()
        r = {name: (status >> i) & 0x1 for i, name in enumerate(STATUS_FIELDS)}
        r["system_control"]  = self.bus.regs.debug_ctrl_system_control.read()
        r["volt"]            = self.bus.regs.debug_ctrl_volt.read()
        r["adc_value"]       = self.bus.regs.debug_ctrl_adc_value.read()
        r["volume"]          = self.bus.regs.debug_ctrl_volume.read()
        r["pmic_sys_status"] = self.bus.regs.debug_ctrl_pmic_sys_status.read()
        return r

    # Buttons.
    def set_buttons(self, buttons=()):
        value = 0
        for button in buttons:
            if button not in BUTTONS:
                raise ValueError(f"Unknown button {button} (valid: {', '.join(BUTTONS)}).")
            value |= (1 << BUTTONS.index(button))
        self.bus.regs.debug_ctrl_buttons.write(value)

    def press(self, buttons, duration=0.1):
        self.set_buttons(buttons)
        time.sleep(duration)
        self.set_buttons([])

    # Virtual Cartridge.
    def vcart_control(self, enable, hold, flush=0, mbc=0, rom_mask=0, ram_mask=0):
        self.bus.regs.vcart_csr_control.write(
            (enable << 0) | (hold << 1) | (flush << 2) | (mbc << 4) | (rom_mask << 8) | (ram_mask << 20))

    def write_psram(self, address, data, chunk=64):
        data = bytes(data) + bytes(-len(data) % 4)
        words = [int.from_bytes(data[i:i + 4], "little") for i in range(0, len(data), 4)]
        for i in range(0, len(words), chunk):
            self.bus.write(PSRAM_BASE + address + 4*i, words[i:i + chunk])

    def read_psram(self, address, length, chunk=64):
        data = b""
        for i in range(0, (length + 3)//4, chunk):
            n = min(chunk, (length + 3)//4 - i)
            data += b"".join(w.to_bytes(4, "little") for w in self.bus.read(PSRAM_BASE + address + 4*i, n))
        return data[:length]

    def load_rom(self, rom, save=None):
        """Run a ROM from the virtual cartridge (optionally with its cartridge RAM/save content)."""
        cfg = rom_config(rom)
        self.vcart_control(enable=0, hold=1)
        self.write_psram(VCART_ROM, rom)
        if save is not None:
            self.write_psram(VCART_RAM, save[:VCART_RAM_SIZES[rom[0x149]] or len(save)])
        self.vcart_control(enable=1, hold=1, flush=1, **cfg)
        time.sleep(0.01)
        self.vcart_control(enable=1, hold=0, **cfg)
        return cfg

    def read_save(self, rom):
        size = VCART_RAM_SIZES.get(rom[0x149], 0) or (512 if rom_config(rom)["mbc"] == 2 else 0)
        return self.read_psram(VCART_RAM, size)

    # Firmware (Doom build).
    def run_firmware(self, firmware, files=(), verify=True):
        """
        Load a firmware at the CPU main RAM start (its reset address) and data files at main RAM
        offsets, with the CPU held in reset (ctrl cpu_rst), then start it.
        """
        self.bus.regs.ctrl_reset.write(0b10) # cpu_rst.
        for offset, data in [(0, firmware), *files]:
            self.write_psram(offset, data)
            if verify and zlib.crc32(self.read_psram(offset, len(data))) != zlib.crc32(data):
                raise IOError(f"Verification failed at main RAM offset 0x{offset:x}.")
        self.bus.regs.ctrl_reset.write(0)

    def unload_rom(self):
        """Back to the physical cartridge."""
        self.vcart_control(enable=0, hold=1)
        time.sleep(0.01)
        self.vcart_control(enable=0, hold=0)

# Helpers ------------------------------------------------------------------------------------------

def _read_file(filename):
    with open(filename, "rb") as f:
        return f.read()

# ROM Header ---------------------------------------------------------------------------------------

def rom_config(rom):
    """Virtual cartridge configuration from the ROM header (MBC, ROM/RAM bank masks)."""
    if len(rom) < 0x150:
        raise ValueError("Not a Game Boy ROM (too small).")
    if len(rom) > VCART_ROM_MAX:
        raise ValueError(f"ROM too large ({len(rom)} bytes, max {VCART_ROM_MAX}).")
    if rom[0x147] not in VCART_MBC:
        raise ValueError(f"Unsupported cartridge type 0x{rom[0x147]:02x} (ROM only, MBC1/2/3/5).")
    banks    = max(2, (len(rom) + 0x3fff)//0x4000)
    rom_mask = (1 << (banks - 1).bit_length()) - 1
    ram_mask = {3: 0x3, 4: 0xf, 5: 0x7}.get(rom[0x149], 0)
    return {"mbc": VCART_MBC[rom[0x147]], "rom_mask": rom_mask, "ram_mask": ram_mask}

def rom_title(rom):
    return rom[0x134:0x143].split(b"\x00")[0].decode("ascii", errors="replace").strip()

# WAD ----------------------------------------------------------------------------------------------

def align_wad(wad):
    """
    Doom WAD with 4-byte aligned lumps (the firmware uses the lumps in place: no misaligned accesses
    on RISC-V), directory at the end. Same as firmware/doom/wad_align.h.
    """
    if len(wad) < 12 or wad[:4] not in [b"IWAD", b"PWAD"]:
        raise ValueError("Not a WAD file.")
    numlumps  = int.from_bytes(wad[4:8],  "little")
    directory = int.from_bytes(wad[8:12], "little")
    if directory + 16*numlumps > len(wad):
        raise ValueError("Invalid WAD directory.")
    lumps = bytearray()
    entries = bytearray()
    for i in range(numlumps):
        entry  = wad[directory + 16*i:directory + 16*(i + 1)]
        filepos = int.from_bytes(entry[0:4], "little")
        length  = int.from_bytes(entry[4:8], "little")
        if filepos + length > len(wad):
            raise ValueError("Invalid WAD lump.")
        entries += (12 + len(lumps)).to_bytes(4, "little") + entry[4:16]
        lumps   += wad[filepos:filepos + length] + bytes(-length % 4)
    return wad[:4] + numlumps.to_bytes(4, "little") + (12 + len(lumps)).to_bytes(4, "little") + lumps + entries

# Capture ------------------------------------------------------------------------------------------

def find_uvc_device():
    """Chromatic UVC video device (first node of the "Chromatic" V4L2 device)."""
    try:
        devices = subprocess.run(["v4l2-ctl", "--list-devices"],
            capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return UVC_DEVICE
    lines = devices.splitlines()
    for i, line in enumerate(lines):
        if "Chromatic" in line:
            for node in lines[i + 1:]:
                if node.strip().startswith("/dev/video"):
                    return node.strip()
    return UVC_DEVICE

def capture(filename, device=None, frames=1, fps=2, scale=1, size=f"{UVC_WIDTH}x{UVC_HEIGHT}"):
    """
    Capture frame(s) from the Chromatic UVC stream (frames > 1: horizontal strip), at the native
    160x144 or at 320x288 (2x2 upscale with exact colors).
    """
    device = device or find_uvc_device()
    width, height = [int(v) for v in size.split("x")]
    filters = [f"fps={fps}"] if frames > 1 else []
    if scale != 1:
        filters.append(f"scale={width*scale}:{height*scale}:flags=neighbor")
    if frames > 1:
        filters.append(f"tile={frames}x1")
    cmd = ["ffmpeg", "-loglevel", "error", "-y",
        "-f", "v4l2", "-input_format", "yuyv422", "-video_size", size,
        "-i", device]
    if filters:
        cmd += ["-vf", ",".join(filters)]
    cmd += ["-frames:v", "1", filename]
    subprocess.run(cmd, check=True, timeout=60)

# Sequence -----------------------------------------------------------------------------------------

def run_sequence(chromatic, sequence, default_duration=0.1):
    """
    Run a space-separated sequence of steps:
    - press:a+b[@duration] : press and release button(s).
    - buttons:a+b          : set the held buttons (buttons: releases all).
    - wait:seconds         : wait.
    - capture:file         : capture a UVC frame.
    """
    for step in sequence.split():
        action, _, arg = step.partition(":")
        if action == "press":
            buttons, _, duration = arg.partition("@")
            chromatic.press(buttons.split("+"), float(duration) if duration else default_duration)
        elif action == "buttons":
            chromatic.set_buttons([b for b in arg.split("+") if b])
        elif action == "wait":
            time.sleep(float(arg))
        elif action == "capture":
            capture(arg)
        else:
            raise ValueError(f"Unknown sequence action: {action}")
        print(f"[{action}] {arg}")

# Main ---------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="ChromatiX control/test utility.")
    parser.add_argument("--host",    default="localhost",    help="LiteX server host.")
    parser.add_argument("--port",    default=1234, type=int, help="LiteX server port.")
    parser.add_argument("--csr-csv", default=None,           help="CSR configuration file.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("ident",  help="Print the SoC identifier.")
    subparsers.add_parser("status", help="Print the status registers.")

    p = subparsers.add_parser("press", help="Press button(s).")
    p.add_argument("buttons",    nargs="+",                help=f"Buttons ({', '.join(BUTTONS)}).")
    p.add_argument("--duration", default=0.1, type=float, help="Press duration (s).")

    p = subparsers.add_parser("capture", help="Capture UVC frame(s) (no debug bridge needed).")
    p.add_argument("filename",                        help="Output image file.")
    p.add_argument("--frames", default=1, type=int,   help="Number of frames (tiled horizontally).")
    p.add_argument("--fps",    default=2, type=float, help="Capture rate for multiple frames.")
    p.add_argument("--scale",  default=1, type=int,   help="Scale factor.")
    p.add_argument("--size",   default="160x144",     help="UVC frame size (160x144 or 320x288).")

    p = subparsers.add_parser("load-rom", help="Run a ROM from the virtual cartridge (PSRAM).")
    p.add_argument("rom",                  help="Game Boy ROM file (.gb/.gbc).")
    p.add_argument("--save", default=None, help="Cartridge RAM content to restore (.sav).")

    p = subparsers.add_parser("save", help="Read the virtual cartridge RAM (save) to a file.")
    p.add_argument("rom",  help="Running ROM file (for the RAM size).")
    p.add_argument("file", help="Output .sav file.")

    subparsers.add_parser("unload", help="Back to the physical cartridge.")

    p = subparsers.add_parser("run", help="Load and start a firmware (--with-doom build).")
    p.add_argument("firmware",                          help="Firmware binary (ex: firmware/doom/doom.bin).")
    p.add_argument("--wad",       default=None,         help=f"Doom WAD file (loaded at main RAM offset 0x{DOOM_WAD_OFFSET:x}).")
    p.add_argument("--file",      default=[], nargs=2, action="append", metavar=("FILE", "OFFSET"),
        help="Data file loaded at a main RAM offset.")
    p.add_argument("--no-verify", action="store_true", help="Don't read back/check the loaded data.")

    p = subparsers.add_parser("sequence", help="Run a sequence (ex: \"press:start wait:2 capture:x.png\").")
    p.add_argument("sequence", help="Space-separated steps (press:a+b[@duration], buttons:a+b, wait:s, capture:file).")

    args = parser.parse_args()

    if args.command == "capture":
        capture(args.filename, frames=args.frames, fps=args.fps, scale=args.scale, size=args.size)
        return

    chromatic = Chromatic(host=args.host, port=args.port, csr_csv=args.csr_csv)
    try:
        if args.command == "ident":
            print(chromatic.ident())
        elif args.command == "status":
            for k, v in chromatic.status().items():
                print(f"{k:16s}: {v} (0x{v:x})")
        elif args.command == "press":
            chromatic.press(args.buttons, args.duration)
        elif args.command == "sequence":
            run_sequence(chromatic, args.sequence)
        elif args.command == "load-rom":
            rom  = _read_file(args.rom)
            save = _read_file(args.save) if args.save else None
            t0   = time.time()
            cfg  = chromatic.load_rom(rom, save)
            mbc  = {0: "ROM only", 1: "MBC1", 2: "MBC2", 3: "MBC3", 5: "MBC5"}[cfg["mbc"]]
            ram  = VCART_RAM_SIZES.get(rom[0x149], 0)
            print(f"{rom_title(rom)}: {len(rom)//1024}KB loaded in {time.time() - t0:.1f}s ({mbc}" +
                (f", RAM {ram//1024}KB" if ram else "") + ").")
        elif args.command == "save":
            rom  = _read_file(args.rom)
            data = chromatic.read_save(rom)
            with open(args.file, "wb") as f:
                f.write(data)
            print(f"{len(data)} bytes saved to {args.file}.")
        elif args.command == "unload":
            chromatic.unload_rom()
        elif args.command == "run":
            firmware = _read_file(args.firmware)
            files    = [(int(offset, 0), _read_file(name)) for name, offset in args.file]
            if args.wad:
                files.append((DOOM_WAD_OFFSET, align_wad(_read_file(args.wad))))
            size = len(firmware) + sum(len(data) for _, data in files)
            t0   = time.time()
            chromatic.run_firmware(firmware, files, verify=not args.no_verify)
            print(f"{size//1024}KB loaded in {time.time() - t0:.1f}s, CPU started.")
    finally:
        # Release the virtual buttons (also on errors/Ctrl-C).
        if args.command in ["press", "sequence"]:
            chromatic.set_buttons([])
        chromatic.close()

if __name__ == "__main__":
    main()

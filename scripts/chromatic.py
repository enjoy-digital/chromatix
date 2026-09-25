#!/usr/bin/env python3

#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
ChromatiX control/test utility (requires a --with-debug-bridge build).

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
import argparse
import subprocess

from litex import RemoteClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from chromatix.gateware.debug import BUTTONS

# Constants ----------------------------------------------------------------------------------------

UVC_DEVICE = "/dev/video0"
UVC_WIDTH  = 160
UVC_HEIGHT = 144

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
    def set_buttons(self, buttons=[]):
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

# Capture ------------------------------------------------------------------------------------------

def capture(filename, device=UVC_DEVICE, frames=1, fps=2, scale=1):
    """Capture frame(s) from the Chromatic UVC stream (frames > 1: horizontal strip)."""
    filters = [f"fps={fps}"] if frames > 1 else []
    if scale != 1:
        filters.append(f"scale={UVC_WIDTH*scale}:{UVC_HEIGHT*scale}:flags=neighbor")
    if frames > 1:
        filters.append(f"tile={frames}x1")
    cmd = ["ffmpeg", "-loglevel", "error", "-y",
        "-f", "v4l2", "-input_format", "yuyv422", "-video_size", f"{UVC_WIDTH}x{UVC_HEIGHT}",
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
    parser.add_argument("--host",    default="localhost", help="LiteX server host.")
    parser.add_argument("--port",    default=1234, type=int, help="LiteX server port.")
    parser.add_argument("--csr-csv", default=None,        help="CSR configuration file.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("ident",  help="Print the SoC identifier.")
    subparsers.add_parser("status", help="Print the status registers.")

    p = subparsers.add_parser("press", help="Press button(s).")
    p.add_argument("buttons", nargs="+", help=f"Buttons ({', '.join(BUTTONS)}).")
    p.add_argument("--duration", default=0.1, type=float, help="Press duration (s).")

    p = subparsers.add_parser("capture", help="Capture UVC frame(s) (no debug bridge needed).")
    p.add_argument("filename")
    p.add_argument("--frames", default=1, type=int, help="Number of frames (tiled horizontally).")
    p.add_argument("--fps",    default=2, type=float, help="Capture rate for multiple frames.")
    p.add_argument("--scale",  default=1, type=int,   help="Scale factor.")

    p = subparsers.add_parser("sequence", help="Run a sequence (ex: \"press:start wait:2 capture:x.png\").")
    p.add_argument("sequence")

    args = parser.parse_args()

    if args.command == "capture":
        capture(args.filename, frames=args.frames, fps=args.fps, scale=args.scale)
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
    finally:
        chromatic.set_buttons([]) if args.command in ["press", "sequence"] else None
        chromatic.close()

if __name__ == "__main__":
    main()

#!/usr/bin/env python3

#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
ChromatiX flasher: install a ChromatiX bitstream on a ModRetro Chromatic, or restore the original one.

The Chromatic's USB-C port exposes a Gowin GWU2X JTAG bridge next to the console's own USB device:
openFPGALoader uses it to read/write the FPGA SPI flash, no programming cable or LiteX install is
needed. Only Python 3 and openFPGALoader (https://github.com/trabucayre/openFPGALoader) are required.

Usage (console connected over USB-C and powered on):

    ./chromatix_flash.py info                          # Check the connection.
    ./chromatix_flash.py backup                        # Save the current (official) flash image.
    ./chromatix_flash.py flash chromatix-standard.fs   # Install ChromatiX (backup first if none).
    ./chromatix_flash.py restore                       # Restore the first backup (original image).
"""

import os
import sys
import time
import shutil
import hashlib
import argparse
import datetime
import subprocess

# Constants ----------------------------------------------------------------------------------------

CABLE        = "gwu2x"
FPGA_MODEL   = "GW5A-25"
FPGA_PART    = "GW5A-EV25UG256"
FLASH_SIZE   = 0x100000 # Bitstream area (the official image uses ~0xE0000 bytes).
USB_VID_PID  = ("374e", "0101") # Chromatic USB device (official and ChromatiX designs).
BACKUP_DIR   = os.path.join(os.path.expanduser("~"), "chromatix-backups") # Default, see --backup-dir.

# Helpers ------------------------------------------------------------------------------------------

class FlashError(Exception):
    pass

def info(msg):
    print(f"[chromatix] {msg}", flush=True)

def openfpgaloader(args, capture=False):
    """Run openFPGALoader on the Chromatic's GWU2X bridge."""
    exe = shutil.which("openFPGALoader")
    if exe is None:
        raise FlashError("openFPGALoader not found (see https://github.com/trabucayre/openFPGALoader).")
    cmd = [exe, "--cable", CABLE] + args
    if capture:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return r.returncode, r.stdout
    return subprocess.run(cmd).returncode, None

def detect():
    """Check that the Chromatic's FPGA is reachable over JTAG."""
    rc, out = openfpgaloader(["--detect"], capture=True)
    if FPGA_MODEL not in out:
        print(out)
        raise FlashError(f"{FPGA_MODEL} FPGA not detected: is the Chromatic connected over USB-C and "
            "powered on (on Linux, are the openFPGALoader udev rules installed)?")
    return out

def sha256(filename):
    h = hashlib.sha256()
    with open(filename, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()

def check_bitstream(filename):
    """Check a Gowin .fs bitstream header (device/part)."""
    if not os.path.exists(filename):
        raise FlashError(f"{filename} not found.")
    if not filename.endswith(".fs"):
        raise FlashError(f"{filename}: a Gowin .fs bitstream is expected.")
    header = {}
    with open(filename, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.startswith("//"):
                break
            if ":" in line:
                key, value = line[2:].split(":", 1)
                header[key.strip()] = value.strip()
    if header.get("Device") != FPGA_MODEL or not header.get("Part Number", "").startswith(FPGA_PART):
        raise FlashError(f"{filename}: not a {FPGA_MODEL} ({FPGA_PART}) bitstream "
            f"(Device: {header.get('Device')}, Part Number: {header.get('Part Number')}).")
    return header

def list_backups():
    if not os.path.isdir(BACKUP_DIR):
        return []
    files = [f for f in os.listdir(BACKUP_DIR) if f.endswith(".bin")]
    return sorted(os.path.join(BACKUP_DIR, f) for f in files)

def usb_device_present():
    """Chromatic USB device enumerated (Linux only, None when unknown)."""
    root = "/sys/bus/usb/devices"
    if not os.path.isdir(root):
        return None
    for dev in os.listdir(root):
        try:
            with open(os.path.join(root, dev, "idVendor"), encoding="utf-8") as f:
                vid = f.read().strip()
            with open(os.path.join(root, dev, "idProduct"), encoding="utf-8") as f:
                pid = f.read().strip()
        except OSError:
            continue
        if (vid, pid) == USB_VID_PID:
            return True
    return False

def wait_usb_device(timeout=20):
    if usb_device_present() is None:
        return
    # Reconfiguration: the running design disconnects, then the new one enumerates.
    for _ in range(10):
        if not usb_device_present():
            break
        time.sleep(0.5)
    for _ in range(timeout):
        if usb_device_present():
            info("Chromatic USB device enumerated: the new design is running.")
            return
        time.sleep(1)
    info("Chromatic USB device not seen yet: power cycle the console if it doesn't start.")

# Commands -----------------------------------------------------------------------------------------

def cmd_info(args):
    out = detect()
    info(f"{FPGA_MODEL} FPGA detected over the GWU2X bridge.")
    usb = usb_device_present()
    if usb is not None:
        info(f"Chromatic USB device (UVC/UAC/CDC): {'present' if usb else 'not present'}.")
    backups = list_backups()
    info(f"Backups in {BACKUP_DIR}: {len(backups)}")
    for b in backups:
        print(f"  {b}")

def cmd_backup(args):
    detect()
    os.makedirs(BACKUP_DIR, exist_ok=True)
    filename = args.output or os.path.join(BACKUP_DIR,
        f"chromatic_flash_{datetime.datetime.now():%Y%m%d_%H%M%S}.bin")
    if os.path.exists(filename):
        raise FlashError(f"{filename} already exists.")
    info(f"Reading the flash ({FLASH_SIZE//1024}KB) to {filename}...")
    rc, _ = openfpgaloader(["--dump-flash", "--file-size", str(FLASH_SIZE), filename])
    if rc != 0 or not os.path.exists(filename) or os.path.getsize(filename) != FLASH_SIZE:
        raise FlashError("Flash dump failed.")
    with open(filename, "rb") as f:
        if not f.read().strip(b"\xff"):
            os.remove(filename)
            raise FlashError("Flash dump is empty (all 0xFF), backup discarded.")
    info(f"Backup saved: {filename} (sha256 {sha256(filename)}).")
    return filename

def cmd_flash(args):
    header = check_bitstream(args.bitstream)
    detect()
    if not list_backups() and not args.no_backup:
        info("No backup found: saving the current flash image first (restore with 'restore').")
        cmd_backup(argparse.Namespace(output=None))
    info(f"Writing {os.path.basename(args.bitstream)} ({header.get('Tool Version', '?')}, "
        f"checksum {header.get('CheckSum', '?')}): keep the console powered and connected...")
    rc, _ = openfpgaloader(["--write-flash", "--verify", "--reset", args.bitstream])
    if rc != 0:
        raise FlashError("Flash write/verify failed: the console can be re-flashed (JTAG stays available), "
            "retry or restore a backup.")
    info("Flash written and verified.")
    wait_usb_device()

def cmd_restore(args):
    backups  = list_backups()
    filename = args.backup or (backups[0] if backups else None)
    if filename is None:
        raise FlashError(f"No backup found in {BACKUP_DIR}: give the backup file to restore.")
    if not os.path.exists(filename) or os.path.getsize(filename) != FLASH_SIZE:
        raise FlashError(f"{filename}: not a {FLASH_SIZE//1024}KB flash backup.")
    detect()
    info(f"Restoring {filename} (sha256 {sha256(filename)}): keep the console powered and connected...")
    rc, _ = openfpgaloader(["--write-flash", "--file-type", "bin", "--verify", "--reset", filename])
    if rc != 0:
        raise FlashError("Flash restore failed: retry (JTAG stays available).")
    info("Flash restored and verified.")
    wait_usb_device()

# Run ----------------------------------------------------------------------------------------------

def main():
    global BACKUP_DIR
    parser = argparse.ArgumentParser(description="ChromatiX flasher for the ModRetro Chromatic.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("--backup-dir", default=BACKUP_DIR, help="Flash backups directory.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Info.
    subparsers.add_parser("info", help="Check the connection to the Chromatic and list the backups.")

    # Backup.
    p = subparsers.add_parser("backup", help="Save the current flash image.")
    p.add_argument("--output", default=None, help="Backup file (default: <backup-dir>/chromatic_flash_<date>.bin).")

    # Flash.
    p = subparsers.add_parser("flash", help="Install a ChromatiX bitstream (backup first if none).")
    p.add_argument("bitstream",                         help="ChromatiX bitstream (.fs).")
    p.add_argument("--no-backup", action="store_true", help="Don't save the current flash image first.")

    # Restore.
    p = subparsers.add_parser("restore", help="Restore a flash backup (default: the oldest one, the original image).")
    p.add_argument("backup", nargs="?", default=None, help="Backup file (.bin).")

    args = parser.parse_args()
    BACKUP_DIR = os.path.expanduser(args.backup_dir)
    try:
        {
            "info"    : cmd_info,
            "backup"  : cmd_backup,
            "flash"   : cmd_flash,
            "restore" : cmd_restore,
        }[args.command](args)
    except FlashError as e:
        print(f"[chromatix] Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()

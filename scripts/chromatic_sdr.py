#!/usr/bin/env python3

#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
Chromatic SDR host client: ESP-SDR protocol over the USB CDC port, relayed by firmware/sdr (USB
link, captures at the USB rate) or the ESP32 directly (standard bitstream USB bridge, 2Mbaud).

    chromatic_sdr.py info
    chromatic_sdr.py bench    [--count 50]
    chromatic_sdr.py spectrum [--freq 2437] [--rate 40] spectrum.png
    chromatic_sdr.py record   [--freq 2437] [--count 100] iq.cs8 (conjugated: true spectrum)
"""

import sys
import time
import zlib
import glob
import argparse

import serial

RATES = {16: 6, 40: 1, 80: 0} # MS/s -> ESP-SDR rate index.

# The ESP32 only tunes reliably on the Wi-Fi channel frequencies (ESP-SDR out of channel frequencies
# don't move its LO, measured on the console crystal harmonics).
CHANNELS = list(range(2412, 2473, 5)) + [2484]

def conjugate(data):
    """ESP32 8-bit I/Q (inverted spectrum) -> conjugated I/Q bytes (int8, Q negated, clipped)."""
    import numpy as np
    iq = np.frombuffer(data, dtype=np.int8).astype(np.int16)
    iq[1::2] = np.clip(-iq[1::2], -128, 127)
    return iq.astype(np.int8).tobytes()

def default_port():
    ports = sorted(glob.glob("/dev/serial/by-id/*Chromatic*if02*"))
    return ports[0] if ports else "/dev/ttyACM0"

# ESP-SDR ------------------------------------------------------------------------------------------

class ESPSDR:
    def __init__(self, port=None, timeout=1.0):
        self.port = serial.Serial()
        self.port.port     = port or default_port()
        self.port.baudrate = 2000000 # Ignored by the USB relay, ESP32 UART rate for the bridge.
        self.port.timeout  = timeout
        self.port.dtr      = False   # Standard bitstream bridge: ESP32 EN/IO0 released.
        self.port.rts      = False
        self.port.open()
        self.sync()

    def close(self):
        self.port.close()

    def readline(self):
        line = self.port.readline()
        if not line.endswith(b"\n"):
            raise TimeoutError("ESP-SDR reply timeout.")
        return line.decode(errors="replace").strip()

    def cmd(self, command, reply=True):
        self.port.write((command + "\n").encode())
        return self.readline() if reply else None

    def sync(self):
        """Resynchronize (incomplete transfers): SYNC with a nonce until it is echoed."""
        nonce = int(time.time()*1000) & 0xffffffff
        for attempt in range(4):
            time.sleep(0.05)
            self.port.reset_input_buffer()
            self.port.write(f"SYNC {nonce}\n".encode())
            deadline = time.time() + 1
            while time.time() < deadline:
                try:
                    if self.readline() == f"SYNC {nonce}":
                        return
                except TimeoutError:
                    break
        raise IOError("ESP-SDR not responding.")

    def capture(self, samples=16380, rate=40, bits=8):
        """I/Q capture: list of (I, Q) bytes (int8 interleaved) as bytes, capture duration in us."""
        cmd = {8: "CAP16", 10: "CAP20"}[bits]
        header = self.cmd(f"{cmd} {samples} {RATES[rate]}")
        if not header.startswith("DATA "):
            raise IOError(f"Capture error: {header}")
        _, n, crc, us = header.split()
        n     = int(n)
        size  = 2*n if bits == 8 else (20*n + 7)//8
        data  = self.port.read(size)
        if len(data) != size:
            raise TimeoutError(f"Capture payload timeout ({len(data)}/{size} bytes).")
        if zlib.crc32(data) != int(crc, 16):
            raise IOError("Capture CRC error.")
        return data, int(us)

# Commands -----------------------------------------------------------------------------------------

def spectrum(data, nfft=1024):
    import numpy as np
    iq  = np.frombuffer(data, dtype=np.int8).astype(np.float32)
    x   = (iq[0::2] - 1j*iq[1::2]) # Conjugated (ESP32 inverted spectrum).
    x   = x[:len(x)//nfft*nfft].reshape(-1, nfft)
    x   = x - x.mean(axis=1, keepdims=True)
    win = np.hanning(nfft)
    p   = (np.abs(np.fft.fftshift(np.fft.fft(x*win, axis=1), axes=1))**2).mean(axis=0)
    return 10*np.log10(p + 1e-12)

def main():
    parser = argparse.ArgumentParser(description="Chromatic SDR host client (ESP-SDR protocol).")
    parser.add_argument("--port",   default=None, help="Serial port (default: Chromatic CDC).")
    parser.add_argument("--freq",   default=2437, type=int, help="Center frequency (MHz).")
    parser.add_argument("--rate",   default=40,   type=int, choices=sorted(RATES),
        help="Sample rate (MS/s).")
    parser.add_argument("--gain",   default=None, type=int, help="Manual gain (default: AGC).")
    parser.add_argument("--count",  default=50,   type=int, help="Captures.")
    parser.add_argument("command",  choices=["info", "bench", "spectrum", "record"])
    parser.add_argument("filename", nargs="?", help="Output file (spectrum: .png, record: .cs8).")
    args = parser.parse_args()

    if args.command != "info" and args.freq not in CHANNELS:
        print(f"Warning: {args.freq} MHz is not a Wi-Fi channel frequency: the LO may not move.")
    sdr = ESPSDR(args.port)
    if args.command == "info":
        for c in ["INFO", "CAPS", "LIMITS?", "RANGE?", "TRANSPORT?", "GAIN?"]:
            print(f"{c:12s} {sdr.cmd(c)}")
        return

    print(f"FREQ {args.freq}: {sdr.cmd(f'FREQ {args.freq}')}")
    print("GAIN: " + sdr.cmd("GAIN HARDWARE" if args.gain is None else f"GAIN MANUAL {args.gain}"))

    captures, errors, nbytes = [], 0, 0
    t0 = time.time()
    for i in range(args.count):
        try:
            data, us = sdr.capture(rate=args.rate)
            nbytes += len(data)
            if args.command != "bench":
                captures.append(data)
        except (IOError, TimeoutError) as e:
            errors += 1
            print(e)
            sdr.sync()
    dt = time.time() - t0
    ok = args.count - errors
    print(f"{ok}/{args.count} captures in {dt:.2f}s: {ok/dt:.1f} captures/s, "
        f"{nbytes/dt/1e6:.2f} MB/s ({nbytes/2/dt/1e6:.2f} MS/s delivered), {errors} errors.")

    if args.command == "spectrum" and captures:
        import numpy as np
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        db    = np.mean([spectrum(d) for d in captures], axis=0)
        peak  = np.max([spectrum(d) for d in captures], axis=0)
        freqs = args.freq + np.linspace(-args.rate/2, args.rate/2, len(db), endpoint=False)
        plt.figure(figsize=(10, 4))
        plt.plot(freqs, peak, lw=0.6, label="max hold")
        plt.plot(freqs, db,   lw=0.8, label="average")
        plt.xlabel("Frequency (MHz)"); plt.ylabel("Power (dB, arbitrary)")
        plt.title(f"Chromatic SDR: {args.freq} MHz, {args.rate} MS/s, {len(captures)} captures")
        plt.grid(alpha=0.3); plt.legend()
        plt.tight_layout()
        plt.savefig(args.filename or "spectrum.png", dpi=120)
    if args.command == "record" and captures:
        with open(args.filename or "iq.cs8", "wb") as f:
            for d in captures:
                f.write(conjugate(d))
    sdr.close()

if __name__ == "__main__":
    main()

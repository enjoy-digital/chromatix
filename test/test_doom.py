#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Doom support: WAD lumps alignment (scripts/chromatic.py, as firmware/doom/wad_align.h)."""

import os
import struct
import importlib.util

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

def load_script():
    spec   = importlib.util.spec_from_file_location("chromatic_script", os.path.join(ROOT, "scripts", "chromatic.py"))
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    return script

def make_wad(lumps):
    """WAD with lumps packed without alignment (directory in the middle, as some tools do)."""
    data   = b"".join(d for _, d in lumps)
    header = 12
    direc  = header + len(data)
    entries, pos = b"", header
    for name, d in lumps:
        entries += struct.pack("<II8s", pos, len(d), name)
        pos += len(d)
    return b"IWAD" + struct.pack("<II", len(lumps), direc) + data + entries

def read_lumps(wad):
    n, direc = struct.unpack("<II", wad[4:12])
    lumps = []
    for i in range(n):
        pos, size, name = struct.unpack("<II8s", wad[direc + 16*i:direc + 16*(i + 1)])
        lumps.append((pos, name, wad[pos:pos + size]))
    return lumps

def test_align_wad():
    script = load_script()
    lumps  = [(b"PLAYPAL\0", bytes(range(7))), (b"E1M1\0\0\0\0", b""), (b"THINGS\0\0", b"\x01\x02\x03"),
        (b"DEMO1\0\0\0", bytes(range(13)))]
    wad     = make_wad(lumps)
    aligned = script.align_wad(wad)
    assert aligned[:4] == b"IWAD"
    result  = read_lumps(aligned)
    assert [(name, data) for _, name, data in result] == lumps
    assert all(pos % 4 == 0 for pos, _, _ in result)
    # Directory at the end (size = directory offset + entries, used by the firmware).
    n, direc = struct.unpack("<II", aligned[4:12])
    assert len(aligned) == direc + 16*n

def test_align_wad_invalid():
    script = load_script()
    with pytest.raises(ValueError):
        script.align_wad(b"NOTAWAD" + bytes(20))
    with pytest.raises(ValueError):
        script.align_wad(b"IWAD" + struct.pack("<II", 10, 1000))

#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""VoxelRenderer: rows rendered by the gateware == the reference model (firmware/voxel/vr.c)."""

import os
import math
import ctypes
import random
import subprocess

import pytest

from migen import *

from litex.gen.sim import run_simulation

from chromatix.gateware.voxel       import VoxelRenderer, WORLD_WORDS, TEXTURE_WORDS, VR_WIDTH

VOXEL_DIR = os.path.join(os.path.dirname(__file__), "..", "firmware", "voxel")

# Reference model ----------------------------------------------------------------------------------

class VRCamera(ctypes.Structure):
    _fields_ = [
        ("cx", ctypes.c_uint8), ("cy", ctypes.c_uint8), ("cz", ctypes.c_uint8),
        ("fx", ctypes.c_uint8), ("fy", ctypes.c_uint8), ("fz", ctypes.c_uint8),
        ("fwd", ctypes.c_int16*3), ("right", ctypes.c_int16*3), ("down", ctypes.c_int16*3),
        ("ox", ctypes.c_uint8), ("oz", ctypes.c_uint8),
        ("t_far", ctypes.c_uint32), ("fog_start", ctypes.c_uint32),
        ("fog_shift", ctypes.c_uint8), ("sky_base", ctypes.c_uint8),
        ("sel_en", ctypes.c_uint8), ("sel_x", ctypes.c_uint8), ("sel_y", ctypes.c_uint8),
        ("sel_z", ctypes.c_uint8), ("sel_index", ctypes.c_uint8),
        ("cross_en", ctypes.c_uint8), ("cross_index", ctypes.c_uint8),
    ]

class VRScene(ctypes.Structure):
    _fields_ = [
        ("world",    ctypes.POINTER(ctypes.c_uint32)),
        ("textures", ctypes.POINTER(ctypes.c_uint32)),
        ("blocks",   ctypes.POINTER(ctypes.c_uint16)),
    ]

@pytest.fixture(scope="module")
def vr(tmp_path_factory):
    lib = str(tmp_path_factory.mktemp("vr") / "libvr.so")
    subprocess.check_call(["gcc", "-O2", "-shared", "-fPIC", "-o", lib, os.path.join(VOXEL_DIR, "vr.c")])
    return ctypes.CDLL(lib)

def reference_rows(vr, scene, cam, rows):
    world    = (ctypes.c_uint32*WORLD_WORDS)(*scene["world"])
    textures = (ctypes.c_uint32*TEXTURE_WORDS)(*scene["textures"])
    blocks   = (ctypes.c_uint16*16)(*scene["blocks"])
    s        = VRScene(world, textures, blocks)
    c        = VRCamera(**{k: (v if not isinstance(v, list) else (ctypes.c_int16*3)(*v)) for k, v in cam.items()})
    image    = {}
    for y in rows:
        line = (ctypes.c_uint8*VR_WIDTH)()
        vr.vr_line(ctypes.byref(s), ctypes.byref(c), y, line)
        image[y] = list(line)
    return image

# Scenes/cameras -----------------------------------------------------------------------------------

def make_scene(seed):
    rng   = random.Random(seed)
    voxel = {}
    for z in range(32):
        for x in range(32):
            h = 8 + int(4*math.sin(x/5 + seed) + 3*math.cos(z/4))
            for y in range(32):
                v = 0
                if y <= h:
                    v = rng.choice([1, 2, 3]) if y < h else 3
                elif y <= h + 4 and rng.random() < 0.03:
                    v = rng.randrange(1, 16) # Floating blocks (all types).
                voxel[(x, y, z)] = v
    world = [0]*WORLD_WORDS
    for (x, y, z), v in voxel.items():
        addr = (y << 10) | (z << 5) | x
        world[addr >> 3] |= v << 4*(addr & 7)
    # Textures: random colors, some transparent texels (textures 7 and 11 and a few others).
    textures = [0]*TEXTURE_WORDS
    for t in range(16):
        for i in range(256):
            color = rng.randrange(1, 31)
            if t in [7, 11] and rng.random() < 0.4:
                color = 0
            texel = color | rng.randrange(0, 8) << 5 # Upper bits ignored.
            addr  = (t << 8) | i
            textures[addr >> 2] |= texel << 8*(addr & 3)
    blocks = [rng.randrange(16) | rng.randrange(16) << 4 | rng.randrange(16) << 8 for _ in range(16)]
    return {"world": world, "textures": textures, "blocks": blocks}

def make_camera(yaw, pitch, pos, window=(0, 0), select=None, scale=143):
    s,  c  = math.sin(yaw),   math.cos(yaw)
    sp, cp = math.sin(pitch), math.cos(pitch)
    q = lambda v: int(round(v*16384))
    cam = dict(
        cx = int(pos[0]) & 0xff, cy = int(pos[1]), cz = int(pos[2]) & 0xff,
        fx = int(pos[0]*256) & 0xff, fy = int(pos[1]*256) & 0xff, fz = int(pos[2]*256) & 0xff,
        fwd   = [q(s*cp), q(sp), q(c*cp)],
        right = [q(c)*scale >> 14, 0, -q(s)*scale >> 14],
        down  = [(q(sp*s)*scale) >> 14, -(q(cp)*scale >> 14), (q(sp*c)*scale) >> 14],
        ox = window[0], oz = window[1],
        t_far = 26 << 12, fog_start = 10 << 12, fog_shift = 13, sky_base = 31 << 3,
        sel_en = 0, sel_x = 0, sel_y = 0, sel_z = 0, sel_index = 29 << 3,
        cross_en = 1, cross_index = 28 << 3,
    )
    if select is not None:
        cam.update(sel_en=1, sel_x=select[0], sel_y=select[1], sel_z=select[2])
    return cam

# Gateware -----------------------------------------------------------------------------------------

def gateware_rows(scene, cam, first, count, div_bits=2):
    dut = VoxelRenderer(div_bits=div_bits, world_init=scene["world"], textures_init=scene["textures"])
    lines  = {}
    slots  = {}
    result = {}

    def csr(reg, value):
        yield from reg.write(value)

    def s16(v):
        return v & 0xffff

    def generator():
        for i, b in enumerate(scene["blocks"]):
            yield from dut.bus.write(0x1800 + i, b)
        yield from csr(dut.rows,      first | count << 8)
        yield from csr(dut.cell,      cam["cx"] | cam["cy"] << 8 | cam["cz"] << 16)
        yield from csr(dut.frac,      cam["fx"] | cam["fy"] << 8 | cam["fz"] << 16)
        for name in ["fwd", "right", "down"]:
            v = cam[name]
            yield from csr(getattr(dut, f"_{name}_xy"), s16(v[0]) | s16(v[1]) << 16)
            yield from csr(getattr(dut, f"_{name}_z"),  s16(v[2]))
        yield from csr(dut.window,    cam["ox"] | cam["oz"] << 8)
        yield from csr(dut.t_far,     cam["t_far"])
        yield from csr(dut.fog_start, cam["fog_start"])
        yield from csr(dut.fog,       cam["fog_shift"] | cam["sky_base"] << 5)
        yield from csr(dut.select,    cam["sel_en"] | cam["sel_x"] << 1 | cam["sel_y"] << 9 | cam["sel_z"] << 17)
        yield from csr(dut.colors,    cam["sel_index"] | cam["cross_en"] << 8 | cam["cross_index"] << 9)
        yield from csr(dut.control,   1 | 2 << 1)
        yield
        while (yield dut.status.fields.busy):
            if (yield dut.fb_wr.we):
                adr = (yield dut.fb_wr.adr)
                dat = (yield dut.fb_wr.dat)
                slot, word = divmod(adr, 40)
                for b in range(4):
                    slots.setdefault(slot, {})[4*word + b] = (dat >> 8*b) & 0xff
            if (yield dut.fb_line.valid):
                assert (yield dut.fb_line.buffer) == 2
                slot = (yield dut.fb_line.slot)
                lines[(yield dut.fb_line.line)] = [slots[slot][x] for x in range(VR_WIDTH)]
                slots[slot] = {}
            yield
        result["cycles"] = (yield dut.cycles.status)

    run_simulation(dut, generator())
    assert sorted(lines) == list(range(first, first + count))
    return lines, result["cycles"]

def check(vr, scene, cam, first, count):
    image, cycles = gateware_rows(scene, cam, first, count)
    ref = reference_rows(vr, scene, cam, range(first, first + count))
    for y in image:
        diff = [x for x in range(VR_WIDTH) if image[y][x] != ref[y][x]]
        assert not diff, f"row {y}: {len(diff)} pixels differ, first x={diff[0]}: " \
            f"gateware {image[y][diff[0]]:#04x}, model {ref[y][diff[0]]:#04x}"
    return image, cycles

# Tests --------------------------------------------------------------------------------------------

def test_voxel_terrain(vr):
    scene = make_scene(1)
    cam   = make_camera(0.6, -0.3, (16.5, 14.6, 16.3))
    image, cycles = check(vr, scene, cam, 60, 6) # Crosshair rows.
    # Not all sky: the terrain is seen.
    assert any((p >> 3) != 31 for line in image.values() for p in line)
    print(f"{cycles/(6*VR_WIDTH):.1f} cycles/pixel")

def test_voxel_axis_aligned(vr):
    # Rays with zero components (TINF), camera on a cell boundary (fraction 0).
    scene = make_scene(2)
    cam   = make_camera(0.0, 0.0, (16.0, 13.0, 4.0))
    check(vr, scene, cam, 62, 3)

def test_voxel_looking_down_select(vr):
    scene = make_scene(3)
    cam   = make_camera(2.4, -1.1, (10.3, 16.2, 20.7), select=(10, 12, 18))
    check(vr, scene, cam, 100, 4)

def test_voxel_window_wrap(vr):
    # Camera at the world (X/Z mod 256) edge, window across the wrap.
    scene = make_scene(4)
    cam   = make_camera(-2.0, 0.2, (255.5, 15.2, 1.4), window=(240, 241))
    check(vr, scene, cam, 30, 4)

def test_voxel_sky_rows(vr):
    scene = make_scene(5)
    cam   = make_camera(1.0, 0.9, (16.5, 20.5, 16.5))
    check(vr, scene, cam, 0, 2)

# Verilog (Verilator) ------------------------------------------------------------------------------

def write_dump(path, scene, cam):
    """Scene/camera file (firmware/voxel hal_sdl.c VOXEL_DUMP format)."""
    import struct
    c = VRCamera(**{k: (v if not isinstance(v, list) else (ctypes.c_int16*3)(*v)) for k, v in cam.items()})
    with open(path, "wb") as f:
        f.write(struct.pack(f"<{WORLD_WORDS}I", *scene["world"]))
        f.write(struct.pack(f"<{TEXTURE_WORDS}I", *scene["textures"]))
        f.write(struct.pack("<16H", *scene["blocks"]))
        f.write(bytes(c))

def build_verilator_tb(build_dir):
    """VoxelRenderer Verilog (CSR storages as top inputs) + test/voxel_tb.cpp + vr.c."""
    from migen.fhdl import verilog
    from litex.soc.interconnect.csr import CSRStorage, CSRStatus
    dut = VoxelRenderer()
    dut.clock_domains.cd_sys = ClockDomain()
    ios = {dut.cd_sys.clk, dut.cd_sys.rst}
    for c in dut.get_csrs():
        if isinstance(c, CSRStorage):
            ios |= {c.storage, c.wr_stb}
            for f in c.fields.fields if hasattr(c, "fields") else []:
                v = c.storage[f.offset:f.offset + f.size]
                dut.comb += getattr(c.fields, f.name).eq(v & c.wr_stb if f.pulse else v)
        elif isinstance(c, CSRStatus):
            ios |= {c.status}
            for f in c.fields.fields if hasattr(c, "fields") else []:
                dut.comb += c.status[f.offset:f.offset + f.size].eq(getattr(c.fields, f.name))
    b = dut.bus
    ios |= {b.adr, b.dat_w, b.dat_r, b.cyc, b.stb, b.we, b.ack, b.sel}
    ios |= set(dut.fb_wr.flatten()) | set(dut.fb_line.flatten()) | {dut.fb_busy}
    conv = verilog.convert(dut, ios, name="voxel")
    conv.write(os.path.join(build_dir, "voxel.v"))
    n = lambda s: "top->" + conv.ns.get_name(s)
    ports = {
        "BUS_ADR": b.adr, "BUS_DAT_W": b.dat_w, "BUS_WE": b.we, "BUS_SEL": b.sel, "BUS_CYC": b.cyc,
        "BUS_STB": b.stb, "BUS_ACK": b.ack,
        "ROWS": dut.rows.storage, "CELL": dut.cell.storage, "FRAC": dut.frac.storage,
        "FWD_XY": dut._fwd_xy.storage, "FWD_Z": dut._fwd_z.storage, "RIGHT_XY": dut._right_xy.storage,
        "RIGHT_Z": dut._right_z.storage, "DOWN_XY": dut._down_xy.storage, "DOWN_Z": dut._down_z.storage,
        "WINDOW": dut.window.storage, "T_FAR": dut.t_far.storage, "FOG_START": dut.fog_start.storage,
        "FOG": dut.fog.storage, "SELECT": dut.select.storage, "COLORS": dut.colors.storage,
        "CONTROL": dut.control.storage, "CONTROL_RE": dut.control.wr_stb,
        "FB_WE": dut.fb_wr.we, "FB_ADR": dut.fb_wr.adr, "FB_DAT": dut.fb_wr.dat,
        "FB_LINE_VALID": dut.fb_line.valid, "FB_LINE_LINE": dut.fb_line.line, "FB_LINE_SLOT": dut.fb_line.slot,
        "CYCLES": dut.cycles.status,
    }
    with open(os.path.join(build_dir, "tb_ports.h"), "w") as f:
        for k, s in ports.items():
            f.write(f"#define {k} {n(s)}\n")
    here = os.path.dirname(os.path.abspath(__file__))
    subprocess.check_call(["verilator", "--cc", "--exe", "--build", "-O2", "-Wno-fatal", "-Wno-lint",
        "--top-module", "voxel", "-Mdir", os.path.join(build_dir, "obj"),
        f"-CFLAGS", f"-I{build_dir} -I{os.path.abspath(VOXEL_DIR)}",
        os.path.join(build_dir, "voxel.v"), os.path.join(here, "voxel_tb.cpp"),
        os.path.abspath(os.path.join(VOXEL_DIR, "vr.c"))], stdout=subprocess.DEVNULL)
    return os.path.join(build_dir, "obj", "Vvoxel")

@pytest.mark.skipif(subprocess.run(["which", "verilator"], capture_output=True).returncode != 0,
    reason="Verilator not installed")
def test_voxel_verilog_frames(tmp_path):
    """Whole frames rendered by the renderer's Verilog (Verilator) == the reference model: catches
    Verilog vs Migen simulation differences (ex: widths of the expressions in comparisons)."""
    tb    = build_verilator_tb(str(tmp_path))
    dumps = []
    for i, (yaw, pitch, pos, kw) in enumerate([
            (0.6, -0.3, (16.5, 14.6, 16.3), {}),
            (0.0,  0.0, (16.0, 13.0,  4.0), {}),
            (2.4, -1.1, (10.3, 16.2, 20.7), {"select": (10, 12, 18)}),
            (-2.0, 0.2, (255.5, 15.2, 1.4), {"window": (240, 241)}),
            (1.0,  0.9, (16.5, 20.5, 16.5), {})]):
        cam = make_camera(yaw, pitch, pos, **kw)
        cam.update(fog_start=2 << 12, fog_shift=11) # Strong fog: all shades.
        path = str(tmp_path / f"scene{i}.bin")
        write_dump(path, make_scene(i + 1), cam)
        dumps.append(path)
    r = subprocess.run([tb, *dumps], capture_output=True, text=True, timeout=1200)
    print(r.stdout)
    assert r.returncode == 0, r.stdout + r.stderr

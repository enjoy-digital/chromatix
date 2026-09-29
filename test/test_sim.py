#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
Verilator simulation of the Game Boy core (chromatix_sim.py) with the test ROM: boot, LCD output,
cartridge banking and buttons. Skipped when Verilator/GHDL are not installed.
"""

import os
import shutil
import importlib.util

import pytest

from chromatix.gateware.sim import parse_button_sequence, button_events, rom_init, check_rom
from chromatix.gateware.sim import CART_ROM_SIZE

from test.gb_test_rom import make_test_rom

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

def load_sim():
    spec = importlib.util.spec_from_file_location("chromatix_sim", os.path.join(ROOT, "chromatix_sim.py"))
    sim  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sim)
    return sim

def stripes(ppm, y=70):
    """Colors of the first 4 8-pixel columns of a frame line."""
    from PIL import Image
    image = Image.open(ppm)
    return [image.getpixel((x, y)) for x in (0, 8, 16, 24)]

WHITE = (255, 255, 255)
BLACK = (0, 0, 0)

# Helpers ------------------------------------------------------------------------------------------

def test_button_events():
    presses = parse_button_sequence("start@10+5,a@12")
    assert presses == [("start", 10, 5), ("a", 12, 5)]
    assert button_events(presses) == [(10, 0b1000), (12, 0b1001), (15, 0b0001), (17, 0b0000)]
    with pytest.raises(ValueError):
        parse_button_sequence("x@10")

def test_rom_init():
    rom  = make_test_rom()
    init = rom_init(rom)
    assert len(init) == CART_ROM_SIZE
    assert init[:len(rom)] == rom and init[len(rom):2*len(rom)] == rom # Mirrored.
    with pytest.raises(ValueError):
        check_rom(rom[:0x100])
    with pytest.raises(ValueError):
        check_rom(rom[:0x147] + bytes([0x0f]) + rom[0x148:]) # MBC3.

# Simulation ---------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def sim_build(tmp_path_factory):
    if shutil.which("verilator") is None or shutil.which("ghdl") is None:
        pytest.skip("Verilator/GHDL not installed.")
    sim      = load_sim()
    gateware = str(tmp_path_factory.mktemp("sim") / "gateware")
    sim.build_sim(gateware, make_test_rom())
    return sim, gateware

def test_sim_rom_only(sim_build):
    """Boot ROM then test ROM: 8-pixel white/black stripes."""
    sim, gateware = sim_build
    ppms = sim.run_sim(gateware, make_test_rom(), frames=31, every=30)
    assert [os.path.basename(p) for p in ppms] == ["frame_0000.ppm", "frame_0030.ppm"]
    assert stripes(ppms[-1]) == [WHITE, BLACK, WHITE, BLACK]

def test_sim_mbc1(sim_build):
    """Another ROM on the same build: MBC1 bank 2 selects the inverted palette."""
    sim, gateware = sim_build
    ppms = sim.run_sim(gateware, make_test_rom(mbc1=True), frames=31, every=30)
    assert stripes(ppms[-1]) == [BLACK, WHITE, BLACK, WHITE]

def test_sim_buttons(sim_build):
    """A pressed at frame 40: palette inverted."""
    sim, gateware = sim_build
    ppms = sim.run_sim(gateware, make_test_rom(), frames=61, every=30,
        presses=parse_button_sequence("a@40+5"))
    assert stripes(ppms[1]) == [WHITE, BLACK, WHITE, BLACK] # Frame 30.
    assert stripes(ppms[2]) == [BLACK, WHITE, BLACK, WHITE] # Frame 60.

# Simulation (Virtual Cartridge) -------------------------------------------------------------------

@pytest.fixture(scope="module")
def sim_build_vcart(tmp_path_factory):
    if shutil.which("verilator") is None or shutil.which("ghdl") is None:
        pytest.skip("Verilator/GHDL not installed.")
    sim      = load_sim()
    gateware = str(tmp_path_factory.mktemp("sim_vcart") / "gateware")
    sim.build_sim(gateware, make_test_rom(), vcart=True)
    return sim, gateware

def test_sim_vcart_rom_only(sim_build_vcart):
    """ROM served by the virtual cartridge (PSRAM model with latency, cache misses freeze the core)."""
    sim, gateware = sim_build_vcart
    ppms = sim.run_sim(gateware, make_test_rom(), frames=31, every=30)
    assert stripes(ppms[-1]) == [WHITE, BLACK, WHITE, BLACK]

def test_sim_vcart_mbc1(sim_build_vcart):
    """Virtual cartridge MBC1 (configured from the ROM header): bank 2 selects the inverted palette."""
    sim, gateware = sim_build_vcart
    ppms = sim.run_sim(gateware, make_test_rom(mbc1=True), frames=31, every=30)
    assert stripes(ppms[-1]) == [BLACK, WHITE, BLACK, WHITE]

#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

import os
import importlib.util

from migen import *

from litex.gen import *
from litex.gen.sim import run_simulation

from litex.soc.interconnect import csr_bus

from chromatix.gateware.debug import DebugControl, BUTTONS

# Helpers ------------------------------------------------------------------------------------------

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

def load_chromatic_script():
    spec   = importlib.util.spec_from_file_location("chromatic_script", os.path.join(ROOT, "scripts", "chromatic.py"))
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    return script

CSRS = ["buttons", "status", "system_control", "volt", "adc_value", "volume", "pmic_sys_status"]

class DebugControlBench(LiteXModule):
    """DebugControl behind a 32-bit CSR bank (CSR i at word address i)."""
    def __init__(self):
        self.dut  = DebugControl()
        self.bank = csr_bus.CSRBank(self.dut.get_csrs(), bus=csr_bus.Interface(data_width=32))

    def read(self, name):
        bus = self.bank.bus
        yield bus.adr.eq(CSRS.index(name))
        yield bus.re.eq(1)
        yield
        yield bus.re.eq(0)
        yield # dat_r is registered.
        return (yield bus.dat_r)

    def write(self, name, value):
        yield from self.bank.bus.write(CSRS.index(name), value)

class FakeCSR:
    def __init__(self, value=0):
        self.value  = value
        self.writes = []

    def read(self):
        return self.value

    def write(self, value):
        self.value = value
        self.writes.append(value)

# Debug Control ------------------------------------------------------------------------------------

def test_debug_control_csrs():
    """CSR names used by scripts/chromatic.py (and scripts/csr.csv) are stable."""
    dut   = DebugControl()
    names = [csr.name for csr in dut.get_csrs()]
    assert names == CSRS

def test_debug_control_buttons():
    """Virtual buttons: one CSR bit per button (in BUTTONS order) to the matching output."""
    tb  = DebugControlBench()
    dut = tb.dut

    def gen():
        for i, name in enumerate(BUTTONS):
            yield from tb.write("buttons", 1 << i)
            yield
            for other in BUTTONS:
                assert (yield getattr(dut, other)) == (other == name)
            assert (yield from tb.read("buttons")) == (1 << i)
        yield from tb.write("buttons", 0)
        yield
        for name in BUTTONS:
            assert (yield getattr(dut, name)) == 0

    run_simulation(tb, gen())

def test_debug_control_status():
    """Status inputs reach the status CSRs (resynchronized ones after the MultiReg latency)."""
    tb     = DebugControlBench()
    dut    = tb.dut
    fields = [f.name for f in dut._status.fields.fields]

    def gen():
        inputs = {
            "bist_done"       : 1,
            "bist_failed"     : 0,
            "lcd_init_done"   : 1,
            "menu_disabled"   : 0,
            "low_battery"     : 1,
            "bat_is_li"       : 0,
            "headphones"      : 1,
            "system_control"  : 0xa5c3,
            "volt"            : 0x2345,
            "adc_value"       : 0x1abc,
            "volume"          : 0x76,
            "pmic_sys_status" : 0x81,
        }
        for name, value in inputs.items():
            yield getattr(dut, name).eq(value)
        for _ in range(4):
            yield
        status = (yield from tb.read("status"))
        for i, name in enumerate(fields):
            assert (status >> i) & 0x1 == inputs[name], name
        for name in CSRS[2:]:
            assert (yield from tb.read(name)) == inputs[name], name

        # Toggle every status bit.
        for name in fields:
            yield getattr(dut, name).eq(1 - inputs[name])
        for _ in range(4):
            yield
        status = (yield from tb.read("status"))
        for i, name in enumerate(fields):
            assert (status >> i) & 0x1 == 1 - inputs[name], name

    run_simulation(tb, gen())

# Control Script -----------------------------------------------------------------------------------

def test_chromatic_script_matches_debug_control():
    """scripts/chromatic.py button/status encodings match the DebugControl CSR fields."""
    script = load_chromatic_script()
    dut    = DebugControl()

    # Status field order.
    assert script.STATUS_FIELDS == [f.name for f in dut._status.fields.fields]

    # Button bits (without a LiteX server: fake CSRs).
    chromatic     = script.Chromatic.__new__(script.Chromatic)
    chromatic.bus = type("Bus", (), {})()
    chromatic.bus.regs = type("Regs", (), {})()
    chromatic.bus.regs.debug_ctrl_buttons = buttons = FakeCSR()
    for field in dut._buttons.fields.fields:
        chromatic.set_buttons([field.name])
        assert buttons.value == (1 << field.offset)
    chromatic.set_buttons(["a", "start"])
    assert buttons.value == (1 << BUTTONS.index("a")) | (1 << BUTTONS.index("start"))
    chromatic.set_buttons([])
    assert buttons.value == 0

    # Sequence.
    buttons.writes.clear()
    script.run_sequence(chromatic, "press:b+sel@0 buttons:menu buttons:")
    assert buttons.writes == [
        (1 << BUTTONS.index("b")) | (1 << BUTTONS.index("sel")), 0,
        (1 << BUTTONS.index("menu")),
        0,
    ]

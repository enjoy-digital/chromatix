#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litex.gen.sim import run_simulation

from chromatix.gateware.buttons import Debouncer, Buttons, BUTTONS

# Debouncer ----------------------------------------------------------------------------------------

def test_debouncer_filters_glitches():
    """Short glitches are ignored, a stable level is propagated after 2**cnt_bit cycles."""
    dut     = Debouncer(cnt_bit=4)
    outputs = []

    def gen():
        # Glitch shorter than the debounce time.
        yield dut.i.eq(1)
        for _ in range(8):
            yield
        yield dut.i.eq(0)
        for _ in range(40):
            outputs.append((yield dut.o))
            yield
        assert not any(outputs), "glitch propagated"

        # Stable press.
        yield dut.i.eq(1)
        for cycle in range(40):
            if (yield dut.o):
                break
            yield
        assert (yield dut.o) == 1
        assert cycle >= 2**4, "press propagated too early"

        # Stable release.
        yield dut.i.eq(0)
        for _ in range(40):
            yield
        assert (yield dut.o) == 0

    run_simulation(dut, gen())

# Buttons ------------------------------------------------------------------------------------------

def test_buttons_debounced_independently():
    """Each button pad goes through its own debouncer to the matching output (Menu excluded)."""
    pads = Record([(name, 1) for name in BUTTONS + ["menu"]])
    dut  = Buttons(pads, cnt_bit=3)

    def gen():
        for name in BUTTONS:
            yield getattr(pads, name).eq(1)
            for _ in range(2**3 + 8):
                yield
            for other in BUTTONS:
                assert (yield getattr(dut, other)) == (other == name), f"{name} -> {other}"
            yield getattr(pads, name).eq(0)
            for _ in range(2**3 + 8):
                yield
            assert (yield getattr(dut, name)) == 0
        assert not hasattr(dut, "menu")

    run_simulation(dut, gen())

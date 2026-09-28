#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: GPL-3.0-only
# Derived from ModRetro's oss-chromatic-console-fpga (GPL-3.0).

from migen import *

from litex.gen import *

# Debouncer ----------------------------------------------------------------------------------------

class Debouncer(LiteXModule):
    """
    Button debouncer.

    Samples the input through a 3-stage synchronizer and only toggles the output once the
    synchronized input has been stable (different from the output) for 2**cnt_bit cycles.
    """
    def __init__(self, cnt_bit=14):
        self.i = Signal()
        self.o = Signal()

        # # #

        sampling = Signal(3)
        count    = Signal(cnt_bit + 1)

        self.sync += [
            sampling.eq(Cat(self.i, sampling[:2])),
            If(sampling[2] == self.o,
                count.eq(0),
            ).Elif(~count[cnt_bit],
                count.eq(count + 1),
            ),
            If(count[cnt_bit],
                self.o.eq(~self.o),
                count.eq(0),
            ),
        ]

# Buttons ------------------------------------------------------------------------------------------

BUTTONS = ["a", "b", "dpad_down", "dpad_left", "dpad_right", "dpad_up", "sel", "start"]

class Buttons(LiteXModule):
    """Debounced Chromatic buttons (Menu excluded: sampled directly by the system monitor)."""
    def __init__(self, pads, cnt_bit=14):
        for name in BUTTONS:
            debouncer = Debouncer(cnt_bit=cnt_bit)
            self.add_module(name=f"{name}_debouncer", module=debouncer)
            self.comb += debouncer.i.eq(getattr(pads, name))
            setattr(self, name, debouncer.o)

#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""LiteX BIOS demo peripherals: buttons and tone generator for firmware."""

from migen import *
from migen.genlib.cdc import MultiReg

from litex.gen import *

from litex.soc.interconnect.csr import *

from chromatix.gateware.buttons import BUTTONS

# Buttons ------------------------------------------------------------------------------------------

class ButtonsCSR(LiteXModule):
    """Buttons state (1: pressed), bits in BUTTONS order, then menu."""
    def __init__(self, buttons, menu):
        self.status = CSRStatus(len(BUTTONS) + 1, description="Buttons (1: pressed): " +
            ", ".join(BUTTONS + ["menu"]) + " (bit 0 first).")

        # # #

        self.specials += MultiReg(Cat(*[getattr(buttons, name) for name in BUTTONS], menu),
            self.status.status)

# Tone Generator -----------------------------------------------------------------------------------

class ToneGenerator(LiteXModule):
    """
    Square wave tone generator (audio sample output in `cd`, `clk_freq`): frequency = clk_freq /
    (2 x period), volume: square wave amplitude (0: silent).
    """
    def __init__(self, clk_freq, cd="hclk"):
        self.period = CSRStorage(20, description=f"Half period ({clk_freq/1e6:.3f}MHz cycles), 0: silent.")
        self.volume = CSRStorage(15, description="Amplitude.")
        self.sample = Signal(16) # Signed.

        # # #

        period = Signal(20)
        volume = Signal(15)
        count  = Signal(20)
        level  = Signal()
        self.specials += [
            MultiReg(self.period.storage, period, cd),
            MultiReg(self.volume.storage, volume, cd),
        ]
        sync = getattr(self.sync, cd)
        sync += [
            If(period == 0,
                count.eq(0),
                level.eq(0),
            ).Elif(count >= (period - 1),
                count.eq(0),
                level.eq(~level),
            ).Else(
                count.eq(count + 1),
            ),
            self.sample.eq(Mux(period == 0, 0, Mux(level, volume, -volume))),
        ]

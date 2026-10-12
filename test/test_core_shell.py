#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litex.gen.sim import run_simulation

from chromatix.gateware.core_shell import CoreTestPattern, COLOR_BARS, rgb555

# CoreTestPattern ----------------------------------------------------------------------------------

def test_core_test_pattern():
    """The bring-up source presents the Game Boy LCD interface: 160 clkena per visible line, vsync on
    line 0 only, display always on, and the expected color-bar/border/marker pixels."""
    dut   = CoreTestPattern()
    white = rgb555(31, 31, 31)

    # Collected over a few visible lines (enough to cover the border top line and middle lines).
    per_line_clkena = {}
    vsync_at_clkena = {}
    pixels          = {} # (line, x) -> gb_data at clkena.

    def gen():
        for _ in range(12000): # ~6 lines (456 dots x 4 hClk per line).
            assert (yield dut.gb_on) == 1, "display not on"
            if (yield dut.gb_clkena):
                line = (yield dut.timing.line)
                x    = (yield dut.timing.x)
                per_line_clkena[line] = per_line_clkena.get(line, 0) + 1
                vsync_at_clkena.setdefault(line, (yield dut.gb_vsync))
                pixels[(line, x)] = (yield dut.gb_data)
            yield

        # Each visible line produces exactly 160 valid pixels.
        for line in range(5):
            assert per_line_clkena.get(line) == 160, (line, per_line_clkena.get(line))

        # vsync is asserted on line 0 only (the pipeline/panel re-align on its rising edge).
        assert vsync_at_clkena[0] == 1
        for line in range(1, 5):
            assert vsync_at_clkena[line] == 0, line

        # Pattern on a middle line (no top/bottom border): 8 color bars of 20 px, white 1 px borders
        # at x=0/159, and the moving marker (at x=1 on the first frame) in white.
        line = 2
        assert pixels[(line, 0)]   == white            # Left border.
        assert pixels[(line, 159)] == white            # Right border.
        assert pixels[(line, 1)]   == white            # Marker (frame 1).
        for x, expected in [(10, 0), (30, 1), (50, 2), (70, 3), (90, 4), (110, 5), (130, 6), (150, 7)]:
            assert pixels[(line, x)] == COLOR_BARS[expected], (x, expected, pixels[(line, x)])

        # The top line (y=0) is all border (white).
        for x in [0, 40, 80, 120, 159]:
            assert pixels[(0, x)] == white, x

    run_simulation(dut, {"hclk": gen()}, clocks={"hclk": 10})

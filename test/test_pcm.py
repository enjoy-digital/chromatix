#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""PCMAudio: CPU samples FIFO played at the sample rate, last sample held on underrun."""

from migen import *

from litex.gen.sim import run_simulation

from chromatix.gateware.demo import PCMAudio

def test_pcm_rate_and_underrun():
    clk_freq    = 1000
    sample_rate = 100 # 1 sample every 10 cycles.
    dut     = PCMAudio(clk_freq=clk_freq, sample_rate=sample_rate, depth=16)
    samples = [(i*1000 - 3500) & 0xffff for i in range(8)]
    out     = []

    def gen():
        for i, s in enumerate(samples):
            yield from dut.data.write(((~s & 0xffff) << 16) | s)
        assert (yield dut.level.status) > 0
        changes, last = [], None
        for cycle in range(200):
            left  = (yield dut.left)
            right = (yield dut.right)
            if left != last:
                changes.append(cycle)
                out.append((left, right))
                last = left
            yield
        # Rate: one new sample every 10 cycles, then the last one is held.
        deltas = [b - a for a, b in zip(changes[1:], changes[2:])]
        assert all(d == 10 for d in deltas)
        assert (yield dut.level.status) == 0

    run_simulation(dut, gen())
    # All samples played in order (after the initial silence), right = ~left.
    assert out[0] == (0, 0)
    assert [l for l, r in out[1:]] == samples
    assert all(r == (~l & 0xffff) for l, r in out[1:])

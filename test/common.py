#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litex.gen.sim import run_simulation

# Simulation Helpers -------------------------------------------------------------------------------

class ClockDomainsWrapper(Module):
    """Declare the clock domains a DUT drives directly (ex: gclk/hclk/pclk) for simulation."""
    def __init__(self, dut, domains):
        self.submodules.dut = dut
        for domain in domains:
            setattr(self.clock_domains, f"cd_{domain}", ClockDomain(domain))

def run_domain_simulation(dut, generators, clocks):
    """Run generators (dict: domain -> generator(s)) with one clock per domain (dict: domain -> period)."""
    run_simulation(ClockDomainsWrapper(dut, clocks.keys()), generators, clocks=clocks)

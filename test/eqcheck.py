#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
Formal equivalence check (bounded, from reset) between an original Verilog module and its LiteX/Migen
port, with Yosys (miter + SAT bounded model check).

Both designs start with all registers at zero (-set-init-zero) and must produce identical outputs
for every input sequence over `depth` cycles. Port names/widths must match (single clock designs,
or multi-clock designs with clocks driven as regular inputs).
"""

import os
import subprocess
import tempfile

from migen import *

from litex.build.generic_platform import GenericPlatform

def export_migen(module, ios, name, filename):
    """Export a Migen module as a standalone Verilog module (generic, no vendor overrides)."""
    from litex.gen.fhdl.verilog import convert
    convert(module, ios=ios, name=name).write(filename)

def eqcheck(gold_files, gold_top, gate_files, gate_top, depth=20, gold_defines=[], gold_params={},
    gold_includes=[], ignore_outputs=[], reset=None, yosys="yosys", workdir=None, verbose=False):
    """
    Return (equivalent, log).

    reset: optional (input_name, active_value): reset asserted on the first cycle only (then
    deasserted), so that power-on values of reset registers and asynchronous vs synchronous reset
    implementations don't matter.
    """
    workdir = workdir or tempfile.mkdtemp(prefix="eqcheck_")
    defines = " ".join([f"-D{d}" for d in gold_defines] + [f"-I{i}" for i in gold_includes])
    params  = " ".join(f"-set {k} {v}" for k, v in gold_params.items())
    script  = []
    # Gold.
    script += [f"read_verilog -sv {defines} {f}" for f in gold_files]
    if params:
        script += [f"chparam {params} {gold_top}"]
    script += [f"hierarchy -top {gold_top}", "proc", "flatten", "opt_clean", f"rename {gold_top} gold", "design -stash gold"]
    # Gate.
    script += [f"read_verilog -sv {f}" for f in gate_files]
    script += [f"hierarchy -top {gate_top}", "proc", "flatten", "opt_clean", f"rename {gate_top} gate", "design -stash gate"]
    # Miter.
    script += [
        "design -copy-from gold -as gold gold",
        "design -copy-from gate -as gate gate",
    ]
    for o in ignore_outputs:
        script += [f"delete -port gold/{o}", f"delete -port gate/{o}"]
    script += [
        "miter -equiv -flatten -make_outputs -ignore_gold_x gold gate miter",
        "hierarchy -top miter",
        "memory_map", "async2sync", "dffunmap", "opt -fast",
        f"sat -verify -seq {depth} -set-init-zero -enable_undef -set-def-inputs -prove trigger 0 -show-inputs -show-outputs"
        + (f" -set-at 1 in_{reset[0]} {reset[1]} -prove-skip 1" +
           "".join(f" -set-at {t} in_{reset[0]} {1 - reset[1]}" for t in range(2, depth + 1))
           if reset is not None else "") + " miter",
    ]
    ys = os.path.join(workdir, "eqcheck.ys")
    with open(ys, "w") as f:
        f.write("\n".join(script) + "\n")
    r = subprocess.run([yosys, "-q", "-s", ys], capture_output=True, text=True, cwd=workdir)
    log = r.stdout + r.stderr
    if verbose:
        print(log)
    return r.returncode == 0, log

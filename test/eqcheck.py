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
import re
import tempfile
import subprocess

from litex.gen.fhdl.verilog import convert

# Helpers ------------------------------------------------------------------------------------------

def export_migen(module, ios, name, filename):
    """Export a Migen module as a standalone Verilog module (generic, no vendor overrides)."""
    convert(module, ios=ios, name=name).write(filename)

def _read(filename):
    with open(filename, encoding="utf-8") as f:
        return f.read()

# Bounded equivalence (SAT) ------------------------------------------------------------------------

def eqcheck(gold_files, gold_top, gate_files, gate_top, depth=20, gold_defines=[], gold_params={},
    gold_includes=[], ignore_outputs=[], reset=None, gold_script=[], gate_script=[], gate_extra_files=[],
    yosys="yosys", workdir=None, verbose=False):
    """
    Return (equivalent, log).

    reset: optional (input_name, active_value): reset asserted on the first cycle only (then
    deasserted), so that power-on values of reset registers and asynchronous vs synchronous reset
    implementations don't matter.

    gold_script/gate_script: extra Yosys commands run on the elaborated designs before flattening
    (ex: "expose -evert -sep __ top/instance" to cut a sub-instance out and check the logic around it).
    gate_extra_files: extra Verilog files read with the gate (ex: blackbox primitive stubs).
    """
    workdir = workdir or tempfile.mkdtemp(prefix="eqcheck_")
    defines = " ".join([f"-D{d}" for d in gold_defines] + [f"-I{i}" for i in gold_includes])
    params  = " ".join(f"-set {k} {v}" for k, v in gold_params.items())
    script  = []
    # Gold.
    script += [f"read_verilog -sv {defines} {f}" for f in gold_files]
    if params:
        script += [f"chparam {params} {gold_top}"]
    script += [f"hierarchy -top {gold_top}", "proc", *gold_script, f"hierarchy -top {gold_top}", "flatten", "opt_clean", f"rename {gold_top} gold", "design -stash gold"]
    # Gate.
    script += [f"read_verilog -sv {f}" for f in gate_files + gate_extra_files]
    script += [f"hierarchy -top {gate_top}", "proc", *gate_script, f"hierarchy -top {gate_top}", "flatten", "opt_clean", f"rename {gate_top} gate", "design -stash gate"]
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

# Unbounded equivalence (PDR) ----------------------------------------------------------------------

def _verilog_ports(filename, top):
    """(direction, width, name) of a (Migen exported) Verilog module's ports."""
    src = _read(filename)
    m   = re.search(rf"module\s+{top}\s*\((.*?)\);", src, re.S)
    ports = []
    for d, rng, name in re.findall(r"(input|output)\s+(?:wire|reg)?\s*(?:signed\s+)?(\[\s*\d+\s*:\s*\d+\s*\])?\s*(\w+)", m.group(1)):
        width = 1
        if rng:
            hi, lo = [int(x) for x in rng.strip("[]").split(":")]
            width = hi - lo + 1
        ports.append((d, width, name))
    return ports

def pdr_eqcheck(gold_files, gold_top, gate_file, gate_top, gold_defines=[], gold_includes=[],
    tie={}, gold_script=[], gate_script=[], gate_extra_files=[], workdir=None, timeout=900):
    """
    Unbounded equivalence (Yosys miter -> AIGER -> ABC PDR): both designs start from all-zero
    registers (Migen initial values are stripped), `tie` inputs are held constant (ex: reset low).
    Returns (proved, log).
    """
    workdir = workdir or tempfile.mkdtemp(prefix="pdr_")
    ports   = _verilog_ports(gate_file, gate_top)
    # Gate: strip register initial values.
    src = _read(gate_file)
    src = re.sub(r"^(\s*reg\s+(?:signed\s+)?(?:\[[^\]]+\]\s*)?\w+)\s*=\s*[^;]+;", r"\1;", src, flags=re.M)
    gate_noinit = os.path.join(workdir, "gate_noinit.v")
    with open(gate_noinit, "w") as f:
        f.write(src)
    # Wrappers with tied inputs.
    def wrapper(name, inst_of):
        decl = ", ".join(f"{d} [{w-1}:0] {n}" for d, w, n in ports if n not in tie)
        conn = ", ".join(f".{n}({tie[n]})" if n in tie else f".{n}({n})" for d, w, n in ports)
        return f"module {name}({decl});\n  {inst_of} u({conn});\nendmodule\n"
    wrap = os.path.join(workdir, "wrappers.v")
    with open(wrap, "w") as f:
        f.write(wrapper("gold_w", gold_top) + wrapper("gate_w", gate_top))
    defines = " ".join([f"-D{d}" for d in gold_defines] + [f"-I{i}" for i in gold_includes])
    script  = [f"read_verilog -sv {defines} {f}" for f in gold_files]
    script += [f"hierarchy -top {gold_top}", "proc", *gold_script]
    script += [f"read_verilog -sv {wrap}", "hierarchy -top gold_w", "proc", "flatten", "opt_clean", "rename gold_w gold", "design -stash gold"]
    script += [f"read_verilog -sv {f}" for f in [gate_noinit] + gate_extra_files]
    script += [f"hierarchy -top {gate_top}", "proc", *gate_script]
    script += [f"read_verilog -sv {wrap}", "hierarchy -top gate_w", "proc", "flatten", "opt_clean", "rename gate_w gate", "design -stash gate"]
    script += [
        "design -copy-from gold -as gold gold",
        "design -copy-from gate -as gate gate",
        "miter -equiv -flatten -make_assert gold gate miter",
        "hierarchy -top miter",
        "memory_map", "opt -fast", "async2sync", "dffunmap",
        "setundef -zero -init",
        "opt -fast -keepdc", "dffunmap", "techmap", "dffunmap", "abc -g AND", "opt_clean",
        "write_aiger -zinit miter.aig",
    ]
    with open(os.path.join(workdir, "pdr.ys"), "w") as f:
        f.write("\n".join(script) + "\n")
    r = subprocess.run(["yosys", "-q", "-s", "pdr.ys"], capture_output=True, text=True, cwd=workdir)
    if r.returncode != 0:
        return False, r.stdout + r.stderr
    r = subprocess.run(["yosys-abc", "-c", "read_aiger miter.aig; fold; strash; pdr"],
        capture_output=True, text=True, cwd=workdir, timeout=timeout)
    log = r.stdout + r.stderr
    return ("Property proved" in log), log

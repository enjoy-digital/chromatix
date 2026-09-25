#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

import os
import re
import shutil
import subprocess
import tempfile

import pytest

from migen import *

from litex.gen import *
from litex.gen.sim import run_simulation

from chromatix.gateware.usb_desc import USBDescriptors

# Simulation ---------------------------------------------------------------------------------------

def read_rom(dut, addr, length):
    data = []
    for i in range(length):
        yield dut.descrom_raddr.eq(addr + i)
        yield
        data.append((yield dut.descrom_rdat))
    return data

def test_usb_desc_sim():
    """Device descriptor, VID/PID and product string follow playerNum."""
    dut     = USBDescriptors()
    layout  = dut.layout
    results = {}

    def gen():
        # Reset, playerNum = 0: idProduct low byte = 0x00, product string = "... Player XX".
        yield dut.reset.eq(1)
        yield
        yield dut.reset.eq(0)
        yield
        results["dev0"]     = yield from read_rom(dut, layout.dev_addr, layout.dev_len)
        results["product0"] = yield from read_rom(dut, layout.strproduct_addr, layout.strproduct_len)
        # playerNum = 1.
        yield dut.player_num.eq(1)
        yield
        yield
        results["dev1"]     = yield from read_rom(dut, layout.dev_addr, layout.dev_len)
        results["product1"] = yield from read_rom(dut, layout.strproduct_addr, layout.strproduct_len)
        results["cfg"]      = yield from read_rom(dut, layout.fscfg_addr, 4)
        results["lang"]     = yield from read_rom(dut, layout.strlang_addr, 4)
        results["serial"]   = yield from read_rom(dut, layout.strserial_addr, layout.strserial_len)
        # playerNum = 0x2c: hex digits.
        yield dut.player_num.eq(0x2c)
        yield
        yield
        results["product2c"] = yield from read_rom(dut, layout.strproduct_addr, layout.strproduct_len)
        results["dev_len"]   = yield dut.dev_len
        results["cfg_len"]   = yield dut.fscfg_len

    def string(data):
        assert data[0] == len(data) and data[1] == 0x03
        return bytes(data[2::2]).decode(), data[3::2]

    run_simulation(dut, gen())

    # Device descriptor.
    dev = results["dev1"]
    assert dev[:8]    == [0x12, 0x01, 0x00, 0x02, 0xEF, 0x02, 0x01, 0x40]
    assert dev[8:10]  == [0x4E, 0x37]  # idVendor  = 0x374E.
    assert dev[10:12] == [0x01, 0x01]  # idProduct = 0x0100 | playerNum.
    assert dev[12:]   == [0x00, 0x02, 0x01, 0x02, 0x03, 0x01]
    assert results["dev0"][10:12] == [0x00, 0x01]
    assert results["dev_len"] == 18

    # Configuration descriptor header.
    assert results["cfg"] == [0x09, 0x02, 364 & 0xff, 364 >> 8]
    assert results["cfg_len"] == 364

    # Strings.
    assert results["lang"] == [0x04, 0x03, 0x09, 0x04]
    assert string(results["product0"])[0]  == "Chromatic - Player XX"
    name, high = string(results["product1"])
    assert name == "Chromatic - Player 01"
    assert set(high) == {0}
    assert string(results["product2c"])[0] == "Chromatic - Player 2C"
    assert string(results["serial"])[0]    == "012345678"

def parse_descriptors(data):
    """Split a descriptor set in (bDescriptorType, descriptor) using bLength."""
    out = []
    while data:
        length = data[0]
        assert 2 <= length <= len(data)
        out.append((data[1], data[:length]))
        data = data[length:]
    return out

def test_usb_desc_configuration():
    """Configuration descriptor read from the ROM is well formed: descriptor lengths add up to
    wTotalLength, bNumInterfaces matches the interfaces, each interface/alt setting declares its
    endpoints, IADs group the UVC/UAC/CDC functions; qualifier and other-speed are consistent."""
    dut    = USBDescriptors()
    layout = dut.layout
    res    = {}

    def gen():
        yield dut.reset.eq(1)
        yield
        yield dut.reset.eq(0)
        yield
        res["cfg"]   = yield from read_rom(dut, layout.fscfg_addr, layout.fscfg_len)
        res["qual"]  = yield from read_rom(dut, layout.qual_addr, layout.qual_len)
        res["other"] = yield from read_rom(dut, layout.oscfg_addr, 1)
        res["have_strings"] = (yield dut.have_strings)
        res["hs"] = [(yield dut.hscfg_addr), (yield dut.hscfg_len)]
        # Out of range (past the ROM end, within the decoded address space): 0.
        res["oor"] = yield from read_rom(dut, len(layout.rom), 1)

    run_simulation(dut, gen())
    cfg   = res["cfg"]
    descs = parse_descriptors(cfg)
    assert sum(len(d) for _, d in descs) == len(cfg) == cfg[2] | (cfg[3] << 8)
    assert descs[0][0] == 0x02 and descs[0][1][4] == 6 # 6 interfaces.
    interfaces = [d for t, d in descs if t == 0x04]
    assert sorted({d[2] for d in interfaces}) == list(range(6))
    # Endpoints following each interface descriptor match its bNumEndpoints.
    endpoints = {}
    current   = None
    for t, d in descs[1:]:
        if t == 0x04:
            current = (d[2], d[3])
            endpoints[current] = []
        elif t == 0x05:
            endpoints[current].append((d[2], d[3] & 0x3, d[4] | (d[5] << 8)))
    for d in interfaces:
        assert len(endpoints[(d[2], d[3])]) == d[4]
    assert endpoints[(0, 0)] == [(0x81, 0x3, 64)]   # UVC interrupt.
    assert endpoints[(1, 0)] == []                  # UVC zero-bandwidth.
    assert endpoints[(1, 1)] == [(0x82, 0x1, 1024)] # UVC isochronous.
    assert endpoints[(2, 0)] == [(0x84, 0x3, 8)]    # CDC notification.
    assert endpoints[(3, 0)] == [(0x83, 0x2, 512), (0x03, 0x2, 512)] # CDC bulk.
    assert endpoints[(5, 1)] == [(0x85, 0x1, 24)]   # UAC isochronous.
    # IADs: (bFirstInterface, bInterfaceCount, bFunctionClass).
    iads = [(d[2], d[3], d[4]) for t, d in descs if t == 0x0b]
    assert iads == [(0, 2, 0x0e), (4, 2, 0x01), (2, 2, 0x02)]
    # Qualifier (bcdUSB 2.00, 64-byte EP0) and other-speed hack.
    assert res["qual"][:8] == [0x0a, 0x06, 0x00, 0x02, 0x01, 0x00, 0x00, 0x40]
    assert res["other"] == [0x07]
    assert res["have_strings"] == 1
    assert res["hs"] == [layout.fscfg_addr, layout.fscfg_len]
    assert res["oor"] == [0]

def test_usb_desc_player_num_reset():
    """While reset is asserted the dynamic bytes read their reset values (idProduct low byte 0x00,
    product string "XX", as the asynchronous reset of the original), then follow playerNum again
    (it differs from the reset value 0)."""
    dut    = USBDescriptors()
    layout = dut.layout
    pid    = layout.pid_lo_addr
    hi, lo = layout.player_hi_addr, layout.player_lo_addr
    res    = {}

    def read(addr):
        yield dut.descrom_raddr.eq(addr)
        yield
        return (yield dut.descrom_rdat)

    def gen():
        yield dut.player_num.eq(0x5a)
        for _ in range(3):
            yield
        res["set"]   = [(yield from read(pid)), (yield from read(hi)), (yield from read(lo))]
        yield dut.reset.eq(1)
        yield
        res["reset"] = [(yield from read(pid)), (yield from read(hi)), (yield from read(lo))]
        yield dut.reset.eq(0)
        for _ in range(3):
            yield
        res["after"] = [(yield from read(pid)), (yield from read(hi)), (yield from read(lo))]

    run_simulation(dut, gen())
    assert res["set"]   == [0x5a, ord("5"), ord("A")]
    assert res["reset"] == [0x00, ord("X"), ord("X")]
    assert res["after"] == [0x5a, ord("5"), ord("A")]

# Equivalence --------------------------------------------------------------------------------------

VERILOG_DIR   = "chromatix/verilog/usb/usb_video"
VERILOG_FILES = ["usb_descriptor_video.v", "usb_defs.v", "uvc_defs.v", "uac_defs.v", "uart_defs.v"]
GOLD_PARAMS   = {
    "VENDORID"    : "16'h374E",
    "PRODUCTID"   : "16'h013f",
    "VERSIONBCD"  : "16'h0200",
    "HSSUPPORT"   : "1",
    "SELFPOWERED" : "0",
}

DESC_OUTPUTS = ["dev_addr", "dev_len", "qual_addr", "qual_len", "fscfg_addr", "fscfg_len",
    "hscfg_addr", "hscfg_len", "oscfg_addr", "strlang_addr", "strvendor_addr", "strvendor_len",
    "strproduct_addr", "strproduct_len", "strserial_addr", "strserial_len"]

# Gold wrapper: forces RESET during the first cycle (descriptor ROM is only initialized on reset).
GOLD_WRAPPER = """
module usb_desc_gold #(
    parameter VENDORID    = 16'h0403,
    parameter PRODUCTID   = 16'h6010,
    parameter VERSIONBCD  = 16'h0100,
    parameter HSSUPPORT   = 0,
    parameter SELFPOWERED = 0
) (
    input         CLK,
    input         RESET,
    input  [7:0]  playerNum,
    input  [63:0] serial,
    input  [15:0] i_descrom_raddr,
    output [7:0]  o_descrom_rdat,
{outputs}
    output        o_descrom_have_strings
);
    reg rst_done = 1'b0;
    always @(posedge CLK) rst_done <= 1'b1;
    usb_desc #(
        .VENDORID    (VENDORID),
        .PRODUCTID   (PRODUCTID),
        .VERSIONBCD  (VERSIONBCD),
        .HSSUPPORT   (HSSUPPORT),
        .SELFPOWERED (SELFPOWERED)
    ) usb_desc (
        .CLK             (CLK),
        .RESET           (RESET | ~rst_done),
        .playerNum       (playerNum),
        .serial          (serial),
        .i_descrom_raddr (i_descrom_raddr),
        .o_descrom_rdat  (o_descrom_rdat),
{connections}
        .o_descrom_have_strings (o_descrom_have_strings)
    );
endmodule
""".format(
    outputs     = "\n".join(f"    output [15:0] o_desc_{n}," for n in DESC_OUTPUTS),
    connections = "\n".join(f"        .o_desc_{n} (o_desc_{n})," for n in DESC_OUTPUTS),
)

class USBDescriptorsWrapper(LiteXModule):
    """USBDescriptors with usb_desc port names and the same first-cycle reset as the gold wrapper."""
    def __init__(self):
        self.CLK                    = Signal()
        self.RESET                  = Signal()
        self.playerNum              = Signal(8)
        self.serial                 = Signal(64) # Unused (as in the original).
        self.i_descrom_raddr        = Signal(16)
        self.o_descrom_rdat         = Signal(8)
        self.o_descrom_have_strings = Signal()
        for name in DESC_OUTPUTS:
            setattr(self, f"o_desc_{name}", Signal(16, name=f"o_desc_{name}"))

        # # #

        self.cd_sys = ClockDomain(reset_less=True)
        self.comb += self.cd_sys.clk.eq(self.CLK)

        rst_done = Signal()
        self.sync += rst_done.eq(1)

        self.desc = desc = USBDescriptors()
        self.comb += [
            desc.reset.eq(self.RESET | ~rst_done),
            desc.player_num.eq(self.playerNum),
            desc.descrom_raddr.eq(self.i_descrom_raddr),
            self.o_descrom_rdat.eq(desc.descrom_rdat),
            self.o_descrom_have_strings.eq(desc.have_strings),
        ]
        for name in DESC_OUTPUTS:
            self.comb += getattr(self, f"o_desc_{name}").eq(getattr(desc, name))

    def get_ios(self):
        ios = {self.CLK, self.RESET, self.playerNum, self.serial, self.i_descrom_raddr,
            self.o_descrom_rdat, self.o_descrom_have_strings}
        ios |= {getattr(self, f"o_desc_{name}") for name in DESC_OUTPUTS}
        return ios

def fetch_gold(workdir):
    """Fetch the original sources from git history (so it keeps working once removed from tree)."""
    root = os.path.join(os.path.dirname(__file__), "..")
    for f in VERILOG_FILES:
        r = subprocess.run(["git", "show", f"ae5fe05:{VERILOG_DIR}/{f}"], cwd=root, capture_output=True)
        if r.returncode != 0:
            return None
        with open(os.path.join(workdir, f), "wb") as fd:
            fd.write(r.stdout)
    # Yosys does not support part-selects of concatenations ({`MACRO}[h:l], accepted by Gowin):
    # select on 32-bit localparams instead (all macros fit in 32-bit, so values are unchanged).
    top = os.path.join(workdir, VERILOG_FILES[0])
    src = open(top).read()
    macros = sorted(set(re.findall(r"\{`(\w+)\}\[", src)))
    src = re.sub(r"\{`(\w+)\}\[", r"__\1[", src)
    decls = "".join(f"    localparam [31:0] __{m} = `{m};\n" for m in macros)
    src = src.replace("\n);\n", "\n);\n" + decls, 1)
    with open(top, "w") as fd:
        fd.write(src)
    wrapper = os.path.join(workdir, "usb_desc_gold.v")
    with open(wrapper, "w") as fd:
        fd.write(GOLD_WRAPPER)
    return [top, wrapper]

def run_eqcheck(depth, workdir=None, verbose=False, gate=None):
    """Run eqcheck, then re-run its SAT proof with x-modelling.

    miter -ignore_gold_x masks each output bit with ($eqx gold_bit, 1'bx), which SAT without
    -enable_undef evaluates as (gold_bit == 0): only bits where gold is 1 would be compared. The
    proof is re-run with -enable_undef -set-def-inputs so that every defined gold bit is checked
    (gold is x only for out-of-range ROM reads).
    """
    from test.eqcheck import export_migen, eqcheck
    workdir = workdir or tempfile.mkdtemp(prefix="usb_desc_eq_")
    gold_files = fetch_gold(workdir)
    if gold_files is None:
        return None, "original sources unavailable"
    gate      = gate or USBDescriptorsWrapper()
    gate_file = os.path.join(workdir, "usb_desc_gate.v")
    export_migen(gate, gate.get_ios(), "usb_desc_gate", gate_file)
    ok, log = eqcheck(
        gold_files   = gold_files,
        gold_top     = "usb_desc_gold",
        gate_files   = [gate_file],
        gate_top     = "usb_desc_gate",
        depth        = depth,
        gold_defines = ["HSSUPPORT"],
        gold_params  = GOLD_PARAMS,
        workdir      = workdir,
        verbose      = verbose,
    )
    if not ok:
        return ok, log
    ys = os.path.join(workdir, "eqcheck.ys")
    script = open(ys).read().replace("sat -verify ", "sat -verify -enable_undef -set-def-inputs ")
    ys = os.path.join(workdir, "eqcheck_undef.ys")
    with open(ys, "w") as f:
        f.write(script)
    r = subprocess.run(["yosys", "-q", "-s", ys], capture_output=True, text=True, cwd=workdir)
    return r.returncode == 0, log + r.stdout + r.stderr

# Depth >= 5 covers all reachable states: after the forced reset (cycle 0), the only state is
# playerNum_prev (+ whether the product string is still "XX", only possible with prev = 0), and any
# value is reached in 2 cycles (reset -> p -> 0 for prev = 0 with digits "00"), + transitions.
EQCHECK_DEPTH = 8

@pytest.mark.skipif(shutil.which("yosys") is None, reason="Yosys not available")
def test_usb_desc_eqcheck():
    """Formal equivalence (bounded, from reset) with the original usb_desc."""
    ok, log = run_eqcheck(depth=EQCHECK_DEPTH)
    if ok is None:
        pytest.skip(log)
    assert ok, log[-4000:]

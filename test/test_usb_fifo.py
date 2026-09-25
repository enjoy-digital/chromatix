#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

import os
import re
import random
import shutil
import subprocess
import tempfile

import pytest

from migen import *

from litex.gen.sim import run_simulation

from chromatix.gateware.usb_fifo import USBEndpointFIFO
from test.eqcheck import export_migen, eqcheck

# Helpers ------------------------------------------------------------------------------------------

ROOT         = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
GOLD_REV     = "ae5fe05" # Last revision with the original Verilog sources in the tree.
GOLD_DIR     = "chromatix/verilog/usb/sync_fifo"
GOLD_SOURCES = ["usb_fifo.v", "sync_rx_pkt_fifo.v", "sync_tx_pkt_fifo.v"]

def fetch_gold_sources(workdir, asize=12, afull=2048, cross_asize=6, cross_afull=32):
    """Fetch the original usb_fifo sources from git history (optionally with reduced buffers)."""
    files = []
    for name in GOLD_SOURCES:
        r = subprocess.run(["git", "show", f"{GOLD_REV}:{GOLD_DIR}/{name}"], capture_output=True,
            text=True, cwd=ROOT)
        if r.returncode != 0:
            pytest.skip("original usb_fifo sources not available (git history)")
        src = r.stdout
        if name == "usb_fifo.v" and asize != 12:
            for define, old, new in [
                ("EP3_IN_BUF_ASIZE",  "4'd12",    asize),
                ("EP3_OUT_BUF_ASIZE", "4'd12",    asize),
                ("EP3_OUT_BUF_AFULL", "13'd2048", afull)]:
                src = re.sub(rf"`define\s+{define}\s+{old}", f"`define {define} {new}", src)
            # ep3_txlen is P_ASIZE+1 bits: zero-extend instead of an out of range [11:0] select.
            src = src.replace("ep3_txlen[11:0]", "ep3_txlen")
        if name == "usb_fifo.v" and (cross_asize, cross_afull) != (6, 32):
            src = src.replace(".ASIZE (6  )", f".ASIZE ({cross_asize})")
            src = src.replace(".AFULL (32 )", f".AFULL ({cross_afull})")
        filename = os.path.join(workdir, name)
        with open(filename, "w") as f:
            f.write(src)
        files.append(filename)
    return files

# Gold wrapper: usb_fifo instantiated exactly as in usbuvcuart_top.v (EP3 only, single pClk).
GOLD_WRAPPER = """
module usb_fifo_gold #(parameter TX_MAX = 64) (
    input         i_clk,
    input         i_reset,
    input  [3:0]  i_usb_endpt,
    input         i_usb_rxact,
    input         i_usb_rxval,
    input         i_usb_rxpktval,
    input  [7:0]  i_usb_rxdat,
    output        o_usb_rxrdy,
    input         i_usb_txact,
    input         i_usb_txpop,
    input         i_usb_txpktfin,
    output        o_usb_txcork,
    output [11:0] o_usb_txlen,
    output [7:0]  o_usb_txdat,
    input         i_ep3_tx_dval,
    input  [7:0]  i_ep3_tx_data,
    input         i_ep3_rx_rdy,
    output        o_ep3_rx_dval,
    output [7:0]  o_ep3_rx_data
);
    usb_fifo usb_fifo (
         .i_clk         (i_clk         )
        ,.i_reset       (i_reset       )
        ,.i_usb_endpt   (i_usb_endpt   )
        ,.i_usb_rxact   (i_usb_rxact   )
        ,.i_usb_rxval   (i_usb_rxval   )
        ,.i_usb_rxpktval(i_usb_rxpktval)
        ,.i_usb_rxdat   (i_usb_rxdat   )
        ,.o_usb_rxrdy   (o_usb_rxrdy   )
        ,.i_usb_txact   (i_usb_txact   )
        ,.i_usb_txpop   (i_usb_txpop   )
        ,.i_usb_txpktfin(i_usb_txpktfin)
        ,.o_usb_txcork  (o_usb_txcork  )
        ,.o_usb_txlen   (o_usb_txlen   )
        ,.o_usb_txdat   (o_usb_txdat   )
        ,.i_ep3_tx_clk  (i_clk         )
        ,.i_ep3_tx_max  (TX_MAX[11:0]  )
        ,.i_ep3_tx_dval (i_ep3_tx_dval )
        ,.i_ep3_tx_data (i_ep3_tx_data )
        ,.i_ep3_rx_clk  (i_clk         )
        ,.i_ep3_rx_rdy  (i_ep3_rx_rdy  )
        ,.o_ep3_rx_dval (o_ep3_rx_dval )
        ,.o_ep3_rx_data (o_ep3_rx_data )
    );
endmodule
"""

class GateWrapper(Module):
    """USBEndpointFIFO with the original usb_fifo port names (used ports only)."""
    def __init__(self, **kwargs):
        self.submodules.fifo = fifo = USBEndpointFIFO(**kwargs)
        self.clock_domains.cd_sys = ClockDomain(reset_less=True)
        mapping = [
            ("i_clk",          self.cd_sys.clk),
            ("i_reset",        fifo.reset),
            ("i_usb_endpt",    fifo.endpt),
            ("i_usb_rxact",    fifo.rxact),
            ("i_usb_rxval",    fifo.rxval),
            ("i_usb_rxpktval", fifo.rxpktval),
            ("i_usb_rxdat",    fifo.rxdat),
            ("o_usb_rxrdy",    fifo.rxrdy),
            ("i_usb_txact",    fifo.txact),
            ("i_usb_txpop",    fifo.txpop),
            ("i_usb_txpktfin", fifo.txpktfin),
            ("o_usb_txcork",   fifo.txcork),
            ("o_usb_txlen",    fifo.txlen),
            ("o_usb_txdat",    fifo.txdat),
            ("i_ep3_tx_dval",  fifo.tx_valid),
            ("i_ep3_tx_data",  fifo.tx_data),
            ("i_ep3_rx_rdy",   fifo.rx_ready),
            ("o_ep3_rx_dval",  fifo.rx_valid),
            ("o_ep3_rx_data",  fifo.rx_data),
        ]
        self.ios = set()
        for name, sig in mapping:
            port = Signal(len(sig), name_override=name)
            if name.startswith("o_"):
                self.comb += port.eq(sig)
            else:
                self.comb += sig.eq(port)
            self.ios.add(port)

def export_gate(workdir, zero_init=False, **kwargs):
    gate     = GateWrapper(**kwargs)
    filename = os.path.join(workdir, "usb_fifo_gate.v")
    export_migen(gate, gate.ios, "usb_fifo_gate", filename)
    if zero_init:
        # Formal check starts from the all-zero state on both sides (gold registers have no init).
        with open(filename) as f:
            src = f.read()
        src = re.sub(r"^(\s*reg\s+(\[[^\]]*\]\s*)?\w+)\s*=\s*[^;]+;", r"\1;", src, flags=re.M)
        with open(filename, "w") as f:
            f.write(src)
    return filename

def export_gold(workdir, **kwargs):
    files    = fetch_gold_sources(workdir, **kwargs)
    filename = os.path.join(workdir, "usb_fifo_gold.v")
    with open(filename, "w") as f:
        f.write(GOLD_WRAPPER)
    return files + [filename]

# Functional Simulation ----------------------------------------------------------------------------

def test_usb_fifo_loopback():
    """EP3 OUT: validated packets reach rx_valid/rx_data, bad packets dropped. EP3 IN: bytes come
    out in txlen-sized packets, a NAKed packet is resent, ACKed ones are released."""
    dut        = USBEndpointFIFO()
    rx_packets = [(list(range(1, 11)), True), ([0xaa]*7, False), (list(range(20, 26)), True)]
    tx_data    = [random.randrange(256) for _ in range(100)]
    rx_out     = []
    tx_out     = []
    tx_packets = []

    def rx_user():
        # Collect OUT bytes, not ready for the first cycles (bytes held in the FIFOs).
        # Note: as the original, rx_ready is sampled one cycle ahead of rx_valid (a rx_ready falling
        # edge while a read is in flight produces a duplicated rx_valid), so it is kept stable here.
        for i in range(3000):
            yield dut.rx_ready.eq(i >= 300)
            if (yield dut.rx_valid):
                rx_out.append((yield dut.rx_data))
            yield

    def controller():
        yield dut.reset.eq(1)
        yield
        yield
        yield dut.reset.eq(0)
        yield dut.endpt.eq(3)
        for _ in range(4):
            yield
        # OUT packets (host -> device).
        for data, crc_ok in rx_packets:
            assert (yield dut.rxrdy)
            yield dut.rxact.eq(1)
            for _ in range(6):
                yield
            for d in data:
                yield dut.rxdat.eq(d)
                yield dut.rxval.eq(1)
                yield
                yield dut.rxval.eq(0)
                yield
            yield dut.rxpktval.eq(crc_ok)
            yield
            yield dut.rxpktval.eq(0)
            yield dut.rxact.eq(0)
            for _ in range(8):
                yield
        # IN data (device -> host).
        for d in tx_data:
            yield dut.tx_data.eq(d)
            yield dut.tx_valid.eq(1)
            yield
        yield dut.tx_valid.eq(0)
        for _ in range(16):
            yield
        # IN transfers (back-to-back txpop, txdat sampled with txpop), 1st one NAKed (no txpktfin).
        for ack in [False, True, True]:
            assert not (yield dut.txcork)
            length = (yield dut.txlen)
            assert length == min(64, len(tx_data) - len(tx_out))
            yield dut.txact.eq(1)
            yield
            yield dut.txpop.eq(1)
            yield
            packet = []
            for i in range(length):
                if i == length - 1:
                    yield dut.txpop.eq(0)
                packet.append((yield dut.txdat))
                yield
            yield dut.txpktfin.eq(ack)
            yield
            yield dut.txpktfin.eq(0)
            yield dut.txact.eq(0)
            for _ in range(4):
                yield
            if ack:
                tx_packets.append(packet)
                tx_out.extend(packet)
            else:
                assert packet == tx_data[:length]
        assert (yield dut.txcork)

    run_simulation(dut, [controller(), rx_user()])
    assert rx_out == [d for data, ok in rx_packets if ok for d in data]
    # Original quirk: the read pointer rewind (txact falling edge without txpktfin) does not update
    # the look-ahead pointer, so the 2nd byte of a resent packet comes from the stale position.
    assert tx_packets[0][1] == tx_data[65]
    tx_packets[0][1] = tx_data[1]
    assert sum(tx_packets, []) == tx_data

# Formal Equivalence (reduced buffers) ------------------------------------------------------------

# Reduced buffers: packet FIFOs 2**asize bytes (RX almost-full at afull), clock-cross FIFOs
# 2**cross_asize bytes (almost-full at cross_afull), i_ep3_tx_max = tx_max. The same reduction is
# applied to the original (defines/parameters) and to the port.
FORMAL_CONFIG = dict(asize=3, afull=4, cross_asize=3, cross_afull=4, tx_max=5)

def export_formal(workdir, c):
    gold = export_gold(workdir, asize=c["asize"], afull=c["afull"],
        cross_asize=c["cross_asize"], cross_afull=c["cross_afull"])
    gate = export_gate(workdir, zero_init=True, tx_max=c["tx_max"], pkt_asize=c["asize"],
        rx_afull=c["afull"], cross_asize=c["cross_asize"], cross_afull=c["cross_afull"])
    return gold, gate

@pytest.mark.skipif(shutil.which("yosys") is None, reason="Yosys not available")
def test_usb_fifo_eqcheck_bmc(depth=12):
    """Bounded equivalence (test/eqcheck.py: Yosys miter + SAT, from the all-zero state, any input
    sequence including resets) vs the original usb_fifo, reduced buffers."""
    workdir    = tempfile.mkdtemp(prefix="eqcheck_usb_fifo_")
    gold, gate = export_formal(workdir, FORMAL_CONFIG)
    ok, log = eqcheck(gold, "usb_fifo_gold", [gate], "usb_fifo_gate", depth=depth,
        gold_params={"TX_MAX": FORMAL_CONFIG["tx_max"]}, workdir=workdir)
    assert ok, log[-4000:]

def pdr_eqcheck(workdir, gold, gate, tx_max, timeout=600):
    """Unbounded equivalence: Yosys miter -> AIGER -> ABC PDR (IC3). Returns (proved, log)."""
    script  = [f"read_verilog -sv {f}" for f in gold]
    script += [f"chparam -set TX_MAX {tx_max} usb_fifo_gold", "hierarchy -top usb_fifo_gold",
        "proc", "flatten", "opt_clean", "rename usb_fifo_gold gold", "design -stash gold"]
    script += [f"read_verilog -sv {gate}", "hierarchy -top usb_fifo_gate",
        "proc", "flatten", "opt_clean", "rename usb_fifo_gate gate", "design -stash gate"]
    script += [
        "design -copy-from gold -as gold gold",
        "design -copy-from gate -as gate gate",
        "miter -equiv -flatten -make_assert gold gate miter",
        "hierarchy -top miter",
        "memory_map", "opt -fast", "async2sync", "dffunmap",
        # X (original o_usb_txdat when i_usb_endpt == 0) -> 0, all registers/memories start at 0.
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

@pytest.mark.skipif(shutil.which("yosys-abc") is None, reason="yosys-abc not available")
@pytest.mark.skipif(not os.environ.get("USB_FIFO_PDR"), reason="slow (~2-4min), set USB_FIFO_PDR=1")
def test_usb_fifo_eqcheck_pdr():
    """Unbounded equivalence (ABC PDR) vs the original usb_fifo, reduced buffers."""
    workdir    = tempfile.mkdtemp(prefix="pdr_usb_fifo_")
    gold, gate = export_formal(workdir, FORMAL_CONFIG)
    ok, log    = pdr_eqcheck(workdir, gold, gate, FORMAL_CONFIG["tx_max"])
    assert ok, log[-4000:]

# Co-Simulation (full size) ------------------------------------------------------------------------

# Verilator testbench: original (usb_fifo_gold) and port (usb_fifo_gate) side by side, driven by a
# USB controller model (OUT packets with good/bad CRC, IN transfers with ACK/NAK/late ACK sized
# from the original txlen/txcork, other endpoints), a user model (IN bytes in bursts, OUT ready with
# long busy periods to fill the buffers), random input segments and resets. All outputs compared
# every cycle (o_usb_txdat only when i_usb_endpt != 0: X in the original).
COSIM_TB = r"""
`timescale 1ns/1ps
module tb;
    parameter [63:0] SEED   = 64'h1234_5678_9abc_def1;
    parameter        CYCLES = 200000;

    reg clk = 0;
    always #5 clk = ~clk;

    // PRNG (xorshift64).
    reg [63:0] s;
    initial begin
        s = SEED;
        if ($value$plusargs("seed=%d", s)) ;
    end
    function [31:0] rnd(input integer n); // uniform in [0, n).
        begin
            s = s ^ (s << 13); s = s ^ (s >> 7); s = s ^ (s << 17);
            rnd = s[47:16] % n;
        end
    endfunction

    // Inputs.
    reg        i_reset = 1;
    reg  [3:0] i_usb_endpt = 0;
    reg        i_usb_rxact = 0, i_usb_rxval = 0, i_usb_rxpktval = 0;
    reg  [7:0] i_usb_rxdat = 0;
    reg        i_usb_txact = 0, i_usb_txpop = 0, i_usb_txpktfin = 0;
    reg        i_ep3_tx_dval = 0;
    reg  [7:0] i_ep3_tx_data = 0;
    reg        i_ep3_rx_rdy = 0;

    // Outputs.
    wire        gold_rxrdy, gate_rxrdy, gold_txcork, gate_txcork, gold_rx_dval, gate_rx_dval;
    wire [11:0] gold_txlen, gate_txlen;
    wire  [7:0] gold_txdat, gate_txdat, gold_rx_data, gate_rx_data;

    usb_fifo_gold gold (
        .i_clk(clk), .i_reset(i_reset), .i_usb_endpt(i_usb_endpt), .i_usb_rxact(i_usb_rxact),
        .i_usb_rxval(i_usb_rxval), .i_usb_rxpktval(i_usb_rxpktval), .i_usb_rxdat(i_usb_rxdat),
        .o_usb_rxrdy(gold_rxrdy), .i_usb_txact(i_usb_txact), .i_usb_txpop(i_usb_txpop),
        .i_usb_txpktfin(i_usb_txpktfin), .o_usb_txcork(gold_txcork), .o_usb_txlen(gold_txlen),
        .o_usb_txdat(gold_txdat), .i_ep3_tx_dval(i_ep3_tx_dval), .i_ep3_tx_data(i_ep3_tx_data),
        .i_ep3_rx_rdy(i_ep3_rx_rdy), .o_ep3_rx_dval(gold_rx_dval), .o_ep3_rx_data(gold_rx_data));

    usb_fifo_gate gate (
        .i_clk(clk), .i_reset(i_reset), .i_usb_endpt(i_usb_endpt), .i_usb_rxact(i_usb_rxact),
        .i_usb_rxval(i_usb_rxval), .i_usb_rxpktval(i_usb_rxpktval), .i_usb_rxdat(i_usb_rxdat),
        .o_usb_rxrdy(gate_rxrdy), .i_usb_txact(i_usb_txact), .i_usb_txpop(i_usb_txpop),
        .i_usb_txpktfin(i_usb_txpktfin), .o_usb_txcork(gate_txcork), .o_usb_txlen(gate_txlen),
        .o_usb_txdat(gate_txdat), .i_ep3_tx_dval(i_ep3_tx_dval), .i_ep3_tx_data(i_ep3_tx_data),
        .i_ep3_rx_rdy(i_ep3_rx_rdy), .o_ep3_rx_dval(gate_rx_dval), .o_ep3_rx_data(gate_rx_data));

    // Compare all outputs every cycle (txdat is X in the original when endpt == 0).
    integer cycle = 0, errors = 0;
    integer n_rx_dval = 0, n_pop = 0, n_fin = 0, n_nak = 0, n_rxpkt = 0, n_rxrdy_low = 0;
    integer n_reset = 0, n_txlen_max = 0, n_chaos = 0, n_tx_full = 0, n_rx_full = 0, n_txc_full = 0, n_rxc_full = 0;
    always @(posedge clk) begin
        cycle <= cycle + 1;
        if ((cycle > 0) && ((gold_rxrdy !== gate_rxrdy) || (gold_txcork !== gate_txcork) ||
            (gold_txlen !== gate_txlen) || (gold_rx_dval !== gate_rx_dval) ||
            (gold_rx_data !== gate_rx_data) || ((i_usb_endpt != 0) && (gold_txdat !== gate_txdat)))) begin
            errors = errors + 1;
            if (errors <= 10)
                $display("MISMATCH cycle %0d: rxrdy %b/%b txcork %b/%b txlen %0d/%0d txdat %02x/%02x rx_dval %b/%b rx_data %02x/%02x",
                    cycle, gold_rxrdy, gate_rxrdy, gold_txcork, gate_txcork, gold_txlen, gate_txlen,
                    gold_txdat, gate_txdat, gold_rx_dval, gate_rx_dval, gold_rx_data, gate_rx_data);
        end
        n_rx_dval   <= n_rx_dval   + gold_rx_dval;
        n_rxrdy_low <= n_rxrdy_low + ((i_usb_endpt == 3) & !gold_rxrdy & !i_reset);
        n_txlen_max <= n_txlen_max + ((i_usb_endpt == 3) & (gold_txlen == 64));
        n_tx_full   <= n_tx_full  + gold.usb_fifo.usb_tx_buf_ep3.sync_tx_pkt_fifo.full;
        n_rx_full   <= n_rx_full  + gold.usb_fifo.usb_rx_buf_ep3.sync_rx_pkt_fifo.full;
        n_txc_full  <= n_txc_full + gold.usb_fifo.usb_tx_buf_ep3.clk_cross_fifo.Full;
        n_rxc_full  <= n_rxc_full + gold.usb_fifo.usb_rx_buf_ep3.clk_cross_fifo.AlmostFull;
    end

    // User side (EP3): IN bytes in bursts, OUT ready with long busy periods.
    integer tx_rate = 50, rx_hold = 0;
    always @(negedge clk) begin
        if (rnd(4000) == 0) tx_rate = rnd(4) == 0 ? 0 : rnd(101);
        i_ep3_tx_dval <= rnd(100) < tx_rate;
        i_ep3_tx_data <= rnd(256);
        if (rx_hold == 0) begin
            case (rnd(4))
                0: begin i_ep3_rx_rdy <= 0; rx_hold = rnd(8000); end
                1: begin i_ep3_rx_rdy <= rnd(2); rx_hold = 0; end
                default: begin i_ep3_rx_rdy <= 1; rx_hold = rnd(3000); end
            endcase
        end else
            rx_hold = rx_hold - 1;
    end

    // USB controller side.
    task tick; begin @(negedge clk); end endtask
    task idle; begin
        i_usb_rxact <= 0; i_usb_rxval <= 0; i_usb_rxpktval <= 0; i_usb_txact <= 0;
        i_usb_txpop <= 0; i_usb_txpktfin <= 0;
    end endtask

    integer i, n, len, fin_early;
    initial begin
        tick; tick; tick;
        i_reset <= 0;
        while (cycle < CYCLES) begin
            case (rnd(16))
            // Idle / other endpoints.
            0, 1: begin
                idle;
                n = 1 + rnd(20);
                for (i = 0; i < n; i = i + 1) begin
                    case (rnd(6))
                        0: i_usb_endpt <= 0; 1: i_usb_endpt <= 1 + rnd(2); 2: i_usb_endpt <= 4 + rnd(12);
                        default: i_usb_endpt <= 3;
                    endcase
                    tick;
                end
            end
            // OUT packet (host -> device).
            2, 3, 4, 5, 6: begin
                idle; i_usb_endpt <= rnd(8) == 0 ? rnd(16) : 3;
                n = 1 + rnd(4); for (i = 0; i < n; i = i + 1) tick;
                if (gold_rxrdy || rnd(4) == 0) begin
                    n_rxpkt = n_rxpkt + 1;
                    i_usb_rxact <= 1;
                    n = rnd(8); for (i = 0; i < n; i = i + 1) tick;
                    len = rnd(4) == 0 ? rnd(8) : 1 + rnd(512);
                    for (i = 0; i < len; ) begin
                        i_usb_rxval <= rnd(8) != 0;
                        i_usb_rxdat <= rnd(256);
                        if (rnd(8) != 0) i = i + 1;
                        tick;
                    end
                    i_usb_rxval <= 0;
                    n = rnd(3); for (i = 0; i < n; i = i + 1) tick;
                    fin_early = rnd(2);
                    if (rnd(8) != 0) begin i_usb_rxpktval <= 1; if (!fin_early) i_usb_rxact <= 0; tick; i_usb_rxpktval <= 0; end
                    i_usb_rxact <= 0; tick;
                end
            end
            // IN transfer (device -> host).
            7, 8, 9, 10, 11, 12: begin
                idle; i_usb_endpt <= rnd(8) == 0 ? rnd(16) : 3;
                n = 1 + rnd(4); for (i = 0; i < n; i = i + 1) tick;
                if (!gold_txcork || rnd(8) == 0) begin
                    len = rnd(8) == 0 ? rnd(80) : gold_txlen;
                    i_usb_txact <= 1;
                    n = rnd(4); for (i = 0; i < n; i = i + 1) tick;
                    for (i = 0; i < len; ) begin
                        i_usb_txpop <= rnd(8) != 0;
                        if (rnd(8) != 0) begin i = i + 1; n_pop = n_pop + 1; end
                        tick;
                    end
                    i_usb_txpop <= 0;
                    n = rnd(3); for (i = 0; i < n; i = i + 1) tick;
                    case (rnd(8))
                        0: begin n_nak = n_nak + 1; end                                   // NAK.
                        1: begin i_usb_txact <= 0; tick; i_usb_txpktfin <= 1; tick; i_usb_txpktfin <= 0; n_fin = n_fin + 1; end // Late ACK.
                        default: begin i_usb_txpktfin <= 1; tick; i_usb_txpktfin <= 0; n_fin = n_fin + 1; end // ACK.
                    endcase
                    i_usb_txact <= 0; tick;
                end
            end
            // Random inputs.
            13: begin
                n_chaos = n_chaos + 1;
                n = 5 + rnd(40);
                for (i = 0; i < n; i = i + 1) begin
                    i_usb_endpt <= rnd(4) == 0 ? rnd(16) : 3;
                    i_usb_rxact <= rnd(2); i_usb_rxval <= rnd(2); i_usb_rxpktval <= rnd(2);
                    i_usb_rxdat <= rnd(256); i_usb_txact <= rnd(2); i_usb_txpop <= rnd(2);
                    i_usb_txpktfin <= rnd(2); i_reset <= rnd(64) == 0;
                    tick;
                end
                i_reset <= 0; idle;
            end
            // Reset.
            default: begin
                if (rnd(64) == 0) begin
                    n_reset = n_reset + 1;
                    i_reset <= 1; n = 1 + rnd(3); for (i = 0; i < n; i = i + 1) tick; i_reset <= 0;
                end
                tick;
            end
            endcase
        end
        $display("COSIM cycles=%0d errors=%0d rx_dval=%0d rx_pkts=%0d pops=%0d acks=%0d naks=%0d rxrdy_low=%0d txlen_max=%0d resets=%0d chaos=%0d tx_full=%0d rx_full=%0d txc_full=%0d rxc_afull=%0d",
            cycle, errors, n_rx_dval, n_rxpkt, n_pop, n_fin, n_nak, n_rxrdy_low, n_txlen_max, n_reset, n_chaos, n_tx_full, n_rx_full, n_txc_full, n_rxc_full);
        $finish;
    end
endmodule
"""

COSIM_CYCLES = 1_000_000
COSIM_SEEDS  = [0x1234_5678_9abc_def1, 0x0bad_cafe_dead_beef, 0x5eed_0000_0000_0003]

@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator not available")
def test_usb_fifo_cosim():
    """Cycle-exact co-simulation of the full-size port vs the original usb_fifo (Verilator)."""
    workdir = tempfile.mkdtemp(prefix="cosim_usb_fifo_")
    files   = export_gold(workdir) + [export_gate(workdir)]
    tb      = os.path.join(workdir, "tb.v")
    with open(tb, "w") as f:
        f.write(COSIM_TB)
    r = subprocess.run(["verilator", "--binary", "--timing", "-Wno-fatal", "-Wno-lint",
        "-Wno-style",
        "--top-module", "tb", "-Mdir", "obj", "-o", "tb", f"-GCYCLES={COSIM_CYCLES}", *files, tb],
        capture_output=True, text=True, cwd=workdir)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    for seed in COSIM_SEEDS:
        r = subprocess.run([os.path.join(workdir, "obj", "tb"), f"+seed={seed}"],
            capture_output=True, text=True, cwd=workdir)
        print(r.stdout)
        m = re.search(r"COSIM cycles=(\d+) errors=(\d+) rx_dval=(\d+) rx_pkts=\d+ pops=(\d+) "
            r"acks=(\d+) naks=(\d+) rxrdy_low=(\d+)", r.stdout)
        assert m, r.stdout + r.stderr
        cycles, errors, rx_dval, pops, acks, naks, rxrdy_low = map(int, m.groups())
        assert cycles >= COSIM_CYCLES
        assert errors == 0, r.stdout
        # Coverage: traffic in both directions, NAK/resend and OUT buffer almost full reached.
        assert rx_dval > 1000 and pops > 1000 and acks > 100 and naks > 10 and rxrdy_low > 0
        assert re.search(r"tx_full=[1-9]", r.stdout) and re.search(r"rx_full=[1-9]", r.stdout)

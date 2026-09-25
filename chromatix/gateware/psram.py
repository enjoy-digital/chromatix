#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
AP Memory OPI (x8, DDR) PSRAM controller for the Chromatic (port of PSRAMController.vhd and
PSRAMBIST_Burst.vhd).

The controller runs in the sys domain (xClk, 67MHz) with the GW5A OSER4/IDES4 4:2 serializers
clocked by fclk (2x sys): each sys cycle carries one PSRAM clock (2 DDR data beats).
"""

from migen import *

from litex.gen import *

# Constants ----------------------------------------------------------------------------------------

READ_LATENCY  = 5
WRITE_LATENCY = 5

# Mode registers (see PSRAMController.vhd generics).
MR0 = (0 << 5) | ((READ_LATENCY - 3) << 2) | 0b00        # LT=0, read latency, drive strength.
MR4 = {3: 0b000, 4: 0b100, 5: 0b010, 6: 0b110, 7: 0b001}[WRITE_LATENCY] << 5 # Write latency, RF=0, PASR=0.
MR8 = (1 << 3) | (1 << 2) | 0b11                         # RBX=1, BT=1, BL=1k wrap.

# PSRAM PHY (GW5A) ---------------------------------------------------------------------------------

class PSRAMGW5APHY(LiteXModule):
    """
    GW5A PSRAM PHY: OSER4 + IODELAY outputs (CS#, CLK, DQ, RWDS), IDES4 inputs (DQ, RWDS).

    OSER4 D0/D1 carry the first half of the sys cycle, D2/D3 the second half (fclk = 2x sys).
    """
    def __init__(self, pads, iodelay_taps=0):
        self.reset     = Signal()
        self.cs_n      = Signal(reset=1)
        self.clk_en    = Signal()
        self.dq_o      = Signal(16) # [15:8] first DDR beat, [7:0] second DDR beat.
        self.dq_oe_n   = Signal(reset=1)
        self.rwds_o    = Signal(2, reset=0b11) # [1] first half, [0] second half.
        self.rwds_oe_n = Signal(reset=1)
        self.dq_i      = [Signal(8) for _ in range(4)] # IDES4 Q0..Q3.
        self.rwds_i    = Signal(4)                     # IDES4 Q0..Q3.

        # # #

        def oser4(name, d, tx=0, q0=None, q1=None):
            self.specials += Instance("OSER4", name=name,
                p_HWL       = "false",
                p_TXCLK_POL = 0,
                i_PCLK  = ClockSignal("sys"),
                i_FCLK  = ClockSignal("fclk"),
                i_RESET = self.reset,
                i_TX0   = tx,
                i_TX1   = tx,
                i_D0    = d[0],
                i_D1    = d[1],
                i_D2    = d[2],
                i_D3    = d[3],
                o_Q0    = q0 if q0 is not None else Signal(),
                o_Q1    = q1 if q1 is not None else Signal(),
            )

        def iodelay(name, di, do):
            self.specials += Instance("IODELAY", name=name,
                p_C_STATIC_DLY = iodelay_taps,
                i_DI      = di,
                i_SDTAP   = 0,
                i_DLYSTEP = Constant(0, 8),
                i_VALUE   = 0,
                o_DO      = do,
                o_DF      = Signal(),
            )

        def ides4(name, d, q):
            self.specials += Instance("IDES4", name=name,
                i_D     = d,
                i_PCLK  = ClockSignal("sys"),
                i_FCLK  = ClockSignal("fclk"),
                i_CALIB = 0,
                i_RESET = self.reset,
                o_Q0    = q[0],
                o_Q1    = q[1],
                o_Q2    = q[2],
                o_Q3    = q[3],
            )

        def iobuf(name, i, oen, o, io):
            self.specials += Instance("IOBUF", name=name,
                i_I   = i,
                i_OEN = oen,
                o_O   = o,
                io_IO = io,
            )

        # CS#.
        cs_n_dly = Signal()
        oser4("psram_oser4_cs_n", [self.cs_n]*4, q0=cs_n_dly)
        iodelay("psram_iodelay_cs_n", cs_n_dly, pads.ce_n)

        # CLK (one clock per sys cycle, centered on the data).
        clk_dly = Signal()
        oser4("psram_oser4_clk", [0, self.clk_en, self.clk_en, 0], q0=clk_dly)
        iodelay("psram_iodelay_clk", clk_dly, pads.clk)

        # RWDS.
        rwds_o     = Signal()
        rwds_o_dly = Signal()
        rwds_oen   = Signal()
        rwds_in    = Signal()
        oser4("psram_oser4_rwds", [self.rwds_o[1], self.rwds_o[1], self.rwds_o[0], self.rwds_o[0]],
            tx=self.rwds_oe_n, q0=rwds_o, q1=rwds_oen)
        iodelay("psram_iodelay_rwds", rwds_o, rwds_o_dly)
        iobuf("psram_iobuf_rwds", rwds_o_dly, rwds_oen, rwds_in, pads.dqs)
        ides4("psram_ides4_rwds", rwds_in, [self.rwds_i[n] for n in range(4)])

        # DQ.
        for n in range(8):
            dq_o     = Signal()
            dq_o_dly = Signal()
            dq_oen   = Signal()
            dq_in    = Signal()
            oser4(f"psram_oser4_dq{n}", [self.dq_o[8 + n], self.dq_o[8 + n], self.dq_o[n], self.dq_o[n]],
                tx=self.dq_oe_n, q0=dq_o, q1=dq_oen)
            iodelay(f"psram_iodelay_dq{n}", dq_o, dq_o_dly)
            iobuf(f"psram_iobuf_dq{n}", dq_o_dly, dq_oen, dq_in, pads.dq[n])
            ides4(f"psram_ides4_dq{n}", dq_in, [self.dq_i[q][n] for q in range(4)])

# PSRAM Controller ---------------------------------------------------------------------------------

class PSRAMController(LiteXModule):
    """
    OPI PSRAM burst controller (port of PSRAMController.vhd).

    Startup: global reset command, MR0/MR4/MR8 writes, MR0..MR8 reads (ready once the vendor ID
    reads 0x0D). Bursts: burst_length bytes from addr (16-bit words), writes split at 1kB rows.
    """
    def __init__(self, phy, startup_cycles=50000):
        self.reset        = Signal()
        self.req_read     = Signal()
        self.req_write    = Signal()
        self.addr         = Signal(23)
        self.din          = Signal(16)
        self.burst_length = Signal(11)

        self.ready        = Signal()
        self.write_next   = Signal()
        self.done         = Signal()
        self.dout_valid   = Signal()
        self.dout         = Signal(16)
        self.vendor_id    = Signal(5)

        # # #

        # Command/address/data shift register: [63:48] (2 DDR beats) sent to the PHY each sys cycle.
        dq_sr      = Signal(64)
        step       = Signal(4)
        startup    = Signal(max=startup_cycles + 1)
        cfg_waddr  = Signal(4, reset=0xf)
        cfg_raddr  = Signal(4)
        burst      = Signal(11)
        writeburst = Signal()
        next_row   = Signal(13)
        row_count  = Signal(9)
        receiving  = Signal()
        dq_adj_l   = Signal(8)
        dq_adj_h   = Signal(8)

        self.comb += [
            phy.reset.eq(self.reset),
            phy.dq_o.eq(dq_sr[48:64]),
        ]

        # Receive: realign IDES4 outputs with RWDS.
        rwds_q = phy.rwds_i
        dq_q   = phy.dq_i
        self.sync += [
            If(self.reset,
                receiving.eq(0),
            ).Else(
                receiving.eq(rwds_q[2] | rwds_q[0]),
            ),
            Case(rwds_q, {
                0b0011:    [dq_adj_l.eq(dq_q[0]), dq_adj_h.eq(dq_q[2])],
                0b0001:    [dq_adj_l.eq(dq_q[0]), dq_adj_h.eq(dq_q[2])],
                0b1001:    [dq_adj_l.eq(dq_q[0]), dq_adj_h.eq(dq_q[2])],
                0b0110:    [dq_adj_l.eq(dq_q[1]), dq_adj_h.eq(dq_q[3])],
                "default": [dq_adj_l.eq(dq_q[3]), dq_adj_h.eq(dq_q[2])],
            }),
        ]

        # Status.
        self.fsm = fsm = ResetInserter()(FSM(reset_state="RESET"))
        self.comb += [
            fsm.reset.eq(self.reset),
            self.ready.eq(fsm.ongoing("IDLE") & (self.vendor_id == 0x0d)),
            self.write_next.eq(fsm.ongoing("WRITING") & writeburst & (burst > 2)),
        ]

        # Defaults / reset.
        self.sync += [
            self.done.eq(0),
            self.dout_valid.eq(0),
            dq_sr.eq(Cat(Constant(0, 16), dq_sr[:48])),
            If(step < 15, step.eq(step + 1)),
            If(self.reset,
                startup.eq(0),
                cfg_raddr.eq(0),
                cfg_waddr.eq(0xf),
                step.eq(0),
                phy.cs_n.eq(1),
                phy.clk_en.eq(0),
                phy.dq_oe_n.eq(1),
                phy.rwds_oe_n.eq(1),
            ),
        ]

        # Startup / Mode registers.
        fsm.act("RESET",
            If(startup < startup_cycles,
                NextValue(startup, startup + 1),
            ).Else(
                NextState("CONFIGWRITE_NEXT"),
                NextValue(step, 0),
            )
        )
        fsm.act("CONFIGWRITE_NEXT",
            If(step >= 8,
                NextState("CONFIGWRITE_START"),
                NextValue(phy.cs_n, 0),
                NextValue(step, 0),
            )
        )
        cfg_value = Signal(8)
        self.comb += Case(cfg_waddr, {
            0x0: cfg_value.eq(MR0),
            0x4: cfg_value.eq(MR4),
            0x8: cfg_value.eq(MR8),
            "default": cfg_value.eq(0),
        })
        fsm.act("CONFIGWRITE_START",
            NextState("CONFIGWRITE"),
            NextValue(phy.dq_oe_n, 0),
            NextValue(phy.clk_en, 1),
            If(cfg_waddr == 0xf,
                # Global reset command.
                NextValue(dq_sr, 2**64 - 1),
            ).Else(
                # Mode register write: 0xC0 0xC0, address, data.
                NextValue(dq_sr, Cat(
                    Constant(0, 8),      # [ 7: 0].
                    cfg_value,           # [15: 8]: Data.
                    cfg_waddr,           # [19:16]: Address.
                    Constant(0, 28),     # [47:20].
                    Constant(0xc0c0, 16) # [63:48]: Command.
                )),
            ),
        )
        fsm.act("CONFIGWRITE",
            If(step == 4,
                NextState("CONFIGWRITE_NEXT"),
                NextValue(phy.clk_en, 0),
                NextValue(phy.dq_oe_n, 1),
                NextValue(phy.cs_n, 1),
                Case(cfg_waddr, {
                    0xf: [NextValue(cfg_waddr, 0x0), NextState("RESET"), NextValue(startup, 0)],
                    0x0: NextValue(cfg_waddr, 0x4),
                    0x4: NextValue(cfg_waddr, 0x8),
                    0x8: NextState("CONFIGREAD_NEXT"),
                    "default": [],
                })
            )
        )
        fsm.act("CONFIGREAD_NEXT",
            If(step >= 8,
                NextState("CONFIGREAD_START"),
                NextValue(phy.cs_n, 0),
                NextValue(step, 0),
            )
        )
        fsm.act("CONFIGREAD_START",
            NextState("CONFIGREAD"),
            # Mode register read: 0x40 0x40, address.
            NextValue(dq_sr, Cat(
                Constant(0, 16),     # [15: 0].
                cfg_raddr,           # [19:16]: Address.
                Constant(0, 28),     # [47:20].
                Constant(0x4040, 16) # [63:48]: Command.
            )),
            NextValue(phy.dq_oe_n, 0),
            NextValue(phy.clk_en, 1),
        )
        fsm.act("CONFIGREAD",
            If(step == 3,
                NextValue(phy.dq_oe_n, 1),
            ),
            If((step > 9) & receiving,
                NextState("CONFIGREAD_NEXT"),
                NextValue(step, 0),
                NextValue(phy.cs_n, 1),
                NextValue(phy.clk_en, 0),
                Case(cfg_raddr, {
                    0x0: NextValue(cfg_raddr, 0x1),
                    0x1: [NextValue(cfg_raddr, 0x2), NextValue(self.vendor_id, dq_adj_l[0:5])],
                    0x2: NextValue(cfg_raddr, 0x3),
                    0x3: NextValue(cfg_raddr, 0x4),
                    0x4: NextValue(cfg_raddr, 0x8),
                    0x8: NextState("IDLE"),
                    "default": [],
                })
            )
        )

        # Bursts.
        cmd_addr = Cat(Constant(0, 16), Constant(0, 1), self.addr[1:23], Constant(0, 9))
        fsm.act("IDLE",
            NextValue(phy.rwds_oe_n, 1),
            NextValue(phy.dq_oe_n, 1),
            NextValue(phy.clk_en, 0),
            NextValue(phy.cs_n, 1),
            NextValue(writeburst, 0),
            NextValue(burst, self.burst_length),
            NextValue(next_row, self.addr[10:23] + 1),
            NextValue(row_count, 0x1ff - self.addr[1:10]),
            If(self.req_read | self.req_write,
                If(self.req_read,
                    NextState("READING"),
                    NextValue(dq_sr, Cat(cmd_addr, Constant(0x2020, 16))),
                ).Else(
                    NextState("WRITING"),
                    NextValue(dq_sr, Cat(cmd_addr, Constant(0xa0a0, 16))),
                ),
                NextValue(phy.cs_n, 0),
                NextValue(phy.clk_en, 1),
                NextValue(phy.dq_oe_n, 0),
                NextValue(step, 1),
            )
        )
        end_burst = [
            NextState("IDLE"),
            NextValue(self.done, 1),
            NextValue(phy.cs_n, 1),
            NextValue(phy.clk_en, 0),
        ]
        fsm.act("READING",
            If(step == 3,
                NextValue(phy.dq_oe_n, 1),
            ).Elif((step > 9) & (rwds_q[3] != rwds_q[1]),
                If(burst == 1,
                    *end_burst,
                ).Else(
                    NextState("READVAL"),
                )
            )
        )
        fsm.act("READVAL",
            If(receiving,
                NextValue(self.dout_valid, 1),
                NextValue(self.dout, Cat(dq_adj_l, dq_adj_h)),
                If(burst <= 2,
                    *end_burst,
                ).Else(
                    NextValue(burst, burst - 2),
                )
            )
        )
        fsm.act("WRITING",
            If(step == (1 + WRITE_LATENCY),
                NextValue(writeburst, 1),
            ),
            If(writeburst,
                NextValue(phy.dq_oe_n, 0),
                NextValue(phy.rwds_oe_n, 0),
                NextValue(phy.rwds_o, 0b00),
                NextValue(dq_sr[48:64], Cat(self.din[8:16], self.din[0:8])),
                NextValue(row_count, row_count - 1),
                If(row_count == 0,
                    NextState("WRITE_NEWROW"),
                    NextValue(writeburst, 0),
                    NextValue(step, 0),
                ),
                If(burst <= 2,
                    NextState("IDLE"),
                    NextValue(self.done, 1),
                ).Else(
                    NextValue(burst, burst - 2),
                )
            )
        )
        fsm.act("WRITE_NEWROW",
            NextValue(phy.dq_oe_n, 1),
            NextValue(phy.rwds_oe_n, 1),
            NextValue(phy.clk_en, 0),
            NextValue(phy.cs_n, 1),
            If(step == 4,
                NextState("WRITING"),
                NextValue(dq_sr, Cat(
                    Constant(0, 26),     # [25: 0].
                    next_row,            # [38:26]: Address (row, byte address 1kB aligned).
                    Constant(0, 9),      # [47:39].
                    Constant(0xa0a0, 16) # [63:48]: Command.
                )),
                NextValue(phy.cs_n, 0),
                NextValue(phy.clk_en, 1),
                NextValue(phy.dq_oe_n, 0),
                NextValue(step, 1),
            )
        )

# PSRAM BIST ---------------------------------------------------------------------------------------

def next_pattern_nv(testtype, din, addr_cnt, from_counter):
    """Next BIST data pattern (as NextValue statements)."""
    return Case(testtype, {
        0: NextValue(din, ~din),
        1: NextValue(din, addr_cnt[:16] if from_counter else din + 1),
        2: NextValue(din, 0xffff),
        3: NextValue(din, 0x0000),
    })


class PSRAMBIST(LiteXModule):
    """
    Short PSRAM BIST (port of PSRAMBIST_Burst.vhd, SHORTTEST): write/read bursts with alternating,
    counting, ones and zeros patterns. Each pattern is written then read back with bursts spread over
    the lower 4MB (word address stepping by 512*burst_words + 1 until bit 21 is set: 8 bursts).

    `ctrl` provides the arbiter shared ready/dout.
    """
    def __init__(self, port, ctrl, burst_words=512):
        self.reset    = Signal()
        self.finished = Signal()
        self.failed   = Signal()

        # # #

        addr_cnt = Signal(22)
        din      = Signal(16, reset=0xaa55)
        testtype = Signal(2) # 0: Alternating, 1: Count, 2: Ones, 3: Zeros.
        req_read = Signal()
        req_wr   = Signal()

        self.comb += [
            port.request.eq(req_read | req_wr),
            port.rnw.eq(req_read),
            port.din.eq(din),
            port.burst_length.eq(2*burst_words),
        ]

        self.fsm = fsm = ResetInserter()(FSM(reset_state="WRITE_START"))
        self.comb += fsm.reset.eq(self.reset)
        self.sync += [
            req_read.eq(0),
            req_wr.eq(0),
            If(self.reset,
                testtype.eq(0),
                addr_cnt.eq(0),
                din.eq(0xaa55),
                self.finished.eq(0),
                self.failed.eq(0),
            )
        ]
        for start, wait, req in [
            ("WRITE_START", "WRITE_WAIT", req_wr),
            ("READ_START",  "READ_WAIT",  req_read),
        ]:
            fsm.act(start,
                If(ctrl.ready,
                    NextState(wait),
                    NextValue(req, 1),
                    NextValue(port.addr, Cat(0, addr_cnt)),
                    NextValue(addr_cnt, addr_cnt + (512*burst_words) + 1), # Short test.
                    next_pattern_nv(testtype, din, addr_cnt, from_counter=True),
                )
            )
        fsm.act("WRITE_WAIT",
            If(port.done,
                NextState("WRITE_START"),
                If(addr_cnt[-1],
                    NextState("READ_START"),
                    NextValue(addr_cnt, 0),
                    NextValue(din, 0xaa55),
                )
            ),
            If(port.write_next,
                next_pattern_nv(testtype, din, addr_cnt, from_counter=False),
            )
        )
        fsm.act("READ_WAIT",
            If(port.done,
                NextState("READ_START"),
                If(addr_cnt[-1],
                    NextState("WRITE_START"),
                    NextValue(addr_cnt, 0),
                    Case(testtype, {
                        0: NextValue(testtype, 1),
                        1: NextValue(testtype, 2),
                        2: NextValue(testtype, 3),
                        3: NextState("TESTDONE"),
                    })
                )
            ),
            If(port.dout_valid,
                If(~port.done,
                    next_pattern_nv(testtype, din, addr_cnt, from_counter=False),
                ),
                If(ctrl.dout != din,
                    NextValue(self.failed, 1),
                )
            )
        )
        fsm.act("TESTDONE",
            NextValue(self.finished, 1),
        )

#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
Generic read-only ROM cache for a hosted core (core-porting methodology, doc/CORES.md).

A hosted console core reads its cartridge ROM with a simple (addr, rd) -> byte interface and expects
the byte within a couple of its own cycles -- fine for the SDRAM the MiSTer/Tang builds assume, but
the Chromatic's octal PSRAM has ~3.4 us random-read latency, far longer than a core cycle. This
module bridges the two: a direct-mapped, 16-byte-line cache (tags + data in BRAM, in the core's
clock domain) in front of one PSRAM arbiter port. Hits are served immediately; on a miss it asserts
`stall` (the core must freeze -- e.g. by gating its clock-enables), fetches the line from the PSRAM
(xClk, via a request/fill CDC), fills the cache, then releases the core. This is the generalisation
of vcart.py's Game-Boy cache (no MBC/cartridge-RAM/writes): usable by any hosted core (SMS/GG first).

`port` is a MultiPortRAMCtrl port (xClk), `port_dout` its shared read data. The ROM must be placed in
the PSRAM at `base` (the host uploads it over the debug bridge). Reads are gated by `rd` so only
cartridge accesses fault in lines. `flush` (a pulse in `core_cd`) invalidates the cache.
"""

from migen import *
from migen.genlib.cdc import MultiReg

from litex.gen import *

from litex.soc.interconnect import stream

class CoreROMCache(LiteXModule):
    def __init__(self, port, port_dout, base=0, addr_width=22, lines=128, core_cd="sms"):
        # Core bus (core_cd domain).
        self.addr  = Signal(addr_width) # Byte address into the ROM.
        self.rd    = Signal()           # Cartridge read active (gates faulting-in).
        self.data  = Signal(8)          # Byte to the core.
        self.stall = Signal()           # 1 = the core must freeze (miss being served / lookup).
        self.flush = Signal()           # Pulse: invalidate the cache.
        # Statistics / debug (core_cd).
        self.misses = Signal(32)
        self.reads  = Signal(32)
        self.state  = Signal(4)         # Current FSM state (debug).

        # # #

        line_bits = log2_int(lines)
        tag_bits  = 23 - 4 - line_bits # PSRAM byte address is 23-bit; 16-byte lines (4 offset bits).
        sync      = getattr(self.sync, core_cd)

        paddr = Signal(23)
        self.comb += paddr.eq(base + self.addr)

        # Cache memories (core_cd): tags (valid + tag), data (16-bit words, 8 per line).
        tags    = Memory(1 + tag_bits, lines)
        data    = Memory(16, lines*8)
        tags_rd = tags.get_port(clock_domain=core_cd)
        tags_wr = tags.get_port(write_capable=True, clock_domain=core_cd)
        data_p  = data.get_port(write_capable=True, we_granularity=8, clock_domain=core_cd)
        self.specials += tags, data, tags_rd, tags_wr, data_p

        # Line fill words (xClk -> core_cd).
        self.fill_cdc = fill_cdc = stream.ClockDomainCrossing([("data", 16)],
            cd_from = "xclk", cd_to = core_cd, depth = 8)
        fill_word = Signal(3)

        # Lookup: tags read for paddr, valid the next cycle if paddr is unchanged.
        paddr_d  = Signal(23)
        stable   = Signal()
        hit      = Signal()
        miss     = Signal()
        fsm_fill = Signal()
        req_addr = Signal(23)
        sync += paddr_d.eq(paddr)
        self.comb += [
            tags_rd.adr.eq(paddr[4:4 + line_bits]),
            stable.eq(paddr_d == paddr),
            hit.eq(stable & tags_rd.dat_r[0] & (tags_rd.dat_r[1:] == paddr[4 + line_bits:23])),
            miss.eq(stable & ~hit),
            If(fsm_fill,
                data_p.adr.eq(Cat(fill_word, req_addr[4:4 + line_bits])),
            ).Else(
                data_p.adr.eq(paddr[1:4 + line_bits]),
            ),
            self.data.eq(Mux(paddr[0], data_p.dat_r[8:16], data_p.dat_r[0:8])),
        ]

        # Request/done handshake (core_cd <-> xClk).
        req_toggle  = Signal()
        done_toggle = Signal()
        done_sync   = Signal()
        done_seen   = Signal()
        self.specials += MultiReg(done_toggle, done_sync, core_cd)

        # core_cd FSM.
        flush_index = Signal(line_bits)
        miss_issue  = Signal()
        self.fsm = fsm = ClockDomainsRenamer(core_cd)(FSM(reset_state="FLUSH"))
        fsm.act("FLUSH",
            self.stall.eq(1),
            tags_wr.adr.eq(flush_index), tags_wr.dat_w.eq(0), tags_wr.we.eq(1),
            NextValue(flush_index, flush_index + 1),
            If(flush_index == (lines - 1), NextState("TAG-WAIT")),
        )
        fsm.act("IDLE",
            If(self.flush,
                NextValue(flush_index, 0), NextState("FLUSH"),
            ).Elif(self.rd & miss,
                self.stall.eq(1),
                miss_issue.eq(1),
                NextValue(req_addr, Cat(Constant(0, 4), paddr[4:])),
                NextValue(req_toggle, ~req_toggle),
                NextValue(fill_word, 0),
                NextState("FILL"),
            ).Elif(self.rd & ~hit,
                self.stall.eq(1), # Lookup not yet stable: hold the core until a confirmed hit.
            ),
        )
        fsm.act("FILL",
            self.stall.eq(1), fsm_fill.eq(1),
            fill_cdc.source.ready.eq(1),
            data_p.dat_w.eq(fill_cdc.source.data),
            data_p.we.eq(Replicate(fill_cdc.source.valid, 2)),
            If(fill_cdc.source.valid,
                NextValue(fill_word, fill_word + 1),
                If(fill_word == 7, NextState("FILL-DONE")),
            ),
        )
        fsm.act("FILL-DONE",
            self.stall.eq(1),
            If(done_sync != done_seen, NextValue(done_seen, done_sync), NextState("TAG")),
        )
        fsm.act("TAG",
            self.stall.eq(1),
            tags_wr.adr.eq(req_addr[4:4 + line_bits]),
            tags_wr.dat_w.eq(Cat(1, req_addr[4 + line_bits:23])),
            tags_wr.we.eq(1),
            NextState("TAG-WAIT"),
        )
        fsm.act("TAG-WAIT",
            self.stall.eq(1), # Tags/data read settles; lookup valid back in IDLE.
            NextState("IDLE"),
        )

        # Statistics / debug.
        access_d = Signal()
        sync += [
            access_d.eq(self.rd),
            If(self.rd & ~access_d, self.reads.eq(self.reads + 1)),
            If(miss_issue, self.misses.eq(self.misses + 1)),
        ]
        for i, n in enumerate(["FLUSH", "IDLE", "FILL", "FILL-DONE", "TAG", "TAG-WAIT"]):
            self.comb += If(fsm.ongoing(n), self.state.eq(i))

        # xClk: PSRAM port (read-only line fetch).
        req_sync = Signal()
        req_seen = Signal()
        busy     = Signal()
        self.specials += MultiReg(req_toggle, req_sync, "xclk")
        self.comb += [
            port.rnw.eq(1),
            port.addr.eq(req_addr),
            port.burst_length.eq(16),
            port.din.eq(0),
            fill_cdc.sink.valid.eq(port.dout_valid & busy),
            fill_cdc.sink.data.eq(port_dout),
        ]
        self.sync.xclk += [
            port.request.eq(0),
            If(~busy & (req_sync != req_seen),
                busy.eq(1), req_seen.eq(req_sync), port.request.eq(1),
            ),
            If(busy & port.done,
                busy.eq(0), done_toggle.eq(~done_toggle),
            ),
        ]

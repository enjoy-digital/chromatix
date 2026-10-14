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

# Core Bank Cache ----------------------------------------------------------------------------------

class CoreBankCache(LiteXModule):
    """
    Read-only bank cache for a core whose data must be served every few cycles from a window of a
    large ROM, ex the NES PPU reading a 128/256 KB MMC1/MMC3 CHR-ROM: far too large for BRAM, and the
    PSRAM's ~3.4 us latency excludes a line cache on such a fetch rate. The ROM (in the PSRAM at
    `base`) is split in `bank_size`-byte banks; `slots` of them are held in a BRAM owned by the caller
    (the core reads it at Cat(addr[:offset], slot)). A bank -> slot map (distributed RAM) translates
    each read combinationally; a read of an absent bank asserts `stall` (the core must freeze), the
    bank is fetched in one PSRAM burst (`fill_*` writes, `cd` domain) into a victim slot (second chance:
    slots read since the last pass are spared), the map is updated, then the core is released. A
    console only sees a few banks at once (the NES PPU: 8 KB), so after a warm-up switching banks is
    free. Generic: the translation is on the ROM address the core (its mapper) presents, so no mapper
    knowledge is needed.

    All in `cd` (a fast clock synchronous to the core's, ex nes4x: the core's addr/rd are stable for
    a core cycle). `port` is a MultiPortRAMCtrl port (xClk), `port_dout` its shared read data.
    """
    def __init__(self, port, port_dout, base=0, bank_bits=8, bank_size=1024, slots=32, cd="nes4x"):
        offset_bits = log2_int(bank_size)
        slot_bits   = log2_int(slots)
        words       = bank_size//2
        self.addr       = Signal(bank_bits + offset_bits) # Byte address into the ROM.
        self.rd         = Signal()                        # ROM read active.
        self.enable     = Signal()                        # 0: no lookup, no stall (direct mapping).
        self.slot       = Signal(slot_bits)               # Slot of addr's bank (valid on a hit).
        self.stall      = Signal()
        self.flush      = Signal()                        # Invalidate all the slots.
        self.fill_we    = Signal()                        # Fill write (cd): 16-bit word...
        self.fill_adr   = Signal(slot_bits + offset_bits - 1) # ... at this word address of the BRAM.
        self.fill_data  = Signal(16)
        self.misses     = Signal(32)
        self.offset_bits = offset_bits

        # # #

        sync = getattr(self.sync, cd)

        # Bank -> (valid, slot) map and slot -> bank owner table (distributed RAM, async read).
        bmap     = Memory(1 + slot_bits, 2**bank_bits)
        owner    = Memory(bank_bits, slots)
        bmap_rd  = bmap.get_port(async_read=True)
        bmap_wr  = bmap.get_port(write_capable=True, clock_domain=cd)
        owner_rd = owner.get_port(async_read=True)
        owner_wr = owner.get_port(write_capable=True, clock_domain=cd)
        self.specials += bmap, owner, bmap_rd, bmap_wr, owner_rd, owner_wr
        owner_valid = Signal(slots)
        referenced  = Signal(slots)

        bank = self.addr[offset_bits:]
        hit  = Signal()
        self.comb += [
            bmap_rd.adr.eq(bank),
            hit.eq(bmap_rd.dat_r[0]),
            self.slot.eq(bmap_rd.dat_r[1:]),
        ]

        # Fill words (xClk -> cd).
        self.fill_cdc = fill_cdc = stream.ClockDomainCrossing([("data", 16)],
            cd_from = "xclk", cd_to = cd, depth = 16)

        req_bank    = Signal(bank_bits)
        victim      = Signal(slot_bits)
        fill_word   = Signal(offset_bits - 1)
        flush_index = Signal(bank_bits)
        settle      = Signal(2)
        req_toggle  = Signal()
        done_toggle = Signal()
        done_sync   = Signal()
        done_seen   = Signal()
        self.specials += MultiReg(done_toggle, done_sync, cd)

        sync += If(self.enable & self.rd & hit & ~self.stall,
            referenced.eq(referenced | (1 << self.slot)))

        self.fsm = fsm = ClockDomainsRenamer(cd)(FSM(reset_state="FLUSH"))
        fsm.act("FLUSH",
            self.stall.eq(self.enable),
            bmap_wr.adr.eq(flush_index), bmap_wr.dat_w.eq(0), bmap_wr.we.eq(1),
            NextValue(flush_index, flush_index + 1),
            NextValue(owner_valid, 0),
            NextValue(referenced, 0),
            If(flush_index == (2**bank_bits - 1), NextState("IDLE")),
        )
        fsm.act("IDLE",
            If(self.flush,
                NextValue(flush_index, 0), NextState("FLUSH"),
            ).Elif(self.enable & self.rd & ~hit,
                self.stall.eq(1),
                NextValue(req_bank, bank),
                NextState("VICTIM"),
            ),
        )
        # Second chance: skip (and clear) the slots read since the last pass.
        fsm.act("VICTIM",
            self.stall.eq(1),
            If(Array(referenced[i] for i in range(slots))[victim],
                NextValue(referenced, referenced & ~(1 << victim)),
                NextValue(victim, victim + 1),
            ).Else(
                NextState("EVICT"),
            ),
        )
        fsm.act("EVICT",
            self.stall.eq(1),
            owner_rd.adr.eq(victim),
            bmap_wr.adr.eq(owner_rd.dat_r), bmap_wr.dat_w.eq(0),
            bmap_wr.we.eq(Array(owner_valid[i] for i in range(slots))[victim]),
            NextValue(fill_word, 0),
            NextValue(req_toggle, ~req_toggle),
            NextState("FILL"),
        )
        fsm.act("FILL",
            self.stall.eq(1),
            fill_cdc.source.ready.eq(1),
            If(fill_cdc.source.valid,
                NextValue(fill_word, fill_word + 1),
                If(fill_word == (words - 1), NextState("FILL-DONE")),
            ),
        )
        fsm.act("FILL-DONE",
            self.stall.eq(1),
            If(done_sync != done_seen, NextValue(done_seen, done_sync), NextState("MAP")),
        )
        fsm.act("MAP",
            self.stall.eq(1),
            bmap_wr.adr.eq(req_bank), bmap_wr.dat_w.eq(Cat(1, victim)), bmap_wr.we.eq(1),
            owner_wr.adr.eq(victim), owner_wr.dat_w.eq(req_bank), owner_wr.we.eq(1),
            NextValue(owner_valid, owner_valid | (1 << victim)),
            NextValue(referenced, referenced | (1 << victim)),
            NextValue(victim, victim + 1),
            NextValue(settle, 0),
            NextState("SETTLE"),
        )
        fsm.act("SETTLE",
            self.stall.eq(1), # The caller's BRAM read (registered) settles on the new slot.
            NextValue(settle, settle + 1),
            If(settle == 3, NextState("IDLE")),
        )
        self.comb += [
            self.fill_we.eq(fsm.ongoing("FILL") & fill_cdc.source.valid),
            self.fill_adr.eq(Cat(fill_word, victim)),
            self.fill_data.eq(fill_cdc.source.data),
        ]
        sync += If(fsm.ongoing("IDLE") & self.enable & self.rd & ~hit & ~self.flush,
            self.misses.eq(self.misses + 1))

        # xClk: PSRAM port (one read burst per bank).
        req_sync = Signal()
        req_seen = Signal()
        busy     = Signal()
        self.specials += MultiReg(req_toggle, req_sync, "xclk")
        self.comb += [
            port.rnw.eq(1),
            port.addr.eq(base + Cat(Constant(0, offset_bits), req_bank)),
            port.burst_length.eq(bank_size),
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

#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
Virtual cartridge: the Game Boy ROM and cartridge RAM are served from the PSRAM (loaded from the host)
in place of the physical cartridge.

- MBC: ROM only, MBC1, MBC2, MBC3 (no RTC), MBC5 bank registers -> ROM/RAM addresses.
- Cache (hClk): direct mapped, 16-byte lines (tags and data in block RAMs, synchronous reads: the
  lookup of an address is known one cycle later, well before the core samples the data at the end
  of its M-cycle). Hits are served without delay; on a miss (or a cartridge RAM write), the Game Boy
  core is frozen (wait, speedcontrol) until the line is fetched from the PSRAM (xClk, highest
  priority memory port).
- Cartridge RAM writes update the cache and are written through to the PSRAM (16-bit words).

PSRAM layout: ROM at rom_base (up to 3.5MB), cartridge RAM at ram_base (128KB).
"""

from migen import *
from migen.genlib.cdc import MultiReg

from litex.gen import *

from litex.soc.interconnect     import stream
from litex.soc.interconnect.csr import *

# Constants ----------------------------------------------------------------------------------------

MBC_NONE = 0
MBC_MBC1 = 1
MBC_MBC2 = 2
MBC_MBC3 = 3
MBC_MBC5 = 5

ROM_BASE = 0x400000
RAM_BASE = 0x780000

# MBC ----------------------------------------------------------------------------------------------

class MBC(LiteXModule):
    """
    Memory Bank Controller registers (written by the Game Boy in $0000-$7FFF) and address mapping:
    rom_addr (byte address in the ROM, masked by rom_mask: 16KB banks), ram_addr (byte address in
    the cartridge RAM, masked by ram_mask: 8KB banks), ram_enabled.
    """
    def __init__(self):
        # Configuration.
        self.mbc         = Signal(3)
        self.rom_mask    = Signal(9)
        self.ram_mask    = Signal(4)
        self.reset       = Signal()
        # Game Boy bus.
        self.a           = Signal(16)
        self.wr          = Signal()
        self.din         = Signal(8)
        # Mapping.
        self.rom_addr    = Signal(23)
        self.ram_addr    = Signal(17)
        self.ram_enabled = Signal()

        # # #

        ram_enable = Signal()
        rom_bank   = Signal(9, reset=1) # 16KB bank in $4000-$7FFF.
        bank2      = Signal(4)          # MBC1: upper ROM/RAM bank bits, MBC3/5: RAM bank.
        mode       = Signal()           # MBC1 banking mode.
        a          = self.a
        d          = self.din

        # Registers.
        self.sync += [
            If(self.reset,
                ram_enable.eq(0),
                rom_bank.eq(1),
                bank2.eq(0),
                mode.eq(0),
            ).Elif(self.wr & ~a[15],
                Case(self.mbc, {
                    MBC_MBC1: Case(a[13:15], {
                        0: ram_enable.eq(d[0:4] == 0xa),
                        1: rom_bank.eq(Mux(d[0:5] == 0, 1, d[0:5])),
                        2: bank2.eq(d[0:2]),
                        3: mode.eq(d[0]),
                    }),
                    MBC_MBC2: If(~a[14],
                        If(a[8],
                            rom_bank.eq(Mux(d[0:4] == 0, 1, d[0:4])),
                        ).Else(
                            ram_enable.eq(d[0:4] == 0xa),
                        )
                    ),
                    MBC_MBC3: Case(a[13:15], {
                        0: ram_enable.eq(d[0:4] == 0xa),
                        1: rom_bank.eq(Mux(d[0:7] == 0, 1, d[0:7])),
                        2: bank2.eq(d[0:4]),
                        3: [],
                    }),
                    MBC_MBC5: Case(a[12:15], {
                        0: ram_enable.eq(d[0:4] == 0xa),
                        1: ram_enable.eq(d[0:4] == 0xa),
                        2: rom_bank[0:8].eq(d),
                        3: rom_bank[8].eq(d[0]),
                        4: bank2.eq(d[0:4]),
                        5: bank2.eq(d[0:4]),
                    }),
                })
            )
        ]

        # ROM mapping.
        bank = Signal(9)
        self.comb += [
            If(self.mbc == MBC_NONE,
                bank.eq(a[14]),
            ).Elif(self.mbc == MBC_MBC1,
                If(~a[14],
                    bank.eq(Mux(mode, bank2[0:2] << 5, 0)),
                ).Else(
                    bank.eq(Cat(rom_bank[0:5], bank2[0:2])),
                )
            ).Else(
                bank.eq(Mux(a[14], rom_bank, 0)),
            ),
            self.rom_addr.eq(Cat(a[0:14], bank & self.rom_mask)),
        ]

        # RAM mapping.
        ram_bank = Signal(4)
        self.comb += [
            If(self.mbc == MBC_MBC1,
                ram_bank.eq(Mux(mode, bank2[0:2], 0)),
            ).Elif((self.mbc == MBC_MBC3) & bank2[3],
                ram_bank.eq(0), # RTC registers (not supported).
            ).Else(
                ram_bank.eq(bank2),
            ),
            If(self.mbc == MBC_MBC2,
                self.ram_addr.eq(a[0:9]),
            ).Else(
                self.ram_addr.eq(Cat(a[0:13], ram_bank & self.ram_mask)),
            ),
            self.ram_enabled.eq(ram_enable & (self.mbc != MBC_NONE)),
        ]

# Virtual Cartridge --------------------------------------------------------------------------------

class VirtualCart(LiteXModule):
    """
    Virtual cartridge (hClk: Game Boy bus, cache; xClk: PSRAM memory port, see module docstring).

    `port` is a memory port of the PSRAM arbiter (xClk), `port_dout` its shared read data.
    """
    def __init__(self, port, port_dout, rom_base=ROM_BASE, ram_base=RAM_BASE, lines=256):
        # Configuration (quasi-static, hClk).
        self.enable   = Signal()
        self.mbc_type = Signal(3)
        self.rom_mask = Signal(9)
        self.ram_mask = Signal(4)
        self.flush    = Signal() # Pulse: invalidate the cache.
        # Game Boy bus (hClk).
        self.a        = Signal(16)
        self.rd       = Signal()
        self.wr       = Signal()
        self.din      = Signal(8) # From the CPU.
        self.dma      = Signal()
        self.reset    = Signal()
        self.data     = Signal(8) # To the CPU.
        self.wait     = Signal()
        # Statistics (hClk).
        self.misses   = Signal(32)
        self.stalls   = Signal(32)
        self.reads    = Signal(32) # ROM/RAM read accesses (rising edges).
        self.state    = Signal(4)

        # # #

        line_bits = log2_int(lines)
        tag_bits  = 23 - 4 - line_bits

        # MBC --------------------------------------------------------------------------------------
        self.mbc = mbc = ClockDomainsRenamer("hclk")(MBC())
        self.comb += [
            mbc.mbc.eq(self.mbc_type),
            mbc.rom_mask.eq(self.rom_mask),
            mbc.ram_mask.eq(self.ram_mask),
            mbc.reset.eq(self.reset),
            mbc.a.eq(self.a),
            mbc.wr.eq(self.wr),
            mbc.din.eq(self.din),
        ]

        # Accesses ---------------------------------------------------------------------------------
        a       = self.a
        read    = self.rd | self.dma
        sel_rom = (a[15] == 0)
        sel_ram = (a[13:16] == 0b101) & mbc.ram_enabled
        paddr   = Signal(23) # PSRAM byte address.
        rom_rd  = Signal()
        ram_rd  = Signal()
        ram_wr  = Signal()
        self.comb += [
            paddr.eq(Mux(sel_rom, rom_base + mbc.rom_addr, ram_base + mbc.ram_addr)),
            rom_rd.eq(self.enable & read & sel_rom),
            ram_rd.eq(self.enable & read & sel_ram),
            ram_wr.eq(self.enable & self.wr & sel_ram),
        ]

        # Cache ------------------------------------------------------------------------------------
        # Tags (valid + tag), data (16-bit words, single hClk port: fills, cartridge RAM writes and
        # reads, never at the same time: the core is frozen during fills/writes).
        tags      = Memory(1 + tag_bits, lines)
        data      = Memory(16, lines*8)
        tags_rd   = tags.get_port(clock_domain="hclk")
        tags_wr   = tags.get_port(write_capable=True, clock_domain="hclk")
        data_hclk = data.get_port(write_capable=True, we_granularity=8, clock_domain="hclk")
        self.specials += tags, data, tags_rd, tags_wr, data_hclk

        # Line fill words (xClk -> hClk).
        self.fill_cdc = fill_cdc = stream.ClockDomainCrossing([("data", 16)],
            cd_from = "xclk",
            cd_to   = "hclk",
            depth   = 8,
        )
        fill_word = Signal(3)

        # Lookup: tags read for paddr, result valid the next cycle if paddr is unchanged.
        paddr_d  = Signal(23)
        stable   = Signal()
        hit      = Signal()
        miss     = Signal()
        fsm_fill = Signal()
        req_addr = Signal(23)
        self.sync.hclk += paddr_d.eq(paddr)
        self.comb += [
            tags_rd.adr.eq(paddr[4:4 + line_bits]),
            stable.eq(paddr_d == paddr),
            hit.eq(stable & tags_rd.dat_r[0] & (tags_rd.dat_r[1:] == paddr[4 + line_bits:23])),
            miss.eq(stable & ~hit),
            If(fsm_fill,
                data_hclk.adr.eq(Cat(fill_word, req_addr[4:4 + line_bits])),
            ).Else(
                data_hclk.adr.eq(paddr[1:4 + line_bits]),
            )
        ]

        # Read data (MBC2: 4-bit RAM, upper bits read as 1, disabled RAM reads as $FF).
        byte = Signal(8)
        self.comb += byte.eq(Mux(paddr[0], data_hclk.dat_r[8:16], data_hclk.dat_r[0:8]))
        self.comb += [
            If(sel_rom,
                self.data.eq(byte),
            ).Elif(sel_ram,
                If(self.mbc_type == MBC_MBC2,
                    self.data.eq(Cat(byte[0:4], Constant(0xf, 4))),
                ).Else(
                    self.data.eq(byte),
                )
            ).Else(
                self.data.eq(0xff),
            )
        ]

        # Requests (hClk -> xClk): line fill or 16-bit word write, one at a time.
        req_toggle  = Signal()
        req_write   = Signal()
        req_data    = Signal(16)
        done_toggle = Signal()
        done_sync   = Signal()
        done_seen   = Signal()
        self.specials += MultiReg(done_toggle, done_sync, "hclk")

        # hClk FSM.
        flush_index = Signal(line_bits)
        miss_issue  = Signal()
        wr_done     = Signal() # Cartridge RAM write of the current wr pulse done.
        wr_done_set = Signal()
        self.sync.hclk += If(wr_done_set, wr_done.eq(1)).Elif(~self.wr, wr_done.eq(0))
        self.fsm = fsm = ClockDomainsRenamer("hclk")(FSM(reset_state="FLUSH"))
        fsm.act("FLUSH",
            self.wait.eq(1),
            tags_wr.adr.eq(flush_index),
            tags_wr.dat_w.eq(0),
            tags_wr.we.eq(1),
            NextValue(flush_index, flush_index + 1),
            If(flush_index == (lines - 1),
                NextState("TAG-WAIT"),
            )
        )
        fsm.act("IDLE",
            If(self.flush,
                NextValue(flush_index, 0),
                NextState("FLUSH"),
            ).Elif((rom_rd | ram_rd | (ram_wr & ~wr_done)) & miss,
                # Miss: line fill.
                self.wait.eq(1),
                miss_issue.eq(1),
                NextValue(req_write, 0),
                NextValue(req_addr, Cat(Constant(0, 4), paddr[4:])),
                NextValue(req_toggle, ~req_toggle),
                NextValue(fill_word, 0),
                NextState("FILL"),
            ).Elif(ram_wr & ~wr_done & hit,
                # Cartridge RAM write (hit): word read from the cache.
                self.wait.eq(1),
                NextState("WRITE"),
            )
        )
        fsm.act("WRITE",
            # Cache byte update and word write through.
            self.wait.eq(1),
            data_hclk.dat_w.eq(Replicate(self.din, 2)),
            data_hclk.we.eq(Mux(paddr[0], 0b10, 0b01)),
            NextValue(req_write, 1),
            NextValue(req_addr, Cat(Constant(0, 1), paddr[1:])),
            NextValue(req_data, Mux(paddr[0],
                Cat(data_hclk.dat_r[0:8], self.din),
                Cat(self.din, data_hclk.dat_r[8:16]))),
            NextValue(req_toggle, ~req_toggle),
            wr_done_set.eq(1),
            NextState("WAIT"),
        )
        fsm.act("FILL",
            # Line words written to the cache as they arrive.
            self.wait.eq(1),
            fsm_fill.eq(1),
            fill_cdc.source.ready.eq(1),
            data_hclk.dat_w.eq(fill_cdc.source.data),
            data_hclk.we.eq(Replicate(fill_cdc.source.valid, 2)),
            If(fill_cdc.source.valid,
                NextValue(fill_word, fill_word + 1),
                If(fill_word == 7,
                    NextState("FILL-DONE"),
                )
            )
        )
        fsm.act("FILL-DONE",
            # PSRAM access end (done), then tag.
            self.wait.eq(1),
            If(done_sync != done_seen,
                NextValue(done_seen, done_sync),
                NextState("TAG"),
            )
        )
        fsm.act("WAIT",
            # Write through end.
            self.wait.eq(1),
            If(done_sync != done_seen,
                NextValue(done_seen, done_sync),
                NextState("IDLE"),
            )
        )
        fsm.act("TAG",
            # Filled line valid.
            self.wait.eq(1),
            tags_wr.adr.eq(req_addr[4:4 + line_bits]),
            tags_wr.dat_w.eq(Cat(1, req_addr[4 + line_bits:23])),
            tags_wr.we.eq(1),
            NextState("TAG-WAIT"),
        )
        fsm.act("TAG-WAIT",
            # Tags/data read after the write (lookup valid in IDLE).
            self.wait.eq(1),
            NextState("IDLE"),
        )

        # Statistics.
        access   = Signal()
        access_d = Signal()
        self.comb += access.eq(rom_rd | ram_rd)
        self.sync.hclk += access_d.eq(access)
        for i, name in enumerate(["FLUSH", "IDLE", "WRITE", "FILL", "FILL-DONE", "WAIT", "TAG", "TAG-WAIT"]):
            self.comb += If(fsm.ongoing(name), self.state.eq(i))
        self.sync.hclk += [
            If(access & ~access_d, self.reads.eq(self.reads + 1)),
            If(self.wait, self.stalls.eq(self.stalls + 1)),
            If(miss_issue, self.misses.eq(self.misses + 1)),
        ]

        # xClk: PSRAM Port -------------------------------------------------------------------------
        req_sync = Signal()
        req_seen = Signal()
        busy     = Signal()
        self.specials += MultiReg(req_toggle, req_sync, "xclk")
        self.comb += [
            port.rnw.eq(~req_write),
            port.addr.eq(req_addr),
            port.burst_length.eq(Mux(req_write, 2, 16)),
            port.din.eq(req_data),
            fill_cdc.sink.valid.eq(port.dout_valid & busy & ~req_write),
            fill_cdc.sink.data.eq(port_dout),
        ]
        self.sync.xclk += [
            port.request.eq(0),
            If(~busy & (req_sync != req_seen),
                busy.eq(1),
                req_seen.eq(req_sync),
                port.request.eq(1),
            ),
            If(busy & port.done,
                busy.eq(0),
                done_toggle.eq(~done_toggle),
            ),
        ]

# Virtual Cartridge CSRs ---------------------------------------------------------------------------

class VirtualCartCSR(LiteXModule):
    """Virtual cartridge control/status (sys domain) for a VirtualCart (hClk)."""
    def __init__(self, vcart):
        self.control = CSRStorage(fields=[
            CSRField("enable",   size=1, offset=0,              description="Serve the ROM/RAM from the PSRAM."),
            CSRField("hold",     size=1, offset=1,              description="Hold the Game Boy in reset."),
            CSRField("flush",    size=1, offset=2,  pulse=True, description="Invalidate the cache."),
            CSRField("mbc",      size=3, offset=4,              description="MBC: 0: None, 1: MBC1, 2: MBC2, 3: MBC3, 5: MBC5."),
            CSRField("rom_mask", size=9, offset=8,              description="ROM bank mask (16KB banks)."),
            CSRField("ram_mask", size=4, offset=20,             description="RAM bank mask (8KB banks)."),
        ])
        self.misses = CSRStatus(32, description="Cache misses.")
        self.stalls = CSRStatus(32, description="Game Boy stall cycles (hClk).")
        self.reads  = CSRStatus(32, description="Cartridge ROM/RAM reads.")
        self.debug  = CSRStatus(32, description="Debug: [15:0] address, 16 rd, 17 wr, 18 reset, 19 wait, [23:20] state, 24 enable.")
        self.hold   = Signal()

        # # #

        fields = self.control.fields
        self.specials += [
            MultiReg(fields.enable,   vcart.enable,   "hclk"),
            MultiReg(fields.mbc,      vcart.mbc_type, "hclk"),
            MultiReg(fields.rom_mask, vcart.rom_mask, "hclk"),
            MultiReg(fields.ram_mask, vcart.ram_mask, "hclk"),
            MultiReg(fields.hold,     self.hold,      "hclk"),
            MultiReg(vcart.misses,    self.misses.status),
            MultiReg(vcart.stalls,    self.stalls.status),
            MultiReg(vcart.reads,     self.reads.status),
            MultiReg(Cat(vcart.a, vcart.rd, vcart.wr, vcart.reset, vcart.wait, vcart.state,
                vcart.enable), self.debug.status),
        ]
        # Flush pulse (sys -> hClk: toggle).
        flush_toggle = Signal()
        flush_sync   = Signal()
        flush_seen   = Signal()
        self.sync += If(fields.flush, flush_toggle.eq(~flush_toggle))
        self.specials += MultiReg(flush_toggle, flush_sync, "hclk")
        self.sync.hclk += flush_seen.eq(flush_sync)
        self.comb += vcart.flush.eq(flush_sync != flush_seen)

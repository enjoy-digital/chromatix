#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
Virtual cartridge: the Game Boy ROM and cartridge RAM are served from the PSRAM (loaded by the ESP32
or the host) in place of the physical cartridge.

- MBC: ROM only, MBC1 (MBC1M multicart), MBC2, MBC3 (MBC30, no RTC: RTC registers read as $FF), MBC5,
  HuC1 bank registers -> ROM/RAM addresses (same selection as ChroMagic's virtual cartridge).
- Cache (hClk): direct mapped, 16-byte lines (tags and data in block RAMs, synchronous reads: the
  lookup of an address is known one cycle later, well before the core samples the data at the end
  of its M-cycle). Hits are served without delay; on a miss (or a cartridge RAM write), the Game Boy
  core is frozen (wait, speedcontrol) until the line is fetched from the PSRAM (xClk, highest
  priority memory port).
- Cartridge RAM writes update the cache and are written through to the PSRAM (16-bit words), and
  set save_dirty (cleared when a snapshot of block 0 is requested).
- Save snapshots: a 1KB block of the cartridge RAM is copied from the PSRAM to a buffer read over
  QSPI by the ESP32 (snapshot_* signals, ChroMagic protocol).
- Quiesce: the Game Boy core is frozen (wait) once pending cartridge RAM writes are done.

PSRAM layout (ChroMagic): ROM at rom_base (up to 4MB), cartridge RAM at ram_base (128KB).
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
MBC_MBC5 = 4
MBC_HUC1 = 5

ROM_BASE = 0x020000
RAM_BASE = 0x420000

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
        self.mbc1m       = Signal() # MBC1 multicart (4-bit ROM bank register).
        self.mbc30       = Signal() # MBC30 (8-bit ROM bank register).
        self.has_ram     = Signal(reset=1)
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
        self.ram_ff      = Signal() # Cartridge RAM area reads $FF (MBC3 RTC registers selected).
        self.ir          = Signal() # HuC1 IR mode (cartridge RAM area reads $C0).

        # # #

        ram_enable = Signal()
        rom_bank   = Signal(9, reset=1) # 16KB bank in $4000-$7FFF.
        bank2      = Signal(4)          # MBC1: upper ROM/RAM bank bits, MBC3/5/HuC1: RAM bank.
        mode       = Signal()           # MBC1 banking mode, MBC3 RTC registers selected.
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
                        1: If(self.mbc30,
                            rom_bank.eq(Mux(d == 0, 1, d)),
                        ).Else(
                            rom_bank.eq(Mux(d[0:7] == 0, 1, d[0:7])),
                        ),
                        2: If(d[3],
                            mode.eq(1), # RTC registers ($08-$0C).
                        ).Else(
                            mode.eq(0),
                            bank2.eq(d[0:3]),
                        ),
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
                    MBC_HUC1: Case(a[13:15], {
                        0: mode.eq(d[0:4] == 0xe), # IR mode.
                        1: rom_bank.eq(Mux(d[0:6] == 0, 1, d[0:6])),
                        2: bank2.eq(d[0:2]),
                        3: [],
                    }),
                })
            )
        ]

        # ROM mapping.
        bank = Signal(9)
        self.comb += [
            If(self.mbc == MBC_NONE,
                bank.eq(a[14]),
            ).Elif((self.mbc == MBC_MBC1) & self.mbc1m,
                If(~a[14],
                    bank.eq(Mux(mode, bank2[0:2] << 4, 0)),
                ).Else(
                    bank.eq(Cat(rom_bank[0:4], bank2[0:2])),
                )
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
            ).Else(
                ram_bank.eq(bank2),
            ),
            If(self.mbc == MBC_MBC2,
                self.ram_addr.eq(a[0:9]),
            ).Else(
                self.ram_addr.eq(Cat(a[0:13], ram_bank & self.ram_mask)),
            ),
            Case(self.mbc, {
                MBC_NONE:  self.ram_enabled.eq(0),
                MBC_MBC2:  self.ram_enabled.eq(ram_enable), # Internal RAM.
                MBC_MBC3:  self.ram_enabled.eq(ram_enable & ~mode & self.has_ram),
                MBC_HUC1:  self.ram_enabled.eq(~mode & self.has_ram),
                "default": self.ram_enabled.eq(ram_enable & self.has_ram),
            }),
            self.ram_ff.eq((self.mbc == MBC_MBC3) & ram_enable & mode),
            self.ir.eq((self.mbc == MBC_HUC1) & mode),
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
        self.mbc1m    = Signal()
        self.mbc30    = Signal()
        self.has_ram  = Signal(reset=1)
        self.rom_mask = Signal(9)
        self.ram_mask = Signal(4)
        self.flush    = Signal() # Pulse: invalidate the cache (also done when enabled).
        # Status / control (hClk).
        self.initialized = Signal() # Enabled and cache invalidated.
        self.save_dirty  = Signal() # Cartridge RAM written since the last block 0 snapshot.
        self.quiesce     = Signal() # Freeze the Game Boy core...
        self.quiesced    = Signal() # ...once pending cartridge RAM writes are done.
        # Save snapshots: request (toggle, any domain: block/sequence stable when toggled), ready
        # and sequence (xClk), 1KB buffer read by bytes (snapshot_addr/data, "qspi" domain).
        self.snapshot_request  = Signal()
        self.snapshot_block    = Signal(7)
        self.snapshot_sequence = Signal(16)
        self.snapshot_ready    = Signal()
        self.snapshot_ready_sequence = Signal(16)
        self.snapshot_addr     = Signal(10)
        self.snapshot_data     = Signal(8)
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
            mbc.mbc1m.eq(self.mbc1m),
            mbc.mbc30.eq(self.mbc30),
            mbc.has_ram.eq(self.has_ram),
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
            ).Elif((a[13:16] == 0b101) & mbc.ir,
                self.data.eq(0xc0), # HuC1 IR: no light detected.
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

        # Flush on enable (and on flush), initialized once done.
        enable_d    = Signal()
        flush       = Signal()
        flushed     = Signal()
        self.sync.hclk += enable_d.eq(self.enable)
        self.comb += flush.eq(self.flush | (self.enable & ~enable_d))

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
                NextValue(flushed, 1),
                NextState("TAG-WAIT"),
            )
        )
        fsm.act("IDLE",
            If(flush,
                NextValue(flushed, 0),
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

        # Initialized / quiesce.
        self.comb += [
            self.initialized.eq(self.enable & flushed & ~flush),
            If(self.quiesce, self.wait.eq(1)),
        ]
        self.sync.hclk += self.quiesced.eq(self.quiesce & fsm.ongoing("IDLE") & ~flush)

        # Save dirty: set on cartridge RAM writes, cleared on block 0 snapshot requests.
        snapshot_request_h = Signal()
        snapshot_seen_h    = Signal()
        snapshot_block_h   = Signal(7)
        self.specials += [
            MultiReg(self.snapshot_request, snapshot_request_h, "hclk"),
            MultiReg(self.snapshot_block,   snapshot_block_h,   "hclk"),
        ]
        self.sync.hclk += [
            snapshot_seen_h.eq(snapshot_request_h),
            If(~self.enable,
                self.save_dirty.eq(0),
            ).Elif(fsm.ongoing("WRITE"),
                self.save_dirty.eq(1),
            ).Elif((snapshot_request_h != snapshot_seen_h) & (snapshot_block_h == 0),
                self.save_dirty.eq(0),
            )
        ]

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
        # Cache requests (line fills/word writes) first, then save snapshots (1KB read bursts).
        req_sync = Signal()
        req_seen = Signal()
        busy     = Signal()
        self.specials += MultiReg(req_toggle, req_sync, "xclk")

        snap_sync     = Signal()
        snap_seen     = Signal()
        snap_block    = Signal(7)
        snap_sequence = Signal(16)
        snap_pending  = Signal()
        snap_busy     = Signal()
        snap_word     = Signal(9)
        self.specials += [
            MultiReg(self.snapshot_request,  snap_sync,     "xclk"),
            MultiReg(self.snapshot_block,    snap_block,    "xclk"),
            MultiReg(self.snapshot_sequence, snap_sequence, "xclk"),
        ]
        snap_mem   = Memory(16, 512)
        snap_wr    = snap_mem.get_port(write_capable=True, clock_domain="xclk")
        snap_rd    = snap_mem.get_port(clock_domain="qspi")
        snap_hi    = Signal()
        self.specials += snap_mem, snap_wr, snap_rd
        self.sync.qspi += snap_hi.eq(self.snapshot_addr[0])
        self.comb += [
            snap_wr.adr.eq(snap_word),
            snap_wr.dat_w.eq(port_dout),
            snap_wr.we.eq(snap_busy & port.dout_valid),
            snap_rd.adr.eq(self.snapshot_addr[1:]),
            self.snapshot_data.eq(Mux(snap_hi, snap_rd.dat_r[8:16], snap_rd.dat_r[0:8])),
        ]

        self.comb += [
            If(snap_busy,
                port.rnw.eq(1),
                port.addr.eq(ram_base + Cat(Constant(0, 10), snap_block)),
                port.burst_length.eq(1024),
            ).Else(
                port.rnw.eq(~req_write),
                port.addr.eq(req_addr),
                port.burst_length.eq(Mux(req_write, 2, 16)),
            ),
            port.din.eq(req_data),
            fill_cdc.sink.valid.eq(port.dout_valid & busy & ~req_write),
            fill_cdc.sink.data.eq(port_dout),
        ]
        self.sync.xclk += [
            port.request.eq(0),
            If(snap_sync != snap_seen,
                snap_seen.eq(snap_sync),
                snap_pending.eq(1),
                self.snapshot_ready.eq(0),
            ),
            If(~busy & ~snap_busy & (req_sync != req_seen),
                busy.eq(1),
                req_seen.eq(req_sync),
                port.request.eq(1),
            ).Elif(~busy & ~snap_busy & snap_pending & (snap_sync == snap_seen),
                snap_busy.eq(1),
                snap_pending.eq(0),
                snap_word.eq(0),
                port.request.eq(1),
            ),
            If(busy & port.done,
                busy.eq(0),
                done_toggle.eq(~done_toggle),
            ),
            If(snap_busy & port.dout_valid,
                snap_word.eq(snap_word + 1),
            ),
            If(snap_busy & port.done,
                snap_busy.eq(0),
                self.snapshot_ready.eq(1),
                self.snapshot_ready_sequence.eq(snap_sequence),
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
            CSRField("mbc",      size=3, offset=4,              description="MBC: 0: None, 1: MBC1, 2: MBC2, 3: MBC3, 4: MBC5, 5: HuC1."),
            CSRField("rom_mask", size=9, offset=8,              description="ROM bank mask (16KB banks)."),
            CSRField("ram_mask", size=4, offset=20,             description="RAM bank mask (8KB banks)."),
            CSRField("mbc1m",    size=1, offset=24,             description="MBC1 multicart."),
            CSRField("mbc30",    size=1, offset=25,             description="MBC30 (8-bit ROM bank register)."),
            CSRField("has_ram",  size=1, offset=26,             description="Cartridge RAM present."),
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
            MultiReg(fields.mbc1m,    vcart.mbc1m,    "hclk"),
            MultiReg(fields.mbc30,    vcart.mbc30,    "hclk"),
            MultiReg(fields.has_ram,  vcart.has_ram,  "hclk"),
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

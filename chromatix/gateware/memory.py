#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: GPL-3.0-only
# Derived from ModRetro's oss-chromatic-console-fpga (GPL-3.0).

from functools import reduce
from operator import or_

from migen import *
from migen.genlib.cdc import MultiReg, PulseSynchronizer

from litex.gen import *

from litex.soc.interconnect import stream
from litex.soc.interconnect import wishbone
from litex.soc.interconnect.csr import *

from litex.soc.cores.ram.opi_psram import OPIPSRAMPHY, OPIPSRAMCore

# Memory Port --------------------------------------------------------------------------------------

def memory_port_layout():
    return [
        # Requester -> Controller.
        ("request",      1),
        ("rnw",          1),
        ("addr",        23),
        ("din",         16),
        ("burst_length", 11), # In bytes.
        # Controller -> Requester.
        ("write_next",   1),
        ("done",         1),
        ("dout_valid",   1),
    ]

# Multi-Port RAM Controller ------------------------------------------------------------------------

class MultiPortRAMCtrl(LiteXModule):
    """
    Round-robin arbiter in front of the PSRAM controller (ported from MultiPortRamCtrl.vhd).

    Requests are latched; when idle, the highest pending port is served, except when the round-robin
    pointer points to a latched request. The granted port gets write_next/dout_valid/done until the
    burst completes. Read data (dout) and ready are shared by all ports.
    """
    def __init__(self, nports):
        self.reset = Signal()
        self.ports = ports = [Record(memory_port_layout(), name=f"port{i}") for i in range(nports)]
        self.ready = Signal()
        self.dout  = Signal(16)

        # PSRAM controller interface.
        self.req_read     = req_read     = Signal()
        self.req_write    = req_write    = Signal()
        self.addr         = addr         = Signal(23)
        self.din          = din          = Signal(16)
        self.burst_length = burst_length = Signal(11)
        self.write_next   = write_next   = Signal()
        self.done         = done         = Signal()
        self.dout_valid   = dout_valid   = Signal()

        # # #

        waitram        = Signal()
        rrb            = Signal(max=nports)
        latched        = Signal(nports)
        last_index     = Signal(max=nports)
        active_request = Signal()
        active_index   = Signal(max=nports)

        requests = Cat(*[port.request for port in ports])
        rnws     = Array(port.rnw          for port in ports)
        addrs    = Array(port.addr         for port in ports)
        dins     = Array(port.din          for port in ports)
        lengths  = Array(port.burst_length for port in ports)

        # Request selection: highest pending port, round-robin pointer has priority (if latched).
        self.comb += active_request.eq((requests | latched) != 0)
        self.comb += active_index.eq(0)
        for i in range(nports):
            self.comb += If(requests[i] | latched[i], active_index.eq(i))
        self.comb += If(Array(latched[i] for i in range(nports))[rrb], active_index.eq(rrb))

        # Granted port routing.
        self.comb += din.eq(dins[last_index])
        for i, port in enumerate(ports):
            self.comb += If(waitram & (last_index == i),
                port.done.eq(done),
                port.dout_valid.eq(dout_valid),
                port.write_next.eq(write_next),
            )

        # Arbitration.
        self.sync += [
            req_read.eq(0),
            req_write.eq(0),
            latched.eq(latched | requests),
            If(self.reset,
                waitram.eq(0),
                latched.eq(0),
            ).Elif(~waitram,
                last_index.eq(active_index),
                If(rrb == (nports - 1),
                    rrb.eq(0),
                ).Else(
                    rrb.eq(rrb + 1),
                ),
                If(active_request & self.ready,
                    waitram.eq(1),
                    *[If(active_index == i, latched[i].eq(0)) for i in range(nports)],
                    If(rnws[active_index],
                        req_read.eq(1),
                    ).Else(
                        req_write.eq(1),
                    ),
                    burst_length.eq(lengths[active_index]),
                    addr.eq(addrs[active_index]),
                ),
            ).Else(
                If(done,
                    waitram.eq(0),
                ),
            ),
        ]

# PSRAM Port Adapter -------------------------------------------------------------------------------

class PSRAMPortAdapter(LiteXModule):
    """
    Arbiter PSRAM interface -> LiteX OPIPSRAMCore native port.

    A request (req_read/req_write pulse, addr/burst_length held) is one native access of
    burst_length/2 words. Writes: din is the current word, write_next requests the next one (not
    after the last word), done follows the last word. Reads: dout_valid per word, done with the last
    one.
    """
    def __init__(self, ctrl, core):
        # # #

        pending = Signal()
        we      = Signal()
        busy    = Signal()
        count   = Signal(11)
        last    = Signal()
        done    = Signal()

        self.comb += [
            ctrl.ready.eq(core.ready),
            last.eq(count == 1),
            # Command.
            core.cmd.valid.eq(pending),
            core.cmd.we.eq(we),
            core.cmd.addr.eq(Cat(0, ctrl.addr[1:])),
            core.cmd.len.eq(ctrl.burst_length[1:]),
            # Write data.
            core.wdata.valid.eq(busy & we),
            core.wdata.data.eq(ctrl.din),
            core.wdata.we.eq(0b11),
            ctrl.write_next.eq(core.wdata.valid & core.wdata.ready & ~last),
            # Read data.
            core.rdata.ready.eq(1),
            ctrl.dout_valid.eq(core.rdata.valid),
            ctrl.dout.eq(core.rdata.data),
            ctrl.done.eq(done | (core.rdata.valid & last)),
        ]
        self.sync += [
            done.eq(0),
            If(ctrl.req_read | ctrl.req_write,
                pending.eq(1),
                we.eq(ctrl.req_write),
            ),
            If(core.cmd.valid & core.cmd.ready,
                pending.eq(0),
                busy.eq(1),
                count.eq(ctrl.burst_length[1:]),
            ),
            If((core.wdata.valid & core.wdata.ready) | core.rdata.valid,
                count.eq(count - 1),
                If(last,
                    busy.eq(0),
                    done.eq(we),
                )
            ),
        ]

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

# Burst Write FIFO ---------------------------------------------------------------------------------

class BurstWriteFIFO(LiteXModule):
    """
    16-bit asynchronous FIFO with "standard" (non-FWFT) read behaviour: q is updated with the next
    word on the cycle following a read enable (replaces the encrypted Gowin fifo1k IP).
    """
    def __init__(self, cd_write, cd_read, depth=1024, with_reset=False):
        self.we    = Signal()
        self.data  = Signal(16)
        self.re    = Signal()
        self.q     = Signal(16)
        self.empty = Signal()
        if with_reset:
            self.reset_write = Signal() # In cd_write.
            self.reset_read  = Signal() # In cd_read.

        # # #

        fifo = stream.AsyncFIFO([("data", 16)], depth=depth, buffered=True)
        if with_reset:
            fifo = ResetInserter(["write", "read"])(fifo)
            self.comb += [
                fifo.reset_write.eq(self.reset_write),
                fifo.reset_read.eq(self.reset_read),
            ]
        fifo = ClockDomainsRenamer({"write": cd_write, "read": cd_read})(fifo)
        self.fifo = fifo

        self.comb += [
            fifo.sink.valid.eq(self.we),
            fifo.sink.data.eq(self.data),
            fifo.source.ready.eq(self.re),
            self.empty.eq(~fifo.source.valid),
        ]
        sync_read  = getattr(self.sync, cd_read)
        sync_read += If(self.re & fifo.source.valid, self.q.eq(fifo.source.data))

# GB Burst Write -----------------------------------------------------------------------------------

class GBBurstWrite(LiteXModule):
    """
    Game Boy video line -> PSRAM burst write (ported from gb_burst_write.v).

    Pixels are written in the hClk domain and a burst request is issued in the xClk domain at each
    new line.
    """
    def __init__(self, port):
        self.ram_ready = Signal() # xClk.
        self.vsync     = Signal() # hClk.
        self.new_line  = Signal() # hClk.
        self.address   = Signal(23)
        self.write     = Signal()
        self.data      = Signal(16)

        # # #

        # hClk: line toggle / RAM ready sampling and FIFO reset on VSync rising edge.
        h_line_toggle = Signal()
        h_ram_ready   = Signal()
        h_vsync_d     = Signal()
        h_fifo_rst    = Signal()
        self.sync.hclk += [
            If(self.new_line,
                h_ram_ready.eq(self.ram_ready),
                h_line_toggle.eq(~h_line_toggle),
            ),
            h_vsync_d.eq(self.vsync),
        ]
        self.comb += h_fifo_rst.eq(~h_vsync_d & self.vsync)

        # FIFO (hClk -> xClk).
        self.fifo = fifo = BurstWriteFIFO("hclk", "xclk", with_reset=True)
        self.fifo_rst_sync = fifo_rst_sync = PulseSynchronizer("hclk", "xclk")
        self.comb += [
            fifo_rst_sync.i.eq(h_fifo_rst),
            fifo.reset_write.eq(h_fifo_rst),
            fifo.reset_read.eq(fifo_rst_sync.o),
            fifo.we.eq(self.write & h_ram_ready),
            fifo.data.eq(self.data),
            fifo.re.eq(port.write_next | port.request),
            port.din.eq(fifo.q),
        ]

        # xClk: burst request on each new line.
        x_line_sr = Signal(4)
        self.sync.xclk += [
            x_line_sr.eq(Cat(h_line_toggle, x_line_sr[:3])),
            port.request.eq(0),
            If(x_line_sr[3] != x_line_sr[2],
                port.request.eq(self.ram_ready & ~fifo.empty),
                port.addr.eq(self.address),
            ),
        ]

# QSPI Burst Write ---------------------------------------------------------------------------------

class QSPIBurstWrite(LiteXModule):
    """
    ESP32 QSPI memory-mapped transfer -> PSRAM burst write (ported from mm_burst_write.v).

    Data is written on QSPI_CLK falling edges ("qspi_n" domain), the burst request is issued in the
    xClk domain when QSPI_CS rises (end of transfer).
    """
    def __init__(self, port):
        self.ram_ready  = Signal() # xClk.
        self.cs         = Signal()
        self.address    = Signal(32)
        self.data_valid = Signal()
        self.data       = Signal(16)

        # # #

        # FIFO (QSPI_CLK falling edge -> xClk).
        self.fifo = fifo = BurstWriteFIFO("qspi_n", "xclk")
        self.comb += [
            fifo.we.eq(self.data_valid),
            fifo.data.eq(self.data),
            fifo.re.eq(port.write_next | port.request),
            port.din.eq(fifo.q),
            port.addr.eq(self.address[:23]),
        ]

        # xClk: burst request on QSPI_CS rising edge.
        x_cs_sr = Signal(4)
        self.sync.xclk += [
            x_cs_sr.eq(Cat(self.cs, x_cs_sr[:3])),
            port.request.eq(0),
            If(x_cs_sr[2:4] == 0b01,
                port.request.eq(self.ram_ready),
            ),
        ]

# QSPI Slave ---------------------------------------------------------------------------------------

class QSPISlave(LiteXModule):
    """
    ESP32 QSPI slave (port of qspi_slave.v): 1 command bit, 10 length bits and 32 address bits on
    MOSI, then data bytes on the 4 lines (2 clocks per byte), assembled into 16-bit words.

    The state registers are asynchronously reset by CS (QSPI_CLK stops while CS is high): they are
    implemented with GW5A DFFC (async clear) flip-flops. Runs in the "qspi" domain (QSPI_CLK rising).
    """
    def __init__(self, pads):
        self.menu_init  = Signal()
        self.data_valid = Signal()
        self.data       = Signal(16)
        self.address    = Signal(32)

        # # #

        cs   = pads.cs_n
        pins = Cat(pads.mosi, pads.miso, pads.wp_n, pads.hd)

        # Async cleared (CS) registers: next values computed combinatorially.
        cycle_count = Signal(8)
        cycle_phase = Signal()
        valid       = Signal()
        valid_phase = Signal()
        async_regs  = {}
        for name, sig in [("cycle_count", cycle_count), ("cycle_phase", cycle_phase),
                          ("valid", valid), ("valid_phase", valid_phase)]:
            nxt = Signal(len(sig), name=f"{name}_next")
            self.comb += nxt.eq(sig)
            async_regs[name] = nxt
            for i in range(len(sig)):
                self.specials += Instance("DFFC", name=f"qspi_{name}_dffc{i}",
                    i_D     = nxt[i],
                    i_CLK   = ClockSignal("qspi"),
                    i_CLEAR = cs,
                    o_Q     = sig[i],
                )

        # Plain registers (only updated while CS is low).
        pins_r1     = Signal(4)
        data_byte   = Signal(8)
        data_byte_r = Signal(8)
        menu_init1  = Signal()
        menu_init2  = Signal()

        self.comb += [
            If(cycle_count <= 50,
                async_regs["cycle_count"].eq(cycle_count + 1),
            ),
            If(cycle_count >= 46,
                async_regs["cycle_phase"].eq(~cycle_phase),
                async_regs["valid"].eq(cycle_phase),
            ),
            If(valid,
                async_regs["valid_phase"].eq(~valid_phase),
            ),
        ]
        self.sync.qspi += If(~cs,
            # Address.
            If((cycle_count >= 11) & (cycle_count <= 42),
                self.address.eq(Cat(pads.mosi, self.address[:31])),
            ),
            # Menu init: second transfer to address 0.
            If((cycle_count == 43) & (self.address == 0),
                menu_init1.eq(1),
                If(menu_init1,
                    menu_init2.eq(1),
                ),
            ),
            # Data.
            If(cycle_count >= 46,
                If(cycle_phase,
                    data_byte.eq(Cat(pins, pins_r1)),
                ).Else(
                    pins_r1.eq(pins),
                ),
            ),
            If(valid,
                data_byte_r.eq(data_byte),
            ),
        )
        self.comb += [
            self.menu_init.eq(menu_init2),
            self.data_valid.eq(valid & valid_phase),
            self.data.eq(Cat(data_byte_r, data_byte)),
        ]

# Line Reader --------------------------------------------------------------------------------------

class LineReader(LiteXModule):
    """
    PSRAM line reader (port of mm_burst_read_to_stream.v).

    At each end of line / start of frame (hClk syncs seen in xClk), requests a 160-word burst read
    into a line buffer, streamed out in hClk along with the Game Boy pixels.
    """
    LINE_DEPTH = 160

    def __init__(self, port, ram_dout, base, ram_ready):
        self.valid = Signal()   # hClk.
        self.hsync = Signal()   # hClk.
        self.vsync = Signal()   # hClk.
        self.data  = Signal(16) # hClk.

        # # #

        # Line buffer (not an attribute: not exposed on the CSR bus).
        mem     = Memory(16, self.LINE_DEPTH, init=[0]*self.LINE_DEPTH)
        wr_port = mem.get_port(write_capable=True, clock_domain="xclk")
        rd_port = mem.get_port(clock_domain="hclk")
        self.specials += mem, wr_port, rd_port

        # hClk: read side.
        vsync_r1 = Signal()
        hsync_r1 = Signal()
        ra       = Signal(15)
        self.sync.hclk += [
            vsync_r1.eq(self.vsync),
            hsync_r1.eq(self.hsync),
            If(~vsync_r1 & self.vsync,
                ra.eq(0),
            ).Elif(self.valid,
                If(ra < self.LINE_DEPTH,
                    ra.eq(ra + 1),
                )
            ).Elif(~hsync_r1 & self.hsync,
                ra.eq(0),
            )
        ]
        self.comb += [
            rd_port.adr.eq(ra),
            self.data.eq(rd_port.dat_r),
        ]

        # xClk: write side.
        wa = Signal(15)
        self.comb += [
            wr_port.adr.eq(wa),
            wr_port.dat_w.eq(ram_dout),
            wr_port.we.eq(port.dout_valid),
        ]
        self.sync.xclk += [
            If(port.dout_valid,
                wa.eq(wa + 1),
            ),
            If(port.done,
                wa.eq(0),
            ),
        ]

        # xClk: burst requests.
        hsync_sr = Signal(4)
        vsync_sr = Signal(4)
        eol      = Signal()
        sof      = Signal()
        self.sync.xclk += [
            hsync_sr.eq(Cat(self.hsync, hsync_sr[:3])),
            vsync_sr.eq(Cat(self.vsync, vsync_sr[:3])),
        ]
        self.comb += [
            eol.eq(hsync_sr[2:4] == 0b10), # HSync falling edge (xHsync_sr[3:2] == 2'b10).
            sof.eq(vsync_sr[2:4] == 0b01), # VSync rising edge (xVsync_sr[3:2] == 2'b01).
        ]
        self.sync.xclk += [
            port.request.eq(0),
            If(eol | sof,
                port.request.eq(ram_ready),
            ),
            If(sof,
                port.addr.eq(base),
            ).Elif(eol,
                port.addr.eq(port.addr + 2*self.LINE_DEPTH),
            ),
        ]

# PSRAM Wishbone -----------------------------------------------------------------------------------

def _slices(signal, width, n):
    """n slices of width bits of signal (LSB first)."""
    return [signal[width*i:width*(i + 1)] for i in range(n)]

class PSRAMWishbone(LiteXModule):
    """
    Wishbone slave (sys domain) -> PSRAM burst port (xClk domain), for a CPU main RAM.

    Each access is a full burst of the bus data width (ex: 64-bit = an 8-byte line of a L2 cache in
    front of it) at `base` + adr * bytes. Only full-width writes are supported (sel is ignored), as
    done by a LiteX L2 cache. The request/acknowledge cross the domains with toggles; the address,
    data and read data are quasi-static during an access.

    synchronous: sys and xClk from the same PLL (related clocks, timing analyzed): the toggles are
    used directly (no synchronizers: lower latency).

    fetch/buffers: reads fetch the aligned block of `fetch` bus words in one burst, kept in
    `buffers` block buffers (round-robin): the next reads in a buffered block are answered without
    a PSRAM access (sequential misses of the cache in front: code, textures...). Writes go to the
    PSRAM and update a buffered copy.
    """
    def __init__(self, port, ram_dout, base=0x000000, data_width=64, synchronous=False,
        fetch=1, buffers=0):
        assert data_width % 16 == 0
        assert fetch & (fetch - 1) == 0
        assert (fetch == 1) or (buffers >= 1)
        nwords   = data_width//16
        nbytes   = data_width//8
        fbits    = log2_int(fetch)
        self.bus = bus = wishbone.Interface(data_width=data_width, address_width=32,
            addressing="word")
        # Statistics (sys): new PSRAM request (pulse), access pending.
        self.request = Signal()
        self.pending = Signal()

        # # #

        # sys: request / acknowledge.
        req_toggle = Signal()
        ack_toggle = Signal()
        ack_sync   = Signal()
        ack_seen   = Signal()
        pending    = Signal()
        miss_ack   = Signal()
        adr        = Signal(23)
        we         = Signal()
        dat_w      = Signal(data_width)
        block      = Signal(data_width*fetch)  # Read data (xClk, quasi-static at the acknowledge).
        slot       = Signal(max=max(fetch, 2)) # Bus word of the request in the block.
        tag        = Signal(len(bus.adr) - fbits)
        if synchronous:
            self.comb += ack_sync.eq(ack_toggle)
        else:
            self.specials += MultiReg(ack_toggle, ack_sync)

        # Block buffers. Lookup registered (timing): a new bus access is looked up in the first
        # cycle (hits/data registered), then answered from a buffer (hit) or requested to the PSRAM
        # (miss).
        req_tag   = bus.adr[fbits:]
        req_slot  = bus.adr[:fbits] if fbits else 0
        req_block = Cat(Constant(0, fbits), req_tag) if fbits else bus.adr
        bvalid    = [Signal(name=f"bvalid{i}") for i in range(buffers)]
        btag      = [Signal(len(tag), name=f"btag{i}") for i in range(buffers)]
        bdata     = [Signal(data_width*fetch, name=f"bdata{i}") for i in range(buffers)]
        hits      = [Signal(name=f"hit{i}") for i in range(buffers)]
        hit       = Signal()
        looked    = Signal() # Lookup done for the current bus access.
        hit_ack   = Signal()
        hit_dat   = Signal(data_width)
        victim    = Signal(max=max(buffers, 2))
        access    = Signal()
        self.comb += access.eq(bus.cyc & bus.stb & ~pending & ~hit_ack & ~miss_ack)
        if buffers:
            hit_dats = [Mux(bvalid[i] & (btag[i] == req_tag),
                Array(_slices(bdata[i], data_width, fetch))[req_slot], 0) for i in range(buffers)]
            self.sync += [
                looked.eq(access & ~looked),
                *[hits[i].eq(bvalid[i] & (btag[i] == req_tag)) for i in range(buffers)],
                hit_dat.eq(reduce(or_, hit_dats)),
            ]
            self.comb += hit.eq(reduce(or_, hits))
        else:
            self.comb += looked.eq(1)

        self.comb += [
            miss_ack.eq(pending & (ack_sync != ack_seen)),
            bus.ack.eq(miss_ack | hit_ack),
            bus.dat_r.eq(Mux(hit_ack, hit_dat, Array(_slices(block, data_width, fetch))[slot])),
            self.request.eq(access & looked & ~(~bus.we & hit)),
            self.pending.eq(pending),
        ]
        self.sync += [
            hit_ack.eq(access & looked & ~bus.we & hit),
            If(miss_ack,
                pending.eq(0),
                ack_seen.eq(ack_sync),
                # Read: keep the block.
                If(~we & (buffers > 0),
                    Case(victim, {i: [bvalid[i].eq(1), btag[i].eq(tag), bdata[i].eq(block)]
                        for i in range(buffers)}),
                    victim.eq(victim + 1 if buffers > 1 else 0),
                ),
            ).Elif(self.request,
                pending.eq(1),
                req_toggle.eq(~req_toggle),
                # Reads: whole aligned block, writes: the bus word.
                adr.eq(base + Mux(bus.we, bus.adr, req_block)*nbytes),
                we.eq(bus.we),
                dat_w.eq(bus.dat_w),
                slot.eq(req_slot),
                tag.eq(req_tag),
            ),
        ]
        # Writes: update the buffered copy.
        for i in range(buffers):
            bwords = _slices(bdata[i], data_width, fetch)
            self.sync += If(self.request & bus.we & hits[i],
                Case(req_slot, {j: bwords[j].eq(bus.dat_w) for j in range(fetch)}))

        # xClk: burst.
        req_sync = Signal()
        req_seen = Signal()
        busy     = Signal()
        index    = Signal(max=max(nwords*fetch, 2))
        words    = _slices(block, 16, nwords*fetch)
        if synchronous:
            self.comb += req_sync.eq(req_toggle)
        else:
            self.specials += MultiReg(req_toggle, req_sync, "xclk")
        self.comb += [
            port.rnw.eq(~we),
            port.addr.eq(adr),
            port.burst_length.eq(Mux(we, nbytes, nbytes*fetch)),
            port.din.eq(Array(_slices(dat_w, 16, nwords))[index[:max(log2_int(nwords), 1)]]),
        ]
        self.sync.xclk += [
            port.request.eq(0),
            If(~busy & (req_sync != req_seen),
                busy.eq(1),
                req_seen.eq(req_sync),
                index.eq(0),
                port.request.eq(1),
            ),
            If(port.write_next | port.dout_valid,
                index.eq(index + 1),
            ),
            If(port.dout_valid,
                Case(index, {i: words[i].eq(ram_dout) for i in range(nwords*fetch)}),
            ),
            If(busy & port.done,
                busy.eq(0),
                ack_toggle.eq(~ack_toggle),
            ),
        ]

# Memory Counters ----------------------------------------------------------------------------------

class MemoryCounters(LiteXModule):
    """
    CPU main RAM statistics (sys domain): accesses (L2 cache front), PSRAM requests (L2 misses and
    write-backs), cycles with a PSRAM request pending and the longest request, over the cycles since
    the last clear (control write).
    """
    def __init__(self, access, request, pending):
        self.control  = CSRStorage(fields=[
            CSRField("clear", size=1, pulse=True, description="Clear the counters."),
        ])
        self.cycles   = CSRStatus(32, description="Cycles since clear.")
        self.accesses = CSRStatus(32, description="Main RAM accesses (L2 cache front).")
        self.requests = CSRStatus(32, description="PSRAM requests (L2 misses/write-backs).")
        self.busy     = CSRStatus(32, description="Cycles with a PSRAM request pending.")
        self.latency  = CSRStatus(16, description="Longest PSRAM request (cycles).")

        # # #

        current = Signal(16)
        self.sync += [
            If(self.control.fields.clear,
                self.cycles.status.eq(0),
                self.accesses.status.eq(0),
                self.requests.status.eq(0),
                self.busy.status.eq(0),
                self.latency.status.eq(0),
            ).Else(
                self.cycles.status.eq(self.cycles.status + 1),
                If(access,  self.accesses.status.eq(self.accesses.status + 1)),
                If(request, self.requests.status.eq(self.requests.status + 1)),
                If(pending, self.busy.status.eq(self.busy.status + 1)),
            ),
            If(pending,
                current.eq(current + 1),
                If(current >= self.latency.status, self.latency.status.eq(current + 1)),
            ).Else(
                current.eq(0),
            ),
        ]

# Memory System ------------------------------------------------------------------------------------

PSRAM_CLK_FREQ = 33.55432e6*2 # xclk.

PORT_BIST  = 0 # PSRAM BIST (short test at startup).
PORT_QSPI  = 1 # ESP32 QSPI writes (menu/OSD framebuffer).
PORT_FBRD  = 2 # Game Boy framebuffer read (previous frame, for frame blending).
PORT_FBWR  = 3 # Game Boy framebuffer write.
PORT_FBOSD = 4 # OSD framebuffer read.
PORT_CPU   = 5 # CPU main RAM (optional, LiteX BIOS demo).
PORT_COUNT = 5

class MemorySystem(LiteXModule):
    """
    PSRAM memory system (replaces mem_system_top.sv).

    Clock domains: xclk (controller/arbiter), fclk (PSRAM 2x clock), hclk (video), qspi/qspi_n
    (QSPI_CLK rising/falling edges, created here).
    """
    def __init__(self, qspi_pads, psram_pads, with_bus=False, bus_base=0x400000, bus_data_width=64,
        with_vcart=False, with_frame_blend=True, with_lcd_framebuffer=False, bus_synchronous=False,
        bus_fetch=1, bus_buffers=0, psram_factory=None):
        self.reset       = Signal()
        self.menu_init   = Signal()
        self.bist_done   = Signal()
        self.bist_failed = Signal()

        # Game Boy framebuffer write (hClk).
        self.gb_new_line = Signal()
        self.gb_address  = Signal(23)
        self.gb_write    = Signal()
        self.gb_data     = Signal(16)

        # Framebuffer/OSD line streams (hClk).
        self.h_valid     = Signal()
        self.h_hsync     = Signal()
        self.h_vsync     = Signal()
        self.fb_data     = Signal(16)
        self.osd_data    = Signal(16)
        self.fb_base     = Signal(23, reset=0x10000) # Framebuffer read address (latched at vsync).

        # # #

        # QSPI_CLK rising/falling edges clock domains.
        self.cd_qspi   = ClockDomain("qspi",   reset_less=True)
        self.cd_qspi_n = ClockDomain("qspi_n", reset_less=True)
        self.comb += [
            self.cd_qspi.clk.eq(qspi_pads.clk),
            self.cd_qspi_n.clk.eq(~qspi_pads.clk),
        ]

        # Arbiter + PSRAM Controller ---------------------------------------------------------------
        # LiteX OPI PSRAM (APS6408L, 67MHz): xclk (with the memory reset) and fclk (2x) clocks.
        self.cd_psram   = ClockDomain("psram")
        self.cd_psram2x = ClockDomain("psram2x", reset_less=True)
        self.comb += [
            self.cd_psram.clk.eq(ClockSignal("xclk")),
            self.cd_psram.rst.eq(self.reset),
            self.cd_psram2x.clk.eq(ClockSignal("fclk")),
        ]
        self.ctrl  = ctrl  = ClockDomainsRenamer("xclk")(MultiPortRAMCtrl(nports=PORT_COUNT + with_bus + with_vcart))
        cd_psram = {"sys": "psram", "sys2x": "psram2x"}
        if psram_factory is None:
            self.phy   = phy   = ClockDomainsRenamer(cd_psram)(OPIPSRAMPHY(psram_pads))
            self.psram = psram = ClockDomainsRenamer(cd_psram)(OPIPSRAMCore(phy, PSRAM_CLK_FREQ))
        else:
            # PSRAM model with the core native port (simulation).
            self.psram = psram = ClockDomainsRenamer(cd_psram)(psram_factory())
        self.psram_adapter = ClockDomainsRenamer("psram")(PSRAMPortAdapter(ctrl, psram))
        ports = ctrl.ports
        self.comb += ctrl.reset.eq(self.reset)

        # BIST -------------------------------------------------------------------------------------
        self.bist = bist = ClockDomainsRenamer("xclk")(PSRAMBIST(ports[PORT_BIST], ctrl))
        self.comb += [
            bist.reset.eq(self.reset),
            self.bist_done.eq(bist.finished),
            self.bist_failed.eq(bist.failed),
        ]

        # QSPI Writes (ESP32) ----------------------------------------------------------------------
        port = ports[PORT_QSPI]
        self.qspi_slave = qspi_slave = QSPISlave(qspi_pads)
        self.qspi_write = qspi_write = QSPIBurstWrite(port)
        self.comb += [
            self.menu_init.eq(qspi_slave.menu_init),
            qspi_write.ram_ready.eq(self.bist_done),
            qspi_write.cs.eq(qspi_pads.cs_n),
            qspi_write.address.eq(qspi_slave.address),
            qspi_write.data_valid.eq(qspi_slave.data_valid),
            qspi_write.data.eq(qspi_slave.data),
            port.rnw.eq(0),
            port.burst_length.eq(1024),
        ]

        # Framebuffer / OSD Line Reads -------------------------------------------------------------
        h_vsync_d = Signal()
        h_hsync_d = Signal()
        h_valid_d = Signal()
        self.sync.hclk += [
            h_vsync_d.eq(self.h_vsync),
            h_hsync_d.eq(self.h_hsync),
            h_valid_d.eq(self.h_valid),
        ]
        h_syncs   = (self.h_vsync, self.h_hsync, self.h_valid)
        h_syncs_d = (h_vsync_d, h_hsync_d, h_valid_d)
        for name, n, base, (vsync, hsync, valid), data in [
            ("fb_reader",  PORT_FBRD,  self.fb_base, h_syncs,   self.fb_data),
            ("osd_reader", PORT_FBOSD, 0x00000,      h_syncs_d, self.osd_data),
        ]:
            port = ports[n]
            self.comb += [
                port.rnw.eq(1),
                port.burst_length.eq(320),
                port.din.eq(0),
            ]
            # Framebuffer reads only with frame blending or a CPU framebuffer (PSRAM bandwidth).
            fb_reads  = with_frame_blend or with_lcd_framebuffer
            ram_ready = self.bist_done if (fb_reads or n != PORT_FBRD) else 0
            reader = LineReader(port, ctrl.dout, base, ram_ready)
            self.add_module(name=name, module=reader)
            self.comb += [
                reader.valid.eq(valid),
                reader.hsync.eq(hsync),
                reader.vsync.eq(vsync),
                data.eq(reader.data),
            ]

        # Game Boy Framebuffer Write (frame blending) / CPU Framebuffer Write Port -----------------
        port = ports[PORT_FBWR]
        if with_frame_blend:
            self.gb_burst_write = gb_burst_write = GBBurstWrite(port)
            self.comb += [
                gb_burst_write.ram_ready.eq(self.bist_done),
                gb_burst_write.vsync.eq(self.h_vsync),
                gb_burst_write.new_line.eq(self.gb_new_line),
                gb_burst_write.address.eq(self.gb_address),
                gb_burst_write.write.eq(self.gb_write),
                gb_burst_write.data.eq(self.gb_data),
                port.rnw.eq(0),
                port.burst_length.eq(320),
            ]
        elif with_lcd_framebuffer:
            self.fb_write_port = port # CPU framebuffer writes (LCDPSRAMFramebuffer).

        # Virtual Cartridge (optional): last port, highest priority --------------------------------
        if with_vcart:
            self.vcart_port = ports[-1]

        # CPU Main RAM (optional) ------------------------------------------------------------------
        if with_bus:
            self.bus_bridge = bus_bridge = PSRAMWishbone(ports[PORT_CPU], ctrl.dout,
                base        = bus_base,
                data_width  = bus_data_width,
                synchronous = bus_synchronous,
                fetch       = bus_fetch,
                buffers     = bus_buffers,
            )
            self.bus = bus_bridge.bus

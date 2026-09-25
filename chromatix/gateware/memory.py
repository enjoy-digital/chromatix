#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *
from migen.genlib.cdc import PulseSynchronizer

from litex.gen import *

from litex.soc.interconnect import stream

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
    pointer points to a pending port. The granted port gets write_next/dout_valid/done until the
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

        # Request selection: highest pending port, round-robin pointer has priority.
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

# Memory System ------------------------------------------------------------------------------------

PORT_BIST   = 0 # PSRAM BIST (short test at startup).
PORT_QSPI   = 1 # ESP32 QSPI writes (menu/OSD framebuffer).
PORT_FBRD   = 2 # Game Boy framebuffer read (previous frame, for frame blending).
PORT_FBWR   = 3 # Game Boy framebuffer write.
PORT_FBOSD  = 4 # OSD framebuffer read.
PORT_COUNT  = 5

class MemorySystem(LiteXModule):
    """
    PSRAM memory system (replaces mem_system_top.sv).

    Clock domains: xclk (controller/arbiter), fclk (PSRAM 2x clock), hclk (video), qspi_n (QSPI_CLK
    falling edge, created here).
    """
    def __init__(self, qspi_pads, psram_pads):
        self.reset        = Signal()
        self.menu_init    = Signal()
        self.bist_done    = Signal()
        self.bist_failed  = Signal()

        # Game Boy framebuffer write (hClk).
        self.gb_new_line  = Signal()
        self.gb_address   = Signal(23)
        self.gb_write     = Signal()
        self.gb_data      = Signal(16)

        # Framebuffer/OSD line streams (hClk).
        self.h_valid      = Signal()
        self.h_hsync      = Signal()
        self.h_vsync      = Signal()
        self.fb_data      = Signal(16)
        self.osd_data     = Signal(16)

        # # #

        # QSPI_CLK falling edge clock domain.
        self.cd_qspi_n = ClockDomain("qspi_n", reset_less=True)
        self.comb += self.cd_qspi_n.clk.eq(~qspi_pads.clk)

        # Arbiter + PSRAM Controller -----------------------------------------------------------
        self.ctrl = ctrl = ClockDomainsRenamer("xclk")(MultiPortRAMCtrl(nports=PORT_COUNT))
        ports = ctrl.ports
        self.comb += ctrl.reset.eq(self.reset)
        self.specials += Instance("PSRAMController",
            i_clk_sys      = ClockSignal("xclk"),
            i_clk_fsys     = ClockSignal("fclk"),
            i_rst          = self.reset,
            i_req_read     = ctrl.req_read,
            i_req_write    = ctrl.req_write,
            i_addr         = ctrl.addr,
            i_din          = ctrl.din,
            i_burst_length = ctrl.burst_length,
            o_ready        = ctrl.ready,
            o_writeNext    = ctrl.write_next,
            o_dout         = ctrl.dout,
            o_dout_valid   = ctrl.dout_valid,
            o_done         = ctrl.done,
            o_psram_clk    = psram_pads.clk,
            o_psram_cs_n   = psram_pads.ce_n,
            io_psram_rwds  = psram_pads.dqs,
            io_psram_dq    = psram_pads.dq,
        )

        # BIST ---------------------------------------------------------------------------------
        port = ports[PORT_BIST]
        bist_req_read  = Signal()
        bist_req_write = Signal()
        self.comb += [
            port.request.eq(bist_req_read | bist_req_write),
            port.rnw.eq(bist_req_read),
        ]
        self.specials += Instance("PSRAMBIST_Burst",
            i_clk            = ClockSignal("xclk"),
            i_rst            = self.reset,
            o_test_finished  = self.bist_done,
            o_test_failed    = self.bist_failed,
            o_ram_req_read   = bist_req_read,
            o_ram_req_write  = bist_req_write,
            o_ram_addr       = port.addr,
            o_ram_din        = port.din,
            o_burst_length   = port.burst_length,
            i_ram_ready      = ctrl.ready,
            i_ram_writeNext  = port.write_next,
            i_ram_done       = port.done,
            i_ram_dout       = ctrl.dout,
            i_ram_dout_valid = port.dout_valid,
        )

        # QSPI Writes (ESP32) ------------------------------------------------------------------
        port = ports[PORT_QSPI]
        q_data_valid = Signal()
        q_data       = Signal(16)
        q_address    = Signal(32)
        self.specials += Instance("QSPI_Slave",
            i_QSPI_CLK    = qspi_pads.clk,
            i_QSPI_CS     = qspi_pads.cs_n,
            i_QSPI_MOSI   = qspi_pads.mosi,
            i_QSPI_MISO   = qspi_pads.miso,
            i_QSPI_WP     = qspi_pads.wp_n,
            i_QSPI_HD     = qspi_pads.hd,
            o_qMenuInit   = self.menu_init,
            o_qDataValid  = q_data_valid,
            o_qData       = q_data,
            o_qAddress    = q_address,
        )
        self.qspi_write = qspi_write = QSPIBurstWrite(port)
        self.comb += [
            qspi_write.ram_ready.eq(self.bist_done),
            qspi_write.cs.eq(qspi_pads.cs_n),
            qspi_write.address.eq(q_address),
            qspi_write.data_valid.eq(q_data_valid),
            qspi_write.data.eq(q_data),
            port.rnw.eq(0),
            port.burst_length.eq(1024),
        ]

        # Framebuffer / OSD Line Reads ---------------------------------------------------------
        h_vsync_d = Signal()
        h_hsync_d = Signal()
        h_valid_d = Signal()
        self.sync.hclk += [
            h_vsync_d.eq(self.h_vsync),
            h_hsync_d.eq(self.h_hsync),
            h_valid_d.eq(self.h_valid),
        ]
        for n, base, (vsync, hsync, valid), data in [
            (PORT_FBRD,  0x10000, (self.h_vsync, self.h_hsync, self.h_valid), self.fb_data),
            (PORT_FBOSD, 0x00000, (h_vsync_d,    h_hsync_d,    h_valid_d),    self.osd_data),
        ]:
            port = ports[n]
            self.comb += [
                port.rnw.eq(1),
                port.burst_length.eq(320),
                port.din.eq(0),
            ]
            self.specials += Instance("mm_burst_read_to_stream",
                p_base_pointer = Constant(base, 23),
                i_hClk         = ClockSignal("hclk"),
                i_hVsync       = vsync,
                i_hHsync       = hsync,
                i_hValid       = valid,
                i_xClk         = ClockSignal("xclk"),
                i_xRamReady    = self.bist_done,
                i_xStreamValid = port.dout_valid,
                i_xStreamData  = ctrl.dout,
                i_xWrBurstDone = port.done,
                o_xGbReqRead   = port.request,
                o_hWrBurstQ    = data,
                o_xGbAddress   = port.addr,
            )

        # Game Boy Framebuffer Write -----------------------------------------------------------
        port = ports[PORT_FBWR]
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

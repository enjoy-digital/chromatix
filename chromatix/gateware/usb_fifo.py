#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
LiteX/Migen port of the legacy usb_fifo (Gowin USB device controller endpoint buffers), for the
configuration used by usbuvcuart_top.v: a single bulk endpoint (EP3, CDC UART data) with both
IN (user -> host) and OUT (host -> user) buffers, all clocks = pClk (sys).

Structure (identical to the original):

  IN  (TX): tx_valid/tx_data -> 64x8 clk_cross_fifo -> 4096x8 TX packet FIFO -> txdat/txlen/txcork.
            The TX packet FIFO only commits bytes on txpktfin and rewinds its read pointer at the
            end of a transfer (txact falling edge), so a non-acknowledged packet is resent.
  OUT (RX): rxdat/rxval -> 4096x8 RX packet FIFO -> 64x8 clk_cross_fifo -> rx_valid/rx_data.
            The RX packet FIFO only commits bytes on rxpktval (CRC OK) and rewinds its write pointer
            at the start of each packet (rxact rising edge), dropping bad packets.

The original resets asynchronously (i_reset). Here reset is synchronous, but the registers that are
visible at the outputs are forced to their reset values while reset is asserted, as the original,
so the module is cycle-exact identical at its outputs (formally checked, see test/test_usb_fifo.py).

The clk_cross_fifo gray-code pointer synchronizers become plain 2-stage delays of the binary
pointers (write and read clocks are the same pClk), with identical flag timing.

Original behaviours kept as is:
- A TX rewind (txact falling edge without txpktfin) does not update the look-ahead read pointer:
  with back-to-back txpop, the 2nd byte of the resent packet comes from the stale position.
- rx_ready is sampled one cycle before rx_valid: a rx_ready falling edge while a read is in flight
  gives a rx_valid pulse with the previous (non-advanced) rx_data.
- The RX packet FIFO level (rxrdy threshold) wraps to 0 when the FIFO is completely full.
- txdat is combinatorial on endpt (not registered), txcork/txlen/rxrdy are registered; for endpt 0
  txdat is X in the original (0 here); disabled EP1/EP2 report rxrdy=1, EP4-15 rxrdy=0.
"""

from migen import *

from litex.gen import *

# Clock Cross FIFO (single clock) ------------------------------------------------------------------

class _CrossFIFO(LiteXModule):
    """
    Port of clk_cross_fifo (DSIZE=8, ASIZE=6, AFULL=32) with WrClock = RdClock.

    Registered Empty/Full/AlmostFull flags computed from 2-cycle delayed pointers of the other side,
    Q is a registered read of mem[rptr] when re (even when empty).
    """
    def __init__(self, reset, asize=6, afull=32):
        self.we          = Signal()
        self.din         = Signal(8)
        self.re          = Signal()
        self.dout        = Signal(8)
        self.empty       = Signal(reset=1)
        self.full        = Signal()
        self.almost_full = Signal()

        # # #

        # Memory (not an attribute: not exposed on the CSR bus).
        mem     = Memory(8, 2**asize)
        wr_port = mem.get_port(write_capable=True)
        rd_port = mem.get_port(has_re=True, mode=READ_FIRST)
        self.specials += mem, wr_port, rd_port

        # Pointers (asize + 1 bits) and 2-stage delayed copies (original gray synchronizers).
        wbin      = Signal(asize + 1)
        rbin      = Signal(asize + 1)
        wbin_d    = [Signal(asize + 1) for _ in range(2)]
        rbin_d    = [Signal(asize + 1) for _ in range(2)]
        wbin_next = Signal(asize + 1)
        rbin_next = Signal(asize + 1)
        wcnt      = Signal(asize + 1)
        self.comb += [
            wbin_next.eq(wbin + (self.we & ~self.full)),
            rbin_next.eq(rbin + (self.re & ~self.empty)),
            wcnt.eq(Cat(wbin_next[:asize], rbin_d[1][asize] ^ wbin_next[asize]) -
                rbin_d[1][:asize]),
        ]
        self.sync += [
            If(reset,
                wbin.eq(0),
                rbin.eq(0),
                wbin_d[0].eq(0),
                wbin_d[1].eq(0),
                rbin_d[0].eq(0),
                rbin_d[1].eq(0),
                self.empty.eq(1),
                self.full.eq(0),
                self.almost_full.eq(0),
            ).Else(
                wbin.eq(wbin_next),
                rbin.eq(rbin_next),
                wbin_d[0].eq(wbin),
                wbin_d[1].eq(wbin_d[0]),
                rbin_d[0].eq(rbin),
                rbin_d[1].eq(rbin_d[0]),
                self.empty.eq(rbin_next == wbin_d[1]),
                self.full.eq(wbin_next == (rbin_d[1] ^ (1 << asize))),
                self.almost_full.eq(wcnt >= afull),
            )
        ]

        # Memory access.
        self.comb += [
            wr_port.we.eq(~reset & self.we & ~self.full),
            wr_port.adr.eq(wbin[:asize]),
            wr_port.dat_w.eq(self.din),
            rd_port.re.eq(~reset & self.re),
            rd_port.adr.eq(rbin[:asize]),
            self.dout.eq(rd_port.dat_r),
        ]

# TX Packet FIFO -----------------------------------------------------------------------------------

class _TXPacketFIFO(LiteXModule):
    """
    Port of sync_tx_pkt_fifo: bytes are popped by the USB controller (read) and only released on
    pktfin (ACK); on txact falling edge the read pointer is rewound to the last released position.
    """
    def __init__(self, reset, asize=12):
        self.write  = Signal()
        self.din    = Signal(8)
        self.pktfin = Signal()
        self.txact  = Signal()
        self.read   = Signal()
        self.dout   = Signal(8)
        self.wrnum  = Signal(asize + 1)
        self.empty  = Signal()

        # # #

        # Memory (not an attribute: not exposed on the CSR bus), 2 read ports (rp, rp_next).
        mem       = Memory(8, 2**asize)
        wr_port   = mem.get_port(write_capable=True)
        rd_port   = mem.get_port(mode=READ_FIRST)
        rdn_port  = mem.get_port(mode=READ_FIRST)
        self.specials += mem, wr_port, rd_port, rdn_port

        wp       = Signal(asize + 1)
        rp       = Signal(asize + 1)
        rp_next  = Signal(asize + 1, reset=1)
        pkt_rp   = Signal(asize + 1)
        txact_d  = Signal()
        req_d    = Signal()
        reset_d  = Signal()
        full     = Signal()
        do_write = Signal()
        do_read  = Signal()
        self.comb += [
            full.eq((wp[asize] ^ pkt_rp[asize]) & (wp[:asize] == pkt_rp[:asize])),
            self.empty.eq(wp == pkt_rp),
            do_write.eq(self.write & ~full),
            do_read.eq(self.read & ~self.empty),
        ]
        self.sync += [
            reset_d.eq(reset),
            If(reset,
                wp.eq(0),
                rp.eq(0),
                rp_next.eq(1),
                pkt_rp.eq(0),
                txact_d.eq(0),
                req_d.eq(0),
                self.wrnum.eq(0),
            ).Else(
                If(do_write, wp.eq(wp + 1)),
                # Rewind on txact falling edge (rp_next is not updated).
                If(txact_d & ~self.txact,
                    rp.eq(pkt_rp + do_read),
                ).Elif(do_read,
                    rp.eq(rp + 1),
                    rp_next.eq(rp + 2),
                ),
                If(self.pktfin, pkt_rp.eq(rp)),
                txact_d.eq(self.txact),
                req_d.eq(do_read),
                If(wp >= pkt_rp,
                    self.wrnum.eq(wp - pkt_rp),
                ).Else(
                    self.wrnum.eq(Cat(wp[:asize], 1) - pkt_rp[:asize]),
                ),
            )
        ]

        # Memory access: oData = req_d ? RAM[rp_next] : RAM[rp] (registered, 0 at/after reset).
        self.comb += [
            wr_port.we.eq(~reset & do_write),
            wr_port.adr.eq(wp[:asize]),
            wr_port.dat_w.eq(self.din),
            rd_port.adr.eq(rp[:asize]),
            rdn_port.adr.eq(rp_next[:asize]),
            If(reset | reset_d,
                self.dout.eq(0),
            ).Elif(req_d,
                self.dout.eq(rdn_port.dat_r),
            ).Else(
                self.dout.eq(rd_port.dat_r),
            ),
        ]

# RX Packet FIFO -----------------------------------------------------------------------------------

class _RXPacketFIFO(LiteXModule):
    """
    Port of sync_rx_pkt_fifo: bytes are only readable once validated by pktval; on rxact rising edge
    the write pointer is rewound to the last validated position (bad packets are dropped).
    """
    def __init__(self, reset, asize=12):
        self.write  = Signal()
        self.din    = Signal(8)
        self.pktval = Signal()
        self.rxact  = Signal()
        self.read   = Signal()
        self.dout   = Signal(8)
        self.wrnum  = Signal(asize + 1)
        self.empty  = Signal()

        # # #

        # Memory (not an attribute: not exposed on the CSR bus).
        mem     = Memory(8, 2**asize)
        wr_port = mem.get_port(write_capable=True)
        rd_port = mem.get_port(has_re=True, mode=READ_FIRST)
        self.specials += mem, wr_port, rd_port

        wp         = Signal(asize + 1)
        rp         = Signal(asize + 1)
        pkg_wp     = Signal(asize + 1)
        rxact_d    = Signal(2)
        rxact_rise = Signal()
        full       = Signal()
        do_write   = Signal()
        do_read    = Signal()
        self.comb += [
            rxact_rise.eq(rxact_d == 0b01),
            full.eq((wp[asize] ^ rp[asize]) & (wp[:asize] == rp[:asize])),
            self.empty.eq(pkg_wp == rp),
            do_write.eq(~rxact_rise & self.write & ~full),
            do_read.eq(self.read & ~self.empty),
        ]
        self.sync += [
            If(reset,
                wp.eq(0),
                rp.eq(0),
                pkg_wp.eq(0),
                rxact_d.eq(0),
                self.wrnum.eq(0),
            ).Else(
                If(rxact_rise,
                    wp.eq(pkg_wp),
                ).Elif(do_write,
                    wp.eq(wp + 1),
                ),
                If(do_read, rp.eq(rp + 1)),
                rxact_d.eq(Cat(self.rxact, rxact_d[0])),
                If(self.pktval, pkg_wp.eq(wp)),
                # Level on asize bits (0 when full).
                If(wp[:asize] >= rp[:asize],
                    self.wrnum.eq(wp[:asize] - rp[:asize]),
                ).Else(
                    self.wrnum.eq(Cat(wp[:asize], 1) - rp[:asize]),
                ),
            )
        ]

        # Memory access (oData only consumed after a read, its reset value is never observed).
        self.comb += [
            wr_port.we.eq(~reset & do_write),
            wr_port.adr.eq(wp[:asize]),
            wr_port.dat_w.eq(self.din),
            rd_port.re.eq(~reset & do_read),
            rd_port.adr.eq(rp[:asize]),
            self.dout.eq(rd_port.dat_r),
        ]

# USB Endpoint FIFO --------------------------------------------------------------------------------

class USBEndpointFIFO(LiteXModule):
    """
    Port of usb_fifo as used by usbuvcuart_top.v (EP3 IN/OUT only, i_ep3_tx_max = tx_max).

    USB controller side (usb_fifo i_usb_* / o_usb_*):
    - endpt/rxact/rxval/rxpktval/rxdat/rxrdy : OUT (host -> device) data.
    - txact/txpop/txpktfin/txcork/txlen/txdat : IN  (device -> host) data.

    User side (EP3):
    - tx_valid/tx_data           : i_ep3_tx_dval/i_ep3_tx_data (bytes to the host, no backpressure).
    - rx_ready/rx_valid/rx_data  : i_ep3_rx_rdy/o_ep3_rx_dval/o_ep3_rx_data (bytes from the host).

    pkt_asize/rx_afull are the packet FIFOs address width and RX almost-full level (original:
    12/2048), cross_asize/cross_afull the clock-cross FIFOs ones (original: 6/32), only reduced for
    formal verification.
    """
    def __init__(self, endpoint=3, tx_max=64, pkt_asize=12, rx_afull=2048,
        cross_asize=6, cross_afull=32):
        self.reset    = Signal()
        # USB controller.
        self.endpt    = Signal(4)
        self.rxact    = Signal()
        self.rxval    = Signal()
        self.rxpktval = Signal()
        self.rxdat    = Signal(8)
        self.rxrdy    = Signal()
        self.txact    = Signal()
        self.txpop    = Signal()
        self.txpktfin = Signal()
        self.txcork   = Signal()
        self.txlen    = Signal(12)
        self.txdat    = Signal(8)
        # User (EP3).
        self.tx_valid = Signal()
        self.tx_data  = Signal(8)
        self.rx_ready = Signal()
        self.rx_valid = Signal()
        self.rx_data  = Signal(8)

        # # #

        reset = self.reset

        # Registered endpoint (endpoint buffers see the endpoint one cycle late).
        endpt_sel = Signal(4)
        self.sync += If(reset, endpt_sel.eq(0)).Else(endpt_sel.eq(self.endpt))
        ep_sel = Signal()
        self.comb += ep_sel.eq(endpt_sel == endpoint)

        # IN (TX) buffer ---------------------------------------------------------------------------
        self.tx_cross = tx_cross = _CrossFIFO(reset, asize=cross_asize, afull=cross_afull)
        self.tx_pkt   = tx_pkt   = _TXPacketFIFO(reset, asize=pkt_asize)
        tx_cross_rd    = Signal()
        tx_cross_rd_ok = Signal()
        tx_wrnum_d     = Signal(pkt_asize + 1)
        self.comb += [
            tx_cross.we.eq(self.tx_valid),
            tx_cross.din.eq(self.tx_data),
            tx_cross.re.eq(tx_cross_rd),
            tx_pkt.write.eq(tx_cross_rd_ok),
            tx_pkt.din.eq(tx_cross.dout),
            tx_pkt.pktfin.eq(self.txpktfin & ep_sel),
            tx_pkt.txact.eq(self.txact & ep_sel),
            tx_pkt.read.eq(self.txpop & ep_sel),
        ]
        self.sync += [
            If(reset,
                tx_cross_rd.eq(0),
                tx_cross_rd_ok.eq(0),
                tx_wrnum_d.eq(0),
            ).Else(
                tx_cross_rd.eq(~tx_cross.empty),
                tx_cross_rd_ok.eq(tx_cross_rd & ~tx_cross.empty),
                tx_wrnum_d.eq(tx_pkt.wrnum),
            )
        ]
        ep_txlen = Signal(12)
        self.comb += If(tx_wrnum_d >= tx_max, ep_txlen.eq(tx_max)).Else(ep_txlen.eq(tx_wrnum_d))

        # OUT (RX) buffer --------------------------------------------------------------------------
        self.rx_pkt   = rx_pkt   = _RXPacketFIFO(reset, asize=pkt_asize)
        self.rx_cross = rx_cross = _CrossFIFO(reset, asize=cross_asize, afull=cross_afull)
        rx_pkt_rd     = Signal()
        rx_pkt_rd_ok  = Signal()
        rx_cross_rd   = Signal()
        rx_valid      = Signal()
        self.sync += [
            If(reset,
                rx_pkt.write.eq(0),
                rx_pkt.rxact.eq(0),
                rx_pkt.pktval.eq(0),
                rx_pkt.din.eq(0),
                rx_pkt_rd.eq(0),
                rx_pkt_rd_ok.eq(0),
                rx_cross_rd.eq(0),
                rx_valid.eq(0),
            ).Else(
                rx_pkt.write.eq(self.rxval & ep_sel),
                rx_pkt.rxact.eq(self.rxact & ep_sel),
                rx_pkt.pktval.eq(self.rxpktval & ep_sel),
                rx_pkt.din.eq(self.rxdat),
                rx_pkt_rd.eq(~(rx_pkt.empty | rx_cross.almost_full)),
                rx_pkt_rd_ok.eq(rx_pkt_rd & ~rx_pkt.empty),
                rx_cross_rd.eq(~(rx_cross_rd | rx_cross.empty | ~self.rx_ready)),
                rx_valid.eq(rx_cross_rd & ~rx_cross.empty),
            )
        ]
        self.comb += [
            rx_pkt.read.eq(rx_pkt_rd),
            rx_cross.we.eq(rx_pkt_rd_ok),
            rx_cross.din.eq(rx_pkt.dout),
            rx_cross.re.eq(rx_cross_rd & self.rx_ready),
        ]
        ep_rxrdy = Signal()
        self.comb += ep_rxrdy.eq(rx_pkt.wrnum < rx_afull)

        # Endpoint mux (registered status, combinatorial data) -------------------------------------
        # Disabled endpoints: txcork=1, txlen=0, rxrdy=1 for EP1/EP2 and 0 for EP4-15 (as original).
        txcork = Signal()
        rxrdy  = Signal()
        txlen  = Signal(12, reset=32)
        self.sync += [
            If(reset,
                txcork.eq(0),
                rxrdy.eq(0),
                txlen.eq(32),
            ).Elif(self.endpt == 0,
                txcork.eq(0),
                rxrdy.eq(1),
                txlen.eq(0),
            ).Elif(self.endpt == endpoint,
                txcork.eq(tx_pkt.empty),
                rxrdy.eq(ep_rxrdy),
                If(~self.txact, txlen.eq(ep_txlen)),
            ).Else(
                txcork.eq(1),
                rxrdy.eq((self.endpt == 1) | (self.endpt == 2)),
                If(~self.txact, txlen.eq(0)),
            )
        ]

        # Outputs (forced to their reset values while reset is asserted, as with the original
        # asynchronous reset).
        self.comb += [
            If(reset,
                self.txcork.eq(0),
                self.rxrdy.eq(0),
                self.txlen.eq(32),
                self.rx_valid.eq(0),
            ).Else(
                self.txcork.eq(txcork),
                self.rxrdy.eq(rxrdy),
                self.txlen.eq(txlen),
                self.rx_valid.eq(rx_valid),
            ),
            If(self.endpt == endpoint, self.txdat.eq(tx_pkt.dout)),
            self.rx_data.eq(rx_cross.dout),
        ]

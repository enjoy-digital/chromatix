#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: GPL-3.0-only
# Derived from ModRetro's oss-chromatic-console-fpga (GPL-3.0).

"""
Chromatic USB composite device: UVC (video) + UAC (audio) + CDC-ACM (UART bridge), port of
usbuvcuart_top.v.

The USB 2.0 protocol engine is LUNA (usb_luna.py/usb_luna_core.py, Amaranth, converted to Verilog
at build time with LiteX's Amaranth2VConverter); the PLL, UTMI PHY (USB2PHY), descriptors ROM,
class request handlers, UVC/UAC data paths and CDC UART are LiteX/Migen.

Clock domains: "phy" (60MHz UTMI clock, created here, also LUNA's "usb" domain), "usb_960" (960MHz
PHY oversampling clock, created here), "gclk" (video and audio samples).
"""

from types import SimpleNamespace

from migen import *
from migen.genlib.cdc import MultiReg

from litex.gen import *

from litex.soc.interconnect     import stream
from litex.soc.interconnect.csr import *

from litex.soc.cores.clock.gowin_gw5a import GW5APLL
from litex.soc.cores.usb2_phy.phy        import USB2PHY
from litex.soc.cores.usb2_phy.gowin_gw5a import GW5AUSB2PHYCRG

from chromatix.gateware.usb_class import *
from chromatix.gateware.usb_desc  import USBDescriptors, VIDEO_FRAMES
from chromatix.gateware.usb_luna  import LUNAUSBController, USBDescriptorRequest

# USB Device ---------------------------------------------------------------------------------------

class USBDevice(LiteXModule):
    """
    USB composite device (UVC + UAC + CDC-ACM) with its own PLL (clk_24 -> 60MHz "phy" / 960MHz
    "usb_960"), LUNA USB 2.0 device core and LiteX UTMI PHY (USB2PHY).
    """
    def __init__(self, platform, clk_24, pads, uvc_frames=VIDEO_FRAMES, with_utmi_monitor=False,
        with_cdc_stream=False):
        self.reset       = Signal() # Held in reset (PLL too) when 1 (async).
        self.locked      = Signal()
        self.player_num  = Signal(8)
        # Video (gClk).
        self.line_valid  = Signal()
        self.enable      = Signal()
        self.frame_valid = Signal()
        self.video       = Signal(18)
        # Audio (gClk).
        self.left        = Signal(16)
        self.right       = Signal(16)
        # CDC UART (phy).
        self.uart_txd    = Signal(reset=1)
        self.uart_rxd    = Signal()
        self.uart_dtr    = Signal()
        self.uart_rts    = Signal()
        # CDC byte stream (phy, with_cdc_stream: in place of the UART): source (host -> device),
        # sink (device -> host).
        self.cdc_source  = stream.Endpoint([("data", 8)])
        self.cdc_sink    = stream.Endpoint([("data", 8)])

        # # #

        # Clocking ---------------------------------------------------------------------------------
        self.cd_phy     = ClockDomain("phy")
        self.cd_usb_960 = ClockDomain("usb_960", reset_less=True)
        self.pll = pll = GW5APLL(devicename=platform.devicename, device=platform.device, name="usb_pll")
        pll.register_clkin(clk_24, 24e6)
        pll.create_clkout(self.cd_usb_960, 960e6, with_reset=False)
        pll.create_clkout(self.cd_phy,      60e6, with_reset=False)
        self.comb += [
            pll.reset.eq(self.reset),
            self.locked.eq(pll.locked),
        ]

        # Reset: held for 32 cycles after the PLL lock / reset release (counter not reset by the "phy"
        # domain reset, which is this reset).
        rst_cnt = Signal(8, reset_less=True)
        rst     = Signal()
        self.sync.phy += [
            If(~pll.locked | self.reset,
                rst_cnt.eq(0),
            ).Elif(rst_cnt < 32,
                rst_cnt.eq(rst_cnt + 1),
            )
        ]
        self.comb += [
            rst.eq(rst_cnt < 32),
            self.cd_phy.rst.eq(rst),
        ]

        # Start-up: disconnected (PHY/LUNA in reset, no pull-up) for 100ms after the reset, so that the
        # host sees a clean connection once the PHY is running (as after a disconnect).
        startup_cnt = Signal(max=int(0.1*60e6) + 1)
        startup     = Signal()
        self.sync.phy += If(rst, startup_cnt.eq(0)).Elif(startup, startup_cnt.eq(startup_cnt + 1))
        self.comb += startup.eq(startup_cnt != int(0.1*60e6))

        # LUNA USB 2.0 Device ----------------------------------------------------------------------
        self.luna = luna = ClockDomainsRenamer("phy")(LUNAUSBController(platform, reset=rst | startup))
        usbrst = Signal()
        self.comb += usbrst.eq(luna.bus_reset)

        # USB 2.0 PHY ------------------------------------------------------------------------------
        self.phy_crg = phy_crg = GW5AUSB2PHYCRG(cd_utmi="phy", cd_960="usb_960")
        self.phy = usb_phy = USB2PHY(pads, cd_utmi="phy", serdes_rst=phy_crg.serdes_rst)
        self.comb += [
            usb_phy.reset.eq(rst | startup),
            usb_phy.tx_data.eq(luna.utmi_tx_data),
            usb_phy.tx_valid.eq(luna.utmi_tx_valid & ~startup),
            usb_phy.op_mode.eq(Mux(startup, 0, luna.utmi_op_mode)),
            usb_phy.xcvr_select.eq(Mux(startup, 0b01, luna.utmi_xcvr_select)),
            usb_phy.term_select.eq(luna.utmi_term_select & ~startup),
            luna.utmi_rx_data.eq(usb_phy.rx_data),
            luna.utmi_tx_ready.eq(usb_phy.tx_ready),
            luna.utmi_rx_valid.eq(usb_phy.rx_valid),
            luna.utmi_rx_active.eq(usb_phy.rx_active),
            luna.utmi_rx_error.eq(usb_phy.rx_error),
            luna.utmi_line_state.eq(usb_phy.line_state),
        ]
        if with_utmi_monitor:
            utmi = SimpleNamespace(
                txvalid    = luna.utmi_tx_valid,
                txready    = usb_phy.tx_ready,
                dataout    = luna.utmi_tx_data,
                rxactive   = usb_phy.rx_active,
                rxvalid    = usb_phy.rx_valid,
                datain     = usb_phy.rx_data,
                linestate  = usb_phy.line_state,
                termselect = luna.utmi_term_select,
                xcvrselect = luna.utmi_xcvr_select,
                opmode     = luna.utmi_op_mode,
            )
            self.utmi_monitor = UTMIMonitor(utmi)

        # Descriptors ------------------------------------------------------------------------------
        self.desc = desc = ClockDomainsRenamer("phy")(USBDescriptors(uvc_frames=uvc_frames))
        self.comb += [
            desc.reset.eq(rst),
            desc.player_num.eq(self.player_num),
        ]

        # EP0: class/descriptor request handlers ---------------------------------------------------
        setup = SimpleNamespace(
            header_ready  = luna.ep0_header_ready,
            bmRequestType = luna.ep0_bmRequestType,
            bRequest      = luna.ep0_bRequest,
            wValue        = luna.ep0_wValue,
            wIndex        = luna.ep0_wIndex,
            wLength       = luna.ep0_wLength,
            cdata_ofs     = luna.ep0_cdata_ofs,
        )
        handlers = []
        for name, cls, kwargs in [
            ("ctrl_uart", CDCACMControl, {}),
            ("ctrl_uvc",  UVCControl,    {"frames": uvc_frames}),
            ("ctrl_uac",  UACControl,    {})]:
            h = ClockDomainsRenamer("phy")(cls(setup, **kwargs))
            self.add_module(name=name, module=h)
            self.comb += [
                h.reset.eq(rst),
                h.rxdat.eq(luna.ep0_rxdat),
                h.rxact.eq(luna.ep0_rxact),
                h.rxval.eq(luna.ep0_rxval),
                h.txpop.eq(luna.ep0_txpop),
            ]
            handlers.append(h)
        ctrl_uart = handlers[0]
        self.ctrl_desc = ctrl_desc = ClockDomainsRenamer("phy")(USBDescriptorRequest(setup, desc))
        handlers.append(ctrl_desc)

        # EP0 data: first active handler.
        ep0_cases = None
        for h in handlers:
            stmt = [luna.ep0_txdat.eq(h.txdat), luna.ep0_txlen.eq(h.txdat_len)]
            ep0_cases = If(h.txval, *stmt) if ep0_cases is None else ep0_cases.Elif(h.txval, *stmt)
        self.comb += [
            ep0_cases,
            luna.ep0_txval.eq(Reduce("OR", [h.txval for h in handlers])),
        ]

        # Interfaces alternate settings.
        alts = {}
        for iface in [UART_DATA_IFACE, UVC_VS_INTERFACE, UAC_AS_INTERFACE]:
            a = ClockDomainsRenamer("phy")(InterfaceAltSelect())
            self.add_module(name=f"alt_iface{iface}", module=a)
            self.comb += [
                a.reset.eq(rst | usbrst),
                a.update.eq(luna.ep0_inf_set & (luna.ep0_inf_sel == iface)),
                a.alt_i.eq(luna.ep0_inf_alt_o),
            ]
            alts[iface] = a
        self.comb += Case(luna.ep0_inf_sel, {
            **{iface: luna.ep0_inf_alt_i.eq(a.alt_o) for iface, a in alts.items()},
            "default": luna.ep0_inf_alt_i.eq(0),
        })

        # UVC (EP2) --------------------------------------------------------------------------------
        self.uvc = uvc = ClockDomainsRenamer({"sys": "phy", "video": "gclk"})(UVCVideo(frames=uvc_frames))
        uvc_txact = Signal()
        self.sync.phy += If(luna.ep2_requested, uvc_txact.eq(1)).Elif(luna.ep2_finished, uvc_txact.eq(0))
        self.comb += [
            uvc.reset.eq(rst),
            uvc.frame_index.eq(self.ctrl_uvc.frame_index),
            uvc.hbw.eq(alts[UVC_VS_INTERFACE].alt_o >= 2),
            uvc.line_valid.eq(self.line_valid),
            uvc.enable.eq(self.enable),
            uvc.frame_valid.eq(self.frame_valid),
            uvc.data.eq(self.video),
            uvc.sof.eq(luna.sof),
            uvc.txact.eq(uvc_txact),
            uvc.txpop.eq(luna.ep2_ready),
            luna.ep2_data.eq(uvc.txdat),
            luna.ep2_valid.eq(1),
            luna.ep2_bytes.eq(uvc.next_len),
        ]

        # UAC (EP5) --------------------------------------------------------------------------------
        self.uac = uac = ClockDomainsRenamer({"sys": "phy", "audio": "gclk"})(UACEndpoint())
        self.comb += [
            uac.reset.eq(rst),
            uac.left.eq(self.left),
            uac.right.eq(self.right),
            uac.txpop.eq(luna.ep5_ready),
            luna.ep5_data.eq(uac.txdat),
            luna.ep5_valid.eq(1),
            luna.ep5_bytes.eq(uac.next_len),
        ]

        # CDC-ACM (EP3) + UART ---------------------------------------------------------------------
        self.uart_rx_fifo = rx_fifo = ClockDomainsRenamer("phy")(ResetInserter()(
            stream.SyncFIFO([("data", 8)], 64)))
        self.comb += [
            # Device -> host (flushed when no more data is buffered).
            rx_fifo.reset.eq(usbrst | rst),
            luna.ep3_in_data.eq(rx_fifo.source.data),
            luna.ep3_in_valid.eq(rx_fifo.source.valid),
            rx_fifo.source.ready.eq(luna.ep3_in_ready),
            luna.ep3_in_flush.eq(~rx_fifo.source.valid),
            # UART control lines.
            self.uart_dtr.eq(ctrl_uart.ctl_sig[0]),
            self.uart_rts.eq(ctrl_uart.ctl_sig[1]),
        ]
        if with_cdc_stream:
            # Byte stream at the USB rate (no UART).
            self.comb += [
                self.cdc_source.valid.eq(luna.ep3_out_valid),
                self.cdc_source.data.eq(luna.ep3_out_data),
                luna.ep3_out_ready.eq(self.cdc_source.ready),
                self.cdc_sink.connect(rx_fifo.sink),
            ]
        else:
            self.uart = uart = ClockDomainsRenamer("phy")(CDCUART())
            self.comb += [
                uart.reset.eq(usbrst | rst),
                uart.baudrate.eq(ctrl_uart.dte_rate),
                # USB -> UART (bytes accepted when the UART FIFO is ready: no ready handshake).
                uart.tx_data.eq(luna.ep3_out_data),
                uart.tx_valid.eq(luna.ep3_out_valid & uart.tx_ready),
                luna.ep3_out_ready.eq(uart.tx_ready),
                # UART -> USB.
                rx_fifo.sink.valid.eq(uart.rx_valid),
                rx_fifo.sink.data.eq(uart.rx_data),
                self.uart_txd.eq(uart.txd),
                uart.rxd.eq(self.uart_rxd),
            ]

# UTMI Monitor -------------------------------------------------------------------------------------

class UTMIMonitor(LiteXModule):
    """
    Debug: UTMI packet/state recorder (to check the USB traffic without a protocol analyzer).

    - all = 0: records the transmitted data packets and received SOFs (PID, length, idle clocks
      before the packet) after the first long (> 600 bytes, video) transmitted packet.
    - all = 1: records every packet (both directions, handshakes included) and the UTMI state
      changes between packets (op_mode/xcvr_select/term_select/line_state, PID field = 0xee) from
      the first bus reset (SE0/no packet for 3ms) after the arming.

    Entries are captured in the "phy" domain and read from the CSRs (sys domain) with sel -> data.
    """
    def __init__(self, utmi, depth=256):
        self._control = CSRStorage(fields=[
            CSRField("arm", size=1, offset=0, pulse=True, description="Re-arm the capture."),
            CSRField("all", size=1, offset=1,             description="Record all packets/state changes."),
            CSRField("ep0", size=1, offset=2,             description="Record the EP0 transactions only (from the arming)."),
            CSRField("sel", size=8, offset=8,             description="Entry to read."),
        ])
        self._status = CSRStatus(fields=[
            CSRField("count", size=9, offset=0, description="Captured entries."),
        ])
        self._data = CSRStatus(32, description="Entry: [31] tx, [30:23] PID (0xee: state), [22:12] length (state: {op_mode, xcvr_select, term_select, line_state}), [11:0] idle clocks (sat.).")

        # # #

        mem = Memory(32, depth)
        wr  = mem.get_port(write_capable=True, clock_domain="phy")
        rd  = mem.get_port(async_read=True, clock_domain="phy")
        self.specials += mem, wr, rd

        arm_toggle   = Signal()
        arm_toggle_p = Signal()
        arm_toggle_d = Signal()
        all_p        = Signal()
        ep0_p        = Signal()
        sel_p        = Signal(8)
        count        = Signal(9)
        data_p       = Signal(32)
        self.sync += If(self._control.fields.arm, arm_toggle.eq(~arm_toggle))
        self.specials += [
            MultiReg(arm_toggle,                 arm_toggle_p, odomain="phy"),
            MultiReg(self._control.fields.all,   all_p,        odomain="phy"),
            MultiReg(self._control.fields.ep0,   ep0_p,        odomain="phy"),
            MultiReg(self._control.fields.sel,   sel_p,        odomain="phy"),
            MultiReg(count,                      self._status.fields.count),
            MultiReg(data_p,                     self._data.status),
        ]

        triggered = Signal()
        tx_d      = Signal()
        rx_d      = Signal()
        first     = Signal()
        pid       = Signal(8)
        length    = Signal(11)
        idle      = Signal(12)
        gap       = Signal(12)
        tx        = Signal()
        active    = Signal()
        record    = Signal()
        state     = Signal(7)
        state_d   = Signal(7)
        se0_count = Signal(18)
        tok_byte1 = Signal(8)
        ep0_trans = Signal() # Current transaction on EP0 (last token).
        is_token  = Signal()
        self.comb += [
            active.eq(utmi.txvalid | utmi.rxactive),
            state.eq(Cat(utmi.linestate, utmi.termselect, utmi.xcvrselect, utmi.opmode)),
            rd.adr.eq(sel_p),
            data_p.eq(rd.dat_r),
            wr.adr.eq(count),
            # Transmitted data packets (no handshakes) and received SOFs (micro-frame delimiters),
            # or everything.
            record.eq(Mux(ep0_p, ep0_trans & ~is_token | (is_token & (pid != 0xa5) & ep0_trans),
                all_p | Mux(tx, (pid != 0x5a) & (pid != 0xd2), pid == 0xa5))),
            is_token.eq(~tx & ((pid == 0x69) | (pid == 0xe1) | (pid == 0x2d) | (pid == 0xb4) | (pid == 0xa5))),
        ]
        self.sync.phy += [
            arm_toggle_d.eq(arm_toggle_p),
            tx_d.eq(utmi.txvalid),
            rx_d.eq(utmi.rxactive),
            wr.we.eq(0),
            If(arm_toggle_p != arm_toggle_d,
                triggered.eq(ep0_p),
                count.eq(0),
                state_d.eq(state),
            ).Elif(all_p & ~triggered & (se0_count == 180000),
                # Bus reset (SE0/no packet for 3ms, also true in HS where idle is SE0): start of the
                # capture (all mode).
                triggered.eq(1),
                state_d.eq(state),
            ),
            If(active | (utmi.linestate != 0b00),
                se0_count.eq(0),
            ).Elif(se0_count != 180000,
                se0_count.eq(se0_count + 1),
            ),
            If(active,
                If(~tx_d & ~rx_d,
                    # Packet start.
                    tx.eq(utmi.txvalid),
                    first.eq(1),
                    length.eq(0),
                    gap.eq(idle),
                ),
                If(utmi.txvalid & utmi.txready,
                    If(first, pid.eq(utmi.dataout), first.eq(0)),
                    length.eq(length + 1),
                ),
                If(utmi.rxactive & utmi.rxvalid,
                    If(first, pid.eq(utmi.datain), first.eq(0)),
                    If(length == 1, tok_byte1.eq(utmi.datain)),
                    If((length == 2) & ((pid == 0x69) | (pid == 0xe1) | (pid == 0x2d) | (pid == 0xb4)),
                        # ENDP = {byte2[2:0], byte1[7]}.
                        ep0_trans.eq(Cat(tok_byte1[7], utmi.datain[0:3]) == 0),
                    ),
                    length.eq(length + 1),
                ),
                idle.eq(0),
            ).Else(
                If(idle != 0xfff, idle.eq(idle + 1)),
                If(tx_d | rx_d,
                    # Packet end: record (trigger on the 1st long transmitted packet).
                    If(triggered | (tx & (length > 600)),
                        triggered.eq(1),
                        If(record & (count < depth),
                            wr.dat_w.eq(Cat(gap, length, pid, tx)),
                            wr.we.eq(1),
                            count.eq(count + 1),
                        )
                    ),
                ).Elif(all_p & ~ep0_p & triggered & (state != state_d),
                    # UTMI state change (between packets).
                    state_d.eq(state),
                    If(count < depth,
                        wr.dat_w.eq(Cat(idle, state, Constant(0, 4), Constant(0xee, 8), Constant(0, 1))),
                        wr.we.eq(1),
                        count.eq(count + 1),
                        idle.eq(0),
                    )
                ),
            ),
        ]

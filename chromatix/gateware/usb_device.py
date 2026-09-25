#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
Chromatic USB composite device: UVC (video) + UAC (audio) + CDC-ACM (UART bridge), port of
usbuvcuart_top.v.

Only the Gowin USB 2.0 Device Controller IP is still instantiated; the PLL, UTMI PHY, descriptors,
class handlers, endpoint buffers, UVC/UAC data paths and CDC UART are LiteX/Migen.

Clock domains: "phy" (60MHz UTMI clock, created here), "usb_960" (960MHz PHY oversampling clock,
created here), "gclk" (video and audio samples).
"""

from migen import *

from litex.gen import *

from migen.genlib.cdc import MultiReg

from litex.soc.interconnect.csr import *

from litex.soc.cores.clock.gowin_gw5a import GW5APLL
from litex.soc.cores.usb2_phy.phy        import USB2PHY
from litex.soc.cores.usb2_phy.gowin_gw5a import GW5AUSB2PHYCRG

from chromatix.gateware.usb_class import *
from chromatix.gateware.usb_desc  import USBDescriptors, VIDEO_FRAMES
from chromatix.gateware.usb_fifo  import USBEndpointFIFO

# USB Device ---------------------------------------------------------------------------------------

class USBDevice(LiteXModule):
    """
    USB composite device (UVC + UAC + CDC-ACM) with its own PLL (clk_24 -> 60MHz "phy" / 960MHz
    "usb_960"), Gowin USB 2.0 Device Controller and LiteX UTMI PHY (USB2PHY).
    """
    def __init__(self, platform, clk_24, pads, uvc_frames=VIDEO_FRAMES, with_utmi_monitor=False,
        with_luna_debug=False):
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

        # # #

        # Clocking ---------------------------------------------------------------------------------
        self.cd_phy     = ClockDomain("phy",     reset_less=True)
        self.cd_usb_960 = ClockDomain("usb_960", reset_less=True)
        self.pll = pll = GW5APLL(devicename=platform.devicename, device=platform.device, name="usb_pll")
        pll.register_clkin(clk_24, 24e6)
        pll.create_clkout(self.cd_usb_960, 960e6, with_reset=False)
        pll.create_clkout(self.cd_phy,      60e6, with_reset=False)
        self.comb += [
            pll.reset.eq(self.reset),
            self.locked.eq(pll.locked),
        ]

        # Reset: held for 32 cycles after the PLL lock / reset release.
        rst_cnt = Signal(8)
        rst     = Signal()
        self.sync.phy += [
            If(~pll.locked | self.reset,
                rst_cnt.eq(0),
            ).Elif(rst_cnt < 32,
                rst_cnt.eq(rst_cnt + 1),
            )
        ]
        self.comb += rst.eq(rst_cnt < 32)

        # USB Controller signals -------------------------------------------------------------------
        usbrst    = Signal()
        txdat     = Signal(8)
        txval     = Signal()
        txdat_len = Signal(12)
        txcork    = Signal()
        txpop     = Signal()
        txact     = Signal()
        txpktfin  = Signal()
        rxdat     = Signal(8)
        rxval     = Signal()
        rxact     = Signal()
        rxrdy     = Signal()
        rxpktval  = Signal()
        setup     = Signal()
        endpt     = Signal(4)
        sof       = Signal()
        alt_i     = Signal(8)
        alt_o     = Signal(8)
        alt_sel   = Signal(8)
        alt_set   = Signal()

        # Descriptors ------------------------------------------------------------------------------
        self.desc = desc = ClockDomainsRenamer("phy")(USBDescriptors(uvc_frames=uvc_frames))
        self.comb += [
            desc.reset.eq(rst),
            desc.player_num.eq(self.player_num),
        ]

        # Control transfers ------------------------------------------------------------------------
        self.setup = sp = ClockDomainsRenamer("phy")(USBSetupParser())
        self.comb += [
            sp.reset.eq(rst),
            sp.setup_active.eq(setup),
            sp.endpt.eq(endpt),
            sp.rxdat.eq(rxdat),
            sp.rxval.eq(rxval),
            sp.rxact.eq(rxact),
            sp.txact.eq(txact),
            sp.txpop.eq(txpop),
        ]
        handlers = []
        for name, cls, kwargs in [
            ("ctrl_uart", CDCACMControl, {}),
            ("ctrl_uvc",  UVCControl,    {"frames": uvc_frames}),
            ("ctrl_uac",  UACControl,    {})]:
            h = ClockDomainsRenamer("phy")(cls(sp, **kwargs))
            self.add_module(name=name, module=h)
            self.comb += [
                h.reset.eq(rst),
                h.rxdat.eq(rxdat),
                h.rxact.eq(rxact),
                h.rxval.eq(rxval),
                h.txpop.eq(txpop),
            ]
            handlers.append(h)
        ctrl_uart = handlers[0]

        # EP0 data: first active handler.
        ep0_dat   = Signal(8)
        ep0_len   = Signal(12)
        ep0_send  = Signal()
        ep0_cases = None
        for h in handlers:
            stmt = [ep0_dat.eq(h.txdat), ep0_len.eq(h.txdat_len)]
            ep0_cases = If(h.txval, *stmt) if ep0_cases is None else ep0_cases.Elif(h.txval, *stmt)
        self.comb += [
            ep0_cases,
            ep0_send.eq(Reduce("OR", [h.txval for h in handlers])),
        ]

        # Interfaces alternate settings.
        alts = {}
        for iface in [UART_DATA_IFACE, UVC_VS_INTERFACE, UAC_AS_INTERFACE]:
            a = ClockDomainsRenamer("phy")(InterfaceAltSelect())
            self.add_module(name=f"alt_iface{iface}", module=a)
            self.comb += [
                a.reset.eq(rst),
                a.update.eq(alt_set & (alt_sel == iface)),
                a.alt_i.eq(alt_o),
            ]
            alts[iface] = a
        self.comb += Case(alt_sel, {
            **{iface: alt_i.eq(a.alt_o) for iface, a in alts.items()},
            "default": alt_i.eq(0),
        })

        # UVC --------------------------------------------------------------------------------------
        self.uvc = uvc = ClockDomainsRenamer({"sys": "phy", "video": "gclk"})(UVCVideo(frames=uvc_frames))
        self.comb += [
            uvc.reset.eq(rst),
            uvc.frame_index.eq(self.ctrl_uvc.frame_index),
            uvc.hbw.eq(alts[UVC_VS_INTERFACE].alt_o >= 2),
            uvc.line_valid.eq(self.line_valid),
            uvc.enable.eq(self.enable),
            uvc.frame_valid.eq(self.frame_valid),
            uvc.data.eq(self.video),
            uvc.sof.eq(sof),
            uvc.txact.eq(Mux(endpt == EP_VS, txact, 0)),
            uvc.txpop.eq(Mux(endpt == EP_VS, txpop, 0)),
        ]

        # UAC --------------------------------------------------------------------------------------
        self.uac = uac = ClockDomainsRenamer({"sys": "phy", "audio": "gclk"})(UACEndpoint())
        self.comb += [
            uac.reset.eq(rst),
            uac.sof_rise.eq(uvc.sof_rise),
            uac.left.eq(self.left),
            uac.right.eq(self.right),
            uac.txact.eq(Mux(endpt == EP_UAC, txact, 0)),
            uac.txpop.eq(Mux(endpt == EP_UAC, txpop, 0)),
        ]

        # CDC-ACM (EP3) + UART ---------------------------------------------------------------------
        self.ep3  = ep3  = ClockDomainsRenamer("phy")(USBEndpointFIFO())
        self.uart = uart = ClockDomainsRenamer("phy")(CDCUART())
        self.comb += [
            ep3.reset.eq(usbrst | rst),
            ep3.endpt.eq(endpt),
            ep3.rxact.eq(Mux(endpt == EP_UART, rxact, 0)),
            ep3.rxval.eq(Mux(endpt == EP_UART, rxval, 0)),
            ep3.rxpktval.eq(rxpktval),
            ep3.rxdat.eq(rxdat),
            ep3.txact.eq(Mux(endpt == EP_UART, txact, 0)),
            ep3.txpop.eq(Mux(endpt == EP_UART, txpop, 0)),
            ep3.txpktfin.eq(txpktfin),
            uart.reset.eq(usbrst | rst),
            uart.baudrate.eq(ctrl_uart.dte_rate),
            uart.tx_data.eq(ep3.rx_data),
            uart.tx_valid.eq(ep3.rx_valid),
            ep3.rx_ready.eq(uart.tx_ready),
            ep3.tx_data.eq(uart.rx_data),
            ep3.tx_valid.eq(uart.rx_valid),
            self.uart_txd.eq(uart.txd),
            uart.rxd.eq(self.uart_rxd),
            self.uart_dtr.eq(ctrl_uart.ctl_sig[0]),
            self.uart_rts.eq(ctrl_uart.ctl_sig[1]),
        ]

        # Endpoints -> Controller mux --------------------------------------------------------------
        self.comb += [
            Case(endpt, {
                EP_CTRL:   [txdat.eq(ep0_dat),   txdat_len.eq(ep0_len),       txcork.eq(0)],
                EP_VS:     [txdat.eq(uvc.txdat), txdat_len.eq(uvc.txdat_len), txcork.eq(uvc.txcork)],
                EP_UAC:    [txdat.eq(uac.txdat), txdat_len.eq(uac.txdat_len), txcork.eq(uac.txcork)],
                EP_UART:   [txdat.eq(ep3.txdat), txdat_len.eq(ep3.txlen),     txcork.eq(ep3.txcork)],
                "default": [txdat.eq(ep3.txdat), txdat_len.eq(0xfae),         txcork.eq(1)],
            }),
            txval.eq(Mux(endpt == EP_CTRL, ep0_send, 0)),
            rxrdy.eq(Mux(endpt == EP_UART, ep3.rxrdy, Mux(endpt == EP_CTRL, 1, 0))),
        ]

        # Gowin USB 2.0 Device Controller ----------------------------------------------------------
        utmi = Record([
            ("dataout", 8), ("txvalid", 1), ("txready", 1), ("datain", 8), ("rxactive", 1),
            ("rxvalid", 1), ("rxerror",  1), ("linestate", 2), ("opmode", 2), ("xcvrselect", 2),
            ("termselect", 1), ("reset", 1),
        ])
        if with_utmi_monitor and not with_luna_debug:
            self.utmi_monitor = UTMIMonitor(utmi)
        d = desc
        self.specials += Instance("USB_Device_Controller_Top", name="u_usb_device_controller_top",
            i_clk_i                  = ClockSignal("phy"),
            i_reset_i                = rst,
            o_usbrst_o               = usbrst,
            o_highspeed_o            = Signal(),
            o_suspend_o              = Signal(),
            o_online_o               = Signal(),
            i_txdat_i                = txdat,
            i_txval_i                = txval,
            i_txdat_len_i            = txdat_len,
            # Isochronous PID: not sampled with endpt by the controller, so driven by the video
            # endpoint (DATA1 -> DATA0 for high-bandwidth micro-frames, else DATA0).
            i_txiso_pid_i            = uvc.txiso_pid,
            i_txcork_i               = txcork,
            o_txpop_o                = txpop,
            o_txact_o                = txact,
            o_txpktfin_o             = txpktfin,
            o_rxdat_o                = rxdat,
            o_rxval_o                = rxval,
            o_rxact_o                = rxact,
            i_rxrdy_i                = rxrdy,
            o_rxpktval_o             = rxpktval,
            o_setup_o                = setup,
            o_endpt_o                = endpt,
            o_sof_o                  = sof,
            i_inf_alter_i            = alt_i,
            o_inf_alter_o            = alt_o,
            o_inf_sel_o              = alt_sel,
            o_inf_set_o              = alt_set,
            i_descrom_rdata_i        = d.descrom_rdat,
            o_descrom_raddr_o        = d.descrom_raddr,
            i_desc_dev_addr_i        = d.dev_addr,
            i_desc_dev_len_i         = d.dev_len,
            i_desc_qual_addr_i       = d.qual_addr,
            i_desc_qual_len_i        = d.qual_len,
            i_desc_fscfg_addr_i      = d.fscfg_addr,
            i_desc_fscfg_len_i       = d.fscfg_len,
            i_desc_hscfg_addr_i      = d.hscfg_addr,
            i_desc_hscfg_len_i       = d.hscfg_len,
            i_desc_oscfg_addr_i      = d.oscfg_addr,
            i_desc_strlang_addr_i    = d.strlang_addr,
            i_desc_strvendor_addr_i  = d.strvendor_addr,
            i_desc_strvendor_len_i   = d.strvendor_len,
            i_desc_strproduct_addr_i = d.strproduct_addr,
            i_desc_strproduct_len_i  = d.strproduct_len,
            i_desc_strserial_addr_i  = d.strserial_addr,
            i_desc_strserial_len_i   = d.strserial_len,
            i_desc_have_strings_i    = d.have_strings,
            i_desc_bos_addr_i        = Constant(0, 16),
            i_desc_bos_len_i         = Constant(0, 16),
            i_desc_hidrpt_addr_i     = Constant(0, 16),
            i_desc_hidrpt_len_i      = Constant(0, 16),
            o_desc_index_o           = Signal(8),
            o_desc_type_o            = Signal(8),
            o_utmi_dataout_o         = utmi.dataout,
            o_utmi_txvalid_o         = utmi.txvalid,
            i_utmi_txready_i         = utmi.txready,
            i_utmi_datain_i          = utmi.datain,
            i_utmi_rxactive_i        = utmi.rxactive,
            i_utmi_rxvalid_i         = utmi.rxvalid,
            i_utmi_rxerror_i         = utmi.rxerror,
            i_utmi_linestate_i       = utmi.linestate,
            o_utmi_opmode_o          = utmi.opmode,
            o_utmi_xcvrselect_o      = utmi.xcvrselect,
            o_utmi_termselect_o      = utmi.termselect,
            o_utmi_reset_o           = utmi.reset,
        )

        # USB 2.0 PHY ------------------------------------------------------------------------------
        self.phy_crg = phy_crg = GW5AUSB2PHYCRG(cd_utmi="phy", cd_960="usb_960")
        self.phy = usb_phy = USB2PHY(pads, cd_utmi="phy", serdes_rst=phy_crg.serdes_rst)
        phy_utmi = utmi
        if with_luna_debug:
            phy_utmi = self.add_luna_debug(platform, uvc_frames, utmi, rst)
        self.comb += [
            usb_phy.reset.eq(phy_utmi.reset | rst),
            usb_phy.tx_data.eq(phy_utmi.dataout),
            usb_phy.tx_valid.eq(phy_utmi.txvalid),
            usb_phy.op_mode.eq(phy_utmi.opmode),
            usb_phy.xcvr_select.eq(phy_utmi.xcvrselect),
            usb_phy.term_select.eq(phy_utmi.termselect),
            phy_utmi.datain.eq(usb_phy.rx_data),
            phy_utmi.txready.eq(usb_phy.tx_ready),
            phy_utmi.rxvalid.eq(usb_phy.rx_valid),
            phy_utmi.rxactive.eq(usb_phy.rx_active),
            phy_utmi.rxerror.eq(usb_phy.rx_error),
            phy_utmi.linestate.eq(usb_phy.line_state),
        ]
        if with_utmi_monitor and with_luna_debug:
            self.utmi_monitor = UTMIMonitor(phy_utmi)

    def add_luna_debug(self, platform, uvc_frames, utmi, rst):
        """
        Debug: LUNA device core (EP0 only) sharing the PHY with the Gowin controller. A write to
        luna_run gives the PHY to LUNA for 5s (with 100ms disconnects before/after so that the host
        re-enumerates), then back to the Gowin controller (the UTMI monitor records LUNA's traffic).
        """
        from types import SimpleNamespace
        from chromatix.gateware.usb_luna import LUNAUSBController, USBDescriptorRequest

        self._luna_run = CSRStorage(fields=[
            CSRField("run", size=1, offset=0, pulse=True, description="Give the PHY to LUNA for 5s."),
        ])
        run_toggle   = Signal()
        run_toggle_p = Signal()
        run_toggle_d = Signal()
        self.sync += If(self._luna_run.fields.run, run_toggle.eq(~run_toggle))
        self.specials += MultiReg(run_toggle, run_toggle_p, odomain="phy")

        # Window FSM (phy): GOWIN -> DISC -> LUNA (5s) -> DISC -> GOWIN.
        GOWIN, DISC1, LUNA, DISC2 = range(4)
        wstate = Signal(2)
        wcount = Signal(29)
        self.sync.phy += [
            run_toggle_d.eq(run_toggle_p),
            wcount.eq(wcount + 1),
            Case(wstate, {
                GOWIN: If(run_toggle_p != run_toggle_d, wstate.eq(DISC1), wcount.eq(0)),
                DISC1: If(wcount == int(0.1*60e6), wstate.eq(LUNA),  wcount.eq(0)),
                LUNA:  If(wcount == int(5.0*60e6), wstate.eq(DISC2), wcount.eq(0)),
                DISC2: If(wcount == int(0.1*60e6), wstate.eq(GOWIN), wcount.eq(0)),
            })
        ]

        # LUNA core (EP0 only, own descriptors ROM), held in reset outside of its window.
        luna_rst = Signal()
        self.comb += luna_rst.eq(rst | (wstate != LUNA))
        self.luna = luna = ClockDomainsRenamer("phy")(LUNAUSBController(platform, reset=luna_rst,
            iso_endpoints={}, bulk_endpoints={}, nak_endpoints=[1, 4]))
        self.luna_desc = luna_desc = ClockDomainsRenamer("phy")(USBDescriptors(uvc_frames=uvc_frames))
        setup = SimpleNamespace(
            header_ready  = luna.ep0_header_ready,
            bmRequestType = luna.ep0_bmRequestType,
            bRequest      = luna.ep0_bRequest,
            wValue        = luna.ep0_wValue,
            wIndex        = luna.ep0_wIndex,
            wLength       = luna.ep0_wLength,
            cdata_ofs     = luna.ep0_cdata_ofs,
        )
        self.luna_ctrl_desc = ctrl_desc = ClockDomainsRenamer("phy")(USBDescriptorRequest(setup, luna_desc))
        self.comb += [
            luna_desc.reset.eq(rst),
            luna_desc.player_num.eq(self.player_num),
            luna.ep0_txval.eq(ctrl_desc.txval),
            luna.ep0_txdat.eq(ctrl_desc.txdat),
            luna.ep0_txlen.eq(ctrl_desc.txdat_len),
            luna.ep0_inf_alt_i.eq(0),
        ]

        # UTMI mux (PHY side).
        phy_utmi = Record([
            ("dataout", 8), ("txvalid", 1), ("txready", 1), ("datain", 8), ("rxactive", 1),
            ("rxvalid", 1), ("rxerror",  1), ("linestate", 2), ("opmode", 2), ("xcvrselect", 2),
            ("termselect", 1), ("reset", 1),
        ])
        is_luna = wstate == LUNA
        disc    = (wstate == DISC1) | (wstate == DISC2)
        self.comb += [
            # To the PHY.
            If(is_luna,
                phy_utmi.dataout.eq(luna.utmi_tx_data),
                phy_utmi.txvalid.eq(luna.utmi_tx_valid),
                phy_utmi.opmode.eq(luna.utmi_op_mode),
                phy_utmi.xcvrselect.eq(luna.utmi_xcvr_select),
                phy_utmi.termselect.eq(luna.utmi_term_select),
                phy_utmi.reset.eq(0),
            ).Elif(disc,
                # Disconnected (no pull-up).
                phy_utmi.opmode.eq(0),
                phy_utmi.xcvrselect.eq(0b01),
                phy_utmi.termselect.eq(0),
                phy_utmi.reset.eq(1),
            ).Else(
                phy_utmi.dataout.eq(utmi.dataout),
                phy_utmi.txvalid.eq(utmi.txvalid),
                phy_utmi.opmode.eq(utmi.opmode),
                phy_utmi.xcvrselect.eq(utmi.xcvrselect),
                phy_utmi.termselect.eq(utmi.termselect),
                phy_utmi.reset.eq(utmi.reset),
            ),
            # From the PHY.
            luna.utmi_rx_data.eq(phy_utmi.datain),
            luna.utmi_tx_ready.eq(phy_utmi.txready & is_luna),
            luna.utmi_rx_valid.eq(phy_utmi.rxvalid & is_luna),
            luna.utmi_rx_active.eq(phy_utmi.rxactive & is_luna),
            luna.utmi_rx_error.eq(phy_utmi.rxerror),
            luna.utmi_line_state.eq(phy_utmi.linestate),
            utmi.datain.eq(phy_utmi.datain),
            utmi.txready.eq(phy_utmi.txready & ~is_luna),
            utmi.rxvalid.eq(phy_utmi.rxvalid & ~is_luna),
            utmi.rxactive.eq(phy_utmi.rxactive & ~is_luna),
            utmi.rxerror.eq(phy_utmi.rxerror),
            utmi.linestate.eq(Mux(is_luna | disc, 0b00, phy_utmi.linestate)),
        ]
        return phy_utmi

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
            CSRField("all", size=1, offset=1, description="Record all packets/state changes."),
            CSRField("ep0", size=1, offset=2, description="Record the EP0 transactions only (from the arming)."),
            CSRField("sel", size=8, offset=8, description="Entry to read."),
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

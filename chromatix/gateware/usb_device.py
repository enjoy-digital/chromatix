#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
Chromatic USB composite device: UVC (video) + UAC (audio) + CDC-ACM (UART bridge), port of
usbuvcuart_top.v.

Only the Gowin USB 2.0 Device Controller IP is still instantiated; the PLL, UTMI PHY (LiteX
USB2PHY), descriptors, class handlers, endpoint buffers, UVC/UAC data paths and CDC UART are
LiteX/Migen.

Clock domains: "phy" (60MHz UTMI clock, created here), "usb_960" (960MHz PHY oversampling clock,
created here), "gclk" (video and audio samples).
"""

from migen import *

from litex.gen import *

from litex.soc.cores.clock.gowin_gw5a import GW5APLL
from litex.soc.cores.usb2_phy.phy        import USB2PHY
from litex.soc.cores.usb2_phy.gowin_gw5a import GW5AUSB2PHYCRG

from chromatix.gateware.usb_class import *
from chromatix.gateware.usb_desc  import USBDescriptors
from chromatix.gateware.usb_fifo  import USBEndpointFIFO

# USB Device ---------------------------------------------------------------------------------------

class USBDevice(LiteXModule):
    """
    USB composite device (UVC + UAC + CDC-ACM) with its own PLL (clk_24 -> 60MHz "phy" / 960MHz
    "usb_960"), Gowin USB 2.0 Device Controller and UTMI PHY (LiteX USB2PHY).
    """
    def __init__(self, platform, clk_24, pads):
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
        self.desc = desc = ClockDomainsRenamer("phy")(USBDescriptors())
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
        for name, cls in [("ctrl_uart", CDCACMControl), ("ctrl_uvc", UVCControl), ("ctrl_uac", UACControl)]:
            h = ClockDomainsRenamer("phy")(cls(sp))
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
        self.uvc = uvc = ClockDomainsRenamer({"sys": "phy", "video": "gclk"})(UVCVideo())
        self.comb += [
            uvc.reset.eq(rst),
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
            i_txiso_pid_i            = Constant(0b0011, 4), # DATA0 (HS, 1 packet per micro-frame).
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
        self.comb += [
            usb_phy.reset.eq(utmi.reset | rst),
            usb_phy.tx_data.eq(utmi.dataout),
            usb_phy.tx_valid.eq(utmi.txvalid),
            usb_phy.op_mode.eq(utmi.opmode),
            usb_phy.xcvr_select.eq(utmi.xcvrselect),
            usb_phy.term_select.eq(utmi.termselect),
            utmi.datain.eq(usb_phy.rx_data),
            utmi.txready.eq(usb_phy.tx_ready),
            utmi.rxvalid.eq(usb_phy.rx_valid),
            utmi.rxactive.eq(usb_phy.rx_active),
            utmi.rxerror.eq(usb_phy.rx_error),
            utmi.linestate.eq(usb_phy.line_state),
        ]

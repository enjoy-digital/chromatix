#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
Chromatic USB composite device (UVC + UAC + CDC-ACM) on the LUNA USB 2.0 device core.

The protocol engine is LUNA (usb_luna_core.py, Amaranth, converted to Verilog at build time with
LiteX's Amaranth2VConverter); the PLL, UTMI PHY (USB2PHY), descriptors ROM, class request handlers,
UVC/UAC data paths and CDC UART are the LiteX/Migen ones also used with the Gowin controller.

Clock domains: "phy" (60MHz UTMI clock, created here, also LUNA's "usb" domain), "usb_960" (960MHz
PHY oversampling clock, created here), "gclk" (video and audio samples).
"""

from types import SimpleNamespace

from migen import *

from litex.gen import *

from litex.soc.interconnect import stream

from litex.soc.cores.clock.gowin_gw5a import GW5APLL
from litex.soc.cores.usb2_phy.phy        import USB2PHY
from litex.soc.cores.usb2_phy.gowin_gw5a import GW5AUSB2PHYCRG

from chromatix.gateware.usb_class import *
from chromatix.gateware.usb_desc  import USBDescriptors, VIDEO_FRAMES

# Versions -----------------------------------------------------------------------------------------

LUNA_REQUIREMENTS = {"amaranth": "0.5.8", "luna-usb": "0.2.3"}

def check_luna():
    """Checks that the pinned Amaranth/LUNA versions are installed (raises otherwise)."""
    from importlib.metadata import version, PackageNotFoundError
    for package, required in LUNA_REQUIREMENTS.items():
        try:
            installed = version(package)
        except PackageNotFoundError:
            raise RuntimeError(f"{package} not installed: pip3 install --user {package}=={required}")
        if not installed.startswith(required):
            raise RuntimeError(f"{package} {installed} installed, {required} required.")

# LUNA Controller ----------------------------------------------------------------------------------

class LUNAUSBController(LiteXModule):
    """LUNADeviceCore converted to Verilog; its ports are Migen Signals (attributes, same names)."""
    def __init__(self, platform, reset=None, **kwargs):
        check_luna()
        from litex.build.amaranth2v_converter import Amaranth2VConverter
        from chromatix.gateware.usb_luna_core import LUNADeviceCore

        self.core = core = LUNADeviceCore(**kwargs)
        ports = {
            "i_usb_clk"  : ClockSignal("phy"),
            "i_usb_rst"  : ResetSignal("phy") if reset is None else reset,
            "i_sync_clk" : ClockSignal("phy"),
            "i_sync_rst" : ResetSignal("phy") if reset is None else reset,
        }
        for s in core.ports:
            name = s.name
            sig  = Signal(len(s), name=name)
            setattr(self, name, sig)
            ports[f"{core.port_dirs[name]}_{name}"] = sig
        self.converter = Amaranth2VConverter(platform,
            name    = "luna_usb_device",
            module  = core,
            ports   = ports,
            domains = ["usb"],
        )

# Descriptors Request Handler ----------------------------------------------------------------------

class USBDescriptorRequest(LiteXModule):
    """
    GET_DESCRIPTOR from the descriptors ROM (USBDescriptors): device, configuration (same for FS/HS),
    device qualifier and strings; other types are not answered (STALL). The descriptor lookup is
    registered (setup fields are stable during the request), the ROM is read asynchronously at the
    descriptor address + cdata_ofs.
    """
    def __init__(self, setup, desc):
        self.reset     = Signal()
        self.txval     = Signal()
        self.txdat     = Signal(8)
        self.txdat_len = Signal(12)
        self.txpop     = Signal() # Unused (the ROM is addressed by cdata_ofs).

        # # #

        l = desc.layout
        s = setup
        addr   = Signal(16)
        length = Signal(16)
        found  = Signal()
        table  = [
            # (type, index, address, length).
            (1, 0, l.dev_addr,        l.dev_len),
            (2, 0, l.hscfg_addr,      l.hscfg_len),
            (6, 0, l.qual_addr,       l.qual_len),
            (3, 0, l.strlang_addr,    4),
            (3, 1, l.strvendor_addr,  l.strvendor_len),
            (3, 2, l.strproduct_addr, l.strproduct_len),
            (3, 3, l.strserial_addr,  l.strserial_len),
        ]
        cases = {"default": [addr.eq(0), length.eq(0), found.eq(0)]}
        for dtype, index, a, n in table:
            cases[(dtype << 8) | index] = [addr.eq(a), length.eq(n), found.eq(1)]
        is_get_descriptor = Signal()
        self.sync += [
            Case(s.wValue, cases),
            is_get_descriptor.eq(s.header_ready & (s.bmRequestType == 0x80) & (s.bRequest == 0x06)),
        ]
        self.comb += [
            self.txval.eq(s.header_ready & is_get_descriptor & found),
            self.txdat_len.eq(length),
            desc.descrom_raddr.eq(addr + s.cdata_ofs),
            self.txdat.eq(desc.descrom_rdat),
        ]

# USB Device (LUNA) --------------------------------------------------------------------------------

class USBDeviceLUNA(LiteXModule):
    """
    USB composite device (UVC + UAC + CDC-ACM) with its own PLL (clk_24 -> 60MHz "phy" / 960MHz
    "usb_960"), LUNA USB 2.0 device core and LiteX UTMI PHY (USB2PHY). Same interface as USBDevice.
    """
    def __init__(self, platform, clk_24, pads, uvc_frames=VIDEO_FRAMES, with_utmi_monitor=False):
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
            from chromatix.gateware.usb_device import UTMIMonitor
            utmi = SimpleNamespace(
                txvalid  = luna.utmi_tx_valid,
                txready  = usb_phy.tx_ready,
                dataout  = luna.utmi_tx_data,
                rxactive = usb_phy.rx_active,
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
        self.uvc = uvc = ClockDomainsRenamer({"sys": "phy", "video": "gclk"})(UVCVideo(frames=uvc_frames, luna=True))
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
        self.uac = uac = ClockDomainsRenamer({"sys": "phy", "audio": "gclk"})(UACEndpoint(luna=True))
        self.comb += [
            uac.reset.eq(rst),
            uac.sof_rise.eq(luna.sof),
            uac.left.eq(self.left),
            uac.right.eq(self.right),
            uac.txpop.eq(luna.ep5_ready),
            luna.ep5_data.eq(uac.txdat),
            luna.ep5_valid.eq(1),
            luna.ep5_bytes.eq(uac.next_len),
        ]

        # CDC-ACM (EP3) + UART ---------------------------------------------------------------------
        self.uart = uart = ClockDomainsRenamer("phy")(CDCUART())
        self.uart_rx_fifo = rx_fifo = ClockDomainsRenamer("phy")(ResetInserter()(
            stream.SyncFIFO([("data", 8)], 64)))
        self.comb += [
            uart.reset.eq(usbrst | rst),
            uart.baudrate.eq(ctrl_uart.dte_rate),
            # USB -> UART (bytes accepted when the UART FIFO is ready: CDCUART has no ready handshake).
            uart.tx_data.eq(luna.ep3_out_data),
            uart.tx_valid.eq(luna.ep3_out_valid & uart.tx_ready),
            luna.ep3_out_ready.eq(uart.tx_ready),
            # UART -> USB (flushed when no more data is buffered).
            rx_fifo.reset.eq(usbrst | rst),
            rx_fifo.sink.valid.eq(uart.rx_valid),
            rx_fifo.sink.data.eq(uart.rx_data),
            luna.ep3_in_data.eq(rx_fifo.source.data),
            luna.ep3_in_valid.eq(rx_fifo.source.valid),
            rx_fifo.source.ready.eq(luna.ep3_in_ready),
            luna.ep3_in_flush.eq(~rx_fifo.source.valid),
            # UART control lines.
            self.uart_txd.eq(uart.txd),
            uart.rxd.eq(self.uart_rxd),
            self.uart_dtr.eq(ctrl_uart.ctl_sig[0]),
            self.uart_rts.eq(ctrl_uart.ctl_sig[1]),
        ]

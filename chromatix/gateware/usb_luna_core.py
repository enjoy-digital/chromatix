#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
Amaranth side of the LUNA USB 2.0 device integration (converted to Verilog by usb_luna.py).

LUNA (https://github.com/greatscottgadgets/luna, BSD-3-Clause) provides the USB 2.0 device protocol
engine: reset/High-Speed chirp, packets/CRC, handshakes/data toggles, SET_ADDRESS/SET_CONFIGURATION/
GET_STATUS/..., endpoint sequencing and high-bandwidth isochronous IN. The Chromatic class logic
stays in Migen: this module exports flat ports for:

- EP0: a request bridge (GET_DESCRIPTOR, SET/GET_INTERFACE and class requests) presenting the setup
  fields and a byte interface (txdat = byte at cdata_ofs / txpop, rxdat/rxval/rxact) to Migen.
- EP2/EP5: isochronous stream IN endpoints (UVC video, 2x1024 high-bandwidth / UAC audio).
- EP3: bulk stream IN/OUT endpoints (CDC-ACM data).
- EP1/EP4: interrupt IN endpoints that always NAK (UVC status / CDC notification, unused).
"""

from amaranth import *

from luna.gateware.usb.usb2.device                          import USBDevice
from luna.gateware.usb.usb2.request                         import USBRequestHandler, StallOnlyRequestHandler
from luna.gateware.usb.usb2.endpoint                        import EndpointInterface
from luna.gateware.usb.usb2.endpoints.stream                import USBStreamInEndpoint, USBStreamOutEndpoint
from luna.gateware.usb.usb2.endpoints.isochronous_stream_in import USBIsochronousStreamInEndpoint
from luna.gateware.usb.request.standard                     import StandardRequestHandler
from luna.gateware.interface.utmi                           import UTMIInterface

from usb_protocol.types                                     import USBRequestType, USBStandardRequests
from usb_protocol.emitters                                  import DeviceDescriptorCollection

# Constants ----------------------------------------------------------------------------------------

EP0_MAX_PACKET_SIZE = 64

# High-Speed USB Device ----------------------------------------------------------------------------

class HSUSBDevice(USBDevice):
    """LUNA USBDevice on a native UTMI bus at High-Speed (LUNA assumes FS-only for raw UTMI buses)."""
    def __init__(self, *, bus):
        super().__init__(bus=bus, handle_clocking=False)
        self.always_fs  = False
        self.data_clock = 60e6

# Request Bridge -----------------------------------------------------------------------------------

def _bridged(setup):
    """Requests handled by the bridge (the Migen handlers), the others by LUNA."""
    standard = setup.type == USBRequestType.STANDARD
    return (setup.type == USBRequestType.CLASS) | (standard & (
        (setup.request == USBStandardRequests.GET_DESCRIPTOR) |
        (setup.request == USBStandardRequests.SET_INTERFACE)  |
        (setup.request == USBStandardRequests.GET_INTERFACE)))

class ChromaticRequestBridge(USBRequestHandler):
    """
    EP0 request handler forwarding the bridged requests to the Migen handlers.

    IN data stages: txdat is the byte at cdata_ofs (registered sources update it on txpop, and
    reload byte 0 when cdata_ofs returns to 0); bytes are prefetched (2-byte buffer) so that the
    Migen sources have no combinatorial path to the LUNA TX logic; the response length is
    min(wLength, txlen); packets of up to 64 bytes are streamed on data requests, a packet not
    ACK'ed is resent (refetched from its first offset). No txval at the request start: STALL.
    OUT data stages: rxdat/rxval per byte while rxact, then the status stage is ZLP'ed.
    SET_INTERFACE: inf_set/inf_sel/inf_alt_o after the status stage; GET_INTERFACE returns inf_alt_i.
    """
    def __init__(self):
        super().__init__()
        # Setup (to Migen).
        self.header_ready  = Signal()
        self.bmRequestType = Signal(8)
        self.bRequest      = Signal(8)
        self.wValue        = Signal(16)
        self.wIndex        = Signal(16)
        self.wLength       = Signal(16)
        self.cdata_ofs     = Signal(16)
        # IN data (from Migen).
        self.txval         = Signal()
        self.txdat         = Signal(8)
        self.txlen         = Signal(16)
        self.txpop         = Signal()
        # OUT data (to Migen).
        self.rxdat         = Signal(8)
        self.rxval         = Signal()
        self.rxact         = Signal()
        # Interfaces alternate settings.
        self.inf_set       = Signal()
        self.inf_sel       = Signal(8)
        self.inf_alt_o     = Signal(8)
        self.inf_alt_i     = Signal(8)

    def elaborate(self, platform):
        m = Module()
        interface = self.interface
        setup     = interface.setup
        tx        = interface.tx

        is_get_interface = Signal()
        is_set_interface = Signal()
        answered         = Signal()   # Migen handler answering the IN request (latched).
        txval_r          = Signal()   # Registered Migen inputs (timing: no combinatorial path from
        txlen_r          = Signal(16) # the Migen handlers to the LUNA TX logic).
        total            = Signal(16) # IN response length (latched with answered: the handlers
                                      # may drop txval/txlen once their last byte is popped).
        pkt_start        = self._pkt_start = Signal(16)
        pkt_len          = self._pkt_len   = Signal(8)
        pkt_count        = self._pkt_count = Signal(8)
        expecting_ack    = Signal()
        settle           = Signal(3)  # Migen handlers latency (registered lookups/answer).
        pending          = Signal()   # Bridged SETUP received, to be started from IDLE.

        m.d.usb += [
            txval_r.eq(Mux(is_get_interface, 1, self.txval)),
            txlen_r.eq(Mux(is_get_interface, 1, self.txlen)),
        ]
        with m.If(total - pkt_start < EP0_MAX_PACKET_SIZE):
            m.d.comb += pkt_len.eq(total - pkt_start)
        with m.Else():
            m.d.comb += pkt_len.eq(EP0_MAX_PACKET_SIZE)

        # IN data prefetch (2 bytes): bytes are popped from the Migen source (byte at cdata_ofs,
        # next one on txpop) ahead of the transmission; the packet data comes from these registers.
        fifo_data  = Array(Signal(8, name=f"fifo_data{i}") for i in range(2))
        fifo_level = Signal(2)
        fifo_rd    = Signal()
        fifo_wr    = Signal()
        fetching   = Signal()
        flush      = Signal()
        txdat      = Mux(is_get_interface, self.inf_alt_i, self.txdat)
        m.d.comb += [
            fetching.eq((settle == 0) & (fifo_level < 2) & (self.cdata_ofs < total) & answered),
            fifo_wr.eq(fetching),
            self.txpop.eq(fetching & ~is_get_interface),
        ]
        with m.If(flush):
            m.d.usb += fifo_level.eq(0)
        with m.Else():
            with m.If(fifo_rd):
                m.d.usb += fifo_data[0].eq(fifo_data[1])
            with m.If(fifo_wr): # After the read shift (the written byte wins).
                m.d.usb += fifo_data[fifo_level - fifo_rd].eq(txdat)
            m.d.usb += fifo_level.eq(fifo_level + fifo_wr - fifo_rd)
            with m.If(fifo_wr):
                m.d.usb += self.cdata_ofs.eq(self.cdata_ofs + 1)
        with m.If(settle != 0):
            m.d.usb += settle.eq(settle - 1)

        with m.If(setup.type == USBRequestType.CLASS):
            m.d.comb += interface.claim.eq(1)
        with m.If(setup.type == USBRequestType.STANDARD):
            m.d.comb += interface.claim.eq(_bridged(setup))

        # A SETUP aborts any control transfer in progress (the host may abandon one, e.g. after a
        # timeout): back to IDLE, which starts the new request when bridged.
        with m.If(setup.received):
            m.d.usb += pending.eq(_bridged(setup))

        def abort_on_setup():
            with m.If(setup.received):
                m.next = "IDLE"

        with m.FSM(domain="usb"):
            with m.State("IDLE"):
                m.d.comb += flush.eq(1)
                m.d.usb += [
                    self.rxval.eq(0),
                    self.header_ready.eq(0),
                    self.cdata_ofs.eq(0),
                    pkt_start.eq(0),
                    answered.eq(0),
                    expecting_ack.eq(0),
                    interface.tx_data_pid.eq(1), # Data stages start with DATA1.
                ]
                with m.If(pending):
                    m.d.usb += [
                        pending.eq(0),
                        self.header_ready.eq(1),
                        self.bmRequestType.eq(Cat(setup.recipient, setup.type, setup.is_in_request)),
                        self.bRequest.eq(setup.request),
                        self.wValue.eq(setup.value),
                        self.wIndex.eq(setup.index),
                        self.wLength.eq(setup.length),
                        is_get_interface.eq((setup.type == USBRequestType.STANDARD) &
                            (setup.request == USBStandardRequests.GET_INTERFACE)),
                        is_set_interface.eq((setup.type == USBRequestType.STANDARD) &
                            (setup.request == USBStandardRequests.SET_INTERFACE)),
                        settle.eq(6),
                    ]
                    with m.If(setup.is_in_request & (setup.length != 0)):
                        m.next = "IN_WAIT"
                    with m.Elif(setup.length != 0):
                        m.next = "OUT_DATA"
                    with m.Else():
                        m.next = "NO_DATA"

            # IN data stage: latch whether a Migen handler answers (registered txval), then data.
            with m.State("IN_WAIT"):
                with m.If(settle == 0):
                    m.d.usb += [
                        answered.eq(txval_r),
                        total.eq(Mux(txlen_r < self.wLength, txlen_r, self.wLength)),
                    ]
                    m.next = "IN_DATA"
                abort_on_setup()
            with m.State("IN_DATA"):
                with m.If(interface.data_requested):
                    with m.If(~answered):
                        m.d.comb += interface.handshakes_out.stall.eq(1)
                        m.next = "IDLE"
                    with m.Elif(pkt_len == 0):
                        m.d.comb += [tx.valid.eq(1), tx.last.eq(1)] # ZLP.
                        m.d.usb  += expecting_ack.eq(1)
                    with m.Else():
                        m.d.usb += [pkt_count.eq(0), expecting_ack.eq(1)]
                        m.next = "IN_SEND"
                with m.Elif(interface.handshakes_in.ack & expecting_ack):
                    m.d.usb += [
                        pkt_start.eq(pkt_start + pkt_len),
                        interface.tx_data_pid.eq(~interface.tx_data_pid),
                        expecting_ack.eq(0),
                    ]
                with m.If(interface.status_requested):
                    m.d.comb += interface.handshakes_out.ack.eq(1)
                    m.next = "IDLE"
                abort_on_setup()
            with m.State("IN_SEND"):
                m.d.comb += [
                    tx.valid.eq(fifo_level != 0),
                    tx.payload.eq(fifo_data[0]),
                    tx.first.eq(pkt_count == 0),
                    tx.last.eq(pkt_count == (pkt_len - 1)),
                    fifo_rd.eq(tx.ready & (fifo_level != 0)),
                ]
                with m.If(fifo_rd):
                    m.d.usb += pkt_count.eq(pkt_count + 1)
                    with m.If(pkt_count == (pkt_len - 1)):
                        m.next = "IN_ACK"
                abort_on_setup()
            with m.State("IN_ACK"):
                # ACK'ed: continue (prefetched data kept), else resend the packet (refetched).
                with m.If(interface.handshakes_in.ack):
                    m.d.usb += [
                        pkt_start.eq(pkt_start + pkt_len),
                        interface.tx_data_pid.eq(~interface.tx_data_pid),
                        expecting_ack.eq(0),
                    ]
                    m.next = "IN_DATA"
                with m.Elif(interface.data_requested):
                    # Not ACK'ed, re-requested: resend (refetched from the packet start).
                    m.d.comb += flush.eq(1)
                    m.d.usb  += [self.cdata_ofs.eq(pkt_start), settle.eq(2), pkt_count.eq(0)]
                    m.next = "IN_SEND"
                with m.Elif(interface.status_requested):
                    m.d.comb += interface.handshakes_out.ack.eq(1)
                    m.next = "IDLE"
                abort_on_setup()

            # OUT data stage (then status: IN ZLP).
            with m.State("OUT_DATA"):
                # rxval/rxdat registered together, cdata_ofs advanced after the byte (rxdat is the
                # byte at cdata_ofs while rxval).
                m.d.comb += self.rxact.eq(1)
                m.d.usb  += self.rxval.eq(interface.rx.valid & interface.rx.next)
                with m.If(interface.rx.valid & interface.rx.next):
                    m.d.usb += self.rxdat.eq(interface.rx.payload)
                with m.If(self.rxval):
                    m.d.usb += self.cdata_ofs.eq(self.cdata_ofs + 1)
                with m.If(interface.rx_ready_for_response):
                    m.d.comb += interface.handshakes_out.ack.eq(1)
                with m.If(interface.status_requested):
                    m.d.comb += self.send_zlp()
                with m.If(interface.handshakes_in.ack):
                    m.next = "IDLE"
                abort_on_setup()

            # No data stage (status: IN ZLP).
            with m.State("NO_DATA"):
                with m.If(interface.status_requested):
                    m.d.comb += self.send_zlp()
                with m.If(interface.handshakes_in.ack):
                    with m.If(is_set_interface):
                        m.d.comb += [
                            self.inf_set.eq(1),
                            self.inf_sel.eq(self.wIndex[:8]),
                            self.inf_alt_o.eq(self.wValue[:8]),
                        ]
                    m.next = "IDLE"
                abort_on_setup()

        # GET_INTERFACE: interface selection for inf_alt_i.
        with m.If(~self.inf_set):
            m.d.comb += self.inf_sel.eq(self.wIndex[:8])
        return m

# NAK Endpoint -------------------------------------------------------------------------------------

class NAKEndpoint(Elaboratable):
    """IN endpoint that always NAKs (declared endpoint without data)."""
    def __init__(self, *, endpoint_number):
        self._endpoint_number = endpoint_number
        self.interface        = EndpointInterface()

    def elaborate(self, platform):
        m = Module()
        tokenizer = self.interface.tokenizer
        m.d.comb += self.interface.handshakes_out.nak.eq(tokenizer.is_in & tokenizer.ready_for_response &
            (tokenizer.endpoint == self._endpoint_number))
        return m

# LUNA Device Core ---------------------------------------------------------------------------------

class LUNADeviceCore(Elaboratable):
    """
    Top-level converted to Verilog: LUNA HS device on UTMI + Chromatic endpoints, flat ports.

    iso_endpoints: {endpoint_number: max_packet_size} (isochronous stream IN).
    bulk_endpoints: {endpoint_number: max_packet_size} (stream IN + OUT).
    nak_endpoints: IN endpoints that always NAK.
    """
    def __init__(self, iso_endpoints={2: 1024, 5: 24}, bulk_endpoints={3: 512}, nak_endpoints=[1, 4],
        bus=None):
        self._bus           = bus # Simulation: UTMI bus used directly (UTMI ports unused).
        self.iso_endpoints  = iso_endpoints
        self.bulk_endpoints = bulk_endpoints
        self.nak_endpoints  = nak_endpoints
        self.ports          = []
        self.port_dirs      = {} # name: "i" (to the core) / "o" (from the core).
        self.endpoints      = {} # Debug/simulation access.

        outputs = {"utmi_tx_data", "utmi_tx_valid", "utmi_op_mode", "utmi_xcvr_select",
            "utmi_term_select", "sof", "bus_reset", "high_speed"}
        outputs |= {"ep0_" + n for n in ["header_ready", "bmRequestType", "bRequest", "wValue", "wIndex",
            "wLength", "cdata_ofs", "txpop", "rxdat", "rxval", "rxact", "inf_set", "inf_sel", "inf_alt_o"]}
        outputs |= {f"ep{n}_{s}" for n in iso_endpoints for s in ["ready", "requested", "finished"]}
        outputs |= {f"ep{n}_{s}" for n in bulk_endpoints for s in ["in_ready", "out_data", "out_valid"]}

        def port(name, width=1):
            s = Signal(width, name=name)
            self.ports.append(s)
            self.port_dirs[name] = "o" if name in outputs else "i"
            return s

        # UTMI.
        self.utmi_rx_data     = port("utmi_rx_data", 8)
        self.utmi_rx_active   = port("utmi_rx_active")
        self.utmi_rx_valid    = port("utmi_rx_valid")
        self.utmi_rx_error    = port("utmi_rx_error")
        self.utmi_line_state  = port("utmi_line_state", 2)
        self.utmi_tx_ready    = port("utmi_tx_ready")
        self.utmi_tx_data     = port("utmi_tx_data", 8)
        self.utmi_tx_valid    = port("utmi_tx_valid")
        self.utmi_op_mode     = port("utmi_op_mode", 2)
        self.utmi_xcvr_select = port("utmi_xcvr_select", 2)
        self.utmi_term_select = port("utmi_term_select")
        # Device status.
        self.sof              = port("sof")
        self.bus_reset        = port("bus_reset")
        self.high_speed       = port("high_speed")
        # EP0 bridge.
        self.bridge           = ChromaticRequestBridge()
        for name in ["header_ready", "bmRequestType", "bRequest", "wValue", "wIndex", "wLength",
            "cdata_ofs", "txval", "txdat", "txlen", "txpop", "rxdat", "rxval", "rxact", "inf_set",
            "inf_sel", "inf_alt_o", "inf_alt_i"]:
            s = getattr(self.bridge, name)
            setattr(self, "ep0_" + name, port("ep0_" + name, len(s)))
        # Isochronous IN endpoints.
        for n in iso_endpoints:
            for name, width in [("data", 8), ("valid", 1), ("ready", 1), ("bytes", 12), ("requested", 1),
                ("finished", 1)]:
                setattr(self, f"ep{n}_{name}", port(f"ep{n}_{name}", width))
        # Bulk IN/OUT endpoints.
        for n in bulk_endpoints:
            for name, width in [("in_data", 8), ("in_valid", 1), ("in_ready", 1), ("in_flush", 1),
                ("out_data", 8), ("out_valid", 1), ("out_ready", 1)]:
                setattr(self, f"ep{n}_{name}", port(f"ep{n}_{name}", width))

    def elaborate(self, platform):
        m = Module()

        # UTMI.
        utmi = UTMIInterface() if self._bus is None else self._bus
        m.d.comb += [] if self._bus is not None else [
            utmi.rx_data.eq(self.utmi_rx_data),
            utmi.rx_active.eq(self.utmi_rx_active),
            utmi.rx_valid.eq(self.utmi_rx_valid),
            utmi.rx_error.eq(self.utmi_rx_error),
            utmi.line_state.eq(self.utmi_line_state),
            utmi.tx_ready.eq(self.utmi_tx_ready),
            utmi.vbus_valid.eq(1),
            utmi.session_valid.eq(1),
            self.utmi_tx_data.eq(utmi.tx_data),
            self.utmi_tx_valid.eq(utmi.tx_valid),
            self.utmi_op_mode.eq(utmi.op_mode),
            self.utmi_xcvr_select.eq(utmi.xcvr_select),
            self.utmi_term_select.eq(utmi.term_select),
        ]

        # Device.
        m.submodules.device = device = HSUSBDevice(bus=utmi)
        m.d.comb += [
            device.connect.eq(1),
            self.sof.eq(device.sof_detected),
            self.bus_reset.eq(device.reset_detected),
            self.high_speed.eq(device.speed == 0),
        ]

        # EP0: LUNA standard requests (except the bridged ones), bridge, stall on the others.
        control = device.add_control_endpoint()
        control.add_request_handler(StandardRequestHandler(DeviceDescriptorCollection(),
            skiplist=[_bridged]))
        control.add_request_handler(self.bridge)
        control.add_request_handler(StallOnlyRequestHandler(
            stall_condition=lambda setup: (setup.type == USBRequestType.VENDOR)))
        b = self.bridge
        for name in ["header_ready", "bmRequestType", "bRequest", "wValue", "wIndex", "wLength",
            "cdata_ofs", "txpop", "rxdat", "rxval", "rxact", "inf_set", "inf_sel", "inf_alt_o"]:
            m.d.comb += getattr(self, "ep0_" + name).eq(getattr(b, name))
        for name in ["txval", "txdat", "txlen", "inf_alt_i"]:
            m.d.comb += getattr(b, name).eq(getattr(self, "ep0_" + name))

        # Isochronous IN endpoints.
        for n, max_packet_size in self.iso_endpoints.items():
            ep = USBIsochronousStreamInEndpoint(endpoint_number=n, max_packet_size=max_packet_size)
            device.add_endpoint(ep)
            self.endpoints[f"iso{n}"] = ep
            m.d.comb += [
                ep.stream.payload.eq(getattr(self, f"ep{n}_data")),
                ep.stream.valid.eq(getattr(self, f"ep{n}_valid")),
                getattr(self, f"ep{n}_ready").eq(ep.stream.ready),
                ep.bytes_in_frame.eq(getattr(self, f"ep{n}_bytes")),
                getattr(self, f"ep{n}_requested").eq(ep.data_requested),
                getattr(self, f"ep{n}_finished").eq(ep.frame_finished),
            ]

        # Bulk IN/OUT endpoints.
        for n, max_packet_size in self.bulk_endpoints.items():
            ep_in  = USBStreamInEndpoint(endpoint_number=n, max_packet_size=max_packet_size)
            ep_out = USBStreamOutEndpoint(endpoint_number=n, max_packet_size=max_packet_size)
            device.add_endpoint(ep_in)
            device.add_endpoint(ep_out)
            self.endpoints[f"bulk{n}_in"]  = ep_in
            self.endpoints[f"bulk{n}_out"] = ep_out
            m.d.comb += [
                ep_in.stream.payload.eq(getattr(self, f"ep{n}_in_data")),
                ep_in.stream.valid.eq(getattr(self, f"ep{n}_in_valid")),
                getattr(self, f"ep{n}_in_ready").eq(ep_in.stream.ready),
                ep_in.flush.eq(getattr(self, f"ep{n}_in_flush")),
                getattr(self, f"ep{n}_out_data").eq(ep_out.stream.payload),
                getattr(self, f"ep{n}_out_valid").eq(ep_out.stream.valid),
                ep_out.stream.ready.eq(getattr(self, f"ep{n}_out_ready")),
            ]

        # NAK-only IN endpoints.
        for n in self.nak_endpoints:
            device.add_endpoint(NAKEndpoint(endpoint_number=n))

        return m

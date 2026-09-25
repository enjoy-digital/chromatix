#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
LUNA USB device core (usb_luna_core.py) in Amaranth simulation, driven at the UTMI level by LUNA's
USBDeviceTest host model, with a Python model of the Migen side (descriptor ROM / class handlers /
alternate settings). Skipped when LUNA/Amaranth are not installed.
"""

import pytest

amaranth = pytest.importorskip("amaranth")
pytest.importorskip("luna")

from amaranth.sim import Passive

from luna.gateware.test.usb2  import USBDeviceTest
from luna.gateware.test.utils import usb_domain_test_case
from luna.gateware.usb.usb2   import USBPacketID

from chromatix.gateware.usb_luna_core import LUNADeviceCore
from chromatix.gateware.usb_desc      import USBDescriptorsLayout

# Migen Side Model ---------------------------------------------------------------------------------

LAYOUT = USBDescriptorsLayout(
    vendor_id    = 0x374E,
    product_id   = 0x013f,
    version_bcd  = 0x0200,
    vendor_str   = "ModRetro",
    product_str  = "Chromatic - Player XX",
    serial_str   = "012345678",
    hs_support   = True,
    self_powered = False,
)

def descriptors():
    rom = LAYOUT.rom
    def get(addr, length):
        return list(rom[addr:addr + length])
    return {
        (1, 0): get(LAYOUT.dev_addr,        LAYOUT.dev_len),
        (2, 0): get(LAYOUT.fscfg_addr,      LAYOUT.fscfg_len),
        (6, 0): get(LAYOUT.qual_addr,       LAYOUT.qual_len),
        (3, 0): get(LAYOUT.strlang_addr,    4),
        (3, 2): get(LAYOUT.strproduct_addr, LAYOUT.strproduct_len),
    }

class _Base(USBDeviceTest):
    FRAGMENT_UNDER_TEST = LUNADeviceCore
    FRAGMENT_ARGUMENTS  = {}

    def setUp(self):
        super().setUp()
        self.received  = []
        self.inf_sets  = []
        self.sim.add_sync_process(self.migen_model, domain="usb")

    def migen_model(self):
        """Migen handlers model: txdat = byte at cdata_ofs (next byte loaded with txpop)."""
        yield Passive()
        # Idle bus (J): no bus reset/High-Speed chirp during the test.
        yield self.utmi.line_state.eq(0b01)
        dut   = self.dut
        descs = descriptors()
        alt   = {1: 0, 3: 0, 5: 0}
        answer_delay = 0
        done         = False
        while True:
            header_ready = (yield dut.ep0_header_ready)
            req_type     = (yield dut.ep0_bmRequestType)
            request      = (yield dut.ep0_bRequest)
            value        = (yield dut.ep0_wValue)
            length       = (yield dut.ep0_wLength)
            ofs          = (yield dut.ep0_cdata_ofs)
            data         = None
            if header_ready and req_type == 0x80 and request == 6:
                data = descs.get((value >> 8, value & 0xff), None)
            elif header_ready and req_type == 0xa1 and request == 0x81:
                data = list(range(34)) # UVC GET_CUR probe (pattern).
            # Answer latency (registered lookup of the Migen handlers, ex descriptors).
            answer_delay = answer_delay + 1 if header_ready else 0
            if data is not None:
                data = data[:length]
            # The Migen handlers drop txval/txlen once their last byte is popped (re-armed at
            # cdata_ofs 0).
            if not header_ready or ofs == 0:
                done = False
            if data is not None and (answer_delay < 3 or done):
                yield dut.ep0_txval.eq(0)
                yield dut.ep0_txlen.eq(0)
            elif data is not None:
                # Registered source: the next byte is loaded with txpop (as the Migen handlers).
                pop  = (yield dut.ep0_txpop)
                done = pop and ofs == len(data) - 1
                ofs += pop
                yield dut.ep0_txval.eq(not done)
                yield dut.ep0_txlen.eq(0 if done else len(data))
                yield dut.ep0_txdat.eq(data[ofs] if ofs < len(data) else 0)
            else:
                yield dut.ep0_txval.eq(0)
                yield dut.ep0_txlen.eq(0)
            if (yield dut.ep0_rxval):
                self.received.append((ofs, (yield dut.ep0_rxdat)))
            if (yield dut.ep0_inf_set):
                sel = (yield dut.ep0_inf_sel)
                alt[sel] = (yield dut.ep0_inf_alt_o)
                self.inf_sets.append((sel, alt[sel]))
            yield dut.ep0_inf_alt_i.eq(alt.get((yield dut.ep0_inf_sel), 0))
            yield

# Tests --------------------------------------------------------------------------------------------

class TestLUNADescriptors(_Base):
    @usb_domain_test_case
    def test_get_descriptors(self):
        yield from self.advance_cycles(10)
        # Device descriptor (1 packet).
        handshake, data = yield from self.get_descriptor(1, length=64)
        self.assertEqual(handshake, USBPacketID.ACK)
        self.assertEqual(data, descriptors()[(1, 0)])
        # Configuration descriptor (multi-packet, truncated to wLength then full).
        handshake, data = yield from self.get_descriptor(2, length=9)
        self.assertEqual(data, descriptors()[(2, 0)][:9])
        cfg = descriptors()[(2, 0)]
        handshake, data = yield from self.get_descriptor(2, length=len(cfg))
        self.assertEqual(handshake, USBPacketID.ACK)
        self.assertEqual(data, cfg)
        # Qualifier / strings.
        handshake, data = yield from self.get_descriptor(6, length=10)
        self.assertEqual(data, descriptors()[(6, 0)][:10])
        handshake, data = yield from self.get_descriptor(3, index=2, length=255)
        self.assertEqual(data, descriptors()[(3, 2)])
        # Unknown descriptor: STALL.
        handshake, data = yield from self.get_descriptor(0x0f, length=64)
        self.assertEqual(handshake, USBPacketID.STALL)

class TestLUNAStandardRequests(_Base):
    @usb_domain_test_case
    def test_address_configuration_interfaces(self):
        yield from self.advance_cycles(10)
        # SET_ADDRESS / SET_CONFIGURATION handled by LUNA.
        yield from self.set_address(5)
        self.assertEqual((yield from self.set_configuration(1)), USBPacketID.DATA1)
        handshake, data = yield from self.get_descriptor(1, length=18)
        self.assertEqual(data, descriptors()[(1, 0)])
        # SET_INTERFACE (interface 1, alt 2) -> bridge, then GET_INTERFACE.
        pid = yield from self.control_request_out(0x01, 11, value=2, index=1)
        self.assertEqual(pid, USBPacketID.DATA1) # Status stage ZLP.
        self.assertIn((1, 2), self.inf_sets)
        handshake, data = yield from self.control_request_in(0x81, 10, index=1, length=1)
        self.assertEqual(data, [2])

class TestLUNAClassRequests(_Base):
    @usb_domain_test_case
    def test_class_requests(self):
        yield from self.advance_cycles(10)
        # Class IN (UVC GET_CUR probe, 34 bytes).
        handshake, data = yield from self.control_request_in(0xa1, 0x81, value=0x0100, index=1, length=34)
        self.assertEqual(data, list(range(34)))
        # Class OUT with data (CDC SET_LINE_CODING, 7 bytes): bytes at their cdata_ofs.
        line_coding = [0x00, 0xc2, 0x01, 0x00, 0x00, 0x00, 0x08]
        pid = yield from self.control_request_out(0x21, 0x20, index=2, data=line_coding)
        self.assertEqual(pid, USBPacketID.DATA1) # Status stage ZLP.
        self.assertEqual(self.received, list(enumerate(line_coding)))
        # Class OUT without data (CDC SET_CONTROL_LINE_STATE).
        pid = yield from self.control_request_out(0x21, 0x22, value=3, index=2)
        self.assertEqual(pid, USBPacketID.DATA1)
        # Class IN not answered by the handlers: STALL.
        handshake, data = yield from self.control_request_in(0xa1, 0x85, index=1, length=1)
        self.assertEqual(handshake, USBPacketID.STALL)

class TestLUNANakEndpoints(_Base):
    @usb_domain_test_case
    def test_interrupt_endpoints_nak(self):
        yield from self.advance_cycles(10)
        for ep in [1, 4]:
            pid, data = yield from self.in_transaction(endpoint=ep)
            self.assertEqual(pid, USBPacketID.NAK)

class TestLUNAIsochronous(_Base):
    def setUp(self):
        super().setUp()
        self.sim.add_sync_process(self.stream_model_ep2, domain="usb")
        self.sim.add_sync_process(self.stream_model_ep5, domain="usb")

    def stream_model_ep2(self):
        yield from self.stream_model(2)

    def stream_model_ep5(self):
        yield from self.stream_model(5)

    def send_sof(self, frame):
        from luna.gateware.test.contrib import usb_packet
        yield from self.provide_bits(usb_packet.sof_packet(frame))

    def stream_model(self, n):
        """Byte counter stream on EP n (always valid)."""
        yield Passive()
        count = 0
        while True:
            yield getattr(self.dut, f"ep{n}_valid").eq(1)
            yield getattr(self.dut, f"ep{n}_data").eq(count & 0xff)
            yield
            if (yield getattr(self.dut, f"ep{n}_ready")):
                count += 1

    def receive_iso(self, endpoint):
        yield from self.send_token(USBPacketID.IN, endpoint=endpoint)
        pid, *data = yield from self.receive_packet(as_bytes=False)
        yield from self.interpacket_delay()
        return pid, data[:-2] # Without CRC.

    @usb_domain_test_case
    def test_high_bandwidth_in(self):
        yield from self.advance_cycles(10)
        # 2048 bytes in the micro-frame: DATA1 (1024) then DATA0 (1024), continuous data.
        yield self.dut.ep2_bytes.eq(2048)
        yield from self.send_sof(1)
        yield from self.interpacket_delay()
        pid0, data0 = yield from self.receive_iso(2)
        pid1, data1 = yield from self.receive_iso(2)
        self.assertEqual((pid0, len(data0)), (USBPacketID.DATA1.byte(), 1024))
        self.assertEqual((pid1, len(data1)), (USBPacketID.DATA0.byte(), 1024))
        self.assertEqual(data0 + data1, [i & 0xff for i in range(2048)])
        # 12 bytes (header only): a single DATA0 packet.
        yield self.dut.ep2_bytes.eq(12)
        yield from self.send_sof(2)
        yield from self.interpacket_delay()
        pid, data = yield from self.receive_iso(2)
        self.assertEqual((pid, len(data)), (USBPacketID.DATA0.byte(), 12))
        self.assertEqual(data, [i & 0xff for i in range(2048, 2060)])

    @usb_domain_test_case
    def test_audio_in(self):
        yield from self.advance_cycles(10)
        yield self.dut.ep5_bytes.eq(24)
        yield from self.send_sof(1)
        yield from self.interpacket_delay()
        pid, data = yield from self.receive_iso(5)
        self.assertEqual((pid, len(data)), (USBPacketID.DATA0.byte(), 24))

class TestLUNABulk(_Base):
    def setUp(self):
        super().setUp()
        self.bulk_received = []
        self.sim.add_sync_process(self.bulk_sink, domain="usb")

    def bulk_sink(self):
        yield Passive()
        dut = self.dut
        yield dut.ep3_out_ready.eq(1)
        while True:
            yield
            if (yield dut.ep3_out_valid):
                self.bulk_received.append((yield dut.ep3_out_data))

    @usb_domain_test_case
    def test_bulk_out_in(self):
        dut = self.dut
        yield from self.advance_cycles(10)
        # OUT: bytes on the ep3 out stream.
        pid = yield from self.out_transaction(*b"hello", endpoint=3, data_pid=USBPacketID.DATA0)
        self.assertEqual(pid, USBPacketID.ACK)
        yield from self.advance_cycles(20)
        self.assertEqual(bytes(self.bulk_received), b"hello")
        # IN: bytes pushed on the ep3 in stream, flushed.
        for c in b"world":
            yield dut.ep3_in_data.eq(c)
            yield dut.ep3_in_valid.eq(1)
            yield
            while not (yield dut.ep3_in_ready):
                yield
        yield dut.ep3_in_valid.eq(0)
        yield dut.ep3_in_flush.eq(1)
        yield from self.advance_cycles(4)
        yield dut.ep3_in_flush.eq(0)
        pid, data = yield from self.in_transaction(endpoint=3)
        self.assertEqual(bytes(data), b"world")

class TestLUNAResend(_Base):
    @usb_domain_test_case
    def test_in_packet_resend(self):
        """An EP0 IN packet not ACK'ed by the host is resent (same PID and data)."""
        yield from self.advance_cycles(10)
        cfg = descriptors()[(2, 0)]
        yield from self.setup_transaction(0x80, 6, 0x0200, 0, len(cfg))
        yield from self.control_interphase_delay()
        packets = []
        for ack in [False, True, True]:
            yield from self.send_token(USBPacketID.IN, endpoint=0)
            pid, *data = yield from self.receive_packet(as_bytes=False)
            yield from self.interpacket_delay()
            if ack:
                yield from self.send_handshake(USBPacketID.ACK)
            yield from self.advance_cycles(8)
            packets.append((pid, data[:-2]))
        self.assertEqual(packets[0], packets[1])                       # Resent.
        self.assertEqual(packets[0][0], USBPacketID.DATA1.byte())
        self.assertEqual(packets[0][1], cfg[:64])
        self.assertEqual(packets[2], (USBPacketID.DATA0.byte(), cfg[64:128])) # Then continues.

class TestLUNAAbort(_Base):
    @usb_domain_test_case
    def test_setup_aborts_transfer(self):
        """A SETUP aborts an abandoned control transfer: the new request is answered."""
        yield from self.advance_cycles(10)
        cfg = descriptors()[(2, 0)]
        # IN data stage abandoned after the first packet (no status stage).
        yield from self.setup_transaction(0x80, 6, 0x0200, 0, len(cfg))
        yield from self.control_interphase_delay()
        pid, data = yield from self.in_transaction(endpoint=0)
        self.assertEqual(data, list(cfg[:64]))
        yield from self.advance_cycles(8)
        handshake, data = yield from self.get_descriptor(1, length=18)
        self.assertEqual(data, list(descriptors()[(1, 0)][:18]))
        # OUT data stage abandoned (SETUP only).
        yield from self.setup_transaction(0x21, 0x20, 0, 2, 7)
        yield from self.advance_cycles(8)
        handshake, data = yield from self.control_request_in(0xa1, 0x81, value=0x0100, index=1, length=34)
        self.assertEqual(handshake, USBPacketID.ACK)

class TestLUNAShortIn(_Base):
    @usb_domain_test_case
    def test_short_in_request(self):
        """IN requests shorter than the prefetch (handler done before the IN token) are answered."""
        yield from self.advance_cycles(10)
        for length in [1, 2, 3]:
            handshake, data = yield from self.get_descriptor(1, length=length)
            self.assertEqual(data, list(descriptors()[(1, 0)][:length]))

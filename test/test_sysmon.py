#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litex.gen import *
from litex.gen.sim import run_simulation

from chromatix.gateware.sysmon import SystemMonitorRxPacket, SystemMonitorTxPacket
from chromatix.gateware.sysmon import SystemMonitorArbiterBridge, SystemMonitorBridge
from chromatix.gateware.sysmon import SystemMonitorPayloads, SystemMonitorUART
from chromatix.gateware.sysmon import SystemMonitorCartLink

# Helpers ------------------------------------------------------------------------------------------

SOF = 0x8F

def crc8(data, crc=0xFF):
    """CRC-8 (poly 0x1D, init 0xFF, MSB first) used by the ESP32 <-> FPGA packet protocol."""
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1D) if (crc & 0x80) else (crc << 1)
            crc &= 0xFF
    return crc

def make_packet(address, payload):
    frame = [SOF, address, len(payload)] + list(payload)
    return frame + [crc8(frame)]

# RX Packet ----------------------------------------------------------------------------------------

def rx_packet_sim(frame):
    dut     = SystemMonitorRxPacket()
    decoded = []

    def send():
        for byte in frame:
            yield dut.uart_rx_data.eq(byte)
            yield dut.uart_rx_val.eq(1)
            yield
            yield dut.uart_rx_val.eq(0)
            for _ in range(16): # Leave time for the bit-serial CRC update.
                yield

    @passive
    def monitor():
        while True:
            if (yield dut.rx_data_val):
                decoded.append(((yield dut.rx_address), (yield dut.rx_data)))
            yield

    run_simulation(dut, [send(), monitor()])
    return decoded

def test_rx_packet_decode():
    """A well-formed packet is decoded: address and payload (last byte in LSBs) are output."""
    payload = [0x12, 0x34, 0x56, 0x78]
    decoded = rx_packet_sim(make_packet(0x0C, payload))
    assert len(decoded) == 1
    address, data = decoded[0]
    assert address == 0x0C
    assert data    == int.from_bytes(bytes(payload), "big")

def test_rx_packet_bad_crc():
    """A packet with a corrupted CRC is dropped."""
    frame = make_packet(0x0D, [0xAA, 0x55])
    frame[-1] ^= 0x01
    assert rx_packet_sim(frame) == []

# TX Packet ----------------------------------------------------------------------------------------

def test_tx_packet_framing():
    """A write request is serialized as SOF, address, count, payload and CRC."""
    dut     = SystemMonitorTxPacket()
    payload = [0x3A, 0xC5]
    sent    = []

    def stimulus():
        yield dut.tx_address.eq(0x06)
        yield dut.tx_byte_count.eq(len(payload))
        for _ in range(4):
            yield
        yield dut.write.eq(1)
        yield
        yield dut.write.eq(0)
        for _ in range(2000):
            if (yield dut.write_done):
                break
            yield

    @passive
    def payload_source():
        while True:
            bytepos = (yield dut.tx_bytepos)
            yield dut.tx_senddata.eq(payload[bytepos] if bytepos < len(payload) else 0)
            yield

    @passive
    def uart():
        # Emulate the UART PHY: capture the byte and stay busy for the byte time.
        while True:
            if (yield dut.uart_tx_val):
                sent.append((yield dut.uart_tx_data))
                yield dut.uart_tx_busy.eq(1)
                for _ in range(24):
                    yield
                yield dut.uart_tx_busy.eq(0)
            yield

    run_simulation(dut, [stimulus(), payload_source(), uart()])
    assert sent == make_packet(0x06, payload)

# Payloads -----------------------------------------------------------------------------------------

def test_payloads_version():
    """Channel 6 returns the 14-bit FPGA version (split as upper 6 bits + lower 8 bits)."""
    dut     = SystemMonitorPayloads()
    version = SystemMonitorPayloads.VERSION
    result  = []

    def gen():
        yield dut.tx_channel.eq(6)
        for bytepos in range(2):
            yield dut.tx_bytepos.eq(bytepos)
            yield
            result.append((yield dut.tx_senddata))
        result.append((yield dut.tx_byte_count))

    run_simulation(dut, gen())
    assert result == [(version >> 8) & 0x3F, version & 0xFF, 2]

# Arbiter ------------------------------------------------------------------------------------------

def test_arbiter_serves_pending_channels():
    """Each pending channel is written once (one write pulse per packet), idle ones are skipped."""
    dut      = SystemMonitorArbiterBridge()
    requests = {0, 6, 9}
    writes   = []

    def stimulus():
        for _ in range(4):
            yield
        yield dut.channels_new_data_valid.eq(sum(1 << c for c in requests))
        yield
        yield dut.channels_new_data_valid.eq(0)
        for _ in range(600):
            yield

    @passive
    def tx_packet():
        # Emulate the TX framer: write -> busy for a few cycles -> write_done.
        while True:
            if (yield dut.write):
                writes.append(((yield dut.tx_address), (yield dut.tx_channel)))
                for _ in range(8):
                    yield
                yield dut.write_done.eq(1)
                yield
                yield dut.write_done.eq(0)
            yield

    run_simulation(dut, [stimulus(), tx_packet()])
    assert sorted(address for address, _ in writes) == sorted(requests)
    assert all(address == channel for address, channel in writes)

# Bridge + UART (TX -> RX Round Trip) --------------------------------------------------------------

class SystemMonitorLoopback(LiteXModule):
    """Payloads -> Bridge -> UART, with the UART TX looped back to the UART RX."""
    def __init__(self, menu_disabled):
        self.pads     = pads     = Record([("tx", 1), ("rx", 1)])
        self.uart     = uart     = SystemMonitorUART(pads, clk_freq=1e6, baudrate=250e3)
        self.bridge   = bridge   = SystemMonitorBridge()
        self.payloads = payloads = SystemMonitorPayloads()
        self.comb += [
            pads.rx.eq(pads.tx),
            uart.enable.eq(1),
            payloads.menu_disabled.eq(menu_disabled),
            payloads.btn_menu.eq(1),
            # UART <-> Bridge.
            uart.tx_data.eq(bridge.uart_tx_data),
            uart.tx_val.eq(bridge.uart_tx_val),
            bridge.uart_tx_busy.eq(uart.tx_busy),
            bridge.uart_rx_data.eq(uart.rx_data),
            bridge.uart_rx_val.eq(uart.rx_val),
            # Bridge <-> Payloads.
            bridge.menu_disabled.eq(payloads.menu_disabled),
            bridge.channels_new_data_valid.eq(payloads.channels_new_data_valid),
            bridge.tx_byte_count.eq(payloads.tx_byte_count),
            bridge.tx_senddata.eq(payloads.tx_senddata),
            payloads.tx_channel.eq(bridge.tx_channel),
            payloads.tx_bytepos.eq(bridge.tx_bytepos),
        ]

def loopback_sim(menu_disabled, stimulus, cycles):
    dut     = SystemMonitorLoopback(menu_disabled)
    decoded = []
    sent    = []

    def gen():
        yield from stimulus(dut)
        for _ in range(cycles):
            yield

    @passive
    def monitor():
        while True:
            if (yield dut.bridge.rx_data_val):
                # rx_data is a shift register: only the last 16 bits belong to 2-byte packets.
                decoded.append(((yield dut.bridge.rx_address), (yield dut.bridge.rx_data) & 0xFFFF))
            if (yield dut.uart.rx_val):
                sent.append((yield dut.uart.rx_data))
            yield

    run_simulation(dut, [gen(), monitor()])
    return decoded, sent

def test_bridge_uart_round_trip():
    """Menu open: channel packets go out on the UART with a valid CRC and decode back on RX."""
    def stimulus(dut):
        yield dut.payloads.btn_a.eq(1)
        yield dut.payloads.system_control.eq(0x1234)
        yield dut.payloads.brightness.eq(5)
        yield dut.payloads.h_volume.eq(0x33)
        yield

    decoded, _ = loopback_sim(menu_disabled=0, stimulus=stimulus, cycles=12000)
    packets    = dict(decoded)
    version    = SystemMonitorPayloads.VERSION
    # Buttons: A (bit 3), Menu released (bit 8 = ~btn_menu = 0), menu open (bit 9 = 0).
    assert packets[2] == 1 << 3
    assert packets[3] == (5 << 8) | 0x33
    assert packets[4] == 0x1234 & 0x3FFF
    assert packets[6] == version
    assert 9 not in packets # No Game Palette Data request.
    assert {address for address, _ in decoded} >= {2, 3, 4, 5, 6, 7, 8}

def test_bridge_uart_wakeup_packet():
    """Menu closed: a requested packet is preceded by 0x00 wake-up bytes and decoded intact."""
    def stimulus(dut):
        for _ in range(16):
            yield
        yield dut.payloads.request_version.eq(1)
        yield
        yield dut.payloads.request_version.eq(0)

    decoded, sent = loopback_sim(menu_disabled=1, stimulus=stimulus, cycles=6000)
    version       = SystemMonitorPayloads.VERSION
    packet        = make_packet(6, [(version >> 8) & 0x3F, version & 0xFF])
    assert decoded == [(6, version)]
    assert sent[-len(packet):] == packet
    assert len(sent) > len(packet) + 15
    assert set(sent[:-len(packet)]) == {0x00}

# Cartridge Link (ChroMagic Protocol) --------------------------------------------------------------

def test_cart_link_request_decode():
    """0x0e packets are decoded as requests (fields, invalid encodings passed as operation 7)."""
    class DUT(LiteXModule):
        def __init__(self):
            self.rx   = rx   = SystemMonitorRxPacket()
            self.link = link = SystemMonitorCartLink()
            self.comb += [
                link.rx_address.eq(rx.rx_address),
                link.rx_data.eq(rx.rx_data),
                link.rx_data_val.eq(rx.rx_data_val),
            ]
    dut      = DUT()
    requests = []
    packets  = [
        [2, 0x11, 0xa0, 0x00, 0x00, 0x00, 0x00, 0x00], # ReadBlock 0xa000, tag 0x11.
        [7, 0x22, 0x56, 0x43, 0x03, 0x12, 0x34, 0x56], # Virtual cartridge PREPARE (op 7, "VC").
        [2, 0x33, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01], # Invalid: aux fields set for op != 7.
    ]

    def send():
        yield dut.link.request.ready.eq(1)
        for payload in packets:
            for byte in make_packet(0x0e, payload):
                yield dut.rx.uart_rx_data.eq(byte)
                yield dut.rx.uart_rx_val.eq(1)
                yield
                yield dut.rx.uart_rx_val.eq(0)
                for _ in range(16):
                    yield
            for _ in range(8):
                yield

    @passive
    def monitor():
        while True:
            r = dut.link.request
            if (yield r.valid) & (yield r.ready):
                requests.append(((yield r.operation), (yield r.tag), (yield r.address), (yield r.value),
                    (yield r.aux_address), (yield r.aux_value)))
            yield

    run_simulation(dut, [send(), monitor()])
    assert requests == [
        (2, 0x11, 0xa000, 0x00, 0x0000, 0x00),
        (7, 0x22, 0x5643, 0x03, 0x1234, 0x56),
        (7, 0x33, 0x0000, 0x00, 0x0000, 0x01),
    ]

def test_cart_link_response_packet():
    """Menu closed (game running): a response is sent on channel 0x0a (after wake-up bytes) and
    consumed once sent."""
    class DUT(SystemMonitorLoopback):
        def __init__(self):
            self.pads     = pads     = Record([("tx", 1), ("rx", 1)])
            self.uart     = uart     = SystemMonitorUART(pads, clk_freq=1e6, baudrate=250e3)
            self.bridge   = bridge   = SystemMonitorBridge(num_channels=14)
            self.payloads = payloads = SystemMonitorPayloads(num_channels=14)
            self.link     = link     = SystemMonitorCartLink()
            self.comb += [
                pads.rx.eq(pads.tx),
                uart.enable.eq(1),
                payloads.menu_disabled.eq(1),
                payloads.btn_menu.eq(1),
                uart.tx_data.eq(bridge.uart_tx_data),
                uart.tx_val.eq(bridge.uart_tx_val),
                bridge.uart_tx_busy.eq(uart.tx_busy),
                bridge.uart_rx_data.eq(uart.rx_data),
                bridge.uart_rx_val.eq(uart.rx_val),
                bridge.menu_disabled.eq(1),
                bridge.channels_new_data_valid.eq(payloads.channels_new_data_valid),
                bridge.tx_byte_count.eq(payloads.tx_byte_count),
                bridge.tx_senddata.eq(payloads.tx_senddata),
                payloads.tx_channel.eq(bridge.tx_channel),
                payloads.tx_bytepos.eq(bridge.tx_bytepos),
                link.tx_channel.eq(bridge.tx_channel),
                link.tx_bytepos.eq(bridge.tx_bytepos),
                link.write_done.eq(bridge.write_done),
                payloads.cart_new_data.eq(link.new_data),
                payloads.cart_byte_count.eq(link.byte_count),
                payloads.cart_senddata.eq(link.senddata),
            ]
    dut  = DUT()
    sent = []
    res  = {}

    def gen():
        for _ in range(16):
            yield
        r = dut.link.response
        yield r.valid.eq(1)
        yield r.operation.eq(7)
        yield r.tag.eq(0x5a)
        yield r.status.eq(3)
        yield r.count.eq(4)
        yield r.data.eq(0x12345678)
        while not ((yield r.valid) & (yield r.ready)):
            yield
        yield
        yield r.valid.eq(0)
        for _ in range(3000):
            yield
        res["sent"] = list(sent)

    @passive
    def monitor():
        while True:
            if (yield dut.uart.rx_val):
                sent.append((yield dut.uart.rx_data))
            yield

    run_simulation(dut, [gen(), monitor()])
    packet = make_packet(0x0a, [7, 0x5a, 3, 4, 0x78, 0x56, 0x34, 0x12])
    assert res["sent"][-len(packet):] == packet
    assert set(res["sent"][:-len(packet)]) == {0x00}

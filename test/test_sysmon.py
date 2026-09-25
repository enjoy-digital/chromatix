#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litex.gen.sim import run_simulation

from test.common import run_domain_simulation

from chromatix.gateware.sysmon import SystemMonitorRxPacket, SystemMonitorTxPacket, SystemMonitorPayloads

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

    run_domain_simulation(dut, {"gclk": [send(), monitor()]}, clocks={"gclk": 10})
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

    run_domain_simulation(dut, {"gclk": [stimulus(), payload_source(), uart()]}, clocks={"gclk": 10})
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

#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from test.common import run_domain_simulation

from chromatix.gateware.codec import TLV320Init, PollingMaster, load_tlv320_registers

# Helpers ------------------------------------------------------------------------------------------

def i2c_responder(dut, transactions, read_data={}, busy_cycles=8):
    """Emulate the enable/busy I2C bridge: capture transactions and return read data."""
    prev_enable = 0
    while True:
        enable = (yield dut.i2c_enable)
        if enable and not prev_enable:
            transaction = (
                (yield dut.i2c_device_address),
                (yield dut.i2c_register_address),
                (yield dut.i2c_read_write),
                (yield dut.i2c_mosi_data),
            )
            transactions.append(transaction)
            yield dut.i2c_busy.eq(1)
            for _ in range(busy_cycles):
                yield
            if hasattr(dut, "i2c_miso_data"):
                yield dut.i2c_miso_data.eq(read_data.get(transaction[:2], 0))
            yield dut.i2c_busy.eq(0)
        prev_enable = enable
        yield

# TLV320 Init --------------------------------------------------------------------------------------

def test_tlv320_init_writes_registers():
    """Each 16-bit register image entry is written as (register, value) to the codec at 0x18."""
    registers    = [0x0001, 0x0B81, 0x0C82, 0x3C08]
    dut          = TLV320Init(registers)
    transactions = []

    def stimulus():
        for _ in range(1000):
            if (yield dut.done):
                return
            yield
        raise AssertionError("TLV320 init never completed")

    run_domain_simulation(dut, {"hclk": [stimulus(), passive(i2c_responder)(dut, transactions)]}, clocks={"hclk": 10})
    assert transactions == [(0x18, reg >> 8, 0, reg & 0xFF) for reg in registers]

def test_tlv320_register_image():
    """The TLV320 register image loads as 16-bit (register, value) words."""
    registers = load_tlv320_registers()
    assert len(registers) > 0
    assert all(0 <= word < 2**16 for word in registers)

# Polling Master -----------------------------------------------------------------------------------

def test_polling_master_captures_status():
    """One polling round reads codec volume/GPIO and PMIC status registers."""
    dut          = PollingMaster()
    transactions = []
    read_data    = {
        (0x18, 117): 0x42, # Codec volume.
        (0x18,  51): 0x02, # Codec GPIO (headphones detected).
        (0x6b, 0x08): 0xA5, # PMIC system status.
    }

    def stimulus():
        yield dut.enable.eq(1)
        for _ in range(3000):
            yield
        assert (yield dut.volume)          == 0x42
        assert (yield dut.gpio)            == 0x02
        assert (yield dut.pmic_sys_status) == 0xA5

    run_domain_simulation(dut, {"hclk": [stimulus(), passive(i2c_responder)(dut, transactions, read_data)]}, clocks={"hclk": 10})
    # Headphones detected: speaker path muted / headphone path enabled (register 0x26 = 0x7F).
    assert (0x18, 0x26, 0, 0x7F) in transactions
    assert transactions[0][:3] == (0x18, 117, 1)

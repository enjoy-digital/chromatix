#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litex.gen.sim import run_simulation

from chromatix.gateware.codec import TLV320Init, PollingMaster, CodecI2S, CodecControl
from chromatix.gateware.codec import load_tlv320_registers

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

    run_simulation(dut, [stimulus(), passive(i2c_responder)(dut, transactions)])
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

    run_simulation(dut, [stimulus(), passive(i2c_responder)(dut, transactions, read_data)])
    # Headphones detected: speaker path muted / headphone path enabled (register 0x26 = 0x7F).
    assert (0x18, 0x26, 0, 0x7F) in transactions
    assert transactions[0][:3] == (0x18, 117, 1)

# Codec I2S ----------------------------------------------------------------------------------------

def test_codec_i2s_stereo_frame():
    """With headphones, inverted samples are sent MSB first: right while WCLK=1, then left (I2S)."""
    pads = Record([("mclk", 1), ("bclk", 1), ("din", 1), ("reset", 1), ("wclk", 1)], name="codec")
    dut  = CodecI2S(pads)
    left, right = 0x1234, 0x0F0F
    frames = []

    def gen():
        yield dut.enable.eq(1)
        yield dut.headphones.eq(1)
        yield dut.left.eq(left)
        yield dut.right.eq(right)
        bits      = None
        prev_bclk = 0
        prev_wclk = 0
        for _ in range(2000):
            bclk = (yield pads.bclk)
            wclk = (yield pads.wclk)
            if wclk and not prev_wclk:
                if bits is not None and len(bits) == 32:
                    frames.append(int("".join(map(str, bits)), 2))
                bits = []
            if bits is not None and bclk and not prev_bclk and len(bits) < 32:
                bits.append((yield pads.din))
            prev_bclk = bclk
            prev_wclk = wclk
            yield

    run_simulation(dut, gen())
    assert len(frames) >= 2
    assert frames[-1] == ((-right & 0xFFFF) << 16) | (-left & 0xFFFF)

def i2s_frames(headphones, mute, left, right, cycles=2000):
    """Run CodecI2S and capture the 32-bit frames (MSB first, sampled on BCLK rising edges)."""
    pads   = Record([("mclk", 1), ("bclk", 1), ("din", 1), ("reset", 1), ("wclk", 1)], name="codec")
    dut    = CodecI2S(pads)
    frames = []

    def gen():
        yield dut.enable.eq(1)
        yield dut.headphones.eq(headphones)
        yield dut.mute.eq(mute)
        yield dut.left.eq(left)
        yield dut.right.eq(right)
        bits      = None
        wclks     = None
        prev_bclk = 0
        prev_wclk = 0
        for _ in range(cycles):
            bclk = (yield pads.bclk)
            wclk = (yield pads.wclk)
            if wclk and not prev_wclk:
                if bits is not None and len(bits) == 32:
                    frames.append((int("".join(map(str, bits)), 2), wclks))
                bits  = []
                wclks = []
            if bits is not None and bclk and not prev_bclk and len(bits) < 32:
                bits.append((yield pads.din))
                wclks.append(wclk)
            prev_bclk = bclk
            prev_wclk = wclk
            yield

    run_simulation(dut, gen())
    return frames

def test_codec_i2s_speaker_mono_mix():
    """Without headphones, the inverted L+R mix (/2) is sent in the second half-frame (speaker)."""
    left, right = 0x1000, 0x0300
    frames      = i2s_frames(headphones=0, mute=0, left=left, right=right)
    mono        = (((-left & 0xFFFF) + (-right & 0xFFFF)) >> 1) & 0xFFFF
    assert len(frames) >= 2
    frame, wclks = frames[-1]
    assert frame == mono
    assert wclks == [1]*16 + [0]*16

def test_codec_i2s_mute():
    """Mute zeroes both channels."""
    frames = i2s_frames(headphones=1, mute=1, left=0x1234, right=0x5678)
    assert len(frames) >= 2
    assert all(frame == 0 for frame, _ in frames)

# Codec Control ------------------------------------------------------------------------------------

class I2CSlaveModel:
    """
    Bit-level I2C slave model (any address, ACKs everything) on split open-drain pads.

    Records writes as (address, register, value), answers reads from read_data[(address, register)].
    """
    def __init__(self, pads, read_data):
        self.pads      = pads
        self.read_data = read_data
        self.writes    = []
        self.reads     = []

    @passive
    def generator(self):
        pads      = self.pads
        prev_scl  = 1
        prev_sda  = 1
        drive_low = 0
        state     = "idle"
        bit       = 0
        byte      = 0
        tx_byte   = 0
        address   = 0
        register  = 0
        while True:
            # Open-drain bus with pull-ups.
            scl = 0 if (yield pads.scl_oe) else 1
            sda = 0 if ((yield pads.sda_oe) or drive_low) else 1
            yield pads.scl_i.eq(scl)
            yield pads.sda_i.eq(sda)
            if scl and prev_scl and prev_sda and not sda:   # (Repeated) START.
                state, bit, byte, drive_low = "addr", -1, 0, 0 # -1: skip START SCL falling edge.
            elif scl and prev_scl and not prev_sda and sda: # STOP.
                state, drive_low = "idle", 0
            elif state != "idle" and scl and not prev_scl:  # SCL rising: sample.
                if bit < 8 and state != "rdata":
                    byte = (byte << 1) | sda
                if bit == 8 and state == "rdata" and sda:   # Master NACK: end of read.
                    state = "rdone"
            elif state != "idle" and not scl and prev_scl:  # SCL falling: drive.
                bit += 1
                if bit == 8:
                    drive_low = int(state != "rdata") # ACK received bytes, release for master ACK.
                elif bit == 9:
                    bit, drive_low = 0, 0
                    if state == "addr":
                        address = byte >> 1
                        state   = "rdata" if (byte & 1) else "reg"
                        if state == "rdata":
                            tx_byte = self.read_data.get((address, register), 0)
                            self.reads.append((address, register))
                    elif state == "reg":
                        register = byte
                        state    = "wdata"
                    elif state == "wdata":
                        self.writes.append((address, register, byte))
                    byte = 0
                    if state == "rdata":
                        drive_low = int(not (tx_byte >> 7) & 1)
                elif state == "rdata":
                    drive_low = int(not (tx_byte >> (7 - bit)) & 1)
            prev_scl = scl
            prev_sda = sda
            yield

def test_codec_control_i2c():
    """TLV320 init writes then polling reads go through the LiteI2C PHY to the I2C bus."""
    pads      = Record([
        ("scl_o", 1), ("scl_oe", 1), ("scl_i", 1),
        ("sda_o", 1), ("sda_oe", 1), ("sda_i", 1),
    ])
    registers = [0x0001, 0x0B81]
    dut       = CodecControl(pads, sys_clk_freq=1e6, registers=registers)
    slave     = I2CSlaveModel(pads, read_data={
        (0x18, 117): 0x42, # Codec volume.
        (0x18,  51): 0x02, # Codec GPIO (headphones detected).
        (0x6b, 0x08): 0xA5, # PMIC system status.
    })

    def gen():
        for _ in range(20000):
            if (yield dut.pmic_sys_status) == 0xA5:
                break
            yield
        assert (yield dut.volume)          == 0x42
        assert (yield dut.gpio)            == 0x02
        assert (yield dut.pmic_sys_status) == 0xA5

    run_simulation(dut, [gen(), slave.generator()])
    assert dut.get_csrs() == [] # I2C PHY CSRs not exposed on the SoC bus.
    # TLV320 init first, then polling writes (headphones: speaker muted).
    assert slave.writes[:2] == [(0x18, reg >> 8, reg & 0xFF) for reg in registers]
    assert (0x18, 0x26, 0x7F) in slave.writes[2:]
    assert slave.reads[:2] == [(0x18, 117), (0x18, 51)]

#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

import os

from migen import *

from litex.gen import *

TLV320_REGS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "tlv320_regs.hex")

def load_tlv320_registers(path=TLV320_REGS_PATH):
    with open(path) as f:
        registers = [int(line.strip(), 16) for line in f if line.strip()]
    if not registers:
        raise ValueError(f"No TLV320 register values found in {path}")
    return registers

# TLV320 Init --------------------------------------------------------------------------------------

class TLV320Init(LiteXModule):
    def __init__(self, registers, device_address=0x18):
        self.reset                = Signal()
        self.done                 = Signal()
        self.i2c_busy             = Signal()
        self.i2c_enable           = Signal()
        self.i2c_read_write       = Signal()
        self.i2c_mosi_data        = Signal(8)
        self.i2c_register_address = Signal(8)
        self.i2c_device_address   = Signal(7)

        regindex   = Signal(max=len(registers))
        reg_word   = Signal(16)
        state      = Signal(2, reset=0)
        S_START    = 0
        S_WAITBUSY = 1
        S_WAITDONE = 2
        S_DONE     = 3

        self.comb += [
            reg_word.eq(Array(Constant(value, 16) for value in registers)[regindex]),
            self.done.eq(~self.reset & (state == S_DONE)),
            self.i2c_enable.eq(~self.reset & ((state == S_START) | (state == S_WAITBUSY))),
            self.i2c_read_write.eq(0),
            self.i2c_mosi_data.eq(reg_word[:8]),
            self.i2c_register_address.eq(reg_word[8:16]),
            self.i2c_device_address.eq(device_address),
        ]

        self.sync.hclk += [
            If(self.reset,
                state.eq(S_START),
                regindex.eq(0),
            ).Else(
                Case(state, {
                    S_START: [
                        If(~self.i2c_busy,
                            state.eq(S_WAITBUSY),
                        )
                    ],
                    S_WAITBUSY: [
                        If(self.i2c_busy,
                            state.eq(S_WAITDONE),
                        )
                    ],
                    S_WAITDONE: [
                        If(~self.i2c_busy,
                            If(regindex == (len(registers) - 1),
                                state.eq(S_DONE),
                            ).Else(
                                regindex.eq(regindex + 1),
                                state.eq(S_START),
                            )
                        )
                    ],
                    S_DONE: [],
                })
            )
        ]

# Codec/PMIC Polling Master ------------------------------------------------------------------------

class PollingMaster(LiteXModule):
    def __init__(self, codec_address=0x18, pmic_address=0x6b):
        self.reset                = Signal()
        self.enable               = Signal()
        self.mute                 = Signal()
        self.i2c_busy             = Signal()
        self.i2c_miso_data        = Signal(8)
        self.volume               = Signal(8)
        self.gpio                 = Signal(8)
        self.pmic_sys_status      = Signal(8)
        self.new_fault            = Signal(8)
        self.inlim                = Signal(8)
        self.charge_current       = Signal(8)
        self.i2c_enable           = Signal()
        self.i2c_read_write       = Signal()
        self.i2c_mosi_data        = Signal(8)
        self.i2c_register_address = Signal(8)
        self.i2c_device_address   = Signal(7)

        step       = Signal(max=13)
        state      = Signal(3, reset=0)
        tx_is_read = Signal()
        tx_data    = Signal(8)
        tx_reg     = Signal(8)
        tx_addr    = Signal(7)
        S_IDLE     = 0
        S_START    = 1
        S_WAITBUSY = 2
        S_WAITDONE = 3
        S_CAPTURE  = 4
        S_NEXT     = 5
        LAST_STEP  = 12

        self.comb += [
            tx_is_read.eq(1),
            tx_data.eq(0),
            tx_reg.eq(117),
            tx_addr.eq(codec_address),
            self.i2c_enable.eq(~self.reset & ((state == S_START) | (state == S_WAITBUSY))),
            self.i2c_read_write.eq(tx_is_read),
            self.i2c_mosi_data.eq(tx_data),
            self.i2c_register_address.eq(tx_reg),
            self.i2c_device_address.eq(tx_addr),
        ]
        self.comb += Case(step, {
            0: [],
            1: [tx_reg.eq(51)],
            2: [tx_is_read.eq(0), tx_reg.eq(0x00), tx_data.eq(0x01)],
            3: [tx_is_read.eq(0), tx_reg.eq(0x26), tx_data.eq(Mux(self.gpio[1], 0x7F, 0x00))],
            4: [tx_is_read.eq(0), tx_reg.eq(0x1F), tx_data.eq(Mux(self.gpio[1], 0xC4, 0x04))],
            5: [tx_is_read.eq(0), tx_reg.eq(0x2E), tx_data.eq(Mux(self.mute, 0x80, 0x00))],
            6: [tx_is_read.eq(0), tx_reg.eq(0x00), tx_data.eq(0x00)],
            7: [tx_is_read.eq(0), tx_reg.eq(0x3F), tx_data.eq(Mux(self.gpio[1], 0xD4, 0x90))],
            8: [tx_reg.eq(0x08), tx_addr.eq(pmic_address)],
            9: [tx_reg.eq(0x09), tx_addr.eq(pmic_address)],
            10: [tx_reg.eq(0x00), tx_addr.eq(pmic_address)],
            11: [tx_is_read.eq(0), tx_reg.eq(0x02), tx_data.eq(0x20), tx_addr.eq(pmic_address)],
            12: [tx_reg.eq(0x02), tx_addr.eq(pmic_address)],
        })

        self.sync.hclk += [
            If(self.reset,
                state.eq(S_IDLE),
                step.eq(0),
                self.volume.eq(0),
                self.gpio.eq(0),
                self.pmic_sys_status.eq(0),
                self.new_fault.eq(0),
                self.inlim.eq(0),
                self.charge_current.eq(0),
            ).Else(
                Case(state, {
                    S_IDLE: [
                        If(~self.i2c_busy & self.enable,
                            step.eq(0),
                            state.eq(S_START),
                        )
                    ],
                    S_START: [
                        If(~self.i2c_busy,
                            state.eq(S_WAITBUSY),
                        )
                    ],
                    S_WAITBUSY: [
                        If(self.i2c_busy,
                            state.eq(S_WAITDONE),
                        )
                    ],
                    S_WAITDONE: [
                        If(~self.i2c_busy,
                            If(tx_is_read,
                                state.eq(S_CAPTURE),
                            ).Else(
                                state.eq(S_NEXT),
                            )
                        )
                    ],
                    S_CAPTURE: [
                        Case(step, {
                            0: [self.volume.eq(self.i2c_miso_data)],
                            1: [self.gpio.eq(self.i2c_miso_data)],
                            8: [self.pmic_sys_status.eq(self.i2c_miso_data)],
                            9: [self.new_fault.eq(self.i2c_miso_data)],
                            10: [self.inlim.eq(self.i2c_miso_data)],
                            12: [self.charge_current.eq(self.i2c_miso_data)],
                        }),
                        state.eq(S_NEXT),
                    ],
                    S_NEXT: [
                        If(step == LAST_STEP,
                            state.eq(S_IDLE),
                        ).Else(
                            step.eq(step + 1),
                            state.eq(S_START),
                        )
                    ],
                })
            )
        ]

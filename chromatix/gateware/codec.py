#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

import os

from migen import *

from litex.gen import *

from litei2c import LiteI2CPHYCore

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

        self.sync += [
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

        self.sync += [
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

# Codec I2S ----------------------------------------------------------------------------------------

class CodecI2S(LiteXModule):
    """
    I2S transmitter for the TLV320 codec (ported from aud_system_top.v).

    MCLK = sys clock, BCLK = sys clock / 2, 32-bit frames (16-bit left + 16-bit right). When no
    headphones are detected, a mono mix is sent on the left channel (speaker).
    """
    def __init__(self, pads):
        self.enable     = Signal() # Held in reset when 0 (PLL not locked).
        self.left       = Signal(16)
        self.right      = Signal(16)
        self.mute       = Signal()
        self.headphones = Signal()

        # # #

        left_m      = Signal(16)
        right_m     = Signal(16)
        mono_spk    = Signal(17)
        clk_half    = Signal()
        clk_half_d  = Signal()
        clk_half_re = Signal()
        count       = Signal(5)
        wclk        = Signal()
        shift       = Signal(32)

        # Mute / inversion / mono mix.
        self.comb += [
            left_m.eq( Mux(self.mute, 0, -self.left)),
            right_m.eq(Mux(self.mute, 0, -self.right)),
        ]
        self.sync += If(self.enable, mono_spk.eq(left_m + right_m))

        # BCLK generation (sys / 2).
        self.sync += [
            clk_half.eq(~clk_half),
            clk_half_d.eq(clk_half),
        ]
        self.comb += clk_half_re.eq(clk_half & ~clk_half_d)

        # Serializer.
        self.sync += [
            If(~self.enable,
                count.eq(0),
            ).Elif(clk_half_re,
                If(count == 0,
                    count.eq(31),
                    wclk.eq(1),
                    If(~self.headphones,
                        shift.eq(Cat(mono_spk[1:17], Constant(0, 16))),
                    ).Else(
                        shift.eq(Cat(left_m, right_m)),
                    ),
                ).Else(
                    count.eq(count - 1),
                    If(count == 16,
                        wclk.eq(0),
                    ),
                    shift.eq(Cat(Constant(0, 1), shift[:31])),
                ),
            ),
        ]

        # Codec pads.
        self.comb += [
            pads.mclk.eq(ClockSignal("sys")),
            pads.bclk.eq(~clk_half),
            pads.din.eq(shift[31]),
            pads.reset.eq(self.enable),
            pads.wclk.eq(wclk),
        ]

# Codec Control ------------------------------------------------------------------------------------

class CodecControl(LiteXModule):
    """
    TLV320 codec + PMIC control over LiteI2C.

    Runs the TLV320 register image init at power-up, then periodically polls the codec (volume,
    headphones GPIO) and the PMIC (system status) through the same LiteI2C PHY.
    """
    def __init__(self, pads, sys_clk_freq, registers):
        self.reset           = Signal()
        self.mute            = Signal()
        self.volume          = Signal(8)
        self.gpio            = Signal(8)
        self.pmic_sys_status = Signal(8)

        # # #

        # TLV320 Init / Polling.
        self.tlv320_init    = tlv320_init    = TLV320Init(registers)
        self.polling_master = polling_master = PollingMaster()
        self.comb += [
            tlv320_init.reset.eq(self.reset),
            polling_master.reset.eq(self.reset),
            polling_master.enable.eq(tlv320_init.done),
            polling_master.mute.eq(self.mute),
            self.volume.eq(polling_master.volume),
            self.gpio.eq(polling_master.gpio),
            self.pmic_sys_status.eq(polling_master.pmic_sys_status),
        ]

        # I2C Mux: TLV320 init until done, then polling.
        i2c_enable           = Signal()
        i2c_read_write       = Signal()
        i2c_mosi_data        = Signal(8)
        i2c_register_address = Signal(8)
        i2c_device_address   = Signal(7)
        i2c_miso_data        = Signal(8)
        i2c_busy             = Signal()
        self.comb += [
            If(tlv320_init.done,
                i2c_enable.eq(polling_master.i2c_enable),
                i2c_read_write.eq(polling_master.i2c_read_write),
                i2c_mosi_data.eq(polling_master.i2c_mosi_data),
                i2c_register_address.eq(polling_master.i2c_register_address),
                i2c_device_address.eq(polling_master.i2c_device_address),
            ).Else(
                i2c_enable.eq(tlv320_init.i2c_enable),
                i2c_read_write.eq(tlv320_init.i2c_read_write),
                i2c_mosi_data.eq(tlv320_init.i2c_mosi_data),
                i2c_register_address.eq(tlv320_init.i2c_register_address),
                i2c_device_address.eq(tlv320_init.i2c_device_address),
            ),
            tlv320_init.i2c_busy.eq(i2c_busy),
            polling_master.i2c_busy.eq(i2c_busy),
            polling_master.i2c_miso_data.eq(i2c_miso_data),
        ]

        # LiteI2C PHY.
        self.i2c_phy = i2c_phy = LiteI2CPHYCore(
            pads         = pads,
            clock_domain = "sys",
            sys_clk_freq = sys_clk_freq,
        )
        self.comb += i2c_phy.active.eq(1)

        # Bridge: enable/busy interface -> LiteI2C stream.
        i2c_enable_d  = Signal()
        i2c_enable_re = Signal()
        self.sync += i2c_enable_d.eq(i2c_enable)
        self.comb += i2c_enable_re.eq(i2c_enable & ~i2c_enable_d)

        self.bridge = bridge = FSM(reset_state="IDLE")
        bridge.act("IDLE",
            i2c_busy.eq(0),
            If(i2c_enable_re,
                NextState("SEND"),
            ),
        )
        bridge.act("SEND",
            i2c_busy.eq(1),
            i2c_phy.sink.valid.eq(1),
            i2c_phy.sink.addr.eq(i2c_device_address),
            If(i2c_read_write,
                # Read: send register address, then read 1 byte.
                i2c_phy.sink.len_tx.eq(1),
                i2c_phy.sink.len_rx.eq(1),
                i2c_phy.sink.data.eq(i2c_register_address),
            ).Else(
                # Write: send register address + data.
                i2c_phy.sink.len_tx.eq(2),
                i2c_phy.sink.len_rx.eq(0),
                i2c_phy.sink.data.eq(Cat(i2c_mosi_data, i2c_register_address)),
            ),
            If(i2c_phy.sink.ready,
                NextState("WAIT"),
            ),
        )
        bridge.act("WAIT",
            i2c_busy.eq(1),
            i2c_phy.source.ready.eq(1),
            If(i2c_phy.source.valid,
                NextValue(i2c_miso_data, i2c_phy.source.data[:8]),
                NextState("IDLE"),
            ),
        )

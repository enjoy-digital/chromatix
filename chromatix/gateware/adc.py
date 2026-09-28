#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: GPL-3.0-only
# Derived from ModRetro's oss-chromatic-console-fpga (GPL-3.0).

from migen import *

from litex.gen import *

# Battery ADC --------------------------------------------------------------------------------------

class BatteryADC(LiteXModule):
    """
    Battery voltage measurement with the GW5A hard ADC (voltage mode, port of adc_wrap.v).

    A conversion is started on each req pulse; ready pulses for one cycle when the 14-bit value is
    updated. Scaling/averaging is done by the system monitor (SystemMonitorControl).
    """
    def __init__(self, pads):
        self.enable = Signal()   # Held in reset when 0.
        self.req    = Signal()
        self.ready  = Signal()
        self.value  = Signal(14)

        # # #

        adc_ready = Signal()
        adc_value = Signal(14)
        adc_en    = Signal()
        state     = Signal(2) # 0: Idle, 1: Request, 2: Wait Ready, 3: Done.

        # Note: The primitives are explicitly named (adc_ibuf/adc_inst): an instance named like its
        # primitive (ex: "ADC") broke the I2C on hardware.

        # Analog input buffer.
        self.specials += Instance("TLVDS_IBUF_ADC", name="adc_ibuf",
            i_I     = pads.p,
            i_IB    = pads.n,
            i_ADCEN = 1,
        )

        # Hard ADC.
        self.specials += Instance("ADC", name="adc_inst",
            p_CLK_SEL              = Constant(0, 1),
            p_DIV_CTL              = Constant(0, 2),
            p_BUF_EN               = Constant(0b010000001001, 12),
            p_BUF_BK0_VREF_EN      = Constant(0, 1),
            p_BUF_BK1_VREF_EN      = Constant(0, 1),
            p_BUF_BK2_VREF_EN      = Constant(0, 1),
            p_BUF_BK3_VREF_EN      = Constant(0, 1),
            p_BUF_BK4_VREF_EN      = Constant(0, 1),
            p_BUF_BK5_VREF_EN      = Constant(0, 1),
            p_BUF_BK6_VREF_EN      = Constant(0, 1),
            p_BUF_BK7_VREF_EN      = Constant(0, 1),
            p_CSR_ADC_MODE         = Constant(1, 1),   # Voltage mode.
            p_CSR_VSEN_CTRL        = Constant(0, 3),
            p_CSR_SAMPLE_CNT_SEL   = Constant(4, 3),
            p_CSR_RATE_CHANGE_CTRL = Constant(4, 3),
            p_CSR_FSCAL            = Constant(653, 10),
            p_CSR_OFFSET           = Constant(0, 12),
            i_CLK         = ClockSignal("sys"),
            i_DRSTN       = self.enable,
            i_ADCEN       = adc_en,
            i_ADCREQI     = (state == 1),
            i_ADCMODE     = 1,                   # 1: Voltage, 0: Temperature.
            i_VSENCTL     = Constant(0b010, 3),
            i_MDRP_CLK    = 0,
            i_MDRP_WDATA  = Constant(0, 8),
            i_MDRP_A_INC  = 0,
            i_MDRP_OPCODE = Constant(0, 2),
            o_ADCRDY      = adc_ready,
            o_ADCVALUE    = adc_value,
            o_MDRP_RDATA  = Signal(8),
        )

        # Request / Ready FSM (ADCREQI held until ADCRDY is deasserted, value captured on ADCRDY).
        self.sync += [
            adc_en.eq(self.enable),
            self.ready.eq(0),
            If((state == 2) & adc_ready,
                self.ready.eq(1),
                self.value.eq(adc_value),
            ),
            If(~self.enable,
                state.eq(0),
            ).Else(
                Case(state, {
                    0: If(self.req, state.eq(1)),
                    1: If(~adc_ready, state.eq(2)),
                    2: If(adc_ready,
                            state.eq(3),
                        ).Elif(self.req,
                            state.eq(1),
                        ),
                    3: state.eq(0),
                })
            )
        ]

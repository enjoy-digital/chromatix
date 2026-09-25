#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *
from migen.fhdl.specials import Instance

from litex.gen import *
from litex.gen.sim import run_simulation

from chromatix.gateware.adc    import BatteryADC
from chromatix.gateware.sysmon import SystemMonitorControl

# Helpers ------------------------------------------------------------------------------------------

class ADCModel(LiteXModule):
    """
    Simulation stand-in for the GW5A ADC primitive (replaces the ADC/TLVDS_IBUF_ADC instances).

    ADCRDY is high when idle; on ADCREQI it drops, then rises again after `latency` cycles with
    ADCVALUE = value (the analog input, sampled at the end of the conversion).
    """
    def __init__(self, adcreqi, adcrdy, adcvalue, drstn, latency=6):
        self.value = Signal(14)

        # # #

        count = Signal(8)
        busy  = Signal()
        self.sync += [
            If(~drstn,
                busy.eq(0),
                adcrdy.eq(0),
            ).Elif(~busy,
                adcrdy.eq(1),
                If(adcreqi,
                    busy.eq(1),
                    adcrdy.eq(0),
                    count.eq(latency),
                )
            ).Else(
                count.eq(count - 1),
                If(count == 0,
                    busy.eq(0),
                    adcrdy.eq(1),
                    adcvalue.eq(self.value),
                )
            )
        ]

def battery_adc_with_model(latency=6):
    """BatteryADC with its hard primitives replaced by ADCModel."""
    pads = Record([("p", 1), ("n", 1)])
    adc  = BatteryADC(pads)
    ports = {}
    for special in list(adc._fragment.specials):
        if isinstance(special, Instance):
            assert special.name_override in ["adc_inst", "adc_ibuf"]
            if special.of == "ADC":
                ports = {item.name: item.expr for item in special.items
                    if isinstance(item, (Instance.Input, Instance.Output))}
            adc._fragment.specials.remove(special)
    adc.model = ADCModel(
        adcreqi  = ports["ADCREQI"],
        adcrdy   = ports["ADCRDY"],
        adcvalue = ports["ADCVALUE"],
        drstn    = ports["DRSTN"],
        latency  = latency,
    )
    return adc

# Battery ADC --------------------------------------------------------------------------------------

def test_battery_adc_instances():
    """The hard ADC primitives keep their explicit names (a primitive-named instance broke I2C)."""
    adc   = BatteryADC(Record([("p", 1), ("n", 1)]))
    names = {s.of: s.name_override for s in adc._fragment.specials if isinstance(s, Instance)}
    assert names == {"ADC": "adc_inst", "TLVDS_IBUF_ADC": "adc_ibuf"}

def test_battery_adc_request_ready():
    """Each req pulse starts one conversion; ready pulses once with the converted value."""
    dut     = battery_adc_with_model()
    results = []

    def gen():
        yield dut.enable.eq(1)
        for value in [0x1234, 1300, 42]:
            for _ in range(8):
                yield
            yield dut.model.value.eq(value)
            yield dut.req.eq(1)
            yield
            yield dut.req.eq(0)
            for _ in range(32):
                if (yield dut.ready):
                    results.append((yield dut.value))
                yield

    run_simulation(dut, gen())
    assert results == [0x1234, 1300, 42]

def test_battery_adc_disabled():
    """With enable low (PLL not locked), requests are ignored."""
    dut     = battery_adc_with_model()
    readies = []

    def gen():
        yield dut.model.value.eq(1000)
        for _ in range(4):
            yield dut.req.eq(1)
            yield
            yield dut.req.eq(0)
            for _ in range(16):
                readies.append((yield dut.ready))
                yield

    run_simulation(dut, gen())
    assert sum(readies) == 0

# Battery ADC + System Monitor ---------------------------------------------------------------------

class BatteryMonitor(LiteXModule):
    """BatteryADC (with model) + SystemMonitorControl, wired as in chromatix.py."""
    def __init__(self, li_value, aa_value):
        self.adc  = adc  = battery_adc_with_model()
        self.ctrl = ctrl = SystemMonitorControl(adc_interval=32, adc_sel_lead=8)
        self.comb += [
            adc.enable.eq(1),
            adc.req.eq(ctrl.adc_req),
            ctrl.adc_ready.eq(adc.ready),
            ctrl.adc_value.eq(adc.value),
            ctrl.btn_menu.eq(1),
            # External ADC_SEL mux: 1: Li-ion rail, 0: AA rail.
            adc.model.value.eq(Mux(ctrl.adc_sel, li_value, aa_value)),
        ]

def battery_monitor_sim(li_value, aa_value, cycles):
    dut    = BatteryMonitor(li_value, aa_value)
    status = {}

    def gen():
        for _ in range(cycles):
            yield
            if (yield dut.ctrl.transmit_volt):
                break
        status["volt"]          = (yield dut.ctrl.volt)
        status["bat_is_li"]     = (yield dut.ctrl.bat_is_li)
        status["transmit_volt"] = (yield dut.ctrl.transmit_volt)

    run_simulation(dut, gen())
    return status

def test_battery_monitor_average():
    """ADC samples are averaged into volt once the battery type is detected (Li-ion here)."""
    status = battery_monitor_sim(li_value=1300, aa_value=200, cycles=36*(512 + 256 + 300))
    assert status["transmit_volt"] == 1
    assert status["bat_is_li"]     == 1
    assert 1290 <= status["volt"] <= 1300 # 256 samples average (1st one can be stale).

def test_battery_monitor_low_raw_values():
    """Raw ADC values below 700 (~1.8V) never complete the type detection: volt stays at 0."""
    status = battery_monitor_sim(li_value=45, aa_value=20, cycles=36*(512 + 256 + 300))
    assert status["transmit_volt"] == 0
    assert status["volt"]          == 0

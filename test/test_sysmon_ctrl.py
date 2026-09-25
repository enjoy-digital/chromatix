#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litex.gen.sim import run_simulation

from chromatix.gateware.sysmon import SystemMonitorControl

# Helpers ------------------------------------------------------------------------------------------

def send_packet(dut, address, data):
    yield dut.rx_address.eq(address)
    yield dut.rx_data.eq(data)
    yield dut.rx_data_val.eq(1)
    yield
    yield dut.rx_data_val.eq(0)
    yield

def idle_buttons(dut):
    yield dut.btn_menu.eq(1) # Menu: 0 = pressed.

# Packet Decode ------------------------------------------------------------------------------------

def test_sysmon_ctrl_packet_decode():
    """ESP32 packets update palettes, MCU buttons, system control and brightness."""
    dut = SystemMonitorControl()

    def gen():
        yield from idle_buttons(dut)
        yield from send_packet(dut, 0xb, 0x0123456789abcdef)
        yield from send_packet(dut, 0xc, 0x8000000000000011) # OBJ1 (bit 63 set).
        yield from send_packet(dut, 0xc, 0x0000000000000022) # OBJ0.
        yield from send_packet(dut, 9,   0x1a5)
        yield from send_packet(dut, 4,   0xbeef)
        yield from send_packet(dut, 5,   0x7)
        yield
        assert (yield dut.palette_bg)     == 0x0123456789abcdef
        assert (yield dut.palette_obj1)   == 0x8000000000000011
        assert (yield dut.palette_obj0)   == 0x0000000000000022
        assert (yield dut.mcu_buttons)    == 0x1a5
        assert (yield dut.system_control) == 0xbeef
        assert (yield dut.brightness)     == 7

    run_simulation(dut, gen())

# Menu / Brightness --------------------------------------------------------------------------------

def test_sysmon_ctrl_menu_toggle_and_brightness():
    """Menu held + Right raises brightness (no menu toggle); a Menu press/release opens the menu."""
    dut = SystemMonitorControl()

    def gen():
        yield from idle_buttons(dut)
        for _ in range(32):
            yield
        assert (yield dut.menu_disabled) == 1
        brightness = (yield dut.brightness)

        # Menu held + Right tap: brightness + 1, update request, menu stays closed.
        updates = 0
        yield dut.btn_menu.eq(0)
        for _ in range(20):
            yield
        yield dut.btn_right.eq(1)
        for _ in range(20):
            yield
        yield dut.btn_right.eq(0)
        for _ in range(20):
            yield
            updates += (yield dut.update_brightness)
        yield dut.btn_menu.eq(1)
        for _ in range(40):
            yield
        assert (yield dut.brightness) == brightness + 1
        assert updates == 1
        assert (yield dut.menu_disabled) == 1

        # Menu press then release: menu opens.
        yield dut.btn_menu.eq(0)
        for _ in range(40):
            yield
        yield dut.btn_menu.eq(1)
        for _ in range(40):
            yield
        assert (yield dut.menu_disabled) == 0

    run_simulation(dut, gen())

# Battery ------------------------------------------------------------------------------------------

def test_sysmon_ctrl_battery_detection_and_average():
    """Battery type is detected from the ADC_SEL mux position, then the voltage is averaged."""
    dut = SystemMonitorControl(adc_interval=32, adc_sel_lead=8)

    def gen():
        yield from idle_buttons(dut)
        # ADC model: answer each request; only the ADC_SEL=1 (Li-ion) rail is populated.
        for _ in range(64*(512 + 256 + 300)):
            if (yield dut.adc_req):
                adc_sel = (yield dut.adc_sel)
                yield dut.adc_value.eq(1300 if adc_sel else 300) # Li-ion rail populated, AA rail empty.
                yield dut.adc_ready.eq(1)
            else:
                yield dut.adc_ready.eq(0)
            yield
            if (yield dut.volt):
                break
        assert (yield dut.bat_is_li) == 1
        assert 1250 <= (yield dut.volt) <= 1300

    run_simulation(dut, gen())

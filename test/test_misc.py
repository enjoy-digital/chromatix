#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litex.gen.sim import run_simulation

from chromatix.gateware.misc import TickGenerator, StatusLed, ESP32Control

# Tick Generator -----------------------------------------------------------------------------------

def test_tick_generator_periods():
    """second/half_second/percent pulse periods, percent re-aligned on each second pulse."""
    second_cycles  = 64
    percent_cycles = 9
    dut   = TickGenerator(second_cycles=second_cycles, percent_cycles=percent_cycles)
    ticks = {"second": [], "half_second": [], "percent": []}

    def gen():
        for cycle in range(3*second_cycles + 2):
            for name in ticks:
                if (yield getattr(dut, name)):
                    ticks[name].append(cycle)
            yield

    run_simulation(dut, gen())

    assert ticks["second"]      == [second_cycles*n for n in range(1, 4)]
    assert ticks["half_second"] == [second_cycles//2*n for n in range(1, 7)]
    # 1% pulses every percent_cycles + 1 cycles, restarted from each second pulse.
    for n in range(3):
        start    = second_cycles*n
        expected = list(range(start + percent_cycles + 1, start + second_cycles, percent_cycles + 1))
        assert [t for t in ticks["percent"] if start <= t < start + second_cycles] == expected

# Status LED ---------------------------------------------------------------------------------------

def test_status_led_boot_flash_and_priority():
    """Three white flashes after reset, then white > green > yellow > red priority."""
    pads   = Record([("en", 1), ("r", 1), ("g", 1), ("b", 1)])
    phase  = 8
    dut    = StatusLed(pads, boot_phase_cycles=phase)
    colors = {
        (0, 0, 0): "white",
        (1, 0, 1): "green",
        (0, 0, 1): "yellow",
        (0, 1, 1): "red",
        (1, 1, 1): "off",
    }

    def color():
        return colors[((yield pads.r), (yield pads.g), (yield pads.b))]

    def gen():
        assert (yield pads.en) == 1

        # Boot flash: white/off/white/off/white/off, then status (off with nothing set).
        yield dut.reset.eq(1)
        yield
        yield dut.reset.eq(0)
        yield dut.red.eq(1) # Masked during boot flash.
        seen = []
        for _ in range(8*phase):
            yield
            c = (yield from color())
            if not seen or seen[-1] != c:
                seen.append(c)
        assert seen == ["white", "off", "white", "off", "white", "off", "red"]

        # Priority.
        for inputs, expected in [
            ({"white": 1, "green": 1, "yellow": 1, "red": 1}, "white"),
            ({"white": 0, "green": 1, "yellow": 1, "red": 1}, "green"),
            ({"white": 0, "green": 0, "yellow": 1, "red": 1}, "yellow"),
            ({"white": 0, "green": 0, "yellow": 0, "red": 1}, "red"),
            ({"white": 0, "green": 0, "yellow": 0, "red": 0}, "off"),
        ]:
            for name, value in inputs.items():
                yield getattr(dut, name).eq(value)
            yield
            yield
            assert (yield from color()) == expected

        # Yellow: green modulated by blink (PWM).
        yield dut.yellow.eq(1)
        yield dut.blink.eq(1)
        yield
        yield
        assert (yield from color()) == "red"

    run_simulation(dut, gen())

# ESP32 Control ------------------------------------------------------------------------------------

def test_esp32_control():
    """UART passthrough and EN/IO0 control from USB CDC DTR/RTS (esptool style)."""
    esp32_pads = Record([("en", 1), ("io0", 1)])
    uart_pads  = Record([("tx", 1), ("rx", 1)])
    dut = ESP32Control(esp32_pads, uart_pads)
    en_delay = 9*4096 # EN released after 8 high samples (one every 4096 sys cycles).

    def wait_en(value, timeout):
        for cycle in range(timeout):
            if (yield esp32_pads.en) == value:
                return cycle
            yield
        raise AssertionError(f"EN did not reach {value}")

    def sys_gen():
        # USB not locked: ESP32 running (IO0 high), EN released after the boot delay.
        assert (yield esp32_pads.en) == 0
        cycles = (yield from wait_en(1, en_delay + 10))
        assert cycles > 8*4096, "EN released too early"
        assert (yield esp32_pads.io0) == 1

        # USB locked, RTS asserted (DTR=0): EN low immediately (IO0 only high when DTR=RTS=0).
        yield dut.usb_locked.eq(1)
        yield dut.usb_rts.eq(1)
        yield from wait_en(0, 10)
        for _ in range(8):
            yield
        assert (yield esp32_pads.io0) == 0

        # RTS released, DTR asserted: IO0 low (bootloader), EN released after the boot delay.
        yield dut.usb_rts.eq(0)
        yield dut.usb_dtr.eq(1)
        for _ in range(8):
            yield
        assert (yield esp32_pads.io0) == 0
        assert (yield esp32_pads.en)  == 0
        cycles = (yield from wait_en(1, en_delay + 10))
        assert cycles > 7*4096, "EN released too early"

        # DTR/RTS released: IO0 high, EN kept high.
        yield dut.usb_dtr.eq(0)
        for _ in range(8):
            yield
        assert (yield esp32_pads.io0) == 1
        assert (yield esp32_pads.en)  == 1

        # EN low glitch shorter than a sample period still resets the ESP32 (EN cleared immediately).
        yield dut.usb_rts.eq(1)
        yield from wait_en(0, 10)
        yield dut.usb_rts.eq(0)
        yield from wait_en(1, en_delay + 10)

    def phy_gen():
        # USB not locked: UART idle (high) in both directions.
        for _ in range(4):
            yield
        yield uart_pads.tx.eq(0)
        yield dut.usb_rxd.eq(0)
        yield
        yield
        assert (yield dut.usb_txd) == 1
        assert (yield uart_pads.rx) == 1
        while not (yield dut.usb_locked):
            yield
        # USB locked: passthrough (1 cycle latency).
        for value in [0, 1, 0, 1]:
            yield uart_pads.tx.eq(value)
            yield dut.usb_rxd.eq(1 - value)
            yield
            yield
            assert (yield dut.usb_txd)  == value
            assert (yield uart_pads.rx) == 1 - value

    run_simulation(dut, {"sys": sys_gen(), "phy": phy_gen()}, clocks={"sys": 10, "phy": 7})

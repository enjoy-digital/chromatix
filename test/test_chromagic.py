#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litex.gen import *
from litex.gen.sim import run_simulation, passive

from chromatix.gateware.chromagic import VirtualCartControl, CoreHoldVideo
from chromatix.gateware.chromagic import VC_MAGIC, VC_STOP, VC_START, VC_STATUS, VC_PREPARE
from chromatix.gateware.chromagic import VC_SAVE_BLOCK, VC_RTC_RESTORE_LOW, VC_QUIESCE, VC_RESUME

# ChroMagic ESP32 status bits (mcu/main/virtual_cart.c).
ENABLED        = 1 << 0
INITIALIZED    = 1 << 1
DISABLE_SEEN   = 1 << 3
RESET_ASSERTED = 1 << 4
RESET_RELEASED = 1 << 5
BOOT_ROM_HIGH  = 1 << 6
BOOT_ROM_EXIT  = 1 << 7
POST_EXIT      = 1 << 8
QUIESCED       = 1 << 9
BOOTED         = (ENABLED | INITIALIZED | DISABLE_SEEN | RESET_ASSERTED | RESET_RELEASED |
    BOOT_ROM_HIGH | BOOT_ROM_EXIT | POST_EXIT)

# Virtual Cartridge Control ------------------------------------------------------------------------

class ControlDUT(LiteXModule):
    def __init__(self):
        self.ctrl = ctrl = VirtualCartControl()
        # Core model (hClk): reset follows hold, boot ROM mapped for a while after reset release,
        # then a valid frame; initialized once enabled; quiesced follows quiesce.
        boot  = Signal(8)
        frame = Signal(8)
        self.sync.hclk += [
            ctrl.core_reset.eq(ctrl.hold),
            ctrl.initialized.eq(ctrl.enable),
            ctrl.quiesced.eq(ctrl.quiesce),
            ctrl.frame_valid.eq(0),
            If(ctrl.core_reset,
                boot.eq(0),
                frame.eq(0),
                ctrl.boot_rom_enabled.eq(1),
            ).Elif(ctrl.boot_rom_enabled,
                boot.eq(boot + 1),
                If(boot == 50, ctrl.boot_rom_enabled.eq(0)),
            ).Else(
                frame.eq(frame + 1),
                If(frame == 20, ctrl.frame_valid.eq(1)),
            )
        ]


def vc_request(dut, command, aux_address=0, aux_value=0, op=7, address=VC_MAGIC, tag=[0]):
    """Cartridge link request (gClk), returns (status, data)."""
    req, resp = dut.ctrl.request, dut.ctrl.response
    tag[0] = (tag[0] + 1) & 0xff
    yield req.valid.eq(1)
    yield req.operation.eq(op)
    yield req.tag.eq(tag[0])
    yield req.address.eq(address)
    yield req.value.eq(command)
    yield req.aux_address.eq(aux_address)
    yield req.aux_value.eq(aux_value)
    yield
    while not (yield req.ready):
        yield
    yield req.valid.eq(0)
    while not (yield resp.valid):
        yield
    assert (yield resp.tag) == tag[0]
    assert (yield resp.operation) == op
    status, data = (yield resp.status), (yield resp.data)
    yield resp.ready.eq(1)
    yield
    yield resp.ready.eq(0)
    yield
    return status, data


def vc_poll(dut, required, polls=400):
    for _ in range(polls):
        status, data = yield from vc_request(dut, VC_STATUS)
        assert status == 0
        if (data & required) == required:
            return data
        for _ in range(8):
            yield
    raise AssertionError(f"status 0x{data:04x}, required 0x{required:04x}")


def test_virtual_cart_control():
    """ESP32 load sequence (ChroMagic virtual_cart.c): PREPARE, reset seen, START, boot followed
    through the lifecycle bits, save snapshot, quiesce/resume, stop, and error cases."""
    dut = ControlDUT()
    res = {}

    def gen():
        ctrl = dut.ctrl
        # Not prepared: START refused, unknown command/RTC refused, maintenance unsupported.
        res["start_early"] = (yield from vc_request(dut, VC_START))[0]
        res["rtc"]         = (yield from vc_request(dut, VC_RTC_RESTORE_LOW))[0]
        res["unknown"]     = (yield from vc_request(dut, 0x55))[0]
        res["ping"]        = (yield from vc_request(dut, 0, op=0, address=0))[0]
        # PREPARE: MBC5 (4), cartridge type 0x1b, ROM mask 0x3f, RAM mask 0xf.
        aux_address = 4 | (0x1b << 3) | ((0x3f & 0x1f) << 11)
        aux_value   = (0x3f >> 5) | (0xf << 4)
        res["prepare"] = (yield from vc_request(dut, VC_PREPARE, aux_address, aux_value))[0]
        data = yield from vc_poll(dut, DISABLE_SEEN | RESET_ASSERTED)
        res["prepared_enabled"] = data & ENABLED
        res["prepare_again"]    = (yield from vc_request(dut, VC_PREPARE, aux_address, aux_value))[0]
        for _ in range(8):
            yield
        res["config"] = ((yield ctrl.mbc_type), (yield ctrl.rom_mask), (yield ctrl.ram_mask),
            (yield ctrl.has_ram), (yield ctrl.mbc1m), (yield ctrl.mbc30))
        # START: boot followed until the first frame.
        res["start"] = (yield from vc_request(dut, VC_START))[0]
        data = yield from vc_poll(dut, BOOTED)
        res["cart_type"] = (data >> 16) & 0xff
        res["rtc_cap"]   = (data >> 15) & 1
        # Save snapshot request.
        toggle = (yield ctrl.snapshot_request)
        res["save"] = (yield from vc_request(dut, VC_SAVE_BLOCK, aux_address=0xbeef, aux_value=5))[0]
        res["snapshot"] = ((yield ctrl.snapshot_request) != toggle, (yield ctrl.snapshot_block),
            (yield ctrl.snapshot_sequence))
        res["save_bad"] = (yield from vc_request(dut, VC_SAVE_BLOCK, aux_address=0, aux_value=0x80))[0]
        # Quiesce / resume.
        res["quiesce"] = (yield from vc_request(dut, VC_QUIESCE))[0]
        yield from vc_poll(dut, QUIESCED)
        res["resume"] = (yield from vc_request(dut, VC_RESUME))[0]
        for _ in range(16):
            yield
        res["resumed"] = ((yield from vc_request(dut, VC_STATUS))[1] & QUIESCED)
        # Stop.
        res["stop"] = (yield from vc_request(dut, VC_STOP))[0]
        for _ in range(16):
            yield
        res["stopped"] = ((yield ctrl.enable), (yield ctrl.hold), (yield ctrl.session))

    run_simulation(dut, {"gclk": gen()}, clocks={"gclk": 10, "hclk": 7})
    assert (res["start_early"], res["rtc"], res["unknown"], res["ping"]) == (3, 3, 1, 1)
    assert (res["prepare"], res["prepared_enabled"], res["prepare_again"]) == (0, 0, 3)
    assert res["config"] == (4, 0x3f, 0xf, 1, 0, 0)
    assert (res["start"], res["cart_type"], res["rtc_cap"]) == (0, 0x1b, 0)
    assert (res["save"], res["snapshot"], res["save_bad"]) == (0, (True, 5, 0xbeef), 3)
    assert (res["quiesce"], res["resume"], res["resumed"]) == (0, 0, 0)
    assert res["stop"] == 0
    assert res["stopped"] == (0, 0, 0)

# Core Hold Video ----------------------------------------------------------------------------------

def test_core_hold_video():
    """Black frames with the Game Boy timing while held, back to the core on its first valid frame."""
    dut = CoreHoldVideo(dot_cycles=1)
    res = {"black_clkena": 0, "vsync_lines": []}

    def gen():
        yield dut.core_on.eq(1)
        yield dut.core_data.eq(0x1234)
        yield dut.hold.eq(1)
        yield
        yield dut.hold.eq(0)
        yield
        # First 2 lines: 160 black pixels per line (mode 3), vsync during line 0 only.
        for line in range(2):
            vsync = 0
            for _ in range(456):
                res["black_clkena"] += (yield dut.clkena)
                assert (yield dut.data) == 0
                vsync |= (yield dut.vsync)
                yield
            res["vsync_lines"].append(vsync)
        res["selected_held"] = (yield dut.selected)
        # Core frame with non-white pixels: black frames until the next core vsync.
        yield dut.core_vsync.eq(1)
        yield
        yield dut.core_vsync.eq(0)
        for _ in range(20):
            yield dut.core_clkena.eq(1)
            yield
        yield dut.core_clkena.eq(0)
        yield dut.core_vsync.eq(1)
        yield
        yield
        res["selected_after"] = (yield dut.selected)

    run_simulation(dut, {"hclk": gen()}, clocks={"hclk": 10})
    assert res["black_clkena"] == 2*160
    assert res["vsync_lines"] == [1, 0]
    assert (res["selected_held"], res["selected_after"]) == (1, 0)

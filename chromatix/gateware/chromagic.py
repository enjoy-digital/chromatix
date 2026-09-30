#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: GPL-3.0-only
# Derived from ChroMagic (https://github.com/cursedtoast2/ChroMagic, GPL-3.0).

"""
ChroMagic ESP32 firmware compatibility: virtual cartridge control over the cartridge link.

The ChroMagic ESP32 firmware (based on ModRetro's) loads Game Boy ROMs from the SD card into the
PSRAM (QSPI writes) and drives the virtual cartridge with cartridge link requests (operation 7,
address 0x5643 "VC", command in value[6:0]):

| Command         | Action                                                                     |
|-----------------|----------------------------------------------------------------------------|
| 0: STOP         | Back to the physical cartridge.                                            |
| 1: START        | Enable the virtual cartridge (after PREPARE): the core boots from it.      |
| 2: STATUS       | Status only.                                                               |
| 3: PREPARE      | Configuration (mapper, cartridge type, ROM/RAM masks, MBC1M), core held.   |
| 4: SAVE_BLOCK   | Snapshot of a 1KB cartridge RAM block (read over QSPI).                    |
| 5/6/7: RTC      | RTC restore/snapshot (not supported: capability bit 15 is 0).              |
| 8/9: QUIESCE/RESUME | Freeze/resume the core (saves on game switch).                         |

Each accepted request is answered (4 data bytes) with the status word: [0] enabled, [1] initialized,
[2] save dirty, [8:3] lifecycle (hold requested, core reset, core reset released, boot ROM, boot ROM
exited, first frame), [9] quiesced, [15] RTC capability, [23:16] cartridge type; status 3 when the
command is not allowed in the current state, 1 when unknown. Other cartridge link requests
(physical cartridge maintenance: backups) are answered with status 1 (not supported).
"""

from migen import *
from migen.genlib.cdc import MultiReg

from litex.gen import *

from litex.soc.interconnect import stream

from chromatix.gateware.sysmon import cart_link_request_layout, cart_link_response_layout

# Constants ----------------------------------------------------------------------------------------

VC_MAGIC = 0x5643

VC_STOP              = 0
VC_START             = 1
VC_STATUS            = 2
VC_PREPARE           = 3
VC_SAVE_BLOCK        = 4
VC_RTC_RESTORE_LOW   = 5
VC_RTC_RESTORE_HIGH  = 6
VC_RTC_SNAPSHOT      = 7
VC_QUIESCE           = 8
VC_RESUME            = 9

STATUS_OK          = 0x0
STATUS_UNSUPPORTED = 0x1
STATUS_STATE       = 0x3

# Cartridge types with RAM (ChroMagic CartTypeHasRam).
CART_TYPES_WITH_RAM = [0x02, 0x03, 0x05, 0x06, 0x08, 0x09, 0x10, 0x12, 0x13, 0x1a, 0x1b, 0x1d, 0x1e, 0xff]

# Virtual Cartridge Control ------------------------------------------------------------------------

class VirtualCartControl(LiteXModule):
    """
    Virtual cartridge control from cartridge link requests (see module docstring).

    Clock domains: "gclk" (requests/responses, session), "hclk" (Game Boy side: core hold,
    configuration, lifecycle).
    """
    def __init__(self):
        # Cartridge link (gClk).
        self.request  = stream.Endpoint(cart_link_request_layout())
        self.response = stream.Endpoint(cart_link_response_layout())
        self.session  = Signal() # gClk: virtual cartridge session (prepared, starting or enabled).
        self.enabled  = Signal() # gClk: virtual cartridge enabled.

        # Game Boy side (hClk).
        self.enable           = Signal() # Virtual cartridge enabled.
        self.hold             = Signal() # Hold the core in reset (prepare/start).
        self.quiesce          = Signal() # Freeze the core (quiesce).
        self.quiesced         = Signal() # From the virtual cartridge.
        self.initialized      = Signal() # From the virtual cartridge.
        self.save_dirty       = Signal() # From the virtual cartridge.
        self.core_reset       = Signal() # Game Boy core reset.
        self.boot_rom_enabled = Signal() # Game Boy boot ROM mapped.
        self.frame_valid      = Signal() # First valid Game Boy frame.
        self.mbc_type         = Signal(3)
        self.mbc1m            = Signal()
        self.mbc30            = Signal()
        self.has_ram          = Signal()
        self.rom_mask         = Signal(9)
        self.ram_mask         = Signal(4)

        # Save snapshots (gClk, see VirtualCart).
        self.snapshot_request  = Signal()
        self.snapshot_block    = Signal(7)
        self.snapshot_sequence = Signal(16)

        # # #

        request  = self.request
        response = self.response

        # Configuration (gClk) ---------------------------------------------------------------------
        mbc_type  = Signal(3)
        mbc1m     = Signal()
        mbc30     = Signal()
        cart_type = Signal(8)
        has_ram   = Signal()
        rom_mask  = Signal(9)
        ram_mask  = Signal(4)
        self.specials += [
            MultiReg(mbc_type, self.mbc_type, "hclk"),
            MultiReg(mbc1m,    self.mbc1m,    "hclk"),
            MultiReg(mbc30,    self.mbc30,    "hclk"),
            MultiReg(has_ram,  self.has_ram,  "hclk"),
            MultiReg(rom_mask, self.rom_mask, "hclk"),
            MultiReg(ram_mask, self.ram_mask, "hclk"),
        ]

        # Session (gClk) ---------------------------------------------------------------------------
        enable_g        = Signal()
        reset_request_g = Signal()
        start_pending_g = Signal()
        prepared_g      = Signal()
        prepare         = Signal()
        start           = Signal()
        stop            = Signal()
        quiesce         = Signal()
        resume          = Signal()
        self.comb += [
            self.session.eq(enable_g | start_pending_g | prepared_g),
            self.enabled.eq(enable_g),
        ]

        # hClk side.
        enable_h      = Signal()
        request_h     = Signal()
        request_h_d   = Signal()
        lifecycle_h   = Signal(6)
        self.specials += [
            MultiReg(enable_g,        enable_h,  "hclk"),
            MultiReg(reset_request_g, request_h, "hclk"),
        ]
        reset_ack_h   = Signal()
        quiesce_ack_h = Signal()
        self.comb += [
            self.enable.eq(enable_h),
            self.hold.eq(request_h & ~enable_h),
            self.quiesce.eq(request_h & enable_h),
            reset_ack_h.eq(request_h & ~enable_h & self.core_reset),
            quiesce_ack_h.eq(request_h & enable_h & self.quiesced),
        ]
        self.sync.hclk += [
            request_h_d.eq(request_h),
            If(request_h & ~request_h_d,
                lifecycle_h.eq(0),
            ).Else(
                If(request_h & ~enable_h,                             lifecycle_h[0].eq(1)),
                If(lifecycle_h[0] & self.core_reset,                  lifecycle_h[1].eq(1)),
                If(lifecycle_h[1] & enable_h & ~self.core_reset,      lifecycle_h[2].eq(1)),
                If(lifecycle_h[2] & self.boot_rom_enabled,            lifecycle_h[3].eq(1)),
                If(lifecycle_h[3] & ~self.boot_rom_enabled,           lifecycle_h[4].eq(1)),
                If(lifecycle_h[4] & self.frame_valid,                 lifecycle_h[5].eq(1)),
            )
        ]

        # gClk side.
        reset_ack_g   = Signal()
        quiesce_ack_g = Signal()
        lifecycle_g   = Signal(6)
        initialized_g = Signal()
        save_dirty_g  = Signal()
        quiesced_g    = Signal()
        self.specials += [
            MultiReg(reset_ack_h,      reset_ack_g,   "gclk"),
            MultiReg(quiesce_ack_h,    quiesce_ack_g, "gclk"),
            MultiReg(lifecycle_h,      lifecycle_g,   "gclk"),
            MultiReg(self.initialized, initialized_g, "gclk"),
            MultiReg(self.save_dirty,  save_dirty_g,  "gclk"),
        ]
        self.comb += quiesced_g.eq(reset_request_g & enable_g & quiesce_ack_g)
        self.sync.gclk += [
            If(stop,
                enable_g.eq(0),
                reset_request_g.eq(0),
                start_pending_g.eq(0),
                prepared_g.eq(0),
            ).Elif(prepare,
                enable_g.eq(0),
                reset_request_g.eq(1),
                start_pending_g.eq(1),
                prepared_g.eq(0),
            ).Elif(start_pending_g & reset_ack_g,
                enable_g.eq(0),
                reset_request_g.eq(1),
                start_pending_g.eq(0),
                prepared_g.eq(1),
            ).Elif(quiesce & enable_g,
                reset_request_g.eq(1),
            ).Elif(resume & enable_g,
                reset_request_g.eq(0),
            ).Elif(start & prepared_g,
                enable_g.eq(1),
                reset_request_g.eq(0),
                prepared_g.eq(0),
            )
        ]

        # Requests (gClk) --------------------------------------------------------------------------
        status_word = Signal(32)
        self.comb += status_word.eq(Cat(
            enable_g,         # [0]
            initialized_g,    # [1]
            save_dirty_g,     # [2]
            lifecycle_g,      # [8:3]
            quiesced_g,       # [9]
            Constant(0, 5),   # [14:10]
            Constant(0, 1),   # [15] RTC capability.
            cart_type,        # [23:16]
            Constant(0, 8),
        ))

        vc            = Signal()
        command       = Signal(7)
        accepted      = Signal()
        pause_allowed = Signal()
        self.comb += [
            vc.eq((request.operation == 7) & (request.address == VC_MAGIC)),
            command.eq(request.value[0:7]),
            request.ready.eq(~response.valid),
            accepted.eq(request.valid & request.ready),
            pause_allowed.eq(enable_g & ~start_pending_g & ~prepared_g),
            prepare.eq(accepted & vc & (command == VC_PREPARE) & ~start_pending_g & ~prepared_g),
            start.eq(  accepted & vc & (command == VC_START)   & ~start_pending_g &  prepared_g),
            stop.eq(   accepted & vc & (command == VC_STOP)),
            quiesce.eq(accepted & vc & (command == VC_QUIESCE) & pause_allowed),
            resume.eq( accepted & vc & (command == VC_RESUME)  & pause_allowed),
        ]
        self.sync.gclk += [
            If(response.valid & response.ready,
                response.valid.eq(0),
            ),
            If(accepted,
                response.valid.eq(1),
                response.operation.eq(request.operation),
                response.tag.eq(request.tag),
                response.status.eq(STATUS_OK),
                response.count.eq(4),
                response.data.eq(status_word),
                If(~vc,
                    # Physical cartridge maintenance (backups): not supported.
                    response.status.eq(STATUS_UNSUPPORTED),
                    response.count.eq(0),
                    response.data.eq(0),
                ).Else(
                    Case(command, {
                        VC_STOP:   [],
                        VC_STATUS: [],
                        VC_PREPARE: If(start_pending_g | prepared_g,
                            response.status.eq(STATUS_STATE),
                        ).Else(
                            mbc_type.eq(request.aux_address[0:3]),
                            cart_type.eq(request.aux_address[3:11]),
                            rom_mask.eq(Cat(request.aux_address[11:16], request.aux_value[0:4])),
                            ram_mask.eq(request.aux_value[4:8]),
                            has_ram.eq(reduce_or(request.aux_address[3:11], CART_TYPES_WITH_RAM)),
                            mbc1m.eq(request.value[7]),
                            mbc30.eq((request.aux_address[0:3] == 3) &
                                (request.aux_value[2] | (request.aux_value[4:8] == 7))),
                        ),
                        VC_START: If(start_pending_g | ~prepared_g,
                            response.status.eq(STATUS_STATE),
                        ),
                        VC_QUIESCE: If(~pause_allowed, response.status.eq(STATUS_STATE)),
                        VC_RESUME:  If(~pause_allowed, response.status.eq(STATUS_STATE)),
                        VC_SAVE_BLOCK: If(~enable_g | request.aux_value[7],
                            response.status.eq(STATUS_STATE),
                        ).Else(
                            self.snapshot_block.eq(request.aux_value[0:7]),
                            self.snapshot_sequence.eq(request.aux_address),
                            self.snapshot_request.eq(~self.snapshot_request),
                        ),
                        VC_RTC_RESTORE_LOW:  response.status.eq(STATUS_STATE),
                        VC_RTC_RESTORE_HIGH: response.status.eq(STATUS_STATE),
                        VC_RTC_SNAPSHOT:     response.status.eq(STATUS_STATE),
                        "default": response.status.eq(STATUS_UNSUPPORTED),
                    })
                )
            )
        ]

def reduce_or(value, constants):
    """value in constants (comparison OR)."""
    r = 0
    for c in constants:
        r = r | (value == c)
    return r

# Core Hold Video ----------------------------------------------------------------------------------

class CoreHoldVideo(LiteXModule):
    """
    Game Boy LCD interface mux (hClk): black frames with the Game Boy LCD timing while the core is
    held (virtual cartridge loading), so that the video pipeline/OSD keep running, back to the core
    once it produces a valid frame (frame_valid: vsync after >= 16 non-white pixels, boot ROM
    exited). dot_cycles: hClk cycles per LCD dot (4).
    """
    def __init__(self, dot_cycles=4):
        self.hold             = Signal()
        self.boot_rom_enabled = Signal()
        # Core LCD interface.
        self.core_clkena = Signal()
        self.core_mode   = Signal(2)
        self.core_on     = Signal()
        self.core_vsync  = Signal()
        self.core_data   = Signal(15)
        # Output LCD interface.
        self.clkena      = Signal()
        self.mode        = Signal(2)
        self.on          = Signal()
        self.vsync       = Signal()
        self.data        = Signal(15)
        self.selected    = Signal() # Black frames selected.
        self.frame_valid = Signal()

        # # #

        # Black frames (Game Boy LCD timing: 154 lines of 456 dots).
        h_total        = 456*dot_cycles
        h_mode2_end    = 80*dot_cycles
        h_active_start = (80 + 8)*dot_cycles
        h_active_end   = h_active_start + 160*dot_cycles
        h_count  = Signal(max=h_total)
        v_count  = Signal(8)
        active   = Signal()
        black_on = self.selected
        self.sync.hclk += [
            If(~black_on,
                h_count.eq(0),
                v_count.eq(0),
            ).Elif(h_count == (h_total - 1),
                h_count.eq(0),
                If(v_count == 153,
                    v_count.eq(0),
                ).Else(
                    v_count.eq(v_count + 1),
                )
            ).Else(
                h_count.eq(h_count + 1),
            )
        ]
        black_clkena = Signal()
        black_mode   = Signal(2)
        self.comb += [
            active.eq((v_count < 144) & (h_count >= h_active_start) & (h_count < h_active_end)),
            black_clkena.eq(black_on & active & (h_count[0:log2_int(dot_cycles)] == 0 if dot_cycles > 1 else 1)),
            If(~black_on,
                black_mode.eq(0),
            ).Elif(v_count >= 144,
                black_mode.eq(1),
            ).Elif(h_count < h_mode2_end,
                black_mode.eq(2),
            ).Elif(h_count < h_active_end,
                black_mode.eq(3),
            ).Else(
                black_mode.eq(0),
            ),
        ]

        # Core frame detection.
        vsync_d       = Signal()
        vsync_rising  = Signal()
        frame_started = Signal()
        valid_pixels  = Signal(15)
        display_ready = Signal()
        self.comb += [
            display_ready.eq(~self.boot_rom_enabled),
            vsync_rising.eq(self.core_vsync & ~vsync_d),
            self.frame_valid.eq(self.core_on & display_ready & vsync_rising & frame_started &
                (valid_pixels >= 16)),
        ]
        self.sync.hclk += [
            vsync_d.eq(self.core_vsync),
            If(self.hold,
                self.selected.eq(1),
            ).Elif(self.selected & self.frame_valid,
                self.selected.eq(0),
            ),
            If(~self.core_on | ~display_ready,
                frame_started.eq(0),
                valid_pixels.eq(0),
            ).Elif(vsync_rising,
                frame_started.eq(1),
                valid_pixels.eq(0),
            ).Elif(frame_started & self.core_clkena & (self.core_data != 0x7fff) &
                (valid_pixels != 0x7fff),
                valid_pixels.eq(valid_pixels + 1),
            )
        ]

        # Mux.
        self.comb += If(self.selected,
            self.clkena.eq(black_clkena),
            self.mode.eq(black_mode),
            self.on.eq(1),
            self.vsync.eq(v_count == 0),
            self.data.eq(0),
        ).Else(
            self.clkena.eq(self.core_clkena),
            self.mode.eq(self.core_mode),
            self.on.eq(self.core_on),
            self.vsync.eq(self.core_vsync),
            self.data.eq(self.core_data),
        )

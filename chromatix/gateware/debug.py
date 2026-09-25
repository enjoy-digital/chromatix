#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *
from migen.genlib.cdc import MultiReg

from litex.gen import *

from litex.soc.interconnect.csr import *

# Debug Control ------------------------------------------------------------------------------------

BUTTONS = ["a", "b", "sel", "start", "dpad_right", "dpad_left", "dpad_up", "dpad_down", "menu"]

class DebugControl(LiteXModule):
    """
    Debug/automation registers (sys domain).

    - buttons: virtual buttons, OR'ed with the physical ones (1 = pressed).
    - status:  PSRAM BIST, LCD init and system monitor status.
    - volt/audio: battery ADC value and codec/PMIC status.
    """
    def __init__(self):
        # Inputs (resynchronized to sys when needed).
        self.bist_done       = Signal()  # xClk.
        self.bist_failed     = Signal()  # xClk.
        self.lcd_init_done   = Signal()  # pClk.
        self.menu_disabled   = Signal()
        self.low_battery     = Signal()
        self.system_control  = Signal(16)
        self.volt            = Signal(14)
        self.adc_value       = Signal(14)
        self.bat_is_li       = Signal()
        self.volume          = Signal(8) # hClk.
        self.headphones      = Signal()  # hClk.
        self.pmic_sys_status = Signal(8) # hClk.
        self.uvc_hbw         = Signal()   # USB PHY clock.
        self.uvc_frame_index = Signal(8)  # USB PHY clock.
        self.uvc_hbw_count   = Signal(16) # USB PHY clock.
        self.uvc_frame_count = Signal(16) # USB PHY clock.
        self.uvc_debug       = Signal(64) # {start_count, skip_count, drop_count, max_level} (debug).

        # Virtual Buttons.
        self._buttons = CSRStorage(len(BUTTONS), fields=[
            CSRField(name, size=1, description=f"Virtual {name} button (1: pressed).") for name in BUTTONS
        ])
        for name in BUTTONS:
            setattr(self, name, getattr(self._buttons.fields, name))

        # Status.
        self._status = CSRStatus(fields=[
            CSRField("bist_done",     size=1, description="PSRAM BIST finished."),
            CSRField("bist_failed",   size=1, description="PSRAM BIST failed."),
            CSRField("lcd_init_done", size=1, description="LCD init sequence done."),
            CSRField("menu_disabled", size=1, description="ESP32 menu closed."),
            CSRField("low_battery",   size=1, description="Low battery."),
            CSRField("bat_is_li",     size=1, description="Li-ion battery detected (else AA)."),
            CSRField("headphones",    size=1, description="Headphones detected."),
        ])
        self._system_control  = CSRStatus(16, description="System control word (from the ESP32).")
        self._volt            = CSRStatus(14, description="Battery ADC value (averaged).")
        self._adc_value       = CSRStatus(14, description="Battery ADC value (last raw sample).")
        self._volume          = CSRStatus(8,  description="Codec volume.")
        self._pmic_sys_status = CSRStatus(8,  description="PMIC system status.")
        self._uvc_status = CSRStatus(fields=[
            CSRField("hbw",         size=1,  offset=0,  description="UVC high-bandwidth alternate setting selected."),
            CSRField("frame_index", size=8,  offset=8,  description="UVC committed frame (1: 320x288, 2: 160x144)."),
        ])
        self._uvc_hbw_count   = CSRStatus(16, description="UVC high-bandwidth (2048-byte) micro-frames (wraps).")
        self._uvc_frame_count = CSRStatus(16, description="UVC frames sent (wraps).")
        self._uvc_debug       = CSRStatus(64, description="UVC debug: {frame starts, skipped frames, dropped lines, max FIFO level}.")

        # # #

        self.specials += [
            MultiReg(self.bist_done,       self._status.fields.bist_done),
            MultiReg(self.bist_failed,     self._status.fields.bist_failed),
            MultiReg(self.lcd_init_done,   self._status.fields.lcd_init_done),
            MultiReg(self.headphones,      self._status.fields.headphones),
            MultiReg(self.volume,          self._volume.status),
            MultiReg(self.pmic_sys_status, self._pmic_sys_status.status),
            # Debug counters (quasi-static, may tear while counting).
            MultiReg(self.uvc_hbw,         self._uvc_status.fields.hbw),
            MultiReg(self.uvc_frame_index, self._uvc_status.fields.frame_index),
            MultiReg(self.uvc_hbw_count,   self._uvc_hbw_count.status),
            MultiReg(self.uvc_frame_count, self._uvc_frame_count.status),
            MultiReg(self.uvc_debug,       self._uvc_debug.status),
        ]
        self.comb += [
            self._status.fields.menu_disabled.eq(self.menu_disabled),
            self._status.fields.low_battery.eq(self.low_battery),
            self._status.fields.bat_is_li.eq(self.bat_is_li),
            self._system_control.status.eq(self.system_control),
            self._volt.status.eq(self.volt),
            self._adc_value.status.eq(self.adc_value),
        ]

#!/usr/bin/env python3

#
# This file is part of Chromatic FPGA LiteX Build.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

import os
import argparse

from migen import *

from litex.gen import *

from litex.build.gowin.platform import GowinPlatform
from litex.build.generic_platform import *

from litex.soc.cores.clock.gowin_gw5a import GW5APLL
from litex.soc.cores.uart import RS232PHY

from litex_boards.platforms.modretro_chromatic import Platform

# CRG (Clock Reset Generator) ---------------------------------------------------------------------

class CRG(LiteXModule):
    def __init__(self, platform):
        self.cd_fclk = ClockDomain("fclk", reset_less=True)
        self.cd_pclk = ClockDomain("pclk", reset_less=True)
        self.cd_hclk = ClockDomain("hclk", reset_less=True)
        self.cd_gclk = ClockDomain("gclk", reset_less=True)
        self.cd_xclk = ClockDomain("xclk", reset_less=True)

        # Main PLL: 33.55432MHz -> fClk/pClk/hClk/gClk/xClk.
        clk_fpga = platform.request("clk_fpga")
        self.pll = pll = GW5APLL(
            devicename = platform.devicename,
            device     = platform.device,
        )
        self.comb += pll.reset.eq(0)
        pll.register_clkin(clk_fpga, 33.55432e6)
        pll.create_clkout(self.cd_fclk, 33.55432e6 * 4, with_reset=False)  # ~134.22 MHz (ODIV=6)
        pll.create_clkout(self.cd_pclk, 33.55432e6,     with_reset=False)  # ~33.55 MHz  (ODIV=24)
        pll.create_clkout(self.cd_hclk, 33.55432e6 / 2, with_reset=False)  # ~16.78 MHz  (ODIV=48)
        pll.create_clkout(self.cd_gclk, 33.55432e6 / 4, with_reset=False)  # ~8.39 MHz   (ODIV=96)
        pll.create_clkout(self.cd_xclk, 33.55432e6 * 2, with_reset=False)  # ~67.11 MHz  (ODIV=12)

# Source Files -------------------------------------------------------------------------------------

def add_sources(platform, base_path):
    """Add all RTL source files (subsystems only -- top.v removed)."""
    src = os.path.join(base_path, "src")

    def add(path, language=None):
        full = os.path.join(src, path)
        if not os.path.exists(full):
            print(f"WARNING: Source file not found: {full}")
            return
        platform.add_source(full, language=language)

    # Gowin IP.
    add("fifo1k/fifo1k.v")
    add("gowin_adc/gowin_adc.v")

    # BSP - Board Support Package.
    add("rtl/BSP/ST7785_init.v")
    add("rtl/BSP/ST7785_panel_master.v")
    add("rtl/BSP/adc_wrap.v")
    add("rtl/BSP/aud_system_top.v")
    add("rtl/BSP/button_debounce.v")
    add("rtl/BSP/gb_burst_write.v")
    add("rtl/BSP/i2c_master.sv")
    add("rtl/BSP/mem_system_top.sv")
    add("rtl/BSP/mm_burst_read_to_stream.v")
    add("rtl/BSP/mm_burst_write.v")
    add("rtl/BSP/mpmc.v")
    add("rtl/BSP/polling_master.v")
    add("rtl/BSP/qspi_slave.v")
    add("rtl/BSP/system_monitor.sv")
    add("rtl/BSP/system_monitor_arbiter.sv")
    add("rtl/BSP/tlv320_init.v")
    add("rtl/BSP/vid_system_top.sv")
    add("rtl/BSP/vid_tpg.v")

    # BSP - UART (UART2 replaced by LiteX RS232PHY, but packet wrappers still needed).
    add("rtl/BSP/uart_packet_wrapper_rx.sv")
    add("rtl/BSP/uart_packet_wrapper_tx.sv")

    # BSP - VHDL.
    add("rtl/BSP/MultiPortRamCtrl.vhd",    language="vhdl")
    add("rtl/BSP/PSRAMBIST_Burst.vhd",     language="vhdl")
    add("rtl/BSP/PSRAMController.vhd",      language="vhdl")
    add("rtl/BSP/overlayBatteryBack.vhd",   language="vhdl")
    add("rtl/BSP/overlayBatteryFront.vhd",  language="vhdl")
    add("rtl/BSP/overlayDebug.vhd",         language="vhdl")
    add("rtl/BSP/overlayTimerBack.vhd",     language="vhdl")
    add("rtl/BSP/overlayTimerFront.vhd",    language="vhdl")
    add("rtl/BSP/overlayTimerNumber.vhd",   language="vhdl")
    # uart_rx.vhd / uart_tx.vhd removed (UART2 replaced by LiteX RS232PHY).

    # BSP - Other.
    add("rtl/BSP/tlv320regs.hex")

    # EMU - Emulation Core.
    add("rtl/EMU/audio_filter.v")
    add("rtl/EMU/cart.v")
    add("rtl/EMU/emu_system_top.v")
    add("rtl/EMU/iir_filter.sv")

    # EMU - Game Boy Core (modified MiSTer).
    add("rtl/EMU/CORE/cheatcodes.sv")
    add("rtl/EMU/CORE/dpramV.v")
    add("rtl/EMU/CORE/dpram_difV.v")
    add("rtl/EMU/CORE/gb.v")
    add("rtl/EMU/CORE/link.v")
    add("rtl/EMU/CORE/sprites.v")
    add("rtl/EMU/CORE/video.v")
    add("rtl/EMU/CORE/videoBypass.v")

    # EMU - Game Boy Core VHDL.
    add("rtl/EMU/CORE/T80/GBse.vhd",           language="vhdl")
    add("rtl/EMU/CORE/T80/T80.vhd",             language="vhdl")
    add("rtl/EMU/CORE/T80/T80_MCode.vhd",       language="vhdl")
    add("rtl/EMU/CORE/bus_savestates.vhd",       language="vhdl")
    add("rtl/EMU/CORE/gbc_snd.vhd",             language="vhdl")
    add("rtl/EMU/CORE/speedcontrol.vhd",         language="vhdl")

    # EMU - MiSTer submodule VHDL.
    add("Gameboy_MiSTer/rtl/T80/T80_ALU.vhd",    language="vhdl")
    add("Gameboy_MiSTer/rtl/T80/T80_Pack.vhd",   language="vhdl")
    add("Gameboy_MiSTer/rtl/T80/T80_Reg.vhd",    language="vhdl")
    add("Gameboy_MiSTer/rtl/gb_savestates.vhd",   language="vhdl")
    add("Gameboy_MiSTer/rtl/gb_statemanager.vhd", language="vhdl")
    add("Gameboy_MiSTer/rtl/reg_savestates.vhd",  language="vhdl")

    # EMU - MiSTer submodule Verilog.
    add("Gameboy_MiSTer/rtl/hdma.v")
    add("Gameboy_MiSTer/rtl/timer.v")

    # USB Subsystem.
    add("rtl/USB/USBUVCUART/usbuvcuart_top.v")
    add("rtl/USB/USBUVCUART/Gowin_PLL_UVC/Gowin_PLL_UVC.v")
    add("rtl/USB/USBUVCUART/color_space_convertor/color_space_convertor.v")
    add("rtl/USB/USBUVCUART/fifo_video/fifo_video.v")
    add("rtl/USB/USBUVCUART/usb_video/usb_defs.v")
    add("rtl/USB/USBUVCUART/usb_video/usb_descriptor_video.v")
    add("rtl/USB/USBUVCUART/usb_video/uvc_defs.v")
    add("rtl/USB/USBUVCUART/usb_device_controller/usb_device_controller.v")
    add("rtl/USB/USBUVCUART/usb2_0_softphy/usb2_0_softphy_top.v")
    add("rtl/USB/USBUVCUART/usb2_0_softphy/usb2_0_softphy_name.v")
    add("rtl/USB/USBUVCUART/usb2_0_softphy/usb2_0_softphy_encryption.v")
    add("rtl/USB/USBUVCUART/usb2_0_softphy/static_macro_define.v")
    add("rtl/USB/USBUVCUART/uart/uart.v")
    add("rtl/USB/USBUVCUART/sync_fifo/usb_fifo.v")
    add("rtl/USB/USBUVCUART/sync_fifo/sync_rx_pkt_fifo.v")
    add("rtl/USB/USBUVCUART/sync_fifo/sync_tx_pkt_fifo.v")

# Top Module (LiteX) ------------------------------------------------------------------------------

class ChromaticTop(Module):
    def __init__(self, platform):
        # CRG: PLL generating fClk/pClk/hClk/gClk/xClk from clk_fpga.
        self.submodules.crg = crg = CRG(platform)

        # PHY_CLKOUT clock domain (generated by USB subsystem).
        self.cd_phy = ClockDomain("phy", reset_less=True)
        self.clock_domains += self.cd_phy
        phy_clkout = Signal()
        self.comb += self.cd_phy.clk.eq(phy_clkout)

        # Request all platform resources.
        clk_24    = platform.request("clk_24")
        clk_27    = platform.request("clk_27")  # Unused but required by platform constraints.
        buttons   = platform.request("buttons")
        rgb_led   = platform.request("rgb_led")
        power     = platform.request("power")
        esp32     = platform.request("esp32_ctrl")
        serial    = platform.request("serial")
        esp_uart  = platform.request("esp32_uart0")
        qspi      = platform.request("qspi")
        ps        = platform.request("ps")
        cart      = platform.request("cart")
        lcd       = platform.request("lcd")
        audio     = platform.request("audio_codec")
        i2s       = platform.request("i2s")
        ir        = platform.request("ir")
        link      = platform.request("link")
        i2c       = platform.request("i2c")
        usb       = platform.request("usb")
        hdmi      = platform.request("hdmi")
        vbat_adc  = platform.request("vbat_adc")

        # SDIO level shifter enable (not in platform, directly driven to 1).
        platform.add_extension([("sdio_ls", 0, Pins("N5"), IOStandard("LVCMOS33"))])
        sdio_ls = platform.request("sdio_ls")
        self.comb += sdio_ls.eq(1)

        # POWER_DOWN_IO = high-Z (directly assign, synthesis handles tristate).
        # In the original: assign POWER_DOWN_IO = 1'bZ;

        # FPGA_LED_EN = always on.
        self.comb += rgb_led.en.eq(1)

        # -----------------------------------------------------------------------------------------
        # Timer / Enable Logic (gClk domain, ~8.39 MHz)
        # -----------------------------------------------------------------------------------------
        second_counter  = Signal(23, reset=0)
        second_ena      = Signal()
        half_second_ena = Signal()
        percent_counter = Signal(17, reset=0)
        percent_ena     = Signal()

        self.sync.gclk += [
            percent_ena.eq(0),
            If(percent_counter == 83886,
                percent_ena.eq(1),
                percent_counter.eq(0),
            ).Else(
                percent_counter.eq(percent_counter + 1),
            ),

            second_ena.eq(0),
            half_second_ena.eq(0),
            If(second_counter == 4194303,
                half_second_ena.eq(1),
            ),
            If(second_counter == 8388607,
                second_ena.eq(1),
                half_second_ena.eq(1),
                second_counter.eq(0),
                percent_counter.eq(0),
            ).Else(
                second_counter.eq(second_counter + 1),
            ),
        ]

        # -----------------------------------------------------------------------------------------
        # Cart Detect Debounce & Memory Reset (xClk domain)
        # -----------------------------------------------------------------------------------------
        cart_det_sr = Signal(18)
        memrst      = Signal(reset=1)

        self.sync.xclk += [
            cart_det_sr.eq(Cat(cart.det, cart_det_sr[:17])),
        ]

        # memrst: set when PLL unlocked, or cart detect transitions.
        self.sync.xclk += [
            If(~crg.pll.locked,
                memrst.eq(1),
            ).Else(
                memrst.eq(
                    (cart_det_sr[2:18] == 0x7FFF) |
                    (cart_det_sr[2:18] == 0x8000)
                ),
            ),
        ]

        # -----------------------------------------------------------------------------------------
        # LED State Machine (xClk domain)
        # -----------------------------------------------------------------------------------------
        led_green  = Signal()
        led_red    = Signal()
        led_yellow = Signal()
        led_white  = Signal()

        self.sync.xclk += [
            If(led_white,
                rgb_led.r.eq(0), rgb_led.b.eq(0), rgb_led.g.eq(0),
            ).Elif(led_green,
                rgb_led.r.eq(1), rgb_led.b.eq(1), rgb_led.g.eq(0),
            ).Elif(led_yellow,
                rgb_led.r.eq(0), rgb_led.b.eq(1), rgb_led.g.eq(second_counter[4]),
            ).Elif(led_red,
                rgb_led.r.eq(0), rgb_led.b.eq(1), rgb_led.g.eq(1),
            ).Else(
                rgb_led.r.eq(1), rgb_led.b.eq(1), rgb_led.g.eq(1),
            ),
        ]

        # -----------------------------------------------------------------------------------------
        # LCD Enable Sync (gClk domain, async reset on memrst)
        # -----------------------------------------------------------------------------------------
        lcd_vsync_r1       = Signal()
        lcd_en0            = Signal()
        lcd_en1            = Signal()
        lcd_en             = Signal()
        lcd_init_done      = Signal()
        lcd_backlight_init = Signal()
        q_menu_init        = Signal()

        self.sync.gclk += lcd_vsync_r1.eq(lcd.vsync)

        self.sync.gclk += [
            If(memrst,
                lcd_en.eq(0), lcd_en0.eq(0), lcd_en1.eq(0),
            ).Else(
                If(lcd.vsync & ~lcd_vsync_r1,
                    lcd_en0.eq(lcd_init_done & lcd_backlight_init),
                    lcd_en1.eq(lcd_en0),
                    lcd_en.eq(lcd_en1),
                ),
            ),
        ]

        # -----------------------------------------------------------------------------------------
        # USB Init Delay (gClk domain)
        # -----------------------------------------------------------------------------------------
        usb_init_cnt = Signal(24, reset=0)
        usb_rst      = Signal(reset=1)

        self.sync.gclk += [
            If(~crg.pll.locked,
                usb_init_cnt.eq(0),
                usb_rst.eq(1),
            ).Elif(usb_init_cnt < 8388607,
                usb_init_cnt.eq(usb_init_cnt + 1),
                usb_rst.eq(1),
            ).Else(
                usb_rst.eq(0),
            ),
        ]

        # -----------------------------------------------------------------------------------------
        # UVC Pipeline Registers (gClk domain)
        # -----------------------------------------------------------------------------------------
        lcd_enable_uvc = Signal()
        lcd_db_uvc     = Signal(18)
        hr1            = Signal()
        vr1            = Signal()
        he1            = Signal()
        d1             = Signal(18)

        self.sync.gclk += [
            If(memrst,
                hr1.eq(0), vr1.eq(0), he1.eq(0), d1.eq(0),
            ).Else(
                hr1.eq(lcd.hsync),
                vr1.eq(lcd.vsync),
                he1.eq(lcd_enable_uvc),
                d1.eq(lcd_db_uvc),
            ),
        ]

        # -----------------------------------------------------------------------------------------
        # ESP32 UART Resync & Boot Control (PHY_CLKOUT & gClk domains)
        # -----------------------------------------------------------------------------------------
        usb_locked   = Signal()
        uart_txd     = Signal(reset=1)
        uart_rxd     = Signal()
        uart_dtr     = Signal()
        uart_rts     = Signal()

        esp32_en_int  = Signal(reset=1)
        esp32_io0_int = Signal(reset=1)

        # PHY_CLKOUT domain: UART resync + ESP32 EN/IO0 from USB DTR/RTS.
        self.sync.phy += [
            If(~usb_locked,
                uart_txd.eq(1),
                esp_uart.rx.eq(1),
                esp32_en_int.eq(1),
                esp32_io0_int.eq(1),
            ).Else(
                uart_txd.eq(esp_uart.tx),
                esp_uart.rx.eq(uart_rxd),
                esp32_en_int.eq(~uart_rts),
                esp32_io0_int.eq(uart_dtr == 0),
            ),
        ]

        # gClk domain: ESP32 boot delay.
        esp_boot_delay_cnt   = Signal(12, reset=0)
        esp_boot_delay_shift = Signal(8, reset=0)

        self.sync.gclk += [
            esp32.io0.eq(esp32_io0_int),
            esp_boot_delay_cnt.eq(esp_boot_delay_cnt + 1),
            If(esp_boot_delay_cnt == 0,
                esp_boot_delay_shift.eq(Cat(esp32_en_int, esp_boot_delay_shift[:7])),
                esp32.en.eq(esp_boot_delay_shift[7]),
            ),
            If(~esp32_en_int,
                esp_boot_delay_shift.eq(0),
                esp32.en.eq(0),
            ),
        ]

        # -----------------------------------------------------------------------------------------
        # Button Debouncers
        # -----------------------------------------------------------------------------------------
        btn_a_f    = Signal()
        btn_b_f    = Signal()
        btn_down_f = Signal()
        btn_left_f = Signal()
        btn_right_f= Signal()
        btn_up_f   = Signal()
        btn_sel_f  = Signal()
        btn_start_f= Signal()

        for name, raw, filt in [
            ("A",     buttons.a,          btn_a_f),
            ("B",     buttons.b,          btn_b_f),
            ("DOWN",  buttons.dpad_down,  btn_down_f),
            ("LEFT",  buttons.dpad_left,  btn_left_f),
            ("RIGHT", buttons.dpad_right, btn_right_f),
            ("UP",    buttons.dpad_up,    btn_up_f),
            ("SEL",   buttons.sel,        btn_sel_f),
            ("START", buttons.start,      btn_start_f),
        ]:
            self.specials += Instance("button_debouncer",
                name = f"debouncer_{name}",
                i_clk    = ClockSignal("gclk"),
                i_button = raw,
                o_state  = filt,
            )

        # Button merge with MCU buttons.
        mcu_buttons   = Signal(9)
        btn_menu_ored = Signal()
        menu_disabled = Signal()
        slide_out_active = Signal()

        self.comb += btn_menu_ored.eq(buttons.menu & ~mcu_buttons[8])

        # Menu gating.
        menu_gated = Signal()
        self.comb += [
            If(q_menu_init & (cart_det_sr[3:7] == 0xF),
                menu_gated.eq(btn_menu_ored),
            ).Else(
                menu_gated.eq(1),
            ),
        ]

        # -----------------------------------------------------------------------------------------
        # Inter-module Signals
        # -----------------------------------------------------------------------------------------
        # Video.
        h_wr_burst_q  = Signal(16)
        h_wr_burst_q2 = Signal(16)
        gb_lcd_clkena  = Signal()
        gb_lcd_data    = Signal(15)
        gb_lcd_mode    = Signal(2)
        gb_lcd_on      = Signal()
        gb_lcd_vsync   = Signal()
        h_gb_newline   = Signal()
        h_gb_address   = Signal(23)
        h_gb_write     = Signal()
        h_gb_data      = Signal(16)
        h_draw_osd     = Signal()

        # Audio.
        left  = Signal(16)
        right = Signal(16)
        volume = Signal(8)
        h_headphones = Signal()

        # System.
        debug_system   = Signal(32)
        system_control = Signal(16)
        low_battery    = Signal()
        boot_rom_enabled = Signal()
        pmic_sys_status  = Signal(8)
        lcd_on_int       = Signal()
        lcd_off_overwrite = Signal()

        # Palette.
        palette_bg_in   = Signal(64)
        palette_obj0_in = Signal(64)
        palette_obj1_in = Signal(64)
        gbc_mode        = Signal()
        gpd             = Signal(64)

        # ADC.
        h_adc_value_r1 = Signal(14)
        h_adc_req_ext  = Signal()
        h_adc_ready_r1 = Signal()

        # UART.
        uart_tx_data = Signal(8)
        uart_tx_busy = Signal()
        uart_tx_val  = Signal()
        uart_rx_data = Signal(16)
        uart_rx_val  = Signal()

        # -----------------------------------------------------------------------------------------
        # HDMI Debug Signals (directly routed internal signals)
        # -----------------------------------------------------------------------------------------
        self.comb += [
            hdmi.d_p[2].eq(lcd_on_int),
            hdmi.d_n[2].eq(h_draw_osd),
            hdmi.d_p[1].eq(lcd_off_overwrite),
            hdmi.d_n[1].eq(gb_lcd_on),
            hdmi.d_p[0].eq(gb_lcd_vsync),
            hdmi.d_n[0].eq(gb_lcd_mode[1]),
            hdmi.clk_p.eq(gb_lcd_clkena),
            hdmi.clk_n.eq(h_gb_write),
        ]

        # I2S_BCLK = menuDisabled.
        self.comb += i2s.bclk.eq(menu_disabled)

        # -----------------------------------------------------------------------------------------
        # Subsystem Instances
        # -----------------------------------------------------------------------------------------

        # Video System.
        self.specials += Instance("vid_system_top",
            p_ISSIMU = 0,
            i_gClk   = ClockSignal("gclk"),
            i_hClk   = ClockSignal("hclk"),
            i_pClk   = ClockSignal("pclk"),
            i_reset  = memrst,

            i_BTN_MENU       = menu_disabled,
            o_slideOutActive = slide_out_active,

            o_LCD_DB         = lcd.db,
            o_LCD_ENABLE_UVC = lcd_enable_uvc,
            o_LCD_DB_UVC     = lcd_db_uvc,
            o_LCD_DOTCLK     = lcd.dotclk,
            o_LCD_ENABLE     = lcd.enable,
            o_LCD_HSYNC      = lcd.hsync,
            i_LCD_EN         = lcd_en,
            o_LCD_RESET      = lcd.reset,
            o_LCD_SPI_CSX    = lcd.spi_csx,
            o_LCD_SPI_SCLK   = lcd.spi_sclk,
            o_LCD_SPI_SDA    = lcd.spi_sda,
            i_LCD_TE         = lcd.te,
            o_LCD_VSYNC      = lcd.vsync,
            o_LCD_GENLOCK    = Signal(),  # Unused.

            i_frameBlendEnable         = system_control[1],
            i_colorCorrectionEnableLCD = system_control[2],
            i_colorCorrectionEnableUVC = system_control[3],
            i_voltageLow               = low_battery,
            i_lowBattDispMode          = system_control[13:15],
            i_showTimer                = 0,
            i_runTimer                 = system_control[9],
            i_resetTimer               = system_control[10],
            i_gSecondEna               = second_ena,
            i_gPercentEna              = percent_ena,
            i_debug_system             = debug_system,
            i_debug_system_on          = 0,

            o_hDrawOSD    = h_draw_osd,
            o_hGBNewLine  = h_gb_newline,
            o_hGBAddress  = h_gb_address,
            o_hGBWrite    = h_gb_write,
            o_hGBData     = h_gb_data,

            o_hValid      = Signal(),  # Connected elsewhere.
            o_hHsync      = Signal(),
            o_hVsync      = Signal(),
            o_hWrBurstQ   = h_wr_burst_q,
            o_hWrBurstQ2  = h_wr_burst_q2,

            o_LCD_INIT_DONE  = lcd_init_done,
            i_gb_lcd_clkena  = gb_lcd_clkena,
            i_gb_lcd_mode    = gb_lcd_mode,
            i_gb_lcd_on      = gb_lcd_on,
            i_gb_lcd_vsync   = gb_lcd_vsync,
            i_gb_lcd_data    = gb_lcd_data,
        )

        # Audio System.
        self.specials += Instance("aud_system_top",
            i_gClk     = ClockSignal("gclk"),
            i_hClk     = ClockSignal("hclk"),
            i_reset_n  = crg.pll.locked,
            i_left     = left,
            i_right    = right,

            o_AUD_BCLK  = audio.bclk,
            o_AUD_DIN   = audio.din,
            o_AUD_DOUT  = Signal(),  # Unused.
            o_AUD_MCLK  = audio.mclk,
            o_AUD_RESET = audio.reset,
            o_AUD_WCLK  = audio.wclk,

            i_software_mute    = system_control[0],
            o_pmic_sys_status  = pmic_sys_status,
            o_volume           = volume,
            o_hHeadphones      = h_headphones,
            io_SCL             = i2c.scl,
            io_SDA             = i2c.sda,
        )

        # Memory System.
        self.specials += Instance("mem_system_top",
            p_ISSIMU = 0,
            i_xClk  = ClockSignal("xclk"),
            i_fClk  = ClockSignal("fclk"),
            i_hClk  = ClockSignal("hclk"),
            i_reset = memrst,

            i_QSPI_CLK  = qspi.clk,
            i_QSPI_MOSI = qspi.mosi,
            i_QSPI_MISO = qspi.miso,
            i_QSPI_CS   = qspi.cs_n,
            i_QSPI_WP   = qspi.wp_n,
            i_QSPI_HD   = qspi.hd,

            o_PS_CE_N = ps.ce_n,
            o_PS_CLK  = ps.clk,
            io_PS_DQ  = ps.dq,
            io_PS_DQS = ps.dqs,

            o_BIST_failed   = Signal(),
            o_BIST_finished = Signal(),
            o_qMenuInit     = q_menu_init,
            i_hGBNewLine    = h_gb_newline,
            i_hGBAddress    = h_gb_address,
            i_hGBWrite      = h_gb_write,
            i_hGBData       = h_gb_data,

            i_hValid      = gb_lcd_clkena,
            i_hHsync      = gb_lcd_mode[1],
            i_hVsync      = gb_lcd_vsync,
            o_hWrBurstQ   = h_wr_burst_q,
            o_hWrBurstQ2  = h_wr_burst_q2,
        )

        # Emulation System.
        self.specials += Instance("emu_system_top",
            i_hclk      = ClockSignal("hclk"),
            i_pclk      = ClockSignal("pclk"),
            i_reset_n   = ~memrst,
            i_POWER_GOOD = ~power.on_fpga,

            i_customPaletteEna = palette_bg_in[63],
            i_paletteOff       = system_control[12],
            i_paletteBGIn      = palette_bg_in,
            i_paletteOBJ0In    = palette_obj0_in,
            i_paletteOBJ1In    = palette_obj1_in,
            o_gbc_mode         = gbc_mode,
            o_gpd              = gpd,

            i_BTN_NODIAGONAL   = system_control[11],
            i_BTN_A            = btn_a_f     | mcu_buttons[3],
            i_BTN_B            = btn_b_f     | mcu_buttons[2],
            i_BTN_DPAD_DOWN    = btn_down_f  | mcu_buttons[7],
            i_BTN_DPAD_LEFT    = btn_left_f  | mcu_buttons[6],
            i_BTN_DPAD_RIGHT   = btn_right_f | mcu_buttons[5],
            i_BTN_DPAD_UP      = btn_up_f    | mcu_buttons[4],
            i_BTN_MENU         = ~btn_menu_ored,
            i_BTN_SEL          = btn_sel_f   | mcu_buttons[1],
            i_BTN_START        = btn_start_f | mcu_buttons[0],
            i_MENU_CLOSED      = menu_disabled & ~slide_out_active,

            o_CART_A          = cart.a,
            o_CART_CLK        = cart.clk,
            o_CART_CS         = cart.cs,
            io_CART_D         = cart.d,
            o_CART_RD         = cart.rd,
            io_CART_RST       = cart.rst,
            o_CART_WR         = cart.wr,
            o_CART_DATA_DIR_E = cart.data_dir_e,

            i_IR_RX  = ir.rx,
            o_IR_LED = ir.led,

            io_LINK_CLK = link.clk,
            i_LINK_IN   = getattr(link, "in"),
            o_LINK_OUT  = link.out,

            o_lcd_on_int       = lcd_on_int,
            o_lcd_off_overwrite = lcd_off_overwrite,
            o_boot_rom_enabled = boot_rom_enabled,

            o_left  = left,
            o_right = right,

            i_LCD_INIT_DONE  = lcd_init_done,
            o_gb_lcd_clkena  = gb_lcd_clkena,
            o_gb_lcd_mode    = gb_lcd_mode,
            o_gb_lcd_on      = gb_lcd_on,
            o_gb_lcd_vsync   = gb_lcd_vsync,
            o_gb_lcd_data    = gb_lcd_data,
        )

        # USB UVC+UART System.
        debugs = Signal(8)
        self.specials += Instance("usbuvcuart_top",
            i_CLK_24MHz = clk_24,
            i_ERST      = usb_rst,
            o_pClk      = phy_clkout,
            o_usblocked = usb_locked,
            i_hClk      = ClockSignal("gclk"),

            o_UART_TXD   = uart_rxd,
            i_UART_RXD   = uart_txd,
            o_E_UART_DTR = uart_dtr,
            o_E_UART_RTS = uart_rts,

            i_left  = left,
            i_right = right,

            i_hLineValid  = hr1,
            i_hEnable     = he1,
            i_hFrameValid = vr1,
            i_hData       = d1,
            o_debugs      = debugs,
            i_playerNum   = Cat(system_control[4:8], Constant(0, 4)),

            io_usb_dxp_io     = usb.dxp,
            io_usb_dxn_io     = usb.dxn,
            i_usb_rxdp_i      = usb.rxdp,
            i_usb_rxdn_i      = usb.rxdn,
            o_usb_pullup_en_o = usb.pullup,
            io_usb_term_dp_io = usb.term_dp,
            io_usb_term_dn_io = usb.term_dn,
        )

        # Battery ADC.
        self.specials += Instance("adc_wrap",
            i_clk         = ClockSignal("gclk"),
            i_reset_n     = crg.pll.locked,
            o_hAdcReq_ext = h_adc_req_ext,
            o_hAdcValue_r1 = h_adc_value_r1,
            o_hAdcReady_r1 = h_adc_ready_r1,
            i_VBAT_ADC_P  = vbat_adc.p,
            i_VBAT_ADC_N  = vbat_adc.n,
        )

        # System Monitor.
        self.specials += Instance("system_monitor",
            i_clk     = ClockSignal("gclk"),
            i_reset   = ~crg.pll.locked,

            i_BTN_A          = btn_a_f,
            i_BTN_B          = btn_b_f,
            i_BTN_DPAD_DOWN  = btn_down_f,
            i_BTN_DPAD_LEFT  = btn_left_f,
            i_BTN_DPAD_RIGHT = btn_right_f,
            i_BTN_DPAD_UP    = btn_up_f,
            i_BTN_MENU       = menu_gated,
            i_BTN_SEL        = btn_sel_f,
            i_BTN_START      = btn_start_f,

            o_menuDisabled       = menu_disabled,
            o_LCD_BACKLIGHT_INIT = lcd_backlight_init,
            i_LCD_INIT_DONE      = lcd_init_done & ~boot_rom_enabled,
            o_LCD_PWM            = lcd.pwm,

            o_hAdcReq_ext  = h_adc_req_ext,
            i_hAdcValue_r1 = h_adc_value_r1,
            i_hAdcReady_r1 = h_adc_ready_r1,
            o_ADC_SEL      = audio.adc_sel,

            i_hButtons         = 0,
            o_MCU_buttons      = mcu_buttons,
            i_hVolume          = volume[:7],
            o_pmic_sys_status  = pmic_sys_status,
            i_hHeadphones      = h_headphones,
            i_gSecondEna       = second_ena,
            i_gHalfSecondEna   = half_second_ena,
            o_debug_system     = debug_system,
            o_low_battery      = low_battery,
            o_LED_Green        = led_green,
            o_LED_Red          = led_red,
            o_LED_Yellow       = led_yellow,
            o_LED_White        = led_white,
            o_system_control   = system_control,
            o_paletteBGIn      = palette_bg_in,
            o_paletteOBJ0In    = palette_obj0_in,
            o_paletteOBJ1In    = palette_obj1_in,
            i_gbc_mode         = gbc_mode,
            i_gpd              = gpd,

            i_uart_rx_data = uart_rx_data[:8],
            i_uart_rx_val  = uart_rx_val,
            o_uart_tx_busy = uart_tx_busy,
            o_uart_tx_data = uart_tx_data,
            o_uart_tx_val  = uart_tx_val,
        )

        # UART (FPGA <-> ESP32 MCU) -- LiteX RS232PHY replacing UART2.
        serial_pads = Record([("tx", 1), ("rx", 1)])
        self.comb += [
            serial.tx.eq(serial_pads.tx),
            serial_pads.rx.eq(serial.rx),
        ]
        self.submodules.uart_phy = ClockDomainsRenamer("gclk")(
            RS232PHY(serial_pads, clk_freq=int(33.55432e6 / 4), baudrate=115200)
        )
        # TX: system_monitor -> UART PHY.
        self.comb += [
            self.uart_phy.sink.valid.eq(uart_tx_val),
            self.uart_phy.sink.data.eq(uart_tx_data),
            uart_tx_busy.eq(~self.uart_phy.sink.ready),
        ]
        # RX: UART PHY -> system_monitor.
        self.comb += [
            uart_rx_val.eq(self.uart_phy.source.valid),
            uart_rx_data.eq(self.uart_phy.source.data),
            self.uart_phy.source.ready.eq(1),
        ]

        # LINK_SD output.
        self.comb += link.sd.eq(0)  # Directly driven by emu_system_top via CART signals.
        # Note: LINK_SD is driven by emu_system_top in the original, but it's in the top port list.
        # The emu_system_top instance doesn't have a LINK_SD port, so it defaults here.

# Build --------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Chromatic FPGA LiteX Build")
    parser.add_argument("--build",      action="store_true", help="Build bitstream.")
    parser.add_argument("--no-compile", action="store_true", help="Generate build files without running toolchain.")
    parser.add_argument("--toolchain",  default="gowin",     help="FPGA toolchain (gowin or apicula).")
    args = parser.parse_args()

    # Create Platform.
    platform = Platform(
        toolchain = args.toolchain,
        device    = "GW5A-EV25UG256CC1/I0",
    )

    # Gowin Build Options.
    platform.toolchain.options["verilog_std"]            = "sysv2017"
    platform.toolchain.options["vhdl_std"]               = "vhd2008"
    platform.toolchain.options["rw_check_on_ram"]        = 1
    platform.toolchain.options["power_on_reset_monitor"] = 1
    platform.toolchain.options["multi_boot"]             = 0
    platform.toolchain.options["bit_format"]             = "bin"
    platform.toolchain.options["bg_programming"]         = "jtag_sspi_qsspi"

    # Add Sources.
    base_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "esp32t")
    add_sources(platform, base_path)

    # Timing Constraints.
    # Note: Base clocks (clk_fpga, clk_24, clk_27) are auto-generated by LiteX in the .sdc.
    # The original evt1_x2.sdc referenced old signal/instance names -- we replace it with
    # constraints using LiteX-generated names.
    platform.toolchain.additional_cst_commands += [
        # Generated PLL clocks (main PLL, managed by LiteX).
        'create_generated_clock -name xclk2 -source [get_ports {clk_fpga}] -master_clock clk_fpga -divide_by 1 -multiply_by 4 [get_pins {PLLA/CLKOUT0}]',
        'create_generated_clock -name pclk  -source [get_ports {clk_fpga}] -master_clock clk_fpga -divide_by 1 -multiply_by 1 [get_pins {PLLA/CLKOUT1}]',
        'create_generated_clock -name hclk  -source [get_ports {clk_fpga}] -master_clock clk_fpga -divide_by 2 -multiply_by 1 [get_pins {PLLA/CLKOUT2}]',
        'create_generated_clock -name gclk  -source [get_ports {clk_fpga}] -master_clock clk_fpga -divide_by 4 -multiply_by 1 [get_pins {PLLA/CLKOUT3}]',
        'create_generated_clock -name xclk  -source [get_ports {clk_fpga}] -master_clock clk_fpga -divide_by 1 -multiply_by 2 [get_pins {PLLA/CLKOUT4}]',
        # QSPI clock.
        'create_clock -name sclk -period 25 [get_ports {qspi_clk}]',
        # Async clock groups.
        'set_clock_groups -asynchronous -group [get_clocks {pclk}] -group [get_clocks {hclk}]',
        'set_clock_groups -asynchronous -group [get_clocks {pclk}] -group [get_clocks {gclk}]',
        'set_clock_groups -asynchronous -group [get_clocks {hclk}] -group [get_clocks {gclk}]',
        # Cartridge bus timing constraints.
        'set_max_delay -from [get_ports {cart_d[*]}] -to [get_clocks {hclk}] 13',
        'set_max_delay -from [get_clocks {hclk}] -to  [get_ports {cart_a[*]}] 14',
        'set_max_delay -from [get_clocks {hclk}] -to  [get_ports {cart_wr}] 14',
        'set_max_delay -from [get_clocks {hclk}] -to  [get_ports {cart_rd}] 14',
        'set_max_delay -from [get_clocks {hclk}] -to  [get_ports {cart_cs}] 14',
        'set_max_delay -from [get_clocks {hclk}] -to  [get_ports {link_sd}] 14',
        'set_max_delay -from [get_clocks {hclk}] -to  [get_ports {cart_d[*]}] 14',
        # USB clocks (inside usbuvcuart_top, paths updated for LiteX hierarchy).
        'create_generated_clock -name PHY_CLKOUT -source [get_ports {clk_24}] -master_clock clk_24 -divide_by 16 -multiply_by 40 [get_pins {usbuvcuart_top/u_Gowin_PLL_USB/PLLA_inst/CLKOUT1}]',
        'create_generated_clock -name fclk_960M  -source [get_ports {clk_24}] -master_clock clk_24 -divide_by 1  -multiply_by 40 [get_nets {usbuvcuart_top/fclk_960M}]',
        'create_generated_clock -name clk24p     -source [get_ports {clk_24}] -master_clock clk_24 -divide_by 1  -multiply_by 1  [get_pins {usbuvcuart_top/u_Gowin_PLL_USB/PLLA_inst/CLKOUT2}]',
        'create_clock -name usbintsclk -period 8 -waveform {0 4} [get_nets {usbuvcuart_top/u_USB_SoftPHY_Top/usb2_0_softphy/u_usb_20_phy_utmi/u_usb2_0_softphy/u_usb_phy_hs/sclk}] -add',
        'set_clock_groups -asynchronous -group [get_clocks {PHY_CLKOUT}] -group [get_clocks {fclk_960M}]',
        'set_clock_groups -asynchronous -group [get_clocks {PHY_CLKOUT}] -group [get_clocks {usbintsclk}]',
    ]

    # Create Top Module & Build.
    top = ChromaticTop(platform)
    if args.build:
        platform.build(top,
            build_name = "chromatic",
            run        = not args.no_compile,
        )

if __name__ == "__main__":
    main()

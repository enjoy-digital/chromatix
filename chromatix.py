#!/usr/bin/env python3

#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
ChromatiX: LiteX based FPGA design for the ModRetro Chromatic handheld.

Integrates clock generation, button debouncing, LCD/video pipeline, audio I2S, I2C codec control,
memory system, Game Boy emulation core, USB UVC+UART, battery ADC and system monitoring.
"""

import argparse
from types import MethodType

from migen import *

from litex.gen import *

from litex.soc.cores.uart import RS232PHY

from litei2c import LiteI2CPHYCore

from chromatix import Platform

from chromatix.gateware.crg     import CRG
from chromatix.gateware.sources import add_verilog_sources
from chromatix.gateware.lcd     import ST7785Init, load_st7785_sequence
from chromatix.gateware.codec   import TLV320Init, PollingMaster, load_tlv320_registers
from chromatix.gateware.sysmon  import SystemMonitorBridge, SystemMonitorPayloads

# Timing Constraints -------------------------------------------------------------------------------

TIMING_CONSTRAINTS = [
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

def add_timing_constraints(platform):
    original_build_timing_constraints = platform.toolchain.build_timing_constraints

    def build_timing_constraints(toolchain, vns):
        sdc = original_build_timing_constraints(vns)
        with open(sdc[0], "a") as f:
            f.write("\n" + "\n".join(TIMING_CONSTRAINTS) + "\n")
        return sdc

    platform.toolchain.build_timing_constraints = MethodType(build_timing_constraints, platform.toolchain)

# BaseSoC ------------------------------------------------------------------------------------------

class BaseSoC(Module):
    """
    ChromatiX Top-Level Module.

    Integrates all subsystems of the ModRetro Chromatic handheld: clock generation, video/LCD
    pipeline, audio I2S with TLV320 codec, Game Boy emulation core, memory controller, USB
    UVC+UART, ESP32 MCU communication, battery ADC, button debouncing, and system monitoring.

    Parameters:
    - platform : GowinPlatform instance providing device pads and build infrastructure.
    """
    def __init__(self, platform):

        # CRG --------------------------------------------------------------------------------------
        self.submodules.crg = crg = CRG(platform)

        # PHY_CLKOUT clock domain (generated by USB subsystem).
        self.cd_phy = ClockDomain("phy", reset_less=True)
        self.clock_domains += self.cd_phy
        phy_clkout = Signal()
        self.comb += self.cd_phy.clk.eq(phy_clkout)

        # Platform Resources -----------------------------------------------------------------------

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

        # SDIO level shifter enable.
        sdio_ls = platform.request("sdio_ls")
        self.comb += sdio_ls.eq(1)

        # POWER_DOWN_IO = high-Z (directly assign, synthesis handles tristate).
        # In the original: assign POWER_DOWN_IO = 1'bZ;

        # FPGA_LED_EN = always on.
        self.comb += rgb_led.en.eq(1)

        # Timer / Enable Logic (gClk domain) -------------------------------------------------------

        second_counter  = Signal(23, reset=0)
        second_ena      = Signal()
        half_second_ena = Signal()
        percent_counter = Signal(17, reset=0)
        percent_ena     = Signal()

        # 1% (~83886 cycles) and 1s/0.5s (~4M/8M cycles) enable pulses at ~8.39 MHz.
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

        # Cart Detect Debounce & Memory Reset (xClk domain) ----------------------------------------

        cart_det_sr = Signal(18)
        memrst      = Signal(reset=1)

        # 18-bit shift register sampling cart detect pin.
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

        # LED State Machine (xClk domain) ----------------------------------------------------------

        led_green         = Signal()
        led_red           = Signal()
        led_yellow        = Signal()
        led_white         = Signal()
        boot_led_counter  = Signal(26)
        boot_led_active   = Signal()
        boot_led_white    = Signal()

        # Flash white three times after configuration so a custom build is obvious on hardware.
        self.sync.xclk += [
            If(~crg.pll.locked,
                boot_led_counter.eq(0),
            ).Elif(boot_led_counter[23:26] != 6,
                boot_led_counter.eq(boot_led_counter + 1),
            )
        ]
        self.comb += [
            boot_led_active.eq(boot_led_counter[23:26] < 6),
            boot_led_white.eq(~boot_led_counter[23]),
        ]

        # Priority-encoded LED color selection (active-low RGB).
        self.sync.xclk += [
            If(boot_led_active & boot_led_white,
                rgb_led.r.eq(0), rgb_led.b.eq(0), rgb_led.g.eq(0),
            ).Elif(boot_led_active,
                rgb_led.r.eq(1), rgb_led.b.eq(1), rgb_led.g.eq(1),
            ).Elif(led_white,
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

        # LCD Enable Sync (gClk domain, async reset on memrst) -------------------------------------

        lcd_vsync_r1       = Signal()
        lcd_en0            = Signal()
        lcd_en1            = Signal()
        lcd_en             = Signal()
        lcd_init_done      = Signal()
        lcd_backlight_init = Signal()
        q_menu_init        = Signal()

        # Three-stage vsync-synchronized LCD enable with init gating.
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

        # USB Init Delay (gClk domain) -------------------------------------------------------------

        usb_init_cnt = Signal(24, reset=0)
        usb_rst      = Signal(reset=1)

        # Hold USB in reset until ~1s after PLL lock.
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

        # UVC Pipeline Registers (gClk domain) -----------------------------------------------------

        lcd_enable_uvc = Signal()
        lcd_db_uvc     = Signal(18)
        hr1            = Signal()
        vr1            = Signal()
        he1            = Signal()
        d1             = Signal(18)

        # One-stage pipeline delay for UVC video path, cleared on memrst.
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

        # ESP32 UART Resync & Boot Control (PHY_CLKOUT & gClk domains) -----------------------------

        usb_locked    = Signal()
        uart_txd      = Signal(reset=1)
        uart_rxd      = Signal()
        uart_dtr      = Signal()
        uart_rts      = Signal()

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
                esp32_io0_int.eq((uart_dtr == 0) & (uart_rts == 0)),
            ),
        ]

        # gClk domain: ESP32 boot delay (shift register debounces EN toggle).
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

        # Button Debouncers (gClk domain) ----------------------------------------------------------

        btn_a_f     = Signal()
        btn_b_f     = Signal()
        btn_down_f  = Signal()
        btn_left_f  = Signal()
        btn_right_f = Signal()
        btn_up_f    = Signal()
        btn_sel_f   = Signal()
        btn_start_f = Signal()

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
            # LiteX debouncer replacing Verilog button_debouncer module.
            # 3-stage input sampling + 15-bit counter (HIGHBIT=14).
            sampling = Signal(3, name=f"btn_{name}_samp")
            count    = Signal(15, name=f"btn_{name}_cnt")
            self.sync.gclk += [
                sampling.eq(Cat(raw, sampling[:2])),
                If(~filt,
                    If(~sampling[2],
                        count.eq(0),
                    ).Elif(~count[14],
                        count.eq(count + 1),
                    ),
                    If(count[14],
                        filt.eq(1),
                        count.eq(0),
                    ),
                ).Else(
                    If(sampling[2],
                        count.eq(0),
                    ).Elif(~count[14],
                        count.eq(count + 1),
                    ),
                    If(count[14],
                        filt.eq(0),
                        count.eq(0),
                    ),
                ),
            ]

        # Button Merge & Menu Gating ---------------------------------------------------------------

        mcu_buttons      = Signal(9)
        btn_menu_ored    = Signal()
        menu_disabled    = Signal()
        slide_out_active = Signal()

        # OR physical menu button with MCU menu bit.
        self.comb += btn_menu_ored.eq(buttons.menu & ~mcu_buttons[8])

        # Gate menu button until cart is stable and menu init is complete.
        menu_gated = Signal()
        self.comb += [
            If(q_menu_init & (cart_det_sr[3:7] == 0xF),
                menu_gated.eq(btn_menu_ored),
            ).Else(
                menu_gated.eq(1),
            ),
        ]

        # Inter-Module Signals ---------------------------------------------------------------------

        # Video.
        h_wr_burst_q   = Signal(16)
        h_wr_burst_q2  = Signal(16)
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
        left         = Signal(16)
        right        = Signal(16)
        volume       = Signal(8)
        h_headphones = Signal()

        # System.
        debug_system      = Signal(32)
        system_control    = Signal(16)
        low_battery       = Signal()
        boot_rom_enabled  = Signal()
        pmic_sys_status   = Signal(8)
        lcd_on_int        = Signal()
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

        # HDMI Debug Signals -----------------------------------------------------------------------

        # Route internal status signals to HDMI pads for logic-analyzer probing.
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

        # LCD SPI Init -----------------------------------------------------------------------------

        self.submodules.st7785_init = st7785_init = ST7785Init(load_st7785_sequence())
        self.comb += [
            st7785_init.reset.eq(memrst),
            lcd.spi_csx.eq(st7785_init.lcd_cs),
            lcd.spi_sclk.eq(st7785_init.lcd_sck),
            lcd.spi_sda.eq(st7785_init.lcd_sda_sdi),
            lcd.reset.eq(st7785_init.lcd_rst),
            lcd_init_done.eq(st7785_init.lcd_init_done),
        ]

        # Video System -----------------------------------------------------------------------------

        # LCD init now runs from the LiteX/Migen ST7785 sequencer at top level.
        self.specials += Instance("vid_system_top",
            p_ISSIMU                   = 0,
            i_gClk                     = ClockSignal("gclk"),
            i_hClk                     = ClockSignal("hclk"),
            i_pClk                     = ClockSignal("pclk"),
            i_reset                    = memrst,
            # Menu.
            i_BTN_MENU                 = menu_disabled,
            o_slideOutActive           = slide_out_active,
            # LCD.
            o_LCD_DB                   = lcd.db,
            o_LCD_ENABLE_UVC           = lcd_enable_uvc,
            o_LCD_DB_UVC               = lcd_db_uvc,
            o_LCD_DOTCLK               = lcd.dotclk,
            o_LCD_ENABLE               = lcd.enable,
            o_LCD_HSYNC                = lcd.hsync,
            i_LCD_EN                   = lcd_en,
            i_LCD_TE                   = lcd.te,
            o_LCD_VSYNC                = lcd.vsync,
            o_LCD_GENLOCK              = Signal(),
            i_LCD_INIT_DONE            = lcd_init_done,
            # Display Options.
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
            # OSD / Frame Buffer.
            o_hDrawOSD                 = h_draw_osd,
            o_hGBNewLine               = h_gb_newline,
            o_hGBAddress               = h_gb_address,
            o_hGBWrite                 = h_gb_write,
            o_hGBData                  = h_gb_data,
            o_hValid                   = Signal(),
            o_hHsync                   = Signal(),
            o_hVsync                   = Signal(),
            o_hWrBurstQ                = h_wr_burst_q,
            o_hWrBurstQ2               = h_wr_burst_q2,
            # Game Boy LCD.
            i_gb_lcd_clkena            = gb_lcd_clkena,
            i_gb_lcd_mode              = gb_lcd_mode,
            i_gb_lcd_on                = gb_lcd_on,
            i_gb_lcd_vsync             = gb_lcd_vsync,
            i_gb_lcd_data              = gb_lcd_data,
        )

        # Audio System (I2S + TLV320 Codec) --------------------------------------------------------

        # I2C control signals (shared between the LiteX TLV320 init and codec/PMIC polling controllers).
        i2c_enable           = Signal()
        i2c_read_write       = Signal()
        i2c_mosi_data        = Signal(8)
        i2c_register_address = Signal(8)
        i2c_device_address   = Signal(7)
        i2c_miso_data        = Signal(8)
        i2c_busy             = Signal()

        # I2S Serialization.
        # ------------------

        # Ported from aud_system_top.v; generates I2S bitstream for TLV320 codec.
        gclk_half   = Signal()
        stereo_sr   = Signal(32)
        i2s_count   = Signal(5)
        aud_wclk    = Signal()
        hp_gpio     = Signal(8)
        mute        = Signal()
        left_m      = Signal(16)
        right_m     = Signal(16)
        g_mono_spk  = Signal(17)

        self.comb += [
            h_headphones.eq(hp_gpio[1]),
            mute.eq(system_control[0] | (volume > 0x76)),
            left_m.eq(Mux(mute, 0, -left)),
            right_m.eq(Mux(mute, 0, -right)),
        ]

        self.sync.gclk += [
            gclk_half.eq(~gclk_half),
            If(crg.pll.locked,
                g_mono_spk.eq(left_m + right_m),
            ),
        ]

        # I2S shift register (runs on gClkHalf -- we use gClk with enable on gclk_half edges).
        gclk_half_d  = Signal()
        gclk_half_re = Signal()
        self.sync.gclk += gclk_half_d.eq(gclk_half)
        self.comb += gclk_half_re.eq(gclk_half & ~gclk_half_d)  # Rising edge of gclk_half.

        self.sync.gclk += [
            If(~crg.pll.locked,
                i2s_count.eq(0),
            ).Elif(gclk_half_re,
                If(i2s_count == 0,
                    i2s_count.eq(31),
                    aud_wclk.eq(1),
                    If(~hp_gpio[1],
                        stereo_sr.eq(Cat(g_mono_spk[1:17], Constant(0, 16))),
                    ).Else(
                        stereo_sr.eq(Cat(left_m, right_m)),
                    ),
                ).Else(
                    i2s_count.eq(i2s_count - 1),
                    If(i2s_count == 16,
                        aud_wclk.eq(0),
                    ),
                    stereo_sr.eq(Cat(Constant(0, 1), stereo_sr[:31])),
                ),
            ),
        ]

        # Audio codec output signals.
        self.comb += [
            audio.mclk.eq(ClockSignal("gclk")),
            audio.bclk.eq(~gclk_half),
            audio.din.eq(stereo_sr[31]),
            audio.reset.eq(crg.pll.locked),
            audio.wclk.eq(aud_wclk),
        ]

        # TLV320 Codec Init.
        # -------------------

        # Sequences I2C register writes from the TLV320 register image at power-up.
        tlv320_init_done     = Signal()
        tlv320_i2c_enable    = Signal()
        tlv320_i2c_rw        = Signal()
        tlv320_i2c_mosi      = Signal(8)
        tlv320_i2c_reg_addr  = Signal(8)
        tlv320_i2c_dev_addr  = Signal(7)

        self.submodules.tlv320_init = tlv320_init = TLV320Init(load_tlv320_registers())
        self.comb += [
            tlv320_init.reset.eq(~crg.pll.locked),
            tlv320_init.i2c_busy.eq(i2c_busy),
            tlv320_init_done.eq(tlv320_init.done),
            tlv320_i2c_enable.eq(tlv320_init.i2c_enable),
            tlv320_i2c_rw.eq(tlv320_init.i2c_read_write),
            tlv320_i2c_mosi.eq(tlv320_init.i2c_mosi_data),
            tlv320_i2c_reg_addr.eq(tlv320_init.i2c_register_address),
            tlv320_i2c_dev_addr.eq(tlv320_init.i2c_device_address),
        ]

        # I2C Polling Master.
        # --------------------

        # Periodic codec + PMIC polling over the shared LiteI2C PHY after init completes.
        pol_i2c_enable    = Signal()
        pol_i2c_rw        = Signal()
        pol_i2c_mosi      = Signal(8)
        pol_i2c_reg_addr  = Signal(8)
        pol_i2c_dev_addr  = Signal(7)

        self.submodules.polling_master = polling_master = PollingMaster()
        self.comb += [
            polling_master.reset.eq(~crg.pll.locked),
            polling_master.enable.eq(tlv320_init_done),
            polling_master.mute.eq(system_control[0]),
            polling_master.i2c_busy.eq(i2c_busy),
            polling_master.i2c_miso_data.eq(i2c_miso_data),
            pol_i2c_enable.eq(polling_master.i2c_enable),
            pol_i2c_rw.eq(polling_master.i2c_read_write),
            pol_i2c_mosi.eq(polling_master.i2c_mosi_data),
            pol_i2c_reg_addr.eq(polling_master.i2c_register_address),
            pol_i2c_dev_addr.eq(polling_master.i2c_device_address),
            volume.eq(polling_master.volume),
            hp_gpio.eq(polling_master.gpio),
            pmic_sys_status.eq(polling_master.pmic_sys_status),
        ]

        # I2C Mux.
        # --------

        # Use the TLV320 init controller until done, then switch to the polling controller.
        self.comb += [
            If(tlv320_init_done,
                i2c_enable.eq(pol_i2c_enable),
                i2c_read_write.eq(pol_i2c_rw),
                i2c_mosi_data.eq(pol_i2c_mosi),
                i2c_register_address.eq(pol_i2c_reg_addr),
                i2c_device_address.eq(pol_i2c_dev_addr),
            ).Else(
                i2c_enable.eq(tlv320_i2c_enable),
                i2c_read_write.eq(tlv320_i2c_rw),
                i2c_mosi_data.eq(tlv320_i2c_mosi),
                i2c_register_address.eq(tlv320_i2c_reg_addr),
                i2c_device_address.eq(tlv320_i2c_dev_addr),
            ),
        ]

        # LiteI2C PHY + Bridge ---------------------------------------------------------------------

        # Replacing i2c_master.sv; pass platform I2C pads directly (LiteI2C uses SDRTristate).
        # Rename "sys" domain to "hclk" (LiteI2C CSRStorage uses "sys" internally).
        self.submodules.i2c_phy = i2c_phy = ClockDomainsRenamer({"sys": "hclk"})(
            LiteI2CPHYCore(
                pads         = i2c,
                clock_domain = "hclk",
                sys_clk_freq = int(33.55432e6 / 2),
            )
        )
        self.comb += i2c_phy.active.eq(1)

        # Bridge: aud_system_top's enable/busy interface -> LiteI2C stream protocol.
        i2c_enable_d  = Signal()
        i2c_enable_re = Signal()
        self.sync.hclk += i2c_enable_d.eq(i2c_enable)
        self.comb += i2c_enable_re.eq(i2c_enable & ~i2c_enable_d)

        # I2C Bridge FSM.
        self.submodules.i2c_bridge = i2c_bridge = ClockDomainsRenamer("hclk")(FSM(reset_state="IDLE"))
        i2c_bridge.act("IDLE",
            i2c_busy.eq(0),
            If(i2c_enable_re,
                NextState("SEND"),
            ),
        )
        i2c_bridge.act("SEND",
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
        i2c_bridge.act("WAIT",
            i2c_busy.eq(1),
            i2c_phy.source.ready.eq(1),
            If(i2c_phy.source.valid,
                NextValue(i2c_miso_data, i2c_phy.source.data[:8]),
                NextState("IDLE"),
            ),
        )

        # Memory System ----------------------------------------------------------------------------

        self.specials += Instance("mem_system_top",
            p_ISSIMU        = 0,
            i_xClk          = ClockSignal("xclk"),
            i_fClk          = ClockSignal("fclk"),
            i_hClk          = ClockSignal("hclk"),
            i_reset         = memrst,
            # QSPI.
            i_QSPI_CLK     = qspi.clk,
            i_QSPI_MOSI    = qspi.mosi,
            i_QSPI_MISO    = qspi.miso,
            i_QSPI_CS      = qspi.cs_n,
            i_QSPI_WP      = qspi.wp_n,
            i_QSPI_HD      = qspi.hd,
            # PSRAM.
            o_PS_CE_N      = ps.ce_n,
            o_PS_CLK       = ps.clk,
            io_PS_DQ       = ps.dq,
            io_PS_DQS      = ps.dqs,
            # BIST / Frame Buffer.
            o_BIST_failed  = Signal(),
            o_BIST_finished = Signal(),
            o_qMenuInit    = q_menu_init,
            i_hGBNewLine   = h_gb_newline,
            i_hGBAddress   = h_gb_address,
            i_hGBWrite     = h_gb_write,
            i_hGBData      = h_gb_data,
            # Burst Read/Write.
            i_hValid       = gb_lcd_clkena,
            i_hHsync       = gb_lcd_mode[1],
            i_hVsync       = gb_lcd_vsync,
            o_hWrBurstQ    = h_wr_burst_q,
            o_hWrBurstQ2   = h_wr_burst_q2,
        )

        # Emulation System -------------------------------------------------------------------------

        self.specials += Instance("emu_system_top",
            i_hclk              = ClockSignal("hclk"),
            i_pclk              = ClockSignal("pclk"),
            i_reset_n           = ~memrst,
            i_POWER_GOOD        = ~power.on_fpga,
            # Palette.
            i_customPaletteEna  = palette_bg_in[63],
            i_paletteOff        = system_control[12],
            i_paletteBGIn       = palette_bg_in,
            i_paletteOBJ0In     = palette_obj0_in,
            i_paletteOBJ1In     = palette_obj1_in,
            o_gbc_mode          = gbc_mode,
            o_gpd               = gpd,
            # Buttons.
            i_BTN_NODIAGONAL    = system_control[11],
            i_BTN_A             = btn_a_f     | mcu_buttons[3],
            i_BTN_B             = btn_b_f     | mcu_buttons[2],
            i_BTN_DPAD_DOWN     = btn_down_f  | mcu_buttons[7],
            i_BTN_DPAD_LEFT     = btn_left_f  | mcu_buttons[6],
            i_BTN_DPAD_RIGHT    = btn_right_f | mcu_buttons[5],
            i_BTN_DPAD_UP       = btn_up_f    | mcu_buttons[4],
            i_BTN_MENU          = ~btn_menu_ored,
            i_BTN_SEL           = btn_sel_f   | mcu_buttons[1],
            i_BTN_START         = btn_start_f | mcu_buttons[0],
            i_MENU_CLOSED       = menu_disabled & ~slide_out_active,
            # Cartridge.
            o_CART_A            = cart.a,
            o_CART_CLK          = cart.clk,
            o_CART_CS           = cart.cs,
            io_CART_D           = cart.d,
            o_CART_RD           = cart.rd,
            io_CART_RST         = cart.rst,
            o_CART_WR           = cart.wr,
            o_CART_DATA_DIR_E   = cart.data_dir_e,
            # IR.
            i_IR_RX             = ir.rx,
            o_IR_LED            = ir.led,
            # Link Cable.
            io_LINK_CLK         = link.clk,
            i_LINK_IN           = getattr(link, "in"),
            o_LINK_OUT          = link.out,
            # LCD Status.
            o_lcd_on_int        = lcd_on_int,
            o_lcd_off_overwrite = lcd_off_overwrite,
            o_boot_rom_enabled  = boot_rom_enabled,
            # Audio.
            o_left              = left,
            o_right             = right,
            # Game Boy LCD.
            i_LCD_INIT_DONE     = lcd_init_done,
            o_gb_lcd_clkena     = gb_lcd_clkena,
            o_gb_lcd_mode       = gb_lcd_mode,
            o_gb_lcd_on         = gb_lcd_on,
            o_gb_lcd_vsync      = gb_lcd_vsync,
            o_gb_lcd_data       = gb_lcd_data,
        )

        # USB UVC+UART System ----------------------------------------------------------------------

        debugs = Signal(8)
        self.specials += Instance("usbuvcuart_top",
            i_CLK_24MHz       = clk_24,
            i_ERST            = usb_rst,
            o_pClk            = phy_clkout,
            o_usblocked       = usb_locked,
            i_hClk            = ClockSignal("gclk"),
            # UART.
            o_UART_TXD        = uart_rxd,
            i_UART_RXD        = uart_txd,
            o_E_UART_DTR      = uart_dtr,
            o_E_UART_RTS      = uart_rts,
            # Audio.
            i_left            = left,
            i_right           = right,
            # UVC Video.
            i_hLineValid      = hr1,
            i_hEnable         = he1,
            i_hFrameValid     = vr1,
            i_hData           = d1,
            o_debugs          = debugs,
            i_playerNum       = Cat(system_control[4:8], Constant(0, 4)),
            # USB PHY.
            io_usb_dxp_io     = usb.dxp,
            io_usb_dxn_io     = usb.dxn,
            i_usb_rxdp_i      = usb.rxdp,
            i_usb_rxdn_i      = usb.rxdn,
            o_usb_pullup_en_o = usb.pullup,
            io_usb_term_dp_io = usb.term_dp,
            io_usb_term_dn_io = usb.term_dn,
        )

        # Battery ADC ------------------------------------------------------------------------------

        self.specials += Instance("adc_wrap",
            i_clk          = ClockSignal("gclk"),
            i_reset_n      = crg.pll.locked,
            o_hAdcReq_ext  = h_adc_req_ext,
            o_hAdcValue_r1 = h_adc_value_r1,
            o_hAdcReady_r1 = h_adc_ready_r1,
            i_VBAT_ADC_P   = vbat_adc.p,
            i_VBAT_ADC_N   = vbat_adc.n,
        )

        # System Monitor ---------------------------------------------------------------------------

        sm_num_channels                  = 10
        sm_rx_address                    = Signal(7)
        sm_rx_data                       = Signal(80)
        sm_rx_data_val                   = Signal()
        sm_tx_channel                    = Signal(max=sm_num_channels)
        sm_tx_bytepos                    = Signal(8)
        sm_write_done                    = Signal()
        sm_request_buttons               = Signal()
        sm_request_version               = Signal()
        sm_update_brightness             = Signal()
        sm_request_system_status_ext     = Signal()
        sm_request_gpd                   = Signal()
        sm_volt                          = Signal(14)
        sm_bat_is_li                     = Signal()
        sm_transmit_volt                 = Signal()
        sm_brightness                    = Signal(4)
        sm_lowpower_backlight            = Signal()
        sm_channels_new_data             = Signal(sm_num_channels)
        sm_tx_byte_count                 = Signal(8)
        sm_tx_senddata                   = Signal(8)

        self.submodules.system_monitor_payloads = system_monitor_payloads = SystemMonitorPayloads(num_channels=sm_num_channels)
        self.submodules.system_monitor_bridge   = system_monitor_bridge   = SystemMonitorBridge(num_channels=sm_num_channels)
        self.comb += [
            system_monitor_payloads.menu_disabled.eq(menu_disabled),
            system_monitor_payloads.btn_a.eq(btn_a_f),
            system_monitor_payloads.btn_b.eq(btn_b_f),
            system_monitor_payloads.btn_down.eq(btn_down_f),
            system_monitor_payloads.btn_left.eq(btn_left_f),
            system_monitor_payloads.btn_right.eq(btn_right_f),
            system_monitor_payloads.btn_up.eq(btn_up_f),
            system_monitor_payloads.btn_menu.eq(menu_gated),
            system_monitor_payloads.btn_sel.eq(btn_sel_f),
            system_monitor_payloads.btn_start.eq(btn_start_f),
            system_monitor_payloads.request_buttons.eq(sm_request_buttons),
            system_monitor_payloads.request_version.eq(sm_request_version),
            system_monitor_payloads.update_brightness.eq(sm_update_brightness),
            system_monitor_payloads.request_system_status_extended.eq(sm_request_system_status_ext),
            system_monitor_payloads.request_gpd.eq(sm_request_gpd),
            system_monitor_payloads.volt.eq(sm_volt),
            system_monitor_payloads.bat_is_li.eq(sm_bat_is_li),
            system_monitor_payloads.transmit_volt.eq(sm_transmit_volt),
            system_monitor_payloads.brightness.eq(sm_brightness),
            system_monitor_payloads.h_headphones.eq(h_headphones),
            system_monitor_payloads.h_volume.eq(volume[:7]),
            system_monitor_payloads.pmic_sys_status.eq(pmic_sys_status),
            system_monitor_payloads.system_control.eq(system_control),
            system_monitor_payloads.lowpower_backlight.eq(sm_lowpower_backlight),
            system_monitor_payloads.gbc_mode.eq(gbc_mode),
            system_monitor_payloads.gpd.eq(gpd),
            system_monitor_payloads.tx_channel.eq(sm_tx_channel),
            system_monitor_payloads.tx_bytepos.eq(sm_tx_bytepos),
            sm_channels_new_data.eq(system_monitor_payloads.channels_new_data_valid),
            sm_tx_byte_count.eq(system_monitor_payloads.tx_byte_count),
            sm_tx_senddata.eq(system_monitor_payloads.tx_senddata),

            system_monitor_bridge.reset.eq(~crg.pll.locked),
            system_monitor_bridge.menu_disabled.eq(menu_disabled),
            system_monitor_bridge.channels_new_data_valid.eq(sm_channels_new_data),
            system_monitor_bridge.tx_byte_count.eq(sm_tx_byte_count),
            system_monitor_bridge.tx_senddata.eq(sm_tx_senddata),
            system_monitor_bridge.uart_tx_busy.eq(uart_tx_busy),
            system_monitor_bridge.uart_rx_data.eq(uart_rx_data[:8]),
            system_monitor_bridge.uart_rx_val.eq(uart_rx_val),
            uart_tx_data.eq(system_monitor_bridge.uart_tx_data),
            uart_tx_val.eq(system_monitor_bridge.uart_tx_val),
            sm_rx_address.eq(system_monitor_bridge.rx_address),
            sm_rx_data.eq(system_monitor_bridge.rx_data),
            sm_rx_data_val.eq(system_monitor_bridge.rx_data_val),
            sm_tx_channel.eq(system_monitor_bridge.tx_channel),
            sm_tx_bytepos.eq(system_monitor_bridge.tx_bytepos),
            sm_write_done.eq(system_monitor_bridge.write_done),
        ]

        self.specials += Instance("system_monitor",
            p_NUM_CH               = sm_num_channels,
            i_clk                  = ClockSignal("gclk"),
            i_reset                = ~crg.pll.locked,
            # Buttons.
            i_BTN_A                = btn_a_f,
            i_BTN_B                = btn_b_f,
            i_BTN_DPAD_DOWN        = btn_down_f,
            i_BTN_DPAD_LEFT        = btn_left_f,
            i_BTN_DPAD_RIGHT       = btn_right_f,
            i_BTN_DPAD_UP          = btn_up_f,
            i_BTN_MENU             = menu_gated,
            i_BTN_SEL              = btn_sel_f,
            i_BTN_START            = btn_start_f,
            # LCD.
            o_menuDisabled         = menu_disabled,
            o_LCD_BACKLIGHT_INIT   = lcd_backlight_init,
            i_LCD_INIT_DONE        = lcd_init_done & ~boot_rom_enabled,
            o_LCD_PWM              = lcd.pwm,
            # ADC.
            o_hAdcReq_ext          = h_adc_req_ext,
            i_hAdcValue_r1         = h_adc_value_r1,
            i_hAdcReady_r1         = h_adc_ready_r1,
            o_ADC_SEL              = audio.adc_sel,
            # System Status.
            i_hButtons             = 0,
            o_MCU_buttons          = mcu_buttons,
            i_hVolume              = volume[:7],
            i_pmic_sys_status      = pmic_sys_status,
            i_hHeadphones          = h_headphones,
            i_gSecondEna           = second_ena,
            i_gHalfSecondEna       = half_second_ena,
            o_debug_system         = debug_system,
            o_low_battery          = low_battery,
            # LED Control.
            o_LED_Green            = led_green,
            o_LED_Red              = led_red,
            o_LED_Yellow           = led_yellow,
            o_LED_White            = led_white,
            # System Control / Palette.
            o_system_control       = system_control,
            o_paletteBGIn          = palette_bg_in,
            o_paletteOBJ0In        = palette_obj0_in,
            o_paletteOBJ1In        = palette_obj1_in,
            i_gbc_mode                         = gbc_mode,
            i_gpd                              = gpd,
            # Decoded monitor transport.
            i_rx_address                       = sm_rx_address,
            i_rx_data                          = sm_rx_data,
            i_rx_data_val                      = sm_rx_data_val,
            i_tx_channel                       = sm_tx_channel,
            i_write_done                       = sm_write_done,
            o_o_request_buttons                = sm_request_buttons,
            o_o_request_version                = sm_request_version,
            o_o_updateBrightness               = sm_update_brightness,
            o_o_request_SystemStatusExtended   = sm_request_system_status_ext,
            o_o_request_gpd                    = sm_request_gpd,
            o_o_volt                           = sm_volt,
            o_o_bat_is_LI                      = sm_bat_is_li,
            o_o_transmitVolt                   = sm_transmit_volt,
            o_o_brightness                     = sm_brightness,
            o_o_lowpowerBacklight              = sm_lowpower_backlight,
        )

        # UART (FPGA <-> ESP32 MCU) ----------------------------------------------------------------

        # LiteX RS232PHY replacing UART2.
        serial_pads     = Record([("tx", 1), ("rx", 1)])
        uart_tx_active  = Signal()
        uart_tx_data_l  = Signal(8)
        self.comb += [
            serial.tx.eq(serial_pads.tx),
            serial_pads.rx.eq(serial.rx),
        ]
        self.submodules.uart_phy = ClockDomainsRenamer("gclk")(
            RS232PHY(serial_pads, clk_freq=int(33.55432e6 / 4), baudrate=115200)
        )
        self.sync.gclk += [
            If(~crg.pll.locked,
                uart_tx_active.eq(0),
            ).Elif(~uart_tx_active & uart_tx_val,
                uart_tx_active.eq(1),
                uart_tx_data_l.eq(uart_tx_data),
            ).Elif(uart_tx_active & self.uart_phy.sink.ready,
                uart_tx_active.eq(0),
            )
        ]
        # TX: system_monitor -> UART PHY. Keep BUSY asserted for the full byte time.
        self.comb += [
            self.uart_phy.sink.valid.eq(~uart_tx_active & uart_tx_val),
            self.uart_phy.sink.data.eq(Mux(uart_tx_active, uart_tx_data_l, uart_tx_data)),
            uart_tx_busy.eq(uart_tx_active),
        ]
        # RX: UART PHY -> system_monitor.
        self.comb += [
            uart_rx_val.eq(self.uart_phy.source.valid),
            uart_rx_data.eq(self.uart_phy.source.data),
            self.uart_phy.source.ready.eq(1),
        ]

        # Link Port --------------------------------------------------------------------------------

        self.comb += link.sd.eq(0)  # Directly driven by emu_system_top via CART signals.
        # Note: LINK_SD is driven by emu_system_top in the original, but it's in the top port list.
        # The emu_system_top instance doesn't have a LINK_SD port, so it defaults here.

# Build --------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="ChromatiX: LiteX based FPGA design for the ModRetro Chromatic.")
    parser.add_argument("--build",      action="store_true", help="Build bitstream.")
    parser.add_argument("--no-compile", action="store_true", help="Generate build files without running the toolchain.")
    parser.add_argument("--load",       action="store_true", help="Load bitstream (to SRAM, USB will not enumerate).")
    parser.add_argument("--flash",      action="store_true", help="Flash bitstream (to SPI Flash) and reboot.")
    parser.add_argument("--toolchain",  default="gowin",     help="FPGA toolchain (gowin).")
    args = parser.parse_args()

    # Platform.
    platform = Platform(toolchain=args.toolchain)
    add_verilog_sources(platform)
    add_timing_constraints(platform)

    # Design.
    soc = BaseSoC(platform)

    # Build.
    build_dir = "build"
    if args.build:
        platform.build(soc,
            build_dir  = build_dir,
            build_name = "chromatic",
            run        = not args.no_compile,
        )

    # Load / Flash.
    bitstream = f"{build_dir}/chromatic.fs"
    if args.load:
        prog = platform.create_programmer()
        prog.load_bitstream(bitstream)
    if args.flash:
        prog = platform.create_programmer()
        prog.flash(0, bitstream, reset=True)

if __name__ == "__main__":
    main()

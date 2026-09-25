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

import os
import argparse
from types import MethodType

from migen import *

from litex.gen import *
from litex.gen.genlib.cdc import BusSynchronizer

from chromatix import Platform

from chromatix.gateware.crg     import CRG
from chromatix.gateware.sources import add_verilog_sources
from chromatix.gateware.misc    import TickGenerator, StatusLed, ESP32Control
from chromatix.gateware.buttons import Buttons
from chromatix.gateware.memory  import MemorySystem
from chromatix.gateware.lcd     import ST7785Init, load_st7785_sequence
from chromatix.gateware.codec   import CodecControl, CodecI2S, load_tlv320_registers
from chromatix.gateware.sysmon  import SystemMonitorUART, SystemMonitorBridge, SystemMonitorPayloads

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

class BaseSoC(LiteXModule):
    """
    ChromatiX Top-Level.

    Integrates all subsystems of the ModRetro Chromatic handheld: clock generation, video/LCD
    pipeline, audio I2S with TLV320 codec, Game Boy emulation core, memory controller, USB
    UVC+UART, ESP32 MCU communication, battery ADC, button debouncing, and system monitoring.
    """
    def __init__(self, platform):
        gclk_freq = int(33.55432e6 / 4)
        hclk_freq = int(33.55432e6 / 2)

        # CRG --------------------------------------------------------------------------------------

        self.crg = crg = CRG(platform)

        # PHY_CLKOUT clock domain (generated by the USB subsystem).
        self.cd_phy = ClockDomain("phy", reset_less=True)
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
        sdio_ls   = platform.request("sdio_ls")

        # SDIO level shifter enable.
        self.comb += sdio_ls.eq(1)

        # Inter-Module Signals ---------------------------------------------------------------------

        # Video.
        h_wr_burst_q      = Signal(16)
        h_wr_burst_q2     = Signal(16)
        gb_lcd_clkena     = Signal()
        gb_lcd_data       = Signal(15)
        gb_lcd_mode       = Signal(2)
        gb_lcd_on         = Signal()
        gb_lcd_vsync      = Signal()
        h_gb_newline      = Signal()
        h_gb_address      = Signal(23)
        h_gb_write        = Signal()
        h_gb_data         = Signal(16)
        h_draw_osd        = Signal()
        slide_out_active  = Signal()
        lcd_init_done     = Signal()
        lcd_enable_uvc    = Signal()
        lcd_db_uvc        = Signal(18)

        # Audio.
        left              = Signal(16)
        right             = Signal(16)

        # System.
        debug_system      = Signal(32)
        system_control    = Signal(16)
        low_battery       = Signal()
        boot_rom_enabled  = Signal()
        lcd_on_int        = Signal()
        lcd_off_overwrite = Signal()
        menu_disabled     = Signal()
        mcu_buttons       = Signal(9)
        q_menu_init       = Signal()

        # Palette.
        palette_bg_in     = Signal(64)
        palette_obj0_in   = Signal(64)
        palette_obj1_in   = Signal(64)
        gbc_mode          = Signal()
        gpd               = Signal(64)

        # Timers (gClk domain) ---------------------------------------------------------------------

        self.ticks = ticks = ClockDomainsRenamer("gclk")(TickGenerator())

        # Cart Detect Debounce & Memory Reset (xClk domain) ----------------------------------------

        cart_det_sr = Signal(18)
        memrst      = Signal(reset=1)

        # 18-bit shift register sampling cart detect pin.
        self.sync.xclk += cart_det_sr.eq(Cat(cart.det, cart_det_sr[:17]))

        # memrst: set when PLL unlocked, or on cart detect transitions.
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

        # Status LED (xClk domain) -----------------------------------------------------------------

        self.status_led = status_led = ClockDomainsRenamer("xclk")(StatusLed(rgb_led))
        self.comb += [
            status_led.reset.eq(~crg.pll.locked),
            status_led.blink.eq(ticks.counter[4]),
        ]

        # LCD Enable Sync (gClk domain) ------------------------------------------------------------

        lcd_vsync_r1       = Signal()
        lcd_en0            = Signal()
        lcd_en1            = Signal()
        lcd_en             = Signal()
        lcd_backlight_init = Signal()

        # Three-stage vsync-synchronized LCD enable with init gating.
        self.sync.gclk += [
            lcd_vsync_r1.eq(lcd.vsync),
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

        hr1 = Signal()
        vr1 = Signal()
        he1 = Signal()
        d1  = Signal(18)

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

        # ESP32 Control (PHY_CLKOUT & gClk domains) ------------------------------------------------

        self.esp32_ctrl = esp32_ctrl = ClockDomainsRenamer("gclk")(ESP32Control(esp32, esp_uart))

        # Buttons (gClk domain) --------------------------------------------------------------------

        self.buttons = btns = ClockDomainsRenamer("gclk")(Buttons(buttons))

        # Menu button: OR physical menu button with MCU menu bit, gate until cart is stable and
        # menu init is complete.
        btn_menu_ored = Signal()
        menu_gated    = Signal()
        self.comb += [
            btn_menu_ored.eq(buttons.menu & ~mcu_buttons[8]),
            If(q_menu_init & (cart_det_sr[3:7] == 0xF),
                menu_gated.eq(btn_menu_ored),
            ).Else(
                menu_gated.eq(1),
            ),
        ]

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

        # I2S_BCLK = menuDisabled (ESP32 menu state).
        self.comb += i2s.bclk.eq(menu_disabled)

        # LCD SPI Init (pClk domain) ---------------------------------------------------------------

        self.st7785_init = st7785_init = ClockDomainsRenamer("pclk")(ST7785Init(load_st7785_sequence()))
        self.comb += [
            st7785_init.reset.eq(memrst),
            lcd.spi_csx.eq(st7785_init.lcd_cs),
            lcd.spi_sclk.eq(st7785_init.lcd_sck),
            lcd.spi_sda.eq(st7785_init.lcd_sda_sdi),
            lcd.reset.eq(st7785_init.lcd_rst),
            lcd_init_done.eq(st7785_init.lcd_init_done),
        ]

        # Video System -----------------------------------------------------------------------------

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
            i_gSecondEna               = ticks.second,
            i_gPercentEna              = ticks.percent,
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

        # Audio: TLV320 Codec Control (hClk domain) + I2S (gClk domain) ----------------------------

        self.codec_ctrl = codec_ctrl = ClockDomainsRenamer("hclk")(CodecControl(
            pads         = i2c,
            sys_clk_freq = hclk_freq,
            registers    = load_tlv320_registers(),
        ))
        self.comb += [
            codec_ctrl.reset.eq(~crg.pll.locked),
            codec_ctrl.mute.eq(system_control[0]),
        ]
        h_headphones = codec_ctrl.gpio[1]

        # Audio samples: Emulation (hClk) -> I2S/USB (gClk).
        self.audio_cdc = audio_cdc = BusSynchronizer(32, "hclk", "gclk")
        g_left  = Signal(16)
        g_right = Signal(16)
        self.comb += [
            audio_cdc.i.eq(Cat(left, right)),
            Cat(g_left, g_right).eq(audio_cdc.o),
        ]

        self.codec_i2s = codec_i2s = ClockDomainsRenamer("gclk")(CodecI2S(audio))
        self.comb += [
            codec_i2s.enable.eq(crg.pll.locked),
            codec_i2s.left.eq(g_left),
            codec_i2s.right.eq(g_right),
            codec_i2s.mute.eq(system_control[0] | (codec_ctrl.volume > 0x76)),
            codec_i2s.headphones.eq(h_headphones),
        ]

        # Memory System ----------------------------------------------------------------------------

        self.memory = memory = MemorySystem(qspi_pads=qspi, psram_pads=ps)
        self.comb += [
            memory.reset.eq(memrst),
            q_menu_init.eq(memory.menu_init),
            # Game Boy framebuffer write.
            memory.gb_new_line.eq(h_gb_newline),
            memory.gb_address.eq(h_gb_address),
            memory.gb_write.eq(h_gb_write),
            memory.gb_data.eq(h_gb_data),
            # Framebuffer/OSD line reads.
            memory.h_valid.eq(gb_lcd_clkena),
            memory.h_hsync.eq(gb_lcd_mode[1]),
            memory.h_vsync.eq(gb_lcd_vsync),
            h_wr_burst_q.eq(memory.fb_data),
            h_wr_burst_q2.eq(memory.osd_data),
        ]

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
            i_BTN_A             = btns.a          | mcu_buttons[3],
            i_BTN_B             = btns.b          | mcu_buttons[2],
            i_BTN_DPAD_DOWN     = btns.dpad_down  | mcu_buttons[7],
            i_BTN_DPAD_LEFT     = btns.dpad_left  | mcu_buttons[6],
            i_BTN_DPAD_RIGHT    = btns.dpad_right | mcu_buttons[5],
            i_BTN_DPAD_UP       = btns.dpad_up    | mcu_buttons[4],
            i_BTN_MENU          = ~btn_menu_ored,
            i_BTN_SEL           = btns.sel        | mcu_buttons[1],
            i_BTN_START         = btns.start      | mcu_buttons[0],
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

        # Link Port: LINK_SD not driven by emu_system_top.
        self.comb += link.sd.eq(0)

        # USB UVC+UAC+UART System ------------------------------------------------------------------

        self.specials += Instance("usbuvcuart_top",
            i_CLK_24MHz       = clk_24,
            i_ERST            = usb_rst,
            o_pClk            = phy_clkout,
            o_usblocked       = esp32_ctrl.usb_locked,
            i_hClk            = ClockSignal("gclk"),
            # UART.
            o_UART_TXD        = esp32_ctrl.usb_rxd,
            i_UART_RXD        = esp32_ctrl.usb_txd,
            o_E_UART_DTR      = esp32_ctrl.usb_dtr,
            o_E_UART_RTS      = esp32_ctrl.usb_rts,
            # Audio.
            i_left            = g_left,
            i_right           = g_right,
            # UVC Video.
            i_hLineValid      = hr1,
            i_hEnable         = he1,
            i_hFrameValid     = vr1,
            i_hData           = d1,
            o_debugs          = Signal(8),
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

        h_adc_value_r1 = Signal(14)
        h_adc_req_ext  = Signal()
        h_adc_ready_r1 = Signal()
        self.specials += Instance("adc_wrap",
            i_clk          = ClockSignal("gclk"),
            i_reset_n      = crg.pll.locked,
            o_hAdcReq_ext  = h_adc_req_ext,
            o_hAdcValue_r1 = h_adc_value_r1,
            o_hAdcReady_r1 = h_adc_ready_r1,
            i_VBAT_ADC_P   = vbat_adc.p,
            i_VBAT_ADC_N   = vbat_adc.n,
        )

        # System Monitor (gClk domain) -------------------------------------------------------------

        sm_num_channels = 10

        # UART (FPGA <-> ESP32 MCU).
        self.sm_uart = sm_uart = ClockDomainsRenamer("gclk")(SystemMonitorUART(serial, clk_freq=gclk_freq))
        self.comb += sm_uart.enable.eq(crg.pll.locked)

        # Packet transport (RX decode, channel arbiter, TX framing).
        self.sm_bridge = sm_bridge = ClockDomainsRenamer("gclk")(SystemMonitorBridge(num_channels=sm_num_channels))
        self.comb += [
            sm_bridge.reset.eq(~crg.pll.locked),
            sm_bridge.menu_disabled.eq(menu_disabled),
            sm_bridge.uart_tx_busy.eq(sm_uart.tx_busy),
            sm_bridge.uart_rx_data.eq(sm_uart.rx_data),
            sm_bridge.uart_rx_val.eq(sm_uart.rx_val),
            sm_uart.tx_data.eq(sm_bridge.uart_tx_data),
            sm_uart.tx_val.eq(sm_bridge.uart_tx_val),
        ]

        # Payloads.
        self.sm_payloads = sm_payloads = SystemMonitorPayloads(num_channels=sm_num_channels)
        self.comb += [
            sm_payloads.menu_disabled.eq(menu_disabled),
            sm_payloads.btn_a.eq(btns.a),
            sm_payloads.btn_b.eq(btns.b),
            sm_payloads.btn_down.eq(btns.dpad_down),
            sm_payloads.btn_left.eq(btns.dpad_left),
            sm_payloads.btn_right.eq(btns.dpad_right),
            sm_payloads.btn_up.eq(btns.dpad_up),
            sm_payloads.btn_menu.eq(menu_gated),
            sm_payloads.btn_sel.eq(btns.sel),
            sm_payloads.btn_start.eq(btns.start),
            sm_payloads.h_headphones.eq(h_headphones),
            sm_payloads.h_volume.eq(codec_ctrl.volume[:7]),
            sm_payloads.pmic_sys_status.eq(codec_ctrl.pmic_sys_status),
            sm_payloads.system_control.eq(system_control),
            sm_payloads.gbc_mode.eq(gbc_mode),
            sm_payloads.gpd.eq(gpd),
            sm_payloads.tx_channel.eq(sm_bridge.tx_channel),
            sm_payloads.tx_bytepos.eq(sm_bridge.tx_bytepos),
            sm_bridge.channels_new_data_valid.eq(sm_payloads.channels_new_data_valid),
            sm_bridge.tx_byte_count.eq(sm_payloads.tx_byte_count),
            sm_bridge.tx_senddata.eq(sm_payloads.tx_senddata),
        ]

        # Menu / UI / Battery / Palette (legacy Verilog).
        self.specials += Instance("system_monitor",
            p_NUM_CH                         = sm_num_channels,
            i_clk                            = ClockSignal("gclk"),
            i_reset                          = ~crg.pll.locked,
            # Buttons.
            i_BTN_A                          = btns.a,
            i_BTN_B                          = btns.b,
            i_BTN_DPAD_DOWN                  = btns.dpad_down,
            i_BTN_DPAD_LEFT                  = btns.dpad_left,
            i_BTN_DPAD_RIGHT                 = btns.dpad_right,
            i_BTN_DPAD_UP                    = btns.dpad_up,
            i_BTN_MENU                       = menu_gated,
            i_BTN_SEL                        = btns.sel,
            i_BTN_START                      = btns.start,
            # LCD.
            o_menuDisabled                   = menu_disabled,
            o_LCD_BACKLIGHT_INIT             = lcd_backlight_init,
            i_LCD_INIT_DONE                  = lcd_init_done & ~boot_rom_enabled,
            o_LCD_PWM                        = lcd.pwm,
            # ADC.
            o_hAdcReq_ext                    = h_adc_req_ext,
            i_hAdcValue_r1                   = h_adc_value_r1,
            i_hAdcReady_r1                   = h_adc_ready_r1,
            o_ADC_SEL                        = audio.adc_sel,
            # System Status.
            i_hButtons                       = 0,
            o_MCU_buttons                    = mcu_buttons,
            i_hVolume                        = codec_ctrl.volume[:7],
            i_pmic_sys_status                = codec_ctrl.pmic_sys_status,
            i_hHeadphones                    = h_headphones,
            i_gSecondEna                     = ticks.second,
            i_gHalfSecondEna                 = ticks.half_second,
            o_debug_system                   = debug_system,
            o_low_battery                    = low_battery,
            # LED Control.
            o_LED_Green                      = status_led.green,
            o_LED_Red                        = status_led.red,
            o_LED_Yellow                     = status_led.yellow,
            o_LED_White                      = status_led.white,
            # System Control / Palette.
            o_system_control                 = system_control,
            o_paletteBGIn                    = palette_bg_in,
            o_paletteOBJ0In                  = palette_obj0_in,
            o_paletteOBJ1In                  = palette_obj1_in,
            i_gbc_mode                       = gbc_mode,
            i_gpd                            = gpd,
            # Decoded monitor transport.
            i_rx_address                     = sm_bridge.rx_address,
            i_rx_data                        = sm_bridge.rx_data,
            i_rx_data_val                    = sm_bridge.rx_data_val,
            i_tx_channel                     = sm_bridge.tx_channel,
            i_write_done                     = sm_bridge.write_done,
            o_o_request_buttons              = sm_payloads.request_buttons,
            o_o_request_version              = sm_payloads.request_version,
            o_o_updateBrightness             = sm_payloads.update_brightness,
            o_o_request_SystemStatusExtended = sm_payloads.request_system_status_extended,
            o_o_request_gpd                  = sm_payloads.request_gpd,
            o_o_volt                         = sm_payloads.volt,
            o_o_bat_is_LI                    = sm_payloads.bat_is_li,
            o_o_transmitVolt                 = sm_payloads.transmit_volt,
            o_o_brightness                   = sm_payloads.brightness,
            o_o_lowpowerBacklight            = sm_payloads.lowpower_backlight,
        )

# Build --------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="ChromatiX: LiteX based FPGA design for the ModRetro Chromatic.")
    parser.add_argument("--build",      action="store_true", help="Build bitstream.")
    parser.add_argument("--no-compile", action="store_true", help="Generate build files without running the toolchain.")
    parser.add_argument("--load",       action="store_true", help="Load bitstream (to SRAM, USB will not enumerate).")
    parser.add_argument("--flash",      action="store_true", help="Flash bitstream (to SPI Flash) and reboot.")
    parser.add_argument("--toolchain",  default="gowin",     help="FPGA toolchain (gowin).")
    parser.add_argument("--gowin-path", default=os.environ.get("GOWIN_PATH", None), help="Gowin IDE install directory (or GOWIN_PATH env variable, ex: ~/tools/gowin_1.9.12.04/IDE).")
    args = parser.parse_args()

    # Gowin IDE selection (bundled libs/Qt are required for the standalone gw_sh).
    if args.gowin_path is not None:
        gowin_path = os.path.expanduser(args.gowin_path)
        os.environ["PATH"]            = os.path.join(gowin_path, "bin") + os.pathsep + os.environ["PATH"]
        os.environ["LD_LIBRARY_PATH"] = os.path.join(gowin_path, "lib") + os.pathsep + os.environ.get("LD_LIBRARY_PATH", "")
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

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

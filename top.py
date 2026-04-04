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
    """Add all RTL source files (matching the .gprj project file, minus gowin_pll.v)."""
    src = os.path.join(base_path, "src")

    def add(path, language=None):
        full = os.path.join(src, path)
        if not os.path.exists(full):
            print(f"WARNING: Source file not found: {full}")
            return
        platform.add_source(full, language=language)

    # Gowin IP (PLL removed -- now managed by LiteX CRG).
    add("fifo1k/fifo1k.v")
    add("gowin_adc/gowin_adc.v")

    # Top.
    add("top.v")

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

    # BSP - UART.
    add("rtl/BSP/uart/fixed_point_divider/fixed_point_divider.v")
    add("rtl/BSP/uart/uart.v")
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
    add("rtl/BSP/uart/uart_rx.vhd",         language="vhdl")
    add("rtl/BSP/uart/uart_tx.vhd",         language="vhdl")

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

# Top Module (LiteX Wrapper) ----------------------------------------------------------------------

class ChromaticTop(Module):
    def __init__(self, platform):
        # CRG: PLL generating fClk/pClk/hClk/gClk/xClk from clk_fpga.
        self.submodules.crg = crg = CRG(platform)

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

        # SDIO level shifter enable (active high, directly driven to 1).
        # Not in platform definition -- add as extension.
        platform.add_extension([("sdio_ls", 0, Pins("N5"), IOStandard("LVCMOS33"))])
        sdio_ls = platform.request("sdio_ls")

        # Instantiate the original top.v (modified: PLL removed, clocks as inputs).
        self.specials += Instance("top",
            # Clocks from LiteX CRG.
            i_fClk   = ClockSignal("fclk"),
            i_pClk   = ClockSignal("pclk"),
            i_hClk   = ClockSignal("hclk"),
            i_gClk   = ClockSignal("gclk"),
            i_xClk   = ClockSignal("xclk"),
            i_lock_o = crg.pll.locked,

            # Clocks (passed through).
            i_CLK_24MHz = clk_24,

            # Buttons.
            i_BTN_A          = buttons.a,
            i_BTN_B          = buttons.b,
            i_BTN_DPAD_DOWN  = buttons.dpad_down,
            i_BTN_DPAD_LEFT  = buttons.dpad_left,
            i_BTN_DPAD_RIGHT = buttons.dpad_right,
            i_BTN_DPAD_UP    = buttons.dpad_up,
            i_BTN_MENU       = buttons.menu,
            i_BTN_SEL        = buttons.sel,
            i_BTN_START      = buttons.start,

            # Audio codec.
            o_ADC_SEL   = audio.adc_sel,
            o_AUD_BCLK  = audio.bclk,
            o_AUD_DIN   = audio.din,
            o_AUD_MCLK  = audio.mclk,
            o_AUD_RESET = audio.reset,
            o_AUD_WCLK  = audio.wclk,

            # RGB LED.
            o_FPGA_LED_EN = rgb_led.en,
            o_FPGA_LED_R  = rgb_led.r,
            o_FPGA_LED_G  = rgb_led.g,
            o_FPGA_LED_B  = rgb_led.b,

            # Cartridge.
            o_CART_A          = cart.a,
            o_CART_CLK        = cart.clk,
            o_CART_CS         = cart.cs,
            io_CART_D         = cart.d,
            o_CART_RD         = cart.rd,
            io_CART_RST       = cart.rst,
            o_CART_WR         = cart.wr,
            o_CART_DATA_DIR_E = cart.data_dir_e,
            i_CART_DET        = cart.det,
            i_CART_AUDIN      = cart.audin,

            # ESP32 control.
            o_ESP32_EN  = esp32.en,
            o_ESP32_IO0 = esp32.io0,

            # Power / misc.
            o_SDIO_LS       = sdio_ls,
            i_POWER_ON_FPGA = power.on_fpga,
            o_POWER_DOWN_IO = power.down_io,
            i_VBUS_DET      = power.vbus_det,

            # ESP32 I2S.
            o_I2S_BCLK = i2s.bclk,
            i_I2S_WS   = i2s.ws,
            i_I2S_DIN  = i2s.din,
            i_I2S_DOUT = i2s.dout,

            # ESP32 UART (direct MCU connection).
            i_ESP32_MCU_D12 = serial.rx,
            o_ESP32_MCU_D11 = serial.tx,

            # ESP32 UART (via USB bridge).
            i_ESP32_MCU_D3  = esp_uart.tx,
            o_ESP32_MCU_D4  = esp_uart.rx,

            # QSPI.
            i_QSPI_CS   = qspi.cs_n,
            i_QSPI_CLK  = qspi.clk,
            i_QSPI_MOSI = qspi.mosi,
            i_QSPI_MISO = qspi.miso,
            i_QSPI_WP   = qspi.wp_n,
            i_QSPI_HD   = qspi.hd,

            # PSRAM.
            o_PS_CE_N = ps.ce_n,
            o_PS_CLK  = ps.clk,
            io_PS_DQ  = ps.dq,
            io_PS_DQS = ps.dqs,

            # LCD.
            o_LCD_PWM      = lcd.pwm,
            o_LCD_DB       = lcd.db,
            o_LCD_DOTCLK   = lcd.dotclk,
            o_LCD_ENABLE   = lcd.enable,
            o_LCD_HSYNC    = lcd.hsync,
            o_LCD_RESET    = lcd.reset,
            o_LCD_SPI_CSX  = lcd.spi_csx,
            o_LCD_SPI_SCLK = lcd.spi_sclk,
            o_LCD_SPI_SDA  = lcd.spi_sda,
            i_LCD_TE       = lcd.te,
            o_LCD_VSYNC    = lcd.vsync,

            # HDMI.
            o_HDMI_D_P      = hdmi.d_p,
            o_HDMI_D_N      = hdmi.d_n,
            o_HDMI_CLK_P    = hdmi.clk_p,
            o_HDMI_CLK_N    = hdmi.clk_n,
            i_HDMI_SBU1_HPD = hdmi.hpd,
            o_HDMI_SBU2_CEC = hdmi.cec,

            # IR.
            i_IR_RX  = ir.rx,
            o_IR_LED = ir.led,

            # Link cable.
            io_LINK_CLK = link.clk,
            i_LINK_IN   = getattr(link, "in"),
            o_LINK_OUT  = link.out,
            o_LINK_SD   = link.sd,

            # I2C.
            io_SCL = i2c.scl,
            io_SDA = i2c.sda,

            # USB.
            i_USBC_FLIP       = power.usbc_flip,
            io_usb_dxp_io     = usb.dxp,
            io_usb_dxn_io     = usb.dxn,
            i_usb_rxdp_i      = usb.rxdp,
            i_usb_rxdn_i      = usb.rxdn,
            o_usb_pullup_en_o = usb.pullup,
            io_usb_term_dp_io = usb.term_dp,
            io_usb_term_dn_io = usb.term_dn,

            # Battery ADC.
            i_VBAT_ADC_P = vbat_adc.p,
            i_VBAT_ADC_N = vbat_adc.n,
        )

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

    # Add Timing Constraints (for clocks not managed by LiteX).
    sdc_path = os.path.join(base_path, "src", "board", "evt1_x2", "evt1_x2.sdc")
    platform.toolchain.additional_tcl_commands.insert(0, f"add_file {sdc_path}")

    # Create Top Module & Build.
    top = ChromaticTop(platform)
    if args.build:
        platform.build(top,
            build_name = "chromatic",
            run        = not args.no_compile,
        )

if __name__ == "__main__":
    main()

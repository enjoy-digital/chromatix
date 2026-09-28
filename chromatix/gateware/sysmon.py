#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: GPL-3.0-only
# Derived from ModRetro's oss-chromatic-console-fpga (GPL-3.0).

from migen import *

from litex.gen import *

from litex.soc.cores.uart import RS232PHY

# System Monitor RX Packet -------------------------------------------------------------------------

class SystemMonitorRxPacket(LiteXModule):
    """
    ESP32 -> FPGA packet decoder (port of uart_packet_wrapper_rx.sv).

    Packet: 0x8F (SOF), address, byte count, payload, CRC-8 (SAE J1850: poly 0x1D, init 0xFF, over
    SOF..payload). rx_data holds the last 10 payload bytes (last byte in LSBs); rx_data_val pulses
    when the CRC matches.
    """
    def __init__(self):
        self.reset        = Signal()
        self.uart_rx_data = Signal(8)
        self.uart_rx_val  = Signal()
        self.rx_address   = Signal(7)
        self.rx_data      = Signal(80)
        self.rx_data_val  = Signal()

        # # #

        rx_state  = Signal(3, reset=1)
        rx_count  = Signal(8)
        crc       = Signal(8, reset=0xFF)
        bit_count = Signal(4)

        RX_IDLE  = 1
        RX_ADDR  = 2
        RX_COUNT = 3
        RX_DATA  = 4
        RX_CRC   = 5
        RX_ERROR = 6

        # CRC: XOR each received byte, then shift it bit-serially over the next 8 cycles.
        self.sync += [
            If(self.reset,
                crc.eq(0xFF),
                bit_count.eq(0),
            ).Elif((rx_state == RX_IDLE) & ~self.uart_rx_val,
                crc.eq(0xFF),
                bit_count.eq(0),
            ).Elif(self.uart_rx_val,
                crc.eq(crc ^ self.uart_rx_data),
                bit_count.eq(8),
            ).Elif(bit_count > 0,
                bit_count.eq(bit_count - 1),
                If(crc[7],
                    crc.eq(Cat(Constant(0, 1), crc[:7]) ^ Constant(0x1D, 8)),
                ).Else(
                    crc.eq(Cat(Constant(0, 1), crc[:7])),
                )
            )
        ]

        # FSM.
        self.sync += [
            self.rx_data_val.eq(0),
            If(self.reset,
                rx_state.eq(RX_IDLE),
                rx_count.eq(0),
                self.rx_address.eq(0),
                self.rx_data.eq(0),
            ).Else(
                Case(rx_state, {
                    RX_IDLE: [
                        If(self.uart_rx_val & (self.uart_rx_data == 0x8F),
                            rx_state.eq(RX_ADDR),
                        )
                    ],
                    RX_ADDR: [
                        If(self.uart_rx_val,
                            rx_state.eq(RX_COUNT),
                            self.rx_address.eq(self.uart_rx_data[:7]),
                        )
                    ],
                    RX_COUNT: [
                        If(self.uart_rx_val,
                            rx_state.eq(RX_DATA),
                            rx_count.eq(self.uart_rx_data),
                        )
                    ],
                    RX_DATA: [
                        If(self.uart_rx_val,
                            If(rx_count < 2,
                                rx_state.eq(RX_CRC),
                            ).Else(
                                rx_count.eq(rx_count - 1),
                            ),
                            self.rx_data.eq(Cat(self.uart_rx_data, self.rx_data[:72])),
                        )
                    ],
                    RX_CRC: [
                        If(self.uart_rx_val,
                            If(crc == self.uart_rx_data,
                                self.rx_data_val.eq(1),
                                rx_state.eq(RX_IDLE),
                            ).Else(
                                rx_state.eq(RX_ERROR),
                            )
                        )
                    ],
                    RX_ERROR: [
                        rx_state.eq(RX_IDLE),
                    ],
                    # Recover from unused states (as the original).
                    "default": rx_state.eq(RX_IDLE),
                })
            )
        ]

# System Monitor Arbiter ---------------------------------------------------------------------------

class SystemMonitorArbiterBridge(LiteXModule):
    """
    FPGA -> ESP32 channel arbiter (port of system_monitor_arbiter.sv).

    Latches the per-channel refresh requests and round-robins over the pending channels, issuing
    one packet write per channel (Buttons, channel 2, are interleaved every other packet). When the
    menu is closed, uart_disabled pulses after each packet to return the TX framer to idle/sleep.
    """
    def __init__(self, num_channels=10):
        self.reset                   = Signal()
        self.channels_new_data_valid = Signal(num_channels)
        self.menu_disabled           = Signal()
        self.uart_tx_busy            = Signal()
        self.write_done              = Signal()
        self.uart_disabled           = Signal()
        self.tx_channel              = Signal(max=num_channels)
        self.tx_address              = Signal(7)
        self.write                   = Signal()

        # # #

        channels_refresh = Signal(num_channels)
        active_channel   = Signal(max=num_channels)
        next_channel     = Signal(max=num_channels)
        write_active     = Signal()
        arbiter_active   = Signal()
        button_next      = Signal()
        active_valid     = Signal()

        active_cases = {i: [active_valid.eq(channels_refresh[i])] for i in range(num_channels)}

        self.comb += [
            self.tx_channel.eq(active_channel),
            self.uart_disabled.eq(~arbiter_active),
            active_valid.eq(0),
            Case(active_channel, active_cases),
        ]

        for i in range(num_channels):
            self.sync += [
                If(self.reset,
                    channels_refresh[i].eq(0),
                ).Elif(self.channels_new_data_valid[i],
                    channels_refresh[i].eq(1),
                ).Elif((active_channel == i) & self.write_done,
                    channels_refresh[i].eq(0),
                )
            ]

        self.sync += [
            If(self.reset,
                active_channel.eq(0),
                next_channel.eq(0),
                self.tx_address.eq(0),
                self.write.eq(0),
                write_active.eq(0),
                arbiter_active.eq(0),
                button_next.eq(0),
            ).Else(
                self.write.eq(0),
                arbiter_active.eq(1),
                If((active_valid == 0) & arbiter_active,
                    If(active_channel < (num_channels - 1),
                        active_channel.eq(active_channel + 1),
                    ).Else(
                        active_channel.eq(0),
                    ),
                ).Else(
                    If(~write_active & arbiter_active,
                        If(~self.uart_tx_busy,
                            write_active.eq(1),
                            self.write.eq(1),
                            self.tx_address.eq(active_channel),
                        )
                    ).Else(
                        If(self.write_done,
                            If(self.menu_disabled,
                                arbiter_active.eq(0),
                            ),
                            write_active.eq(0),
                            button_next.eq(~button_next),
                            If(button_next,
                                active_channel.eq(2),
                            ).Else(
                                If(next_channel < (num_channels - 1),
                                    next_channel.eq(next_channel + 1),
                                    active_channel.eq(next_channel + 1),
                                ).Else(
                                    next_channel.eq(0),
                                    active_channel.eq(0),
                                )
                            )
                        )
                    )
                )
            )
        ]

# System Monitor TX Packet -------------------------------------------------------------------------

class SystemMonitorTxPacket(LiteXModule):
    """
    FPGA -> ESP32 packet framer (port of uart_packet_wrapper_tx.sv).

    Sends SOF (0x8F), address, byte count, payload (tx_senddata for each tx_bytepos) and CRC-8 (same
    format as SystemMonitorRxPacket), one byte every 16 cycles at most. When the menu is closed
    (ESP32 may be asleep), each packet is preceded by 0x00 bytes to wake the ESP32 UART up.
    """
    def __init__(self):
        self.reset         = Signal()
        self.uart_tx_busy  = Signal()
        self.write         = Signal()
        self.menu_disabled = Signal()
        self.uart_disabled = Signal()
        self.tx_address    = Signal(7)
        self.tx_byte_count = Signal(8)
        self.tx_bytepos    = Signal(8)
        self.tx_senddata   = Signal(8)
        self.uart_tx_data  = Signal(8)
        self.uart_tx_val   = Signal()
        self.write_done    = Signal()

        # # #

        cnt       = Signal(4) # Rate limiter between UART bytes.
        tx_state  = Signal(5, reset=1)
        bytecount = Signal(8)
        crc       = Signal(8, reset=0xFF)
        bit_count = Signal(4)

        TX_IDLE  = 1
        TX_ADDR  = 2
        TX_COUNT = 3
        TX_DATA  = 4
        TX_CRC   = 5
        TX_DONE  = 6
        TX_SLEEP = 7
        TX_AWAKE = 8
        TX_START = 9

        self.comb += self.write_done.eq((tx_state == TX_DONE) & ~self.uart_tx_busy & (cnt == 0))

        self.sync += [
            If(self.reset,
                cnt.eq(0),
            ).Else(
                cnt.eq(cnt + 1),
            )
        ]

        self.sync += [
            If(self.reset,
                self.uart_tx_val.eq(0),
            ).Else(
                self.uart_tx_val.eq(0),
                If(~self.uart_tx_busy & ~self.uart_disabled,
                    If((tx_state == TX_IDLE) | (tx_state == TX_SLEEP),
                        self.uart_tx_val.eq(self.write),
                    ).Else(
                        If((cnt == 0) & (tx_state != TX_DONE),
                            self.uart_tx_val.eq(1),
                        )
                    )
                )
            )
        ]

        self.sync += [
            If(self.reset,
                self.uart_tx_data.eq(0x8F),
            ).Else(
                Case(tx_state, {
                    TX_IDLE:  [self.uart_tx_data.eq(0x8F)],
                    TX_ADDR:  [self.uart_tx_data.eq(Cat(self.tx_address, Constant(0, 1)))],
                    TX_COUNT: [self.uart_tx_data.eq(self.tx_byte_count)],
                    TX_DATA:  [self.uart_tx_data.eq(self.tx_senddata)],
                    TX_CRC:   [self.uart_tx_data.eq(crc)],
                    TX_SLEEP: [self.uart_tx_data.eq(0x00)],
                    TX_AWAKE: [self.uart_tx_data.eq(0x00)],
                    TX_START: [self.uart_tx_data.eq(0x8F)],
                    "default": [self.uart_tx_data.eq(0xCA)],
                })
            )
        ]

        # CRC: XOR each sent byte (SOF..payload, not the wake-up bytes), then shift it bit-serially.
        self.sync += [
            If(self.reset,
                crc.eq(0xFF),
                bit_count.eq(0),
            ).Elif(self.uart_disabled,
                crc.eq(0xFF),
                bit_count.eq(0),
            ).Elif((tx_state == TX_IDLE) & ~self.write,
                crc.eq(0xFF),
                bit_count.eq(0),
            ).Elif(self.uart_tx_val & (tx_state != TX_SLEEP) & (tx_state != TX_AWAKE) & (tx_state != TX_START),
                crc.eq(crc ^ self.uart_tx_data),
                bit_count.eq(8),
            ).Elif(bit_count > 0,
                bit_count.eq(bit_count - 1),
                If(crc[7],
                    crc.eq(Cat(Constant(0, 1), crc[:7]) ^ Constant(0x1D, 8)),
                ).Else(
                    crc.eq(Cat(Constant(0, 1), crc[:7])),
                )
            )
        ]

        self.sync += [
            If(self.reset,
                tx_state.eq(TX_IDLE),
                bytecount.eq(0),
                self.tx_bytepos.eq(0),
            ).Else(
                If(self.uart_disabled,
                    tx_state.eq(TX_IDLE),
                ).Else(
                    Case(tx_state, {
                        TX_IDLE: [
                            If(self.menu_disabled,
                                tx_state.eq(TX_SLEEP),
                            ).Elif(~self.uart_tx_busy & self.write,
                                tx_state.eq(TX_ADDR),
                            )
                        ],
                        TX_ADDR: [
                            If(~self.uart_tx_busy & (cnt == 0),
                                tx_state.eq(TX_COUNT),
                            )
                        ],
                        TX_COUNT: [
                            If(~self.uart_tx_busy & (cnt == 0),
                                bytecount.eq(self.tx_byte_count),
                                self.tx_bytepos.eq(0),
                                tx_state.eq(TX_DATA),
                            )
                        ],
                        TX_DATA: [
                            If(~self.uart_tx_busy & (cnt == 0),
                                If(bytecount == 1,
                                    tx_state.eq(TX_CRC),
                                ).Else(
                                    bytecount.eq(bytecount - 1),
                                    self.tx_bytepos.eq(self.tx_bytepos + 1),
                                )
                            )
                        ],
                        TX_CRC: [
                            If(~self.uart_tx_busy & (cnt == 0),
                                tx_state.eq(TX_DONE),
                            )
                        ],
                        TX_DONE: [
                            If(~self.uart_tx_busy & (cnt == 0),
                                tx_state.eq(TX_IDLE),
                            )
                        ],
                        TX_SLEEP: [
                            If(~self.uart_tx_busy & self.write,
                                tx_state.eq(TX_AWAKE),
                                bytecount.eq(20),
                            )
                        ],
                        TX_AWAKE: [
                            If(~self.uart_tx_busy & (cnt == 0),
                                If(bytecount == 1,
                                    tx_state.eq(TX_START),
                                ).Else(
                                    bytecount.eq(bytecount - 1),
                                )
                            )
                        ],
                        TX_START: [
                            If(~self.uart_tx_busy & (cnt == 0),
                                tx_state.eq(TX_ADDR),
                            )
                        ],
                        # Recover from unused states (as the original).
                        "default": tx_state.eq(TX_IDLE),
                    })
                )
            )
        ]

# System Monitor Bridge ----------------------------------------------------------------------------

class SystemMonitorBridge(LiteXModule):
    """
    System monitor packet transport: RX decoder, channel arbiter and TX framer.

    Payload bytes are provided externally (see SystemMonitorPayloads) from tx_channel/tx_bytepos.
    """
    def __init__(self, num_channels=10):
        self.reset                   = Signal()
        self.menu_disabled           = Signal()
        self.channels_new_data_valid = Signal(num_channels)
        self.tx_byte_count           = Signal(8)
        self.tx_senddata             = Signal(8)
        self.uart_tx_busy            = Signal()
        self.uart_tx_data            = Signal(8)
        self.uart_tx_val             = Signal()
        self.uart_rx_data            = Signal(8)
        self.uart_rx_val             = Signal()
        self.rx_address              = Signal(7)
        self.rx_data                 = Signal(80)
        self.rx_data_val             = Signal()
        self.tx_channel              = Signal(max=num_channels)
        self.tx_bytepos              = Signal(8)
        self.write_done              = Signal()

        # # #

        uart_disabled = Signal()
        tx_address    = Signal(7)
        write         = Signal()

        self.rx_packet = rx_packet = SystemMonitorRxPacket()
        self.arbiter   = arbiter   = SystemMonitorArbiterBridge(num_channels=num_channels)
        self.tx_packet = tx_packet = SystemMonitorTxPacket()

        self.comb += [
            rx_packet.reset.eq(self.reset),
            rx_packet.uart_rx_data.eq(self.uart_rx_data),
            rx_packet.uart_rx_val.eq(self.uart_rx_val),
            self.rx_address.eq(rx_packet.rx_address),
            self.rx_data.eq(rx_packet.rx_data),
            self.rx_data_val.eq(rx_packet.rx_data_val),

            arbiter.reset.eq(self.reset),
            arbiter.channels_new_data_valid.eq(self.channels_new_data_valid),
            arbiter.menu_disabled.eq(self.menu_disabled),
            arbiter.uart_tx_busy.eq(self.uart_tx_busy),
            arbiter.write_done.eq(tx_packet.write_done),
            uart_disabled.eq(arbiter.uart_disabled),
            tx_address.eq(arbiter.tx_address),
            write.eq(arbiter.write),
            self.tx_channel.eq(arbiter.tx_channel),

            tx_packet.reset.eq(self.reset),
            tx_packet.uart_tx_busy.eq(self.uart_tx_busy),
            tx_packet.write.eq(write),
            tx_packet.menu_disabled.eq(self.menu_disabled),
            tx_packet.uart_disabled.eq(uart_disabled),
            tx_packet.tx_address.eq(tx_address),
            tx_packet.tx_byte_count.eq(self.tx_byte_count),
            tx_packet.tx_senddata.eq(self.tx_senddata),
            self.tx_bytepos.eq(tx_packet.tx_bytepos),
            self.write_done.eq(tx_packet.write_done),
            self.uart_tx_data.eq(tx_packet.uart_tx_data),
            self.uart_tx_val.eq(tx_packet.uart_tx_val),
        ]

# System Monitor Payloads --------------------------------------------------------------------------

class SystemMonitorPayloads(LiteXModule):
    """
    System monitor TX payloads (per channel byte count and data, from system_monitor.sv).

    Channels: 0/1: AA/Li-ion voltage, 2: Buttons, 3: Audio + Brightness, 4: System Control, 5: PMIC
    status, 6: Version, 7: Reserved, 8: System Status Extended, 9: Game Palette Data. 14-bit values
    are sent as upper 6 bits then lower 8 bits.
    """
    # Version: 1 bit reserved, 1 bit LiteX build marker, 6 bits minor (42), 6 bits major (63).
    VERSION = (1 << 12) | (42 << 6) | 63

    def __init__(self, num_channels=10):
        self.menu_disabled                  = Signal()
        self.btn_a                          = Signal()
        self.btn_b                          = Signal()
        self.btn_down                       = Signal()
        self.btn_left                       = Signal()
        self.btn_right                      = Signal()
        self.btn_up                         = Signal()
        self.btn_menu                       = Signal()
        self.btn_sel                        = Signal()
        self.btn_start                      = Signal()
        self.request_buttons                = Signal()
        self.request_version                = Signal()
        self.update_brightness              = Signal()
        self.request_system_status_extended = Signal()
        self.request_gpd                    = Signal()
        self.volt                           = Signal(14)
        self.bat_is_li                      = Signal()
        self.transmit_volt                  = Signal()
        self.brightness                     = Signal(4)
        self.h_headphones                   = Signal()
        self.h_volume                       = Signal(7)
        self.pmic_sys_status                = Signal(8)
        self.system_control                 = Signal(16)
        self.lowpower_backlight             = Signal()
        self.gbc_mode                       = Signal()
        self.gpd                            = Signal(64)
        self.tx_channel                     = Signal(max=num_channels)
        self.tx_bytepos                     = Signal(8)
        self.channels_new_data_valid        = Signal(num_channels)
        self.tx_byte_count                  = Signal(8)
        self.tx_senddata                    = Signal(8)

        # # #

        buttons          = Signal(14)
        audio_brightness = Signal(14)
        mic_sys_status   = Signal(14)
        version          = Constant(self.VERSION, 14)
        byte_counts      = Array(Constant(v, 8) for v in [2, 2, 2, 2, 2, 2, 2, 4, 4, 8])

        def upper14(value):
            return Cat(value[8:14], Constant(0, 2))

        self.comb += [
            buttons.eq(Cat(
                self.btn_start,
                self.btn_sel,
                self.btn_b,
                self.btn_a,
                self.btn_up,
                self.btn_right,
                self.btn_left,
                self.btn_down,
                ~self.btn_menu,
                self.menu_disabled,
                Constant(0, 4),
            )),
            audio_brightness.eq(Cat(self.h_volume, self.h_headphones, self.brightness, Constant(0, 2))),
            mic_sys_status.eq(Cat(self.pmic_sys_status, Constant(0, 6))),
            self.channels_new_data_valid.eq(Cat(
                (~self.menu_disabled & self.transmit_volt & ~self.bat_is_li),
                (~self.menu_disabled & self.transmit_volt & self.bat_is_li),
                (~self.menu_disabled | self.request_buttons),
                (~self.menu_disabled | self.update_brightness),
                ~self.menu_disabled,
                ~self.menu_disabled,
                (~self.menu_disabled | self.request_version),
                ~self.menu_disabled,
                (~self.menu_disabled | self.request_system_status_extended),
                self.request_gpd,
            )),
            self.tx_byte_count.eq(byte_counts[self.tx_channel]),
            self.tx_senddata.eq(0),
        ]

        channel_cases = {
            0: [Case(self.tx_bytepos, {
                0: [self.tx_senddata.eq(upper14(self.volt))],
                1: [self.tx_senddata.eq(self.volt[:8])],
            })],
            1: [Case(self.tx_bytepos, {
                0: [self.tx_senddata.eq(upper14(self.volt))],
                1: [self.tx_senddata.eq(self.volt[:8])],
            })],
            2: [Case(self.tx_bytepos, {
                0: [self.tx_senddata.eq(upper14(buttons))],
                1: [self.tx_senddata.eq(buttons[:8])],
            })],
            3: [Case(self.tx_bytepos, {
                0: [self.tx_senddata.eq(upper14(audio_brightness))],
                1: [self.tx_senddata.eq(audio_brightness[:8])],
            })],
            4: [Case(self.tx_bytepos, {
                0: [self.tx_senddata.eq(upper14(self.system_control))],
                1: [self.tx_senddata.eq(self.system_control[:8])],
            })],
            5: [Case(self.tx_bytepos, {
                0: [self.tx_senddata.eq(upper14(mic_sys_status))],
                1: [self.tx_senddata.eq(mic_sys_status[:8])],
            })],
            6: [Case(self.tx_bytepos, {
                0: [self.tx_senddata.eq(upper14(version))],
                1: [self.tx_senddata.eq(version[:8])],
            })],
            8: [Case(self.tx_bytepos, {
                0: [self.tx_senddata.eq(Cat(self.lowpower_backlight, self.gbc_mode, Constant(0, 6)))],
            })],
            9: [Case(self.tx_bytepos, {
                0: [self.tx_senddata.eq(self.gpd[0:8])],
                1: [self.tx_senddata.eq(self.gpd[8:16])],
                2: [self.tx_senddata.eq(self.gpd[16:24])],
                3: [self.tx_senddata.eq(self.gpd[24:32])],
                4: [self.tx_senddata.eq(self.gpd[32:40])],
                5: [self.tx_senddata.eq(self.gpd[40:48])],
                6: [self.tx_senddata.eq(self.gpd[48:56])],
                7: [self.tx_senddata.eq(self.gpd[56:64])],
            })],
        }
        self.comb += Case(self.tx_channel, channel_cases)

# System Monitor UART ------------------------------------------------------------------------------

class SystemMonitorUART(LiteXModule):
    """
    ESP32 UART for the system monitor packets (LiteX RS232PHY, replacing UART2).

    Exposes the val/busy byte interface expected by the packet transport: tx_busy stays asserted for
    the full byte time.
    """
    def __init__(self, pads, clk_freq, baudrate=115200):
        self.enable  = Signal()
        self.tx_data = Signal(8)
        self.tx_val  = Signal()
        self.tx_busy = Signal()
        self.rx_data = Signal(8)
        self.rx_val  = Signal()

        # # #

        # PHY.
        self.phy = phy = RS232PHY(pads, clk_freq=clk_freq, baudrate=baudrate)

        # TX: the PHY latches the byte on valid and acks (ready) at the end of the stop bit.
        tx_active = Signal()
        self.sync += [
            If(~self.enable,
                tx_active.eq(0),
            ).Elif(~tx_active & self.tx_val,
                tx_active.eq(1),
            ).Elif(tx_active & phy.sink.ready,
                tx_active.eq(0),
            )
        ]
        self.comb += [
            phy.sink.valid.eq(~tx_active & self.tx_val),
            phy.sink.data.eq(self.tx_data),
            self.tx_busy.eq(tx_active),
        ]

        # RX.
        self.comb += [
            self.rx_val.eq(phy.source.valid),
            self.rx_data.eq(phy.source.data),
            phy.source.ready.eq(1),
        ]

# System Monitor Control ---------------------------------------------------------------------------

class SystemMonitorControl(LiteXModule):
    """
    System monitor control logic (port of system_monitor.sv).

    - Decodes ESP32 packets: palettes, MCU buttons, brightness, system control and requests.
    - Menu button handling (menu open/close toggle on release).
    - LCD backlight PWM, brightness adjust with Menu + Left/Right when the menu is closed.
    - Battery ADC scheduling, AA/Li-ion detection, voltage averaging, low battery / LED status and
      low power backlight mode (AA).
    """
    def __init__(self, num_channels=10, adc_interval=41946, adc_sel_lead=1000):
        # adc_interval: ADC sampling interval in cycles (41946: 5ms at 8.388608MHz).
        # adc_sel_lead: ADC_SEL mux toggle lead time (cycles) before a measurement.
        self.reset              = Signal()

        # Buttons (1 = pressed, except menu: 0 = pressed).
        self.btn_a              = Signal()
        self.btn_b              = Signal()
        self.btn_down           = Signal()
        self.btn_left           = Signal()
        self.btn_right          = Signal()
        self.btn_up             = Signal()
        self.btn_menu           = Signal()
        self.btn_sel            = Signal()
        self.btn_start          = Signal()

        # Menu / LCD.
        self.menu_disabled      = Signal(reset=1)
        self.lcd_init_done      = Signal()
        self.lcd_pwm            = Signal()
        self.lcd_backlight_init = Signal()

        # ADC.
        self.adc_sel            = Signal()
        self.adc_req            = Signal()
        self.adc_ready          = Signal()
        self.adc_value          = Signal(14)

        # Status inputs.
        self.pmic_sys_status    = Signal(8)
        self.second             = Signal()
        self.half_second        = Signal()

        # Outputs.
        self.mcu_buttons        = Signal(9)
        self.low_battery        = Signal()
        self.led_green          = Signal()
        self.led_red            = Signal()
        self.led_yellow         = Signal()
        self.led_white          = Signal()
        self.system_control     = Signal(16)
        self.debug_system       = Signal(32)
        self.palette_bg         = Signal(64)
        self.palette_obj0       = Signal(64)
        self.palette_obj1       = Signal(64)

        # Packet transport.
        self.rx_address         = Signal(7)
        self.rx_data            = Signal(80)
        self.rx_data_val        = Signal()
        self.tx_channel         = Signal(max=num_channels)
        self.write_done         = Signal()

        # Payload requests/values.
        self.request_buttons                = Signal()
        self.request_version                = Signal()
        self.update_brightness              = Signal()
        self.request_system_status_extended = Signal()
        self.request_gpd                    = Signal()
        self.volt                           = Signal(14)
        self.bat_is_li                      = Signal()
        self.transmit_volt                  = Signal()
        self.brightness                     = Signal(4, reset=3)
        self.lowpower_backlight             = Signal()

        # # #

        brightness     = self.brightness
        volt           = self.volt
        block_receive  = Signal(2)
        lowpower_old   = Signal(4)

        # Button sampling (2 synchronization stages + 16-bit history for D-Pad/Menu).
        def sampled(btn, with_history=False):
            r1 = Signal()
            r2 = Signal()
            sr = Signal(16) if with_history else None
            self.sync += If(~self.reset,
                r1.eq(btn),
                r2.eq(r1),
                *([sr.eq(Cat(r2, sr[:15]))] if with_history else []),
            )
            return r2, sr
        btn_menu_r2,  btn_menu_sr  = sampled(self.btn_menu,  True)
        btn_down_r2,  _            = sampled(self.btn_down,  True)
        btn_up_r2,    _            = sampled(self.btn_up,    True)
        btn_left_r2,  btn_left_sr  = sampled(self.btn_left,  True)
        btn_right_r2, btn_right_sr = sampled(self.btn_right, True)
        btn_sel_r2,   _            = sampled(self.btn_sel)
        btn_start_r2, _            = sampled(self.btn_start)
        btn_a_r2,     _            = sampled(self.btn_a)
        btn_b_r2,     _            = sampled(self.btn_b)

        # Menu toggle: menu button pressed then released (16 stable samples each).
        menu_down = Signal()
        self.sync += [
            If(self.reset,
                self.menu_disabled.eq(1),
            ).Else(
                If(btn_menu_sr == 0x8000,
                    menu_down.eq(1),
                ),
                If((btn_menu_sr == 0x7fff) & menu_down,
                    self.menu_disabled.eq(~self.menu_disabled),
                    menu_down.eq(0),
                ),
                If(btn_a_r2 | btn_b_r2 | btn_down_r2 | btn_up_r2 | btn_left_r2 | btn_right_r2 | btn_sel_r2 | btn_start_r2,
                    menu_down.eq(0),
                ),
            )
        ]

        # Packet decode / brightness / low power backlight.
        self.sync += [
            If(self.reset,
                self.system_control.eq(0),
                self.mcu_buttons.eq(0),
                self.request_gpd.eq(0),
            ).Else(
                self.request_buttons.eq(0),
                self.request_version.eq(0),
                self.update_brightness.eq(0),
                self.request_system_status_extended.eq(0),

                If(self.half_second,
                    self.lcd_backlight_init.eq(1),
                ),

                If(self.rx_data_val,
                    If(self.rx_address == 0xd,
                        self.request_gpd.eq(1),
                    ),
                    If(self.rx_address == 0xc,
                        If(self.rx_data[63],
                            self.palette_obj1.eq(self.rx_data[:64]),
                        ).Else(
                            self.palette_obj0.eq(self.rx_data[:64]),
                        )
                    ),
                    If(self.rx_address == 0xb,
                        self.palette_bg.eq(self.rx_data[:64]),
                    ),
                    If(self.rx_address == 9,
                        self.mcu_buttons.eq(self.rx_data[:9]),
                    ),
                    If(self.rx_address == 6,
                        self.request_version.eq(1),
                    ),
                    If(self.rx_address == 5,
                        If(block_receive == 0,
                            brightness.eq(self.rx_data[:4]),
                        ).Else(
                            block_receive.eq(block_receive - 1),
                        )
                    ),
                    If(self.rx_address == 4,
                        self.system_control.eq(self.rx_data[:16]),
                    ),
                    If(self.rx_address == 2,
                        self.request_buttons.eq(1),
                    ),
                ),

                # Brightness adjust with Menu (held) + Left/Right when the menu is closed.
                If(self.menu_disabled,
                    If((btn_left_sr == 0x8000) & ~btn_menu_r2,
                        If(brightness >= 1,
                            brightness.eq(brightness - 1),
                            block_receive.eq(3),
                            self.update_brightness.eq(1),
                        )
                    ),
                    If((btn_right_sr == 0x8000) & ~btn_menu_r2,
                        If(brightness != 15,
                            brightness.eq(brightness + 1),
                            block_receive.eq(3),
                            self.update_brightness.eq(1),
                        )
                    ),
                ),

                If(self.lowpower_backlight,
                    brightness.eq(0),
                    self.update_brightness.eq(0),
                ),

                # Low power backlight (AA batteries only).
                If(volt >= 700, # ~1.8V.
                    If(~self.lowpower_backlight & ~self.bat_is_li & (volt < 979), # Below 2.55V.
                        self.request_system_status_extended.eq(1),
                        self.lowpower_backlight.eq(1),
                        lowpower_old.eq(brightness),
                    ),
                    If(self.lowpower_backlight & ~self.bat_is_li & (volt > 1293), # Above 3.4V.
                        self.request_system_status_extended.eq(1),
                        self.lowpower_backlight.eq(0),
                        brightness.eq(lowpower_old),
                    ),
                ),

                If(self.write_done & (self.tx_channel == 9) & self.request_gpd,
                    self.request_gpd.eq(0),
                ),
            )
        ]

        # LCD backlight PWM.
        lcd_count = Signal(8)
        self.sync += lcd_count.eq(lcd_count + 1)
        self.comb += If(self.lcd_init_done & self.lcd_backlight_init,
            self.lcd_pwm.eq(lcd_count <= Cat(Constant(0, 4), brightness))
        )

        # ADC scheduling (every 5ms), with ADC_SEL toggling (AA/Li-ion) until detection is done.
        adc_timer      = Signal(16)
        startup_cnt    = Signal(10)
        startup_select = Signal((11, True))
        startup_done   = Signal()
        self.sync += [
            If(adc_timer < adc_interval,
                adc_timer.eq(adc_timer + 1),
                self.adc_req.eq(0),
                If(startup_done,
                    self.adc_sel.eq(startup_select[10]),
                ).Elif(adc_timer == (adc_interval - adc_sel_lead),
                    self.adc_sel.eq(~self.adc_sel),
                )
            ).Else(
                adc_timer.eq(0),
                self.adc_req.eq(1),
            )
        ]

        # Battery: type detection, averaging (256 samples), status/LEDs.
        # Type detection only counts samples >= 700 (~1.8V) and volt is only updated once the type
        # is detected (|startup_select| > 127): with a disconnected/low ADC input, volt stays at 0.
        volt_sum      = Signal(22)
        volt_cnt      = Signal(9)
        blink         = Signal()
        voltage_full  = Signal(14)
        voltage_red   = Signal(14)
        self.comb += [
            self.bat_is_li.eq(startup_select[10]),
            voltage_full.eq(Mux(self.bat_is_li, 1423, 1367)), # 3.75V Li-ion : 3.6V AA.
            voltage_red.eq( Mux(self.bat_is_li, 1182, 1071)), # 3.1V  Li-ion : 2.8V AA.
        ]
        self.sync += [
            If(self.reset,
                self.low_battery.eq(0),
                self.led_red.eq(0),
                self.led_green.eq(0),
                self.led_yellow.eq(0),
                blink.eq(0),
                volt.eq(0),
                volt_sum.eq(0),
                volt_cnt.eq(0),
                startup_cnt.eq(0),
                startup_select.eq(0),
                startup_done.eq(0),
                self.transmit_volt.eq(0),
            ).Else(
                self.transmit_volt.eq(0),
                self.debug_system.eq(Cat(volt, Constant(0, 6), startup_select, Constant(0, 1))),
                If(self.adc_ready,
                    If(~startup_cnt[9], # Wait ~4s for stable measurements.
                        startup_cnt.eq(startup_cnt + 1),
                    ),
                    If(startup_done,
                        volt_sum.eq(volt_sum + self.adc_value),
                        volt_cnt.eq(volt_cnt + 1),
                    ).Elif(startup_cnt[9] & (self.adc_value >= 700),
                        # Negative: Li-ion, positive: AA.
                        If(self.adc_sel,
                            startup_select.eq(startup_select - 1),
                        ).Else(
                            startup_select.eq(startup_select + 1),
                        )
                    ),
                    If((startup_select > 127) | (startup_select < -127),
                        startup_done.eq(1),
                    ),
                ),
                If(volt_cnt[8],
                    volt_sum.eq(0),
                    volt_cnt.eq(0),
                    volt.eq(volt_sum[8:22]),
                    self.transmit_volt.eq(1),
                ),
                If(self.second,
                    blink.eq(~blink),
                ),
                self.low_battery.eq(0),
                self.led_red.eq(0),
                self.led_green.eq(0),
                self.led_yellow.eq(0),
                self.led_white.eq(0),
                If(volt >= 700, # ~1.8V.
                    If(self.pmic_sys_status[2], # Charging.
                        If(self.bat_is_li & (volt < voltage_full),
                            self.led_white.eq(1),
                        )
                    ).Else(
                        If(volt < voltage_red,
                            self.low_battery.eq(1),
                            If(blink,
                                self.led_red.eq(1),
                            )
                        )
                    )
                ),
            )
        ]

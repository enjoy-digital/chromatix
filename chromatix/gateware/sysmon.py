#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litex.gen import *

# System Monitor RX Packet -------------------------------------------------------------------------

class SystemMonitorRxPacket(LiteXModule):
    def __init__(self):
        self.reset        = Signal()
        self.uart_rx_data = Signal(8)
        self.uart_rx_val  = Signal()
        self.rx_address   = Signal(7)
        self.rx_data      = Signal(80)
        self.rx_data_val  = Signal()

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

        self.sync.gclk += [
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

        self.sync.gclk += [
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
                })
            )
        ]

# System Monitor Arbiter ---------------------------------------------------------------------------

class SystemMonitorArbiterBridge(LiteXModule):
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
            self.sync.gclk += [
                If(self.reset,
                    channels_refresh[i].eq(0),
                ).Elif(self.channels_new_data_valid[i],
                    channels_refresh[i].eq(1),
                ).Elif((active_channel == i) & self.write_done,
                    channels_refresh[i].eq(0),
                )
            ]

        self.sync.gclk += [
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

        cnt       = Signal(4)
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

        self.sync.gclk += [
            If(self.reset,
                cnt.eq(0),
            ).Else(
                cnt.eq(cnt + 1),
            )
        ]

        self.sync.gclk += [
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

        self.sync.gclk += [
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

        self.sync.gclk += [
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

        self.sync.gclk += [
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
                    })
                )
            )
        ]

# System Monitor Bridge ----------------------------------------------------------------------------

class SystemMonitorBridge(LiteXModule):
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

        uart_disabled = Signal()
        tx_address    = Signal(7)
        write         = Signal()

        self.submodules.rx_packet = rx_packet = SystemMonitorRxPacket()
        self.submodules.arbiter   = arbiter   = SystemMonitorArbiterBridge(num_channels=num_channels)
        self.submodules.tx_packet = tx_packet = SystemMonitorTxPacket()

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

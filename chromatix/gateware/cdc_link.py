#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
USB CDC byte stream switch for CPU applications: debug bridge (UARTBone, default) or application
link (CPU UART for commands/replies + DMA for bulk data from the main RAM) at the USB rate.
"""

from migen import *

from litex.gen import *

from litex.soc.interconnect import stream
from litex.soc.interconnect import wishbone
from litex.soc.interconnect.csr import *
from litex.soc.cores.uart import UART
from litex.soc.cores.dma import WishboneDMAReader

# CDC Link -----------------------------------------------------------------------------------------

class CDCLink(LiteXModule):
    """
    CDC byte stream switch: the host stream (source: host -> device, sink: device -> host) goes to
    the debug bridge (debug_source/debug_sink, UARTBone PHY) or, when the application selects it
    (control.app), to the application UART (`uart`) and DMA reader (`dma`, bulk data from the
    bus, little endian bytes).

    Device -> host in application mode: DMA bytes have priority over the UART bytes, the
    application keeps them ordered (UART TX FIFO empty before starting the DMA, DMA idle before
    writing new UART bytes).

    The host switches back to the debug bridge with a "1200 baud touch" (`touch`: CDC line coding
    set to 1200 baud, rising edge), so a stuck application can always be replaced. The debug session
    lasts while the host keeps the port open (`dtr`: the host asserts DTR while the port is open);
    the stream goes back to the application `release_cycles` after DTR is released.
    """
    def __init__(self, uart_fifo_depth=64, dma_fifo_depth=64, bus_data_width=32,
        bus_address_width=32, release_cycles=int(67e6)):
        # CDC byte stream (sys).
        self.source       = stream.Endpoint([("data", 8)]) # Host -> device.
        self.sink         = stream.Endpoint([("data", 8)]) # Device -> host.
        # Debug bridge PHY (UARTBone).
        self.debug_source = stream.Endpoint([("data", 8)])
        self.debug_sink   = stream.Endpoint([("data", 8)])
        # Host "1200 baud touch" (level, sys), DTR (sys).
        self.touch        = Signal()
        self.dtr          = Signal()
        # DMA bus (master).
        self.bus          = wishbone.Interface(data_width=bus_data_width,
            address_width=bus_address_width, addressing="word")

        self.control = CSRStorage(fields=[
            CSRField("app", size=1, offset=0,
                description="CDC stream to the application (else debug bridge, also on a touch)."),
        ])
        self.status = CSRStatus(fields=[
            CSRField("dma_idle", size=1, offset=0,
                description="DMA done (or disabled) and its data sent."),
            CSRField("touch",    size=1, offset=1,
                description="Debug session (host 1200 baud touch, until DTR released)."),
        ])

        # # #

        # Application UART (CPU) and DMA reader -> bytes.
        class _UARTPHY:
            pass
        phy        = _UARTPHY()
        phy.source = stream.Endpoint([("data", 8)]) # Host -> UART RX.
        phy.sink   = stream.Endpoint([("data", 8)]) # UART TX -> host.
        self.uart  = UART(phy, tx_fifo_depth=uart_fifo_depth, rx_fifo_depth=uart_fifo_depth,
            rx_fifo_rx_we=True)
        self.dma   = WishboneDMAReader(self.bus, fifo_depth=dma_fifo_depth, with_csr=True,
            with_byteswap=False)
        self.conv  = conv = stream.Converter(bus_data_width, 8)
        self.comb += self.dma.source.connect(conv.sink)

        # Application selection / host touch.
        app       = Signal()
        touch_d   = Signal()
        touched   = Signal()
        release   = Signal(max=release_cycles + 1)
        self.sync += [
            touch_d.eq(self.touch),
            If(self.dtr | ~touched,
                release.eq(0),
            ).Elif(release != release_cycles,
                release.eq(release + 1),
            ),
            If(self.control.re | (release == release_cycles),
                touched.eq(0),
            ).Elif(self.touch & ~touch_d,
                touched.eq(1),
            ),
        ]
        self.comb += [
            app.eq(self.control.fields.app & ~touched),
            self.status.fields.dma_idle.eq((~self.dma.enable | self.dma.done) &
                ~self.dma.source.valid & ~conv.source.valid),
            self.status.fields.touch.eq(touched),
        ]

        # Host -> device.
        self.comb += [
            If(app,
                self.source.connect(phy.source),
            ).Else(
                self.source.connect(self.debug_source),
            )
        ]

        # Device -> host.
        self.comb += [
            If(~app,
                self.debug_sink.connect(self.sink),
            ).Elif(conv.source.valid,
                conv.source.connect(self.sink, omit={"last"}),
            ).Else(
                phy.sink.connect(self.sink),
            )
        ]

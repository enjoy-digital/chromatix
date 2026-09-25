#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
USB class logic of the Chromatic composite device (UVC + UAC + CDC-ACM), on top of the Gowin USB 2.0
Device Controller (port of the class handlers of usbuvcuart_top.v).

All modules run in the "sys" domain (the 60MHz UTMI clock of the USB PHY).
"""

from migen import *
from migen.genlib.cdc import MultiReg

from litex.gen import *

from litex.soc.interconnect import stream

from litex.soc.cores.uart import RS232PHYTX, RS232PHYRX

from chromatix.gateware.usb import ColorSpaceConvertor, VideoFIFO, CSC_LATENCY
from chromatix.gateware.usb_desc import VIDEO_FRAMES, video_frame_size, video_transactions

# Constants ----------------------------------------------------------------------------------------

# Interfaces.
UVC_VC_INTERFACE  = 0
UVC_VS_INTERFACE  = 1
UART_CTRL_IFACE   = 2
UART_DATA_IFACE   = 3
UAC_AC_INTERFACE  = 4
UAC_AS_INTERFACE  = 5

# Endpoints.
EP_CTRL = 0
EP_VC   = 1
EP_VS   = 2
EP_UART = 3
EP_UAC  = 5

# UVC.
UVC_WIDTH            = 160
UVC_HEIGHT           = 144
UVC_FPS              = 60
UVC_PACKET_SIZE      = 1024
UVC_HEADER_SIZE      = 12
UVC_PAYLOAD_SIZE     = UVC_PACKET_SIZE
UVC_MAX_FRAME_SIZE   = UVC_WIDTH*UVC_HEIGHT*16//8
UVC_FRAME_INTERVAL   = 10000000//UVC_FPS
UVC_VS_PROBE_CONTROL  = 0x01
UVC_VS_COMMIT_CONTROL = 0x02
UVC_SET_CUR          = 0x01
UVC_GET_CUR          = 0x81
UVC_GET_MIN          = 0x82
UVC_GET_MAX          = 0x83
UVC_GET_DEF          = 0x87

# UAC.
UAC_FREQUENCY       = 44100
UAC_CLOCK_ID        = 1
UAC_CUR_ATTR        = 1
UAC_RANGE_ATTR      = 2
CS_SAM_FREQ_CONTROL = 1

# CDC-ACM.
SET_LINE_CODING        = 0x20
GET_LINE_CODING        = 0x21
SET_CONTROL_LINE_STATE = 0x22

# Helpers ------------------------------------------------------------------------------------------

def byte(value, n):
    """Byte n (little-endian) of value."""
    return (value >> (8*n)) & 0xff

# Setup Parser -------------------------------------------------------------------------------------

class USBSetupParser(LiteXModule):
    """Control transfers: SETUP header capture and data stage offset tracking."""
    def __init__(self):
        self.reset         = Signal()
        self.setup_active  = Signal()
        self.endpt         = Signal(4)
        self.rxdat         = Signal(8)
        self.rxval         = Signal()
        self.rxact         = Signal()
        self.txact         = Signal()
        self.txpop         = Signal()

        self.header_ready  = Signal()
        self.bmRequestType = Signal(8)
        self.bRequest      = Signal(8)
        self.wValue        = Signal(16)
        self.wIndex        = Signal(16)
        self.wLength       = Signal(16)
        self.cdata_ofs     = Signal(16)

        # # #

        hdr_len      = Signal(3)
        cdata_rxtx   = Signal()
        cdata_active = Signal()
        clength      = Signal(16)
        self.comb += [
            self.header_ready.eq(hdr_len == 7),
            clength.eq(Cat(self.wLength[:8], self.rxdat)),
        ]
        self.sync += [
            If(self.reset,
                hdr_len.eq(0),
                cdata_rxtx.eq(0),
                cdata_active.eq(0),
            ).Elif(self.setup_active,
                If(self.rxval,
                    If(~self.header_ready,
                        hdr_len.eq(hdr_len + 1),
                    ),
                    Case(hdr_len, {
                        0: [
                            self.bmRequestType.eq(self.rxdat),
                            cdata_rxtx.eq(0),
                            cdata_active.eq(0),
                            self.cdata_ofs.eq(0),
                        ],
                        1: self.bRequest.eq(self.rxdat),
                        2: self.wValue[0:8].eq(self.rxdat),
                        3: self.wValue[8:16].eq(self.rxdat),
                        4: self.wIndex[0:8].eq(self.rxdat),
                        5: self.wIndex[8:16].eq(self.rxdat),
                        6: self.wLength[0:8].eq(self.rxdat),
                        7: [
                            self.wLength[8:16].eq(self.rxdat),
                            cdata_active.eq(0),
                            cdata_rxtx.eq(clength != 0),
                        ],
                    })
                )
            ).Elif(self.header_ready & (self.endpt == EP_CTRL),
                If(cdata_rxtx,
                    If((self.rxact & self.rxval) | (self.txact & self.txpop),
                        self.cdata_ofs.eq(self.cdata_ofs + 1),
                    ),
                    If(self.rxact | self.txact,
                        cdata_active.eq(1),
                    ).Elif(cdata_active,
                        cdata_active.eq(0),
                        cdata_rxtx.eq(0),
                        hdr_len.eq(0),
                    )
                ).Else(
                    hdr_len.eq(0),
                )
            )
        ]

# Control Request Handler (base) -------------------------------------------------------------------

class _ControlHandler(LiteXModule):
    """
    Common interface of the EP0 class request handlers: requests are decoded from the USBSetupParser
    outputs, IN data stage bytes are provided on txdat (next byte loaded on each txpop) while txval
    is set.
    """
    def __init__(self, setup):
        self.reset     = Signal()
        self.rxdat     = Signal(8)
        self.rxact     = Signal()
        self.rxval     = Signal()
        self.txpop     = Signal()
        self.txval     = Signal()
        self.txdat_len = Signal(12)
        self.txdat     = Signal(8)
        self.last      = Signal() # Last byte of the data stage.

        # (usb_txdat_len - 16'd1) == cdata_ofs, on 16-bit (as the original).
        last_ofs = Signal(16)
        self.comb += [
            last_ofs.eq(self.txdat_len - 1),
            self.last.eq(last_ofs == setup.cdata_ofs),
        ]

# CDC-ACM Control ----------------------------------------------------------------------------------

class CDCACMControl(_ControlHandler):
    """CDC-ACM class requests: line coding and control line state (port of ctrl_uart)."""
    def __init__(self, setup):
        _ControlHandler.__init__(self, setup)
        self.ctl_sig     = Signal(2)
        self.dte_rate    = Signal(32)
        self.char_format = Signal(8)
        self.parity_type = Signal(8)
        self.data_bits   = Signal(8)

        # # #

        s = setup
        line_coding = [self.dte_rate[0:8], self.dte_rate[8:16], self.dte_rate[16:24], self.dte_rate[24:32],
                       self.char_format, self.parity_type, self.data_bits]
        self.sync += [
            If(self.reset,
                self.txval.eq(0),
                self.ctl_sig.eq(0),
                self.dte_rate.eq(115200),
                self.char_format.eq(0),
                self.parity_type.eq(0),
                self.data_bits.eq(8),
            ).Elif(s.header_ready & (s.wIndex == UART_CTRL_IFACE),
                If(s.bmRequestType == 0x21, # Set requests.
                    If((s.bRequest == SET_CONTROL_LINE_STATE) & (s.wLength == 0),
                        self.ctl_sig.eq(s.wValue[0:2]),
                    ).Elif(self.rxact & (s.bRequest == SET_LINE_CODING) & (s.wLength == 7),
                        Case(s.cdata_ofs, {i: line_coding[i].eq(self.rxdat) for i in range(7)}),
                    )
                ).Elif(s.bmRequestType == 0xa1, # Get requests.
                    If((s.bRequest == GET_LINE_CODING) & (s.wLength != 0),
                        If(self.txpop,
                            Case(s.cdata_ofs, {
                                **{i: self.txdat.eq(line_coding[i + 1]) for i in range(6)},
                                6: self.txdat.eq(0),
                            }),
                            If(self.last,
                                self.txval.eq(0),
                            )
                        ).Elif(s.cdata_ofs == 0,
                            self.txval.eq(1),
                            self.txdat_len.eq(s.wLength[:12]),
                            self.txdat.eq(self.dte_rate[0:8]),
                        )
                    )
                )
            )
        ]

# UVC Control --------------------------------------------------------------------------------------

class UVCControl(_ControlHandler):
    """
    UVC VideoStreaming probe/commit controls (port of ctrl_uvc).

    With a single frame (the original), the probe is constant and SET requests are ignored. With
    several frames, the bFrameIndex of SET_CUR(PROBE/COMMIT) is tracked: the probe returns the
    frame size/payload transfer size of the probed frame (the host selects the VS alternate setting
    from dwMaxPayloadTransferSize) and frame_index is the committed frame (1: default).
    """
    def __init__(self, setup, frames=[(UVC_WIDTH, UVC_HEIGHT)]):
        _ControlHandler.__init__(self, setup)
        self.frame_index = Signal(8, reset=1) # Committed frame (bFrameIndex).

        # # #

        s = setup
        # Probe/commit control (UVC 1.1, 34 bytes) of each frame.
        def probe(index, width, height):
            payload_size = UVC_PAYLOAD_SIZE*(video_transactions(width, height) if len(frames) > 1 else 1)
            probe = []
            probe += [0, 0]                                                   # bmHint.
            probe += [1]                                                      # bFormatIndex.
            probe += [index]                                                  # bFrameIndex.
            probe += [byte(UVC_FRAME_INTERVAL, n) for n in range(4)]          # dwFrameInterval.
            probe += [0, 0]                                                   # wKeyFrameRate.
            probe += [0, 0]                                                   # wPFrameRate.
            probe += [0, 0]                                                   # wCompQuality.
            probe += [0, 0]                                                   # wCompWindowSize.
            probe += [0, 0]                                                   # wDelay.
            probe += [byte(video_frame_size(width, height), n) for n in range(4)] # dwMaxVideoFrameSize.
            probe += [byte(payload_size, n) for n in range(4)]               # dwMaxPayloadTransferSize.
            probe += [byte(60000000, n) for n in range(4)]                   # dwClockFrequency.
            probe += [0]                                                      # bmFramingInfo.
            probe += [0, 0, 0]                                                # bPreferedVersion, bMin/MaxVersion.
            assert len(probe) == 34
            return probe
        probes = [probe(i, w, h) for i, (w, h) in enumerate(frames, start=1)]

        # Probed/committed frames (SET_CUR data stage, bFrameIndex at offset 3).
        probe_frame = Signal(8, reset=1)
        probe_sel   = Signal(max=max(len(frames), 2))
        if len(frames) > 1:
            is_set_cur = Signal()
            valid      = Signal()
            self.comb += [
                is_set_cur.eq(s.header_ready & (s.wIndex == UVC_VS_INTERFACE) &
                    (s.bmRequestType == 0x21) & (s.bRequest == UVC_SET_CUR)),
                valid.eq((self.rxdat >= 1) & (self.rxdat <= len(frames))),
            ]
            self.sync += [
                If(self.reset,
                    probe_frame.eq(1),
                    self.frame_index.eq(1),
                ).Elif(is_set_cur & self.rxact & self.rxval & (s.cdata_ofs == 3) & valid,
                    If(s.wValue[8:16] == UVC_VS_PROBE_CONTROL,
                        probe_frame.eq(self.rxdat),
                    ).Elif(s.wValue[8:16] == UVC_VS_COMMIT_CONTROL,
                        self.frame_index.eq(self.rxdat),
                    )
                )
            ]
            # GET_DEF returns the default frame, GET_CUR/MIN/MAX the probed one.
            self.comb += If(s.bRequest != UVC_GET_DEF, probe_sel.eq(probe_frame - 1))

        def probe_byte(i):
            values = [p[i] for p in probes]
            if len(set(values)) == 1:
                return values[0]
            return Array(values)[probe_sel]

        self.sync += [
            If(self.reset,
                self.txval.eq(0),
            ).Elif(s.header_ready & (s.wIndex == UVC_VS_INTERFACE),
                If(s.bmRequestType == 0xa1,
                    If((s.wLength != 0) & (s.wValue[8:16] == UVC_VS_PROBE_CONTROL) &
                       ((s.bRequest == UVC_GET_CUR) | (s.bRequest == UVC_GET_DEF) |
                        (s.bRequest == UVC_GET_MIN) | (s.bRequest == UVC_GET_MAX)),
                        If(self.txpop,
                            Case(s.cdata_ofs, {
                                **{i: self.txdat.eq(probe_byte(i + 1)) for i in range(33)},
                                "default": self.txdat.eq(0),
                            }),
                            If(self.last,
                                self.txval.eq(0),
                            )
                        ).Elif(s.cdata_ofs == 0,
                            self.txval.eq(1),
                            self.txdat_len.eq(Mux(s.wLength < 34, s.wLength[:12], 34)),
                            self.txdat.eq(probe_byte(0)),
                        )
                    )
                )
            )
        ]

# UAC Control --------------------------------------------------------------------------------------

class UACControl(_ControlHandler):
    """UAC2 clock source sampling frequency CUR/RANGE requests (port of ctrl_uac)."""
    def __init__(self, setup):
        _ControlHandler.__init__(self, setup)

        # # #

        s = setup
        freq   = [byte(UAC_FREQUENCY, n) for n in range(4)]
        range_ = [0x01, 0x00] + freq + freq + [0, 0, 0, 0] # wNumSubRanges=1, MIN, MAX, RES.
        self.sync += [
            If(self.reset,
                self.txval.eq(0),
            ).Elif(s.header_ready & (s.wIndex[0:8] == UAC_AC_INTERFACE),
                If((s.bmRequestType == 0xa1) & (s.wLength != 0),
                    If((s.wValue[0:8] == 0) & (s.wValue[8:16] == CS_SAM_FREQ_CONTROL) &
                       (s.wIndex[8:16] == UAC_CLOCK_ID),
                        If(s.bRequest == UAC_CUR_ATTR,
                            If(self.txpop,
                                Case(s.cdata_ofs, {
                                    **{i: self.txdat.eq(freq[i + 1]) for i in range(3)},
                                    "default": self.txdat.eq(0),
                                })
                            ).Elif(s.cdata_ofs == 0,
                                self.txval.eq(1),
                                self.txdat_len.eq(Mux(s.wLength < 4, s.wLength[:12], 4)),
                                self.txdat.eq(freq[0]),
                            )
                        ).Elif(s.bRequest == UAC_RANGE_ATTR,
                            If(self.txpop,
                                Case(s.cdata_ofs, {
                                    **{i: self.txdat.eq(range_[i + 1]) for i in range(13)},
                                    "default": self.txdat.eq(0),
                                })
                            ).Elif(s.cdata_ofs == 0,
                                self.txval.eq(1),
                                self.txdat_len.eq(Mux(s.wLength < 14, s.wLength[:12], 14)),
                                self.txdat.eq(range_[0]),
                            )
                        ),
                        If(self.txpop & self.txval & self.last,
                            self.txval.eq(0),
                        )
                    )
                )
            )
        ]

# Interface Alternate Setting ----------------------------------------------------------------------

class InterfaceAltSelect(LiteXModule):
    """Alternate setting register of an interface (port of interface_alt_select)."""
    def __init__(self):
        self.reset  = Signal()
        self.update = Signal()
        self.alt_i  = Signal(8)
        self.alt_o  = Signal(8)

        # # #

        self.sync += [
            If(self.reset,
                self.alt_o.eq(0),
            ).Elif(self.update,
                self.alt_o.eq(self.alt_i),
            )
        ]

# UAC Endpoint -------------------------------------------------------------------------------------

class UACEndpoint(LiteXModule):
    """
    UAC isochronous IN endpoint: 44.1kHz stereo 16-bit samples captured from the audio clock
    ("audio" domain, gClk), buffered per micro-frame (port of usbuac_ep/sample_get_p/audioclk_gen/
    sync_audio).
    """
    SAMPLE_RATE        = 44100
    SAMPLES_PER_MFRAME = (SAMPLE_RATE + 7999)//8000
    MAXBUFFER          = 16*2*SAMPLES_PER_MFRAME//8 # Bytes.

    def __init__(self, sys_clk_freq=60e6, luna=False):
        self.reset     = Signal()
        self.sof_rise  = Signal()
        self.next_len  = Signal(12) # LUNA: bytes of the next micro-frame (latched at SOF).
        self.left      = Signal(16) # "audio" domain.
        self.right     = Signal(16) # "audio" domain.
        self.txpop     = Signal()
        self.txact     = Signal()
        self.txdat     = Signal(8)
        self.txdat_len = Signal(12)
        self.txcork    = Signal()

        # # #

        # Note: reset and txact are unused (as the original, usbuac_ep ignores them).
        n = self.MAXBUFFER

        # Audio clock generator (44.1kHz frame clock, phase accumulator).
        freq_sum = int(sys_clk_freq)//2
        bfreq    = self.SAMPLE_RATE*32
        count    = Signal(32)
        acount   = Signal(5)
        bclk     = Signal()
        aclk     = Signal()
        self.sync += [
            If(count < freq_sum,
                count.eq(count + bfreq),
            ).Else(
                count.eq(count - freq_sum + bfreq),
                bclk.eq(~bclk),
                If(~bclk,
                    acount.eq(acount + 1),
                )
            )
        ]
        self.comb += aclk.eq(acount[4] == 0)

        # Audio samples: sampled on the audio clock (gClk) rising edges seen in sys.
        g_clk_sr  = Signal(2)
        g_prdy    = Signal()
        g_sample  = Signal(32)
        self.sync += [
            g_clk_sr.eq(Cat(ClockSignal("audio"), g_clk_sr[0])),
            g_prdy.eq(g_clk_sr[1]),
            If(~g_prdy & g_clk_sr[1],
                g_sample.eq(Cat(self.left, self.right)),
            )
        ]

        # Sample capture at 44.1kHz.
        sample       = Signal(32)
        sample_ready = Signal()
        self.sync += [
            If(~sample_ready & aclk,
                sample.eq(g_sample),
            ),
            sample_ready.eq(aclk),
        ]

        # LUNA: samples buffered in a byte FIFO, each micro-frame sends the complete samples available
        # (up to MAXBUFFER bytes); txdat is the FIFO output, popped with txpop.
        if luna:
            self.converter = converter = stream.Converter(32, 8)
            self.fifo      = fifo      = ResetInserter()(stream.SyncFIFO([("data", 8)], 64, buffered=True))
            p_sample_ready = Signal()
            pending        = Signal()
            level          = Signal(8)
            self.sync += [
                p_sample_ready.eq(sample_ready),
                If(sample_ready & ~p_sample_ready,
                    pending.eq(1),
                ).Elif(converter.sink.ready,
                    pending.eq(0),
                ),
            ]
            self.comb += [
                fifo.reset.eq(self.reset),
                converter.sink.valid.eq(pending),
                converter.sink.data.eq(sample),
                converter.source.connect(fifo.sink),
                self.txdat.eq(fifo.source.data),
                fifo.source.ready.eq(self.txpop),
                level.eq(fifo.level & ~0x3),
                self.next_len.eq(Mux(level >= n, n, level)),
                self.txcork.eq(0),
            ]
            return

        # Micro-frame buffers.
        mem0            = Signal(8*n)
        mem1            = Signal(8*n)
        write_ptr0      = Signal(12)
        write_ptr1      = Signal(12)
        store_state     = Signal()
        switch_active   = Signal()
        switch_complete = Signal()
        p_sample_ready  = Signal()
        self.sync += [
            If(self.sof_rise,
                switch_active.eq(1),
            ),
            p_sample_ready.eq(sample_ready),
            If(sample_ready & ~p_sample_ready,
                store_state.eq(1),
            ),
            switch_complete.eq(0),
            If(store_state,
                If(write_ptr0 != n,
                    mem0.eq(Cat(mem0[32:], sample)),
                    write_ptr0.eq(write_ptr0 + 4),
                ),
                store_state.eq(0),
            ).Elif(switch_active,
                write_ptr1.eq(write_ptr0),
                If(write_ptr0 != n,
                    mem1.eq(Cat(mem0[32:], Constant(0, 32))),
                ).Else(
                    mem1.eq(mem0),
                ),
                write_ptr0.eq(0),
                switch_active.eq(0),
                switch_complete.eq(1),
            ),
            If(switch_complete,
                self.txdat.eq(mem1[:8]),
                mem1.eq(Cat(mem1[8:], Constant(0, 8))),
                self.txdat_len.eq(Mux(write_ptr1 >= (n - 4), write_ptr1, 0)),
                self.txcork.eq(0),
            ).Elif(self.txpop,
                self.txdat.eq(mem1[:8]),
                mem1.eq(Cat(mem1[8:], Constant(0, 8))),
            )
        ]

# CDC UART -----------------------------------------------------------------------------------------

class CDCUART(LiteXModule):
    """
    CDC-ACM <-> UART bridge (replaces uart.v/uart_rx.vhd/uart_tx.vhd and the Gowin divider).

    8N1 UART at the baudrate set by the host (SET_LINE_CODING), LiteX RS232PHYTX/RX with a
    tuning word computed from the baudrate: baudrate * 2**32 / sys_clk_freq.
    """
    def __init__(self, sys_clk_freq=60e6, fifo_depth=16):
        self.reset    = Signal()
        self.baudrate = Signal(32)
        # USB -> UART (EP3 OUT).
        self.tx_data  = Signal(8)
        self.tx_valid = Signal()
        self.tx_ready = Signal()
        # UART -> USB (EP3 IN).
        self.rx_data  = Signal(8)
        self.rx_valid = Signal()
        # UART pins.
        self.txd      = Signal(reset=1)
        self.rxd      = Signal()

        # # #

        # Tuning word (fixed point: 2**48/sys_clk_freq with 16 fractional bits).
        # Note: explicit 64-bit product (Verilog would evaluate the product in the 32-bit context of
        # tuning_word, before the shift).
        k           = round(2**48/sys_clk_freq)
        product     = Signal(64)
        tuning_word = Signal(32)
        self.comb  += product.eq(self.baudrate*k)
        self.sync  += tuning_word.eq(product[16:48])

        # PHY.
        pads = Record([("tx", 1), ("rx", 1)])
        self.comb += [
            self.txd.eq(pads.tx),
            pads.rx.eq(self.rxd),
        ]
        self.tx = tx = ResetInserter()(RS232PHYTX(pads, tuning_word))
        self.rx = rx = ResetInserter()(RS232PHYRX(pads, tuning_word))
        self.comb += [tx.reset.eq(self.reset), rx.reset.eq(self.reset)]

        # USB -> UART (buffered, ready while the FIFO is not almost full).
        self.fifo = fifo = ResetInserter()(stream.SyncFIFO([("data", 8)], fifo_depth, buffered=False))
        self.comb += [
            fifo.reset.eq(self.reset),
            fifo.sink.valid.eq(self.tx_valid),
            fifo.sink.data.eq(self.tx_data),
            self.tx_ready.eq(fifo.level < (fifo_depth - 4)),
            fifo.source.connect(tx.sink),
        ]

        # UART -> USB.
        self.comb += [
            rx.source.ready.eq(1),
            self.rx_valid.eq(rx.source.valid),
            self.rx_data.eq(rx.source.data),
        ]

# UVC Video ----------------------------------------------------------------------------------------

class UVCVideo(LiteXModule):
    """
    UVC isochronous video IN endpoint (port of the UVC part of usbuvcuart_top.v, extended with
    integer upscaling and high-bandwidth transfers).

    "video" domain (hClk): the LCD copy (160x144 RGB666, 1 pixel every 3 clocks while enable) is
    captured in ping-pong line buffers.
    "sys" domain (60MHz UTMI clock): lines are replayed at the committed frame geometry (160x144
    or 320x288: each pixel/line repeated, so YUY2 chroma pairs are single source pixels), converted
    to YCbCr, packed as YUYV in a FIFO and sent with one or two (high-bandwidth, alt setting >= 2)
    1024-byte transactions per micro-frame (12-byte UVC header per micro-frame payload transfer).
    """
    def __init__(self, frames=VIDEO_FRAMES, n_buffers=4, fifo_depth=4096, luna=False):
        assert all((w % UVC_WIDTH == 0) and (h % UVC_HEIGHT == 0) and (w//UVC_WIDTH == h//UVC_HEIGHT)
            for w, h in frames)
        max_scale = max(w//UVC_WIDTH for w, h in frames)
        # The FIFO must be able to buffer 2 transactions (+ header) with room for a whole line.
        assert fifo_depth - (2*UVC_WIDTH*max_scale + 64) >= 2*UVC_PACKET_SIZE - UVC_HEADER_SIZE
        self.reset       = Signal() # sys.
        # Video input ("video" domain).
        self.line_valid  = Signal() # Unused.
        self.enable      = Signal()
        self.frame_valid = Signal()
        self.data        = Signal(18) # {B, G, R}, 6-bit each.
        # Control (sys).
        self.frame_index = Signal(8, reset=1) # Committed bFrameIndex.
        self.hbw         = Signal()           # High-bandwidth alternate setting selected.
        # USB (sys).
        self.sof         = Signal()
        self.txact       = Signal()
        self.txpop       = Signal()
        self.txdat       = Signal(8)
        self.txdat_len   = Signal(12)
        self.txcork      = Signal()
        self.txiso_pid   = Signal(4, reset=0b0011) # DATA1 (0b1011) when a 2nd transaction follows.
        self.next_len    = Signal(12) # Length of the next micro-frame transfer (latched at SOF).
        self.sof_rise    = Signal() # SOF rising edge (also used by the UAC endpoint).
        # Statistics (sys).
        self.hbw_count   = Signal(16) # 2nd transactions sent.
        self.frame_count = Signal(16) # Frames sent (EOF).
        self.max_level   = Signal(13) # Maximum FIFO level.
        self.drop_count  = Signal(16) # Dropped lines (video domain).
        self.skip_count  = Signal(16) # Skipped frames (video domain).
        self.start_count = Signal(16) # Frame starts (replay).

        # # #

        # Line Capture (video) ---------------------------------------------------------------------
        # Pixels are captured at the middle of their 3 clocks into one of n_buffers line buffers;
        # a complete line is published with a toggle (and freed by the replay with another toggle)
        # so only single-bit signals cross clock domains.
        sel_bits   = log2_int(n_buffers)
        line_bits  = log2_int(UVC_WIDTH, need_pow2=False)
        stride     = 2**line_bits
        storage    = Memory(18, n_buffers*stride)
        wrport     = storage.get_port(write_capable=True, clock_domain="video")
        rdport     = storage.get_port(clock_domain="sys")
        self.specials += storage, wrport, rdport

        w_phase     = Signal(2)
        w_sel       = Signal(sel_bits)
        w_idx       = Signal(line_bits + 1)
        w_drop      = Signal()                # Line dropped (buffer still in use).
        w_tog       = Signal(n_buffers)       # Published lines.
        w_frame_tog = Signal()                # Frame starts.
        w_enable_d  = Signal()
        w_fv_d      = Signal()
        w_skip      = Signal()                # Frame skipped (replay still busy with the previous one).
        w_broken    = Signal()                # Frame with dropped lines (replay will never complete it).
        r_tog       = Signal(n_buffers)       # Consumed lines (sys).
        r_tog_v     = Signal(n_buffers)
        r_busy      = Signal()                # Replay busy (sys).
        r_busy_v    = Signal()
        self.specials += [
            MultiReg(r_tog,  r_tog_v,  odomain="video"),
            MultiReg(r_busy, r_busy_v, odomain="video"),
        ]
        self.comb += [
            wrport.adr.eq(Cat(w_idx[:line_bits], w_sel)),
            wrport.dat_w.eq(self.data),
            wrport.we.eq(self.enable & (w_phase == 1) & (w_idx < UVC_WIDTH) & ~w_drop & ~w_skip),
        ]
        self.sync.video += [
            w_enable_d.eq(self.enable),
            w_fv_d.eq(self.frame_valid),
            If(self.frame_valid & ~w_fv_d,
                # Frame start (skipped when the previous frame is still being sent, ex when the USB
                # bandwidth is lower than the video rate).
                If(r_busy_v & ~w_broken,
                    w_skip.eq(1),
                    self.skip_count.eq(self.skip_count + 1),
                ).Else(
                    # Accepted (the replay realigns on it, also aborting a broken frame).
                    w_skip.eq(0),
                    w_broken.eq(0),
                    w_frame_tog.eq(~w_frame_tog),
                ),
                w_sel.eq(0),
                w_idx.eq(0),
                w_phase.eq(0),
                w_drop.eq(0),
            ).Elif(self.enable,
                If(~w_enable_d & ~w_skip,
                    # Line start: drop it if its buffer has not been replayed yet.
                    If(((w_tog ^ r_tog_v) >> w_sel)[0],
                        w_drop.eq(1),
                        w_broken.eq(1),
                        self.drop_count.eq(self.drop_count + 1),
                    ).Else(
                        w_drop.eq(0),
                    )
                ),
                If(w_phase == 2,
                    w_phase.eq(0),
                    If(w_idx < UVC_WIDTH, w_idx.eq(w_idx + 1)),
                ).Else(
                    w_phase.eq(w_phase + 1),
                )
            ).Elif(w_enable_d,
                # Line end: publish it.
                If(~w_drop & ~w_skip,
                    w_tog.eq(w_tog ^ (1 << w_sel)),
                    w_sel.eq(w_sel + 1),
                ),
                w_idx.eq(0),
                w_phase.eq(0),
            )
        ]

        # Line Replay (sys) ------------------------------------------------------------------------
        w_tog_s   = Signal(n_buffers)
        frame_s   = Signal()
        frame_d   = Signal()
        self.specials += [
            MultiReg(w_tog,       w_tog_s, odomain="sys"),
            MultiReg(w_frame_tog, frame_s, odomain="sys"),
        ]
        frame_start = Signal()
        self.sync += frame_d.eq(frame_s)
        self.comb += frame_start.eq(frame_s != frame_d)

        # Frame geometry, latched at frame start.
        scale      = Signal(max=max_scale + 1, reset=1)
        width      = Signal(10, reset=UVC_WIDTH)
        height     = Signal(10, reset=UVC_HEIGHT)
        scale_next = Signal(max=max_scale + 1)
        self.comb += Case(self.frame_index, {
            **{i: scale_next.eq(w//UVC_WIDTH) for i, (w, h) in enumerate(frames, start=1)},
            "default": scale_next.eq(frames[0][0]//UVC_WIDTH),
        })

        r_sel      = Signal(sel_bits)
        r_px       = Signal(line_bits + 1)
        r_dup      = Signal(max=max(max_scale, 2))
        r_pass     = Signal(max=max(max_scale, 2))
        r_phase    = Signal(2)
        r_lines    = Signal(10)
        r_gap      = Signal(5)
        r_enable   = Signal()
        v_frame_valid = Signal()
        v_enable      = Signal()
        v_data        = Signal(18)
        line_ready = Signal()
        fifo_room  = Signal()
        self.comb += line_ready.eq(((w_tog_s ^ r_tog) >> r_sel)[0])

        last_packet = Signal() # Last data of the frame buffered (packetizer).
        eof_pending = Signal() # Frame end not sent yet (until the EOF transfer is done).
        R_IDLE, R_LEAD, R_SETTLE, R_WAIT, R_PIXELS, R_GAP = range(6)
        r_state = Signal(3)
        self.comb += [
            r_enable.eq(r_state == R_PIXELS),
            r_busy.eq(r_state != R_IDLE),
        ]
        self.sync += [
            If(self.reset,
                r_state.eq(R_IDLE),
                v_frame_valid.eq(0),
                r_tog.eq(0),
            ).Elif(frame_start,
                # Realign to the source frame (skip the lines of the previous one): frame_valid is
                # low for 16 clocks (lead), then high until all the lines are sent.
                r_state.eq(R_LEAD),
                v_frame_valid.eq(0),
                r_tog.eq(w_tog_s),
                r_sel.eq(0),
                r_lines.eq(0),
                r_pass.eq(0),
                r_gap.eq(0),
                scale.eq(scale_next),
                width.eq(UVC_WIDTH*scale_next),
                height.eq(UVC_HEIGHT*scale_next),
            ).Else(
                Case(r_state, {
                    R_LEAD: [
                        # Wait for the previous frame end to be sent (the FIFO is reset at the
                        # start of the frame).
                        If(r_gap != 15, r_gap.eq(r_gap + 1)),
                        If((r_gap == 15) & ~eof_pending,
                            r_gap.eq(0),
                            v_frame_valid.eq(1),
                            r_state.eq(R_SETTLE),
                        )
                    ],
                    R_SETTLE: [
                        # Let the FIFO reset (on the frame_valid rising edge) complete.
                        r_gap.eq(r_gap + 1),
                        If(r_gap == 15,
                            r_gap.eq(0),
                            r_state.eq(R_WAIT),
                        )
                    ],
                    R_WAIT: [
                        If(r_lines == height,
                            v_frame_valid.eq(0),
                            r_state.eq(R_IDLE),
                        ).Elif(line_ready & fifo_room,
                            r_px.eq(0),
                            r_dup.eq(0),
                            r_phase.eq(0),
                            r_state.eq(R_PIXELS),
                        )
                    ],
                    R_PIXELS: [
                        r_phase.eq(r_phase + 1),
                        If(r_phase == 2,
                            r_phase.eq(0),
                            r_dup.eq(r_dup + 1),
                            If(r_dup == (scale - 1),
                                r_dup.eq(0),
                                r_px.eq(r_px + 1),
                                If(r_px == (UVC_WIDTH - 1),
                                    r_state.eq(R_GAP),
                                )
                            )
                        )
                    ],
                    R_GAP: [
                        # Blanking: ends the line for the packer, lets the CSC pipeline drain.
                        r_gap.eq(r_gap + 1),
                        If(r_gap == 15,
                            r_gap.eq(0),
                            r_lines.eq(r_lines + 1),
                            r_pass.eq(r_pass + 1),
                            If(r_pass == (scale - 1),
                                # Line replayed scale times: free its buffer.
                                r_pass.eq(0),
                                r_tog.eq(r_tog ^ (1 << r_sel)),
                                r_sel.eq(r_sel + 1),
                            ),
                            r_state.eq(R_WAIT),
                        )
                    ],
                })
            )
        ]
        # Synchronous read: data of pixel r_px available 1 clock later, enable delayed to match.
        self.comb += rdport.adr.eq(Cat(r_px[:line_bits], r_sel))
        self.sync += v_enable.eq(r_enable)
        self.comb += v_data.eq(rdport.dat_r)

        # Video: Color Space Conversion ------------------------------------------------------------
        csc = ColorSpaceConvertor(clock_pins=False)
        self.csc = csc
        self.comb += [
            csc.I_rst_n.eq(~self.reset),
            csc.I_din0.eq(Cat(Constant(0, 2), v_data[0:6])),   # R.
            csc.I_din1.eq(Cat(Constant(0, 2), v_data[6:12])),  # G.
            csc.I_din2.eq(Cat(Constant(0, 2), v_data[12:18])), # B.
            csc.I_dinvalid.eq(v_enable),
        ]
        y_enable  = csc.O_doutvalid
        y, cb, cr = csc.O_dout0, csc.O_dout1, csc.O_dout2
        # Frame valid delayed to match the CSC latency.
        y_frame_valid = Signal()
        fv_sr         = Signal(CSC_LATENCY + 1)
        self.sync += [
            If(self.reset,
                fv_sr.eq(0),
            ).Else(
                fv_sr.eq(Cat(v_frame_valid, fv_sr[:-1])),
            )
        ]
        self.comb += y_frame_valid.eq(fv_sr[-1])

        # Video: YUYV Packing ----------------------------------------------------------------------
        y_frame_valid_r1 = Signal()
        y_enable_r1      = Signal()
        h_sof            = Signal()
        count_x          = Signal(10)
        count_y          = Signal(10)
        h_image_eof      = Signal()
        count3           = Signal(3)
        self.sync += [
            y_frame_valid_r1.eq(y_frame_valid),
            y_enable_r1.eq(y_enable),
            If(~y_enable,
                count3.eq(0b001),
            ).Else(
                count3.eq(Cat(count3[2], count3[0:2])),
            ),
        ]
        self.comb += h_sof.eq(y_frame_valid & ~y_frame_valid_r1)
        self.sync += [
            If(h_sof,
                count_x.eq(0),
                count_y.eq(0),
                h_image_eof.eq(0),
            ).Else(
                h_image_eof.eq(0),
                If(y_enable,
                    If(count3[2],
                        count_x.eq(count_x + 1),
                    )
                ).Elif(y_enable_r1,
                    count_y.eq(count_y + 1),
                    If(count_y == (height - 1),
                        h_image_eof.eq(1),
                    ),
                    count_x.eq(0),
                )
            )
        ]
        # For each pair of pixels: P1 P1 P1 P2 P2 P2 -> Y1 MU MV Us Y2 Vs.
        vnu       = count_x[0]
        can_write = Signal()
        h_enable0 = Signal()
        h_enable1 = Signal()
        h_enable2 = Signal()
        store_u   = Signal()
        store_v   = Signal()
        mu        = Signal(8)
        mv        = Signal(8)
        fram_d    = Signal(8)
        # Note: explicit 9-bit sums (Verilog would evaluate (mu + cb) >> 1 in the 8-bit context of
        # fram_d, losing the carry).
        sum_u     = Signal(9)
        sum_v     = Signal(9)
        self.comb += [
            sum_u.eq(mu + cb),
            sum_v.eq(mv + cr),
            can_write.eq((count_x < width) & y_enable),
            h_enable2.eq(count3[2] & vnu & can_write),
            h_enable1.eq(count3[0] & vnu & can_write),
            h_enable0.eq(((count3[0] & ~vnu) | (count3[1] & vnu)) & can_write),
            store_u.eq(count3[1] & ~vnu & can_write),
            store_v.eq(count3[2] & ~vnu & can_write),
            If(h_enable0,
                fram_d.eq(y),
            ).Elif(h_enable1,
                fram_d.eq(sum_u[1:9]),
            ).Elif(h_enable2,
                fram_d.eq(sum_v[1:9]),
            ),
        ]
        self.sync += [
            If(store_u, mu.eq(cb)),
            If(store_v, mv.eq(cr)),
        ]

        # FIFO -------------------------------------------------------------------------------------
        fifo = VideoFIFO(depth=fifo_depth, clock_pins=False)
        fifo = ClockDomainsRenamer({"write": "sys", "read": "sys"})(fifo)
        self.fifo = fifo
        rden = Signal()
        self.comb += [
            fifo.Data.eq(fram_d),
            fifo.Reset.eq(self.reset | h_sof),
            fifo.WrEn.eq(h_enable0 | h_enable1 | h_enable2),
            fifo.RdEn.eq(rden),
            fifo.AlmostFullTh.eq(UVC_PACKET_SIZE - UVC_HEADER_SIZE),
            # Room for a whole output line (+ margin for the level latency).
            fifo_room.eq(fifo.Rnum <= (fifo_depth - 2*UVC_WIDTH*max_scale - 64)),
        ]

        # USB: Packetizer (sys) --------------------------------------------------------------------
        txact_d0     = Signal()
        txact_d1     = Signal()
        txact_fall   = Signal()
        state        = Signal(3, reset=0b001) # IDLE (001), UNCORK (010), TXACTIVE (100).
        last_read    = Signal()
        read_active  = Signal()
        second       = Signal() # 2nd transaction of the micro-frame (payload only).
        two          = Signal() # 2 transactions in this micro-frame.
        byte_count   = Signal(11)
        pts_counter  = Signal(32)
        pts_reg      = Signal(32)
        frame        = Signal(8, reset=0x8c)
        sof_counts   = Signal(11)
        sof_1ms      = Signal(4)
        sof_d0       = Signal()
        sof_d1       = Signal()
        eof_sr       = Signal(4)
        len_m1       = Signal(32)
        preload      = Signal()
        DATA0, DATA1 = 0b0011, 0b1011
        IDLE, UNCORK, TXACTIVE = 0b001, 0b010, 0b100
        # 2 transactions: header + 1012 bytes, then 1024 bytes (only when all are buffered).
        two_bytes    = 2*UVC_PACKET_SIZE - UVC_HEADER_SIZE

        # Micro-frame transfer decision (latched at SOF). Note: the FIFO almost full triggers at 1012
        # bytes, while 1011 are needed for a full packet (one extra byte is always kept at the FIFO
        # output). High-bandwidth: 2 transactions (Gowin controller: 2 x 1024 with a re-armed 2nd
        # transaction, LUNA: a single 2048-byte transfer split in DATA1/DATA0 packets by LUNA).
        next_two         = Signal()
        next_last_read   = Signal()
        next_read_active = Signal()
        self.comb += [
            next_two.eq(0),
            If(self.hbw & (fifo.Rnum >= two_bytes),
                next_two.eq(1),
                self.next_len.eq(2*UVC_PACKET_SIZE if luna else UVC_PACKET_SIZE),
                next_last_read.eq(0),
                next_read_active.eq(1),
            ).Elif(fifo.Almost_Full,
                self.next_len.eq(UVC_PACKET_SIZE),
                next_last_read.eq(0),
                next_read_active.eq(1),
            ).Elif(last_packet,
                self.next_len.eq(fifo.Rnum[:12] + UVC_HEADER_SIZE),
                next_last_read.eq(1),
                next_read_active.eq(1),
            ).Else(
                self.next_len.eq(UVC_HEADER_SIZE),
                next_last_read.eq(0),
                next_read_active.eq(0),
            ),
        ]
        self.comb += txact_fall.eq(txact_d1 & ~txact_d0)
        self.sync += [
            If(self.reset,
                txact_d0.eq(0),
                txact_d1.eq(0),
            ).Else(
                txact_d0.eq(self.txact),
                txact_d1.eq(txact_d0),
            )
        ]
        self.sync += [
            preload.eq(0),
            If(self.reset,
                state.eq(IDLE),
                self.txiso_pid.eq(DATA0),
                second.eq(0),
            ).Elif(self.sof,
                self.txdat_len.eq(self.next_len),
                self.txiso_pid.eq(Mux(next_two & ~luna, DATA1, DATA0)),
                two.eq(next_two & ~luna),
                last_read.eq(next_last_read),
                read_active.eq(next_read_active),
                second.eq(0),
                self.txcork.eq(0),
                state.eq(UNCORK),
            ).Elif(self.txact & (state == UNCORK),
                state.eq(TXACTIVE),
            ).Elif(~self.txact & (state == TXACTIVE),
                If(two & ~second,
                    # Re-arm for the 2nd transaction (payload only, 1st byte preloaded).
                    self.txdat_len.eq(UVC_PACKET_SIZE),
                    self.txiso_pid.eq(DATA0),
                    second.eq(1),
                    preload.eq(1),
                    state.eq(UNCORK),
                ).Else(
                    state.eq(IDLE),
                )
            )
        ]
        self.sync += [
            If(state != TXACTIVE,
                byte_count.eq(0),
            ).Elif(self.txpop,
                byte_count.eq(byte_count + 1),
            )
        ]
        self.sync += [
            If(self.reset,
                pts_counter.eq(0),
            ).Else(
                pts_counter.eq(pts_counter + 1),
            ),
            If(self.reset,
                frame.eq(0x8c),
                pts_reg.eq(0),
            ).Elif(txact_fall & last_read,
                frame.eq(Cat(~frame[0], frame[1:8])),
                pts_reg.eq(pts_counter),
            ),
        ]
        last_data = (self.txdat_len - 1) if luna else (UVC_PACKET_SIZE - 1) # Last byte: no read.
        self.comb += If(second,
            rden.eq(preload | (self.txpop & (byte_count < (UVC_PACKET_SIZE - 1)))),
        ).Else(
            rden.eq(self.txpop & (byte_count >= (UVC_HEADER_SIZE - 1)) &
                                 (byte_count <  last_data) & read_active),
        )

        # End of image.
        p_image_eof = Signal()
        self.sync += eof_sr.eq(Cat(h_image_eof, eof_sr[:3]))
        self.comb += p_image_eof.eq(eof_sr[2:4] == 0b01)
        self.sync += [
            If(p_image_eof,
                last_packet.eq(1),
            ).Elif(~self.sof & last_read,
                last_packet.eq(0),
            )
        ]
        self.sync += [
            If(self.txact & (state == UNCORK) & second, self.hbw_count.eq(self.hbw_count + 1)),
            If(txact_fall & last_read, self.frame_count.eq(self.frame_count + 1)),
        ]
        self.sync += [
            If(fifo.Rnum > self.max_level, self.max_level.eq(fifo.Rnum)),
            If(frame_start, self.start_count.eq(self.start_count + 1)),
        ]
        self.sync += [
            If(self.reset,
                eof_pending.eq(0),
            ).Elif(p_image_eof,
                eof_pending.eq(1),
            ).Elif(txact_fall & last_read,
                eof_pending.eq(0),
            )
        ]

        # SOF counter (1ms frames = 8 micro-frames).
        sof_rise = self.sof_rise
        self.comb += sof_rise.eq(sof_d0 & ~sof_d1)
        self.sync += [
            If(self.reset,
                sof_d0.eq(0),
                sof_d1.eq(0),
                sof_counts.eq(0),
                sof_1ms.eq(0),
            ).Else(
                sof_d0.eq(self.sof),
                sof_d1.eq(sof_d0),
                If(sof_rise,
                    If(sof_1ms >= 7,
                        sof_1ms.eq(0),
                        sof_counts.eq(sof_counts + 1),
                    ).Else(
                        sof_1ms.eq(sof_1ms + 1),
                    )
                )
            )
        ]

        # Packet data: UVC payload header (1st transaction), then FIFO data.
        self.comb += len_m1.eq(self.txdat_len - 1)
        header = {
            1: pts_reg[0:8],   2: pts_reg[8:16],  3: pts_reg[16:24], 4: pts_reg[24:32], # dwPresentationTime.
            5: pts_reg[0:8],   6: pts_reg[8:16],  7: pts_reg[16:24], 8: pts_reg[24:32], # SCR: STC.
            9: sof_counts[0:8], 10: Cat(sof_counts[8:11], Constant(0, 5)),              # SCR: SOF.
        }
        self.sync += [
            If(self.sof,
                self.txdat.eq(UVC_HEADER_SIZE), # bHeaderLength.
            ).Elif(preload,
                self.txdat.eq(fifo.Q),
            ).Elif(self.txpop,
                If(second,
                    self.txdat.eq(fifo.Q),
                ).Else(
                    Case(byte_count, {
                        0: self.txdat.eq(Mux(last_read, frame | 0x02, frame)), # bmHeaderInfo (EOF on last).
                        **{n: self.txdat.eq(v) for n, v in header.items()},
                        "default": If(byte_count >= len_m1,
                            self.txdat.eq(UVC_HEADER_SIZE),
                        ).Else(
                            self.txdat.eq(fifo.Q),
                        ),
                    })
                )
            )
        ]

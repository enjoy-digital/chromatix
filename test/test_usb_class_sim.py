#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
Behavioral simulations of the USB class logic (usb_class.py): control requests (SETUP -> data
stage), interface alternate settings, UAC/UVC isochronous endpoints and the CDC UART.
"""

import random

import pytest

from migen import *

from litex.gen import *
from litex.gen.sim import run_simulation

from chromatix.gateware import usb_class
from chromatix.gateware.usb_class import *
from chromatix.gateware.usb import CSC_COEFFICIENTS, CSC_FRAC_BITS

# Control Transfers Helpers ------------------------------------------------------------------------

class ControlBench(LiteXModule):
    """USBSetupParser + class request handlers, EP0 IN data from the first active handler."""
    def __init__(self, uvc_frames=[(UVC_WIDTH, UVC_HEIGHT)]):
        self.setup = setup = USBSetupParser()
        self.uart  = CDCACMControl(setup)
        self.uvc   = UVCControl(setup, frames=uvc_frames)
        self.uac   = UACControl(setup)
        self.handlers = handlers = [self.uart, self.uvc, self.uac]
        self.reset = Signal()
        self.rxdat = Signal(8)
        self.rxval = Signal()
        self.rxact = Signal()
        self.txact = Signal()
        self.txpop = Signal()
        self.txval = Signal()
        self.txdat = Signal(8)
        self.txlen = Signal(12)

        # # #

        for m in [setup] + handlers:
            self.comb += [
                m.reset.eq(self.reset),
                m.rxdat.eq(self.rxdat),
                m.rxval.eq(self.rxval),
                m.rxact.eq(self.rxact),
                m.txpop.eq(self.txpop),
            ]
        self.comb += setup.txact.eq(self.txact)
        mux = None
        for h in handlers:
            stmt = [self.txdat.eq(h.txdat), self.txlen.eq(h.txdat_len)]
            mux  = If(h.txval, *stmt) if mux is None else mux.Elif(h.txval, *stmt)
        self.comb += [mux, self.txval.eq(Reduce("OR", [h.txval for h in handlers]))]

def setup_packet(bmRequestType, bRequest, wValue, wIndex, wLength):
    return [bmRequestType, bRequest, *byte_list(wValue), *byte_list(wIndex), *byte_list(wLength)]

def byte_list(v):
    return [v & 0xff, (v >> 8) & 0xff]

def send_setup(dut, packet):
    """SETUP stage (EP0): 8 bytes with setup_active."""
    s = dut.setup
    yield s.endpt.eq(EP_CTRL)
    yield s.setup_active.eq(1)
    for b in packet:
        yield dut.rxdat.eq(b)
        yield dut.rxval.eq(1)
        yield
        yield dut.rxval.eq(0)
        yield
    yield s.setup_active.eq(0)
    for _ in range(4):
        yield

def pop(dut, n):
    """n back-to-back txpop, txdat sampled with txpop (next byte loaded on txpop)."""
    data = []
    yield dut.txpop.eq(1)
    yield
    for i in range(n):
        if i == n - 1:
            yield dut.txpop.eq(0)
        data.append((yield dut.txdat))
        yield
    return data

def data_in(dut, n):
    """IN data stage: txact then n txpop. Returns (txval, txlen, data)."""
    txval = (yield dut.txval)
    txlen = (yield dut.txlen)
    yield dut.txact.eq(1)
    yield
    data = yield from pop(dut, n)
    yield
    yield dut.txact.eq(0)
    for _ in range(4):
        yield
    return txval, txlen, data

def data_out(dut, data):
    """OUT data stage: rxact with a rxval per byte."""
    yield dut.rxact.eq(1)
    yield
    for b in data:
        yield dut.rxdat.eq(b)
        yield dut.rxval.eq(1)
        yield
        yield dut.rxval.eq(0)
        yield
    yield dut.rxact.eq(0)
    for _ in range(4):
        yield

def reset(dut):
    yield dut.reset.eq(1)
    yield
    yield dut.reset.eq(0)
    yield

# Setup Parser -------------------------------------------------------------------------------------

def test_setup_parser_header_and_data_stage():
    """SETUP header fields are captured, cdata_ofs counts data stage bytes and the parser returns
    idle (header_ready = 0) at the end of the data stage or right after a no-data request."""
    dut = ControlBench()
    s   = dut.setup
    res = {}

    def gen():
        yield from reset(dut)
        res["idle"] = (yield s.header_ready)
        yield from send_setup(dut, setup_packet(0xa1, 0x81, 0x0100, 0x0001, 0x0022))
        res["fields"] = [(yield s.header_ready), (yield s.bmRequestType), (yield s.bRequest),
            (yield s.wValue), (yield s.wIndex), (yield s.wLength), (yield s.cdata_ofs)]
        yield dut.txact.eq(1)
        yield
        for _ in range(10):
            yield dut.txpop.eq(1)
            yield
        yield dut.txpop.eq(0)
        yield
        res["ofs"]   = (yield s.cdata_ofs)
        res["ready"] = (yield s.header_ready)
        yield dut.txact.eq(0)
        yield
        yield
        res["done"] = (yield s.header_ready)
        # No-data request (SET_CONTROL_LINE_STATE): header released right after the SETUP stage.
        yield from send_setup(dut, setup_packet(0x21, SET_CONTROL_LINE_STATE, 0x0003, 2, 0))
        res["nodata"] = (yield s.header_ready)

    run_simulation(dut, gen())
    assert res["idle"] == 0
    assert res["fields"] == [1, 0xa1, 0x81, 0x0100, 0x0001, 0x0022, 0]
    assert res["ofs"]    == 10
    assert res["ready"]  == 1
    assert res["done"]   == 0
    assert res["nodata"] == 0

# CDC-ACM Control ----------------------------------------------------------------------------------

def test_cdc_acm_line_coding():
    """GET_LINE_CODING returns the reset line coding (115200 8N1), SET_LINE_CODING updates it and
    SET_CONTROL_LINE_STATE sets DTR/RTS."""
    dut = ControlBench()
    res = {}

    def gen():
        yield from reset(dut)
        # GET_LINE_CODING (reset values).
        yield from send_setup(dut, setup_packet(0xa1, GET_LINE_CODING, 0, UART_CTRL_IFACE, 7))
        res["get0"] = yield from data_in(dut, 7)
        # SET_LINE_CODING: 1000000 baud, 2 stop bits, even parity, 7 data bits.
        yield from send_setup(dut, setup_packet(0x21, SET_LINE_CODING, 0, UART_CTRL_IFACE, 7))
        yield from data_out(dut, [0x40, 0x42, 0x0f, 0x00, 2, 2, 7])
        res["line"] = [(yield dut.uart.dte_rate), (yield dut.uart.char_format),
            (yield dut.uart.parity_type), (yield dut.uart.data_bits)]
        yield from send_setup(dut, setup_packet(0xa1, GET_LINE_CODING, 0, UART_CTRL_IFACE, 7))
        res["get1"] = yield from data_in(dut, 7)
        # SET_CONTROL_LINE_STATE (DTR=1, RTS=0).
        yield from send_setup(dut, setup_packet(0x21, SET_CONTROL_LINE_STATE, 0x0001, UART_CTRL_IFACE, 0))
        res["ctl"] = (yield dut.uart.ctl_sig)
        # Request to another interface is ignored.
        yield from send_setup(dut, setup_packet(0x21, SET_CONTROL_LINE_STATE, 0x0003, 0, 0))
        res["ctl_other"] = (yield dut.uart.ctl_sig)
        res["txval_end"] = (yield dut.txval)

    run_simulation(dut, gen())
    assert res["get0"] == (1, 7, [0x00, 0xc2, 0x01, 0x00, 0, 0, 8])
    assert res["line"] == [1000000, 2, 2, 7]
    assert res["get1"] == (1, 7, [0x40, 0x42, 0x0f, 0x00, 2, 2, 7])
    assert res["ctl"]       == 0b01
    assert res["ctl_other"] == 0b01
    assert res["txval_end"] == 0

# UVC Control --------------------------------------------------------------------------------------

def uvc_probe():
    le = lambda v, n: [(v >> 8*i) & 0xff for i in range(n)]
    return ([0, 0, 1, 1] + le(UVC_FRAME_INTERVAL, 4) + [0]*10 + le(UVC_MAX_FRAME_SIZE, 4) +
        le(UVC_PAYLOAD_SIZE, 4) + le(60_000_000, 4) + [0]*4)

def test_uvc_probe_control():
    """GET_CUR/GET_MAX on VS_PROBE_CONTROL return the 34-byte probe structure (length limited by
    wLength); other selectors/interfaces are not answered."""
    dut = ControlBench()
    res = {}

    def gen():
        yield from reset(dut)
        yield from send_setup(dut, setup_packet(0xa1, UVC_GET_CUR, UVC_VS_PROBE_CONTROL << 8, UVC_VS_INTERFACE, 34))
        res["cur"] = yield from data_in(dut, 34)
        yield from send_setup(dut, setup_packet(0xa1, UVC_GET_MAX, UVC_VS_PROBE_CONTROL << 8, UVC_VS_INTERFACE, 26))
        res["max26"] = yield from data_in(dut, 26)
        yield from send_setup(dut, setup_packet(0xa1, UVC_GET_DEF, UVC_VS_PROBE_CONTROL << 8, UVC_VS_INTERFACE, 64))
        res["def64"] = (yield dut.txval), (yield dut.txlen)
        yield from data_in(dut, 34)
        # Commit control (selector 2): not handled.
        yield from send_setup(dut, setup_packet(0xa1, UVC_GET_CUR, 0x02 << 8, UVC_VS_INTERFACE, 34))
        res["commit"] = (yield dut.txval)

    run_simulation(dut, gen())
    probe = uvc_probe()
    assert res["cur"]   == (1, 34, probe)
    assert res["max26"] == (1, 26, probe[:26])
    assert res["def64"] == (1, 34)
    assert res["commit"] == 0

def test_uvc_probe_commit_frames():
    """With several frames: GET_DEF returns the default frame (1: 320x288, 2 transactions),
    SET_CUR(PROBE) selects the probed frame returned by GET_CUR (frame 2: 160x144, 1 transaction),
    SET_CUR(COMMIT) sets frame_index, invalid frame indexes are ignored."""
    from chromatix.gateware.usb_desc import VIDEO_FRAMES
    dut = ControlBench(uvc_frames=VIDEO_FRAMES)
    res = {}

    def probe(index):
        p = [0, 0, 1, index] + [0]*30
        return p

    def gen():
        yield from reset(dut)
        res["frame_reset"] = (yield dut.uvc.frame_index)
        yield from send_setup(dut, setup_packet(0xa1, UVC_GET_DEF, UVC_VS_PROBE_CONTROL << 8, UVC_VS_INTERFACE, 34))
        res["def"] = yield from data_in(dut, 34)
        yield from send_setup(dut, setup_packet(0x21, UVC_SET_CUR, UVC_VS_PROBE_CONTROL << 8, UVC_VS_INTERFACE, 34))
        yield from data_out(dut, probe(2))
        yield from send_setup(dut, setup_packet(0xa1, UVC_GET_CUR, UVC_VS_PROBE_CONTROL << 8, UVC_VS_INTERFACE, 34))
        res["cur2"] = yield from data_in(dut, 34)
        yield from send_setup(dut, setup_packet(0xa1, UVC_GET_DEF, UVC_VS_PROBE_CONTROL << 8, UVC_VS_INTERFACE, 34))
        res["def_after"] = yield from data_in(dut, 34)
        res["frame_probe"] = (yield dut.uvc.frame_index) # Probe does not commit.
        yield from send_setup(dut, setup_packet(0x21, UVC_SET_CUR, UVC_VS_COMMIT_CONTROL << 8, UVC_VS_INTERFACE, 34))
        yield from data_out(dut, probe(2))
        res["frame_commit"] = (yield dut.uvc.frame_index)
        yield from send_setup(dut, setup_packet(0x21, UVC_SET_CUR, UVC_VS_COMMIT_CONTROL << 8, UVC_VS_INTERFACE, 34))
        yield from data_out(dut, probe(3)) # Invalid.
        res["frame_invalid"] = (yield dut.uvc.frame_index)
        yield from send_setup(dut, setup_packet(0x21, UVC_SET_CUR, UVC_VS_COMMIT_CONTROL << 8, UVC_VS_INTERFACE, 26))
        yield from data_out(dut, probe(1)[:26])
        res["frame_commit1"] = (yield dut.uvc.frame_index)

    run_simulation(dut, gen())
    le = lambda v, n: [(v >> 8*i) & 0xff for i in range(n)]
    def expected(index, width, height, transactions):
        return ([0, 0, 1, index] + le(UVC_FRAME_INTERVAL, 4) + [0]*10 + le(width*height*2, 4) +
            le(1024*transactions, 4) + le(60_000_000, 4) + [0]*4)
    assert res["frame_reset"] == 1
    assert res["def"]       == (1, 34, expected(1, 320, 288, 2))
    assert res["cur2"]      == (1, 34, expected(2, 160, 144, 1))
    assert res["def_after"] == (1, 34, expected(1, 320, 288, 2))
    assert res["frame_probe"]   == 1
    assert res["frame_commit"]  == 2
    assert res["frame_invalid"] == 2
    assert res["frame_commit1"] == 1

# UAC Control --------------------------------------------------------------------------------------

def test_uac_sampling_frequency_control():
    """Clock source SAM_FREQ CUR returns 44100 and RANGE a single [44100, 44100, 0] sub-range."""
    dut  = ControlBench()
    res  = {}
    wIdx = (UAC_CLOCK_ID << 8) | UAC_AC_INTERFACE

    def gen():
        yield from reset(dut)
        yield from send_setup(dut, setup_packet(0xa1, UAC_CUR_ATTR, CS_SAM_FREQ_CONTROL << 8, wIdx, 4))
        res["cur"] = yield from data_in(dut, 4)
        res["cur_end"] = (yield dut.txval)
        yield from send_setup(dut, setup_packet(0xa1, UAC_RANGE_ATTR, CS_SAM_FREQ_CONTROL << 8, wIdx, 14))
        res["range"] = yield from data_in(dut, 14)
        yield from send_setup(dut, setup_packet(0xa1, UAC_RANGE_ATTR, CS_SAM_FREQ_CONTROL << 8, wIdx, 2))
        res["range2"] = yield from data_in(dut, 2)
        # Other clock ID: not answered.
        yield from send_setup(dut, setup_packet(0xa1, UAC_CUR_ATTR, CS_SAM_FREQ_CONTROL << 8, (2 << 8) | UAC_AC_INTERFACE, 4))
        res["other"] = (yield dut.txval)

    run_simulation(dut, gen())
    freq = [44100 & 0xff, (44100 >> 8) & 0xff, 0, 0]
    assert res["cur"]     == (1, 4, freq)
    assert res["cur_end"] == 0
    assert res["range"]   == (1, 14, [1, 0] + freq + freq + [0]*4)
    assert res["range2"]  == (1, 2, [1, 0])
    assert res["other"]   == 0

# Interface Alternate Setting ----------------------------------------------------------------------

def test_interface_alt_select():
    """Alternate setting is loaded on update, held otherwise and cleared on reset."""
    dut = InterfaceAltSelect()
    out = []

    def gen():
        for reset, update, alt in [(1, 0, 0), (0, 0, 5), (0, 1, 1), (0, 0, 7), (0, 1, 2), (1, 1, 3), (0, 0, 0)]:
            yield dut.reset.eq(reset)
            yield dut.update.eq(update)
            yield dut.alt_i.eq(alt)
            yield
            yield
            out.append((yield dut.alt_o))

    run_simulation(dut, gen())
    assert out == [0, 0, 1, 1, 2, 0, 0]

# UAC Endpoint -------------------------------------------------------------------------------------

def test_uac_endpoint_packets():
    """Each micro-frame (sof_rise), the samples captured at 44.1kHz are sent as 20 or 24-byte
    packets of stereo 16-bit little-endian samples, in capture order."""
    # Reduced sys clock (6MHz): 136 cycles per sample, 750 cycles per micro-frame.
    dut     = UACEndpoint(sys_clk_freq=6e6)
    dut.cd_audio = ClockDomain(reset_less=True)
    packets = []

    # Audio samples: left increments on each audio clock, right = ~left.
    counter = Signal(16)
    dut.sync.audio += counter.eq(counter + 1)
    dut.comb += [dut.left.eq(counter), dut.right.eq(~counter)]

    def controller():
        for mframe in range(8):
            for _ in range(749):
                yield
            yield dut.sof_rise.eq(1)
            yield
            yield dut.sof_rise.eq(0)
            for _ in range(8):
                yield
            length = (yield dut.txdat_len)
            assert (yield dut.txcork) == 0
            packets.append((yield from pop(dut, length)))

    run_simulation(dut, controller(), clocks={"sys": 10, "audio": 70})
    lengths = [len(p) for p in packets[1:]]
    assert set(lengths) <= {20, 24} and 20 in lengths and 24 in lengths
    # 5.5125 samples per micro-frame on average.
    assert abs(sum(lengths)/4/len(lengths) - 44100/8000) < 0.5
    lefts = []
    for p in packets[1:]:
        for i in range(0, len(p), 4):
            left  = p[i + 0] | (p[i + 1] << 8)
            right = p[i + 2] | (p[i + 3] << 8)
            assert right == (~left & 0xffff)
            lefts.append(left)
    # Audio clock at 6MHz/7: ~19.4 audio clocks per sample.
    deltas = [(b - a) & 0xffff for a, b in zip(lefts, lefts[1:])]
    assert all(17 <= d <= 22 for d in deltas), deltas

# CDC UART -----------------------------------------------------------------------------------------

def test_cdc_uart_bit_timing():
    """TXD is 8N1 at the host baudrate (bits checked against a UART model) and a 8N1 frame on RXD
    is received, both at 3Mbaud (20 sys cycles per bit at 60MHz)."""
    dut        = CDCUART(sys_clk_freq=60e6)
    bit        = 20
    tx_data    = [0x5a, 0x01, 0xfe]
    rx_data    = [0xc3, 0x3c]
    tx_samples = []
    rx_out     = []

    def tx_gen():
        yield dut.baudrate.eq(3_000_000)
        yield dut.rxd.eq(1)
        for _ in range(4):
            yield
        for d in tx_data:
            yield dut.tx_data.eq(d)
            yield dut.tx_valid.eq(1)
            yield
        yield dut.tx_valid.eq(0)
        for _ in range(bit*10*(len(tx_data) + 1)):
            tx_samples.append((yield dut.txd))
            yield

    def rx_gen():
        for _ in range(16):
            yield
        for d in rx_data:
            for b in [0] + [(d >> i) & 1 for i in range(8)] + [1]:
                yield dut.rxd.eq(b)
                for _ in range(bit):
                    yield
        for _ in range(4*bit):
            yield

    def rx_mon():
        for _ in range(bit*10*(len(rx_data) + 2) + 16):
            if (yield dut.rx_valid):
                rx_out.append((yield dut.rx_data))
            yield

    run_simulation(dut, [tx_gen(), rx_gen(), rx_mon()])

    # Decode TXD: sample each bit at its middle from the start bit falling edge.
    decoded = []
    i = 0
    while i < len(tx_samples):
        if tx_samples[i] == 0:
            bits = [tx_samples[i + bit//2 + n*bit] for n in range(10)]
            assert bits[0] == 0 and bits[9] == 1
            decoded.append(sum(b << n for n, b in enumerate(bits[1:9])))
            i += 9*bit + bit//2
        else:
            i += 1
    assert decoded == tx_data
    assert rx_out  == rx_data

def test_cdc_uart_tx_ready():
    """tx_ready deasserts when the TX FIFO is almost full (no byte lost), reset flushes it."""
    dut   = CDCUART(sys_clk_freq=60e6, fifo_depth=16)
    res   = {}

    def gen():
        yield dut.baudrate.eq(115200)
        for _ in range(4):
            yield
        n = 0
        while (yield dut.tx_ready):
            yield dut.tx_data.eq(n)
            yield dut.tx_valid.eq(1)
            yield
            n += 1
        yield dut.tx_valid.eq(0)
        yield
        res["accepted"] = n
        res["level"]    = (yield dut.fifo.level)
        yield dut.reset.eq(1)
        yield
        yield dut.reset.eq(0)
        yield
        res["level_rst"] = (yield dut.fifo.level)
        res["ready_rst"] = (yield dut.tx_ready)
        res["txd_rst"]   = (yield dut.txd)

    run_simulation(dut, gen())
    # FIFO filled up to depth - 4 (+ the byte in flight while tx_ready deasserts), without overflow
    # (the byte being transmitted is only released at the end of its UART frame).
    assert 16 - 4 <= res["level"] < 16
    assert res["accepted"] == res["level"]
    assert res["level_rst"] == 0
    assert res["ready_rst"] == 1
    assert res["txd_rst"]   == 1

# UVC Video ----------------------------------------------------------------------------------------

def csc(r, g, b):
    out = []
    for ka, kb, kc, s in CSC_COEFFICIENTS:
        coefs = [round(k*(2**CSC_FRAC_BITS)) for k in (ka, kb, kc)]
        v = (coefs[0]*r + coefs[1]*g + coefs[2]*b + (s << CSC_FRAC_BITS) + (1 << (CSC_FRAC_BITS - 1))) >> CSC_FRAC_BITS
        out.append(min(max(v, 0), 255))
    return out

def yuyv_model(pixels):
    """YUYV (4:2:2) stream of a line of 6-bit RGB pixels (U/V: average of the 2 pixels)."""
    out = []
    for p0, p1 in zip(pixels[0::2], pixels[1::2]):
        y0, cb0, cr0 = csc(*[c << 2 for c in p0])
        y1, cb1, cr1 = csc(*[c << 2 for c in p1])
        out += [y0, (cb0 + cb1) >> 1, y1, (cr0 + cr1) >> 1]
    return out

@pytest.mark.parametrize("frame_index, hbw", [(2, 0), (1, 1), (1, 0)])
def test_uvc_video_framing(monkeypatch, frame_index, hbw):
    """UVC payload transfers (one per micro-frame): 12-byte header (bHeaderLength, bmHeaderInfo
    with FID/EOF), full transfers when enough data is buffered, header-only transfers otherwise, a
    last transfer with the remaining bytes and EOF; with high-bandwidth (alt setting 2) two 1024-byte
    transactions (DATA1 -> DATA0, header in the 1st only) when enough data is buffered. Payloads
    carry the YUYV conversion of the frame, upscaled 2x2 for frame 1 (320x288); FID toggles
    between frames. Reduced frame height (4 source lines) to keep the simulation short."""
    height = 4
    monkeypatch.setattr(usb_class, "UVC_HEIGHT", height)
    frames = [(2*UVC_WIDTH, 2*height), (UVC_WIDTH, height)]
    scale  = frames[frame_index - 1][0]//UVC_WIDTH
    dut = UVCVideo(frames=frames)
    dut.cd_video = ClockDomain(reset_less=True)
    random.seed(0)
    src_frames = [[[tuple(random.randrange(64) for _ in range(3)) for _ in range(UVC_WIDTH)]
        for _ in range(height)] for _ in range(2)]
    transfers = []
    pids      = []
    done      = []

    def video():
        for _ in range(20):
            yield
        for frame in src_frames:
            yield dut.frame_valid.eq(1)
            for _ in range(10):
                yield
            for line in frame:
                yield dut.line_valid.eq(1)
                for p in line:
                    yield dut.data.eq(p[0] | (p[1] << 6) | (p[2] << 12))
                    yield dut.enable.eq(1)
                    for _ in range(3):
                        yield
                yield dut.enable.eq(0)
                yield dut.line_valid.eq(0)
                for _ in range(200):
                    yield
            yield dut.frame_valid.eq(0)
            for _ in range(600):
                yield
        done.append(1)

    def transaction():
        length = (yield dut.txdat_len)
        pid    = (yield dut.txiso_pid)
        assert (yield dut.txcork) == 0
        yield dut.txact.eq(1)
        yield
        data = yield from pop(dut, length)
        yield
        yield dut.txact.eq(0)
        for _ in range(8):
            yield
        return pid, data

    def controller():
        yield dut.frame_index.eq(frame_index)
        yield dut.hbw.eq(hbw)
        yield dut.reset.eq(1)
        for _ in range(8):
            yield
        yield dut.reset.eq(0)
        extra = 16 # Micro-frames after the end of the video (to send the buffered data).
        while extra:
            if done:
                extra -= 1
            # Micro-frame period: shorter than on hardware (7500 clocks) to keep the simulation
            # short, but long enough for the replay to buffer 2 transactions.
            for _ in range(3000):
                yield
            yield dut.sof.eq(1)
            yield
            yield dut.sof.eq(0)
            for _ in range(4):
                yield
            pid, data = yield from transaction()
            if pid == 0b1011: # DATA1: 2nd transaction (DATA0).
                assert hbw and len(data) == UVC_PACKET_SIZE
                pid2, data2 = yield from transaction()
                assert pid2 == 0b0011 and len(data2) == UVC_PACKET_SIZE
                data = data + data2
            else:
                assert pid == 0b0011
            pids.append(pid)
            transfers.append(data)

    run_simulation(dut, {"sys": controller(), "video": video()}, clocks={"sys": 10, "video": 70})

    # Headers.
    for t in transfers:
        assert t[0] == UVC_HEADER_SIZE
        assert t[1] & 0xfc == 0x8c
    # Split transfers in frames (EOF = bit 1 of bmHeaderInfo).
    payloads = [[]]
    fids     = []
    for t in transfers:
        if len(t) > UVC_HEADER_SIZE:
            assert len(t) <= (2 if hbw else 1)*UVC_PACKET_SIZE
            payloads[-1] += t[UVC_HEADER_SIZE:]
            fids.append(t[1] & 0x01)
            # Non-last transfers are full.
            assert (t[1] & 0x02) or len(t) in [UVC_PACKET_SIZE, 2*UVC_PACKET_SIZE]
        if t[1] & 0x02:
            payloads.append([])
    assert len(payloads) == 3 and payloads[-1] == []
    # All bytes checked (including the 1st byte of the following frames: no stale word from the
    # previous frame after the per-frame VideoFIFO reset).
    for frame, payload in zip(src_frames, payloads):
        lines    = [[p for p in line for _ in range(scale)] for line in frame for _ in range(scale)]
        expected = sum([yuyv_model(line) for line in lines], [])
        assert len(payload) == len(expected)
        mismatch = [i for i, (a, b) in enumerate(zip(payload, expected)) if a != b]
        assert not mismatch, (len(payload), mismatch[:8], len(mismatch))
    # FID toggles after each frame.
    assert fids[0] == 0 and fids[-1] == 1 and fids == sorted(fids)
    # Header-only transfers are sent while waiting for data.
    assert any(len(t) == UVC_HEADER_SIZE for t in transfers)
    # High-bandwidth transfers are used (only) when enabled.
    assert (0b1011 in pids) == bool(hbw and scale == 2)

def test_uvc_video_late_start(monkeypatch):
    """Host starting the stream late (the FIFO fills, lines are dropped in the first frame): the
    broken frame must not stall the pipeline, the following frames are sent complete and correct."""
    height = 16 # More than the FIFO (+ line buffers) holds.
    monkeypatch.setattr(usb_class, "UVC_HEIGHT", height)
    dut = UVCVideo(frames=[(2*UVC_WIDTH, 2*height), (UVC_WIDTH, height)], fifo_depth=4096)
    dut.cd_video = ClockDomain(reset_less=True)
    random.seed(1)
    src_frames = [[[tuple(random.randrange(64) for _ in range(3)) for _ in range(UVC_WIDTH)]
        for _ in range(height)] for _ in range(3)]
    frame_starts = []
    transfers    = []
    done         = []

    def video():
        for _ in range(20):
            yield
        for n, frame in enumerate(src_frames):
            frame_starts.append(n)
            yield dut.frame_valid.eq(1)
            for _ in range(10):
                yield
            for line in frame:
                for p in line:
                    yield dut.data.eq(p[0] | (p[1] << 6) | (p[2] << 12))
                    yield dut.enable.eq(1)
                    for _ in range(3):
                        yield
                yield dut.enable.eq(0)
                for _ in range(200):
                    yield
            yield dut.frame_valid.eq(0)
            for _ in range(3000):
                yield
        done.append(1)

    def controller():
        yield dut.frame_index.eq(2)
        yield dut.reset.eq(1)
        for _ in range(8):
            yield
        yield dut.reset.eq(0)
        # No stream during the first source frame.
        while len(frame_starts) < 2:
            yield
        extra = 16
        while extra:
            if done:
                extra -= 1
            for _ in range(3000):
                yield
            yield dut.sof.eq(1)
            yield
            yield dut.sof.eq(0)
            for _ in range(4):
                yield
            length = (yield dut.txdat_len)
            yield dut.txact.eq(1)
            yield
            data = yield from pop(dut, length)
            yield
            yield dut.txact.eq(0)
            for _ in range(8):
                yield
            transfers.append(data)

    run_simulation(dut, {"sys": controller(), "video": video()}, clocks={"sys": 10, "video": 70})

    # Complete frames (EOF): each one is exactly one of the source frames, the last ones are sent.
    payloads, current = [], []
    for t in transfers:
        current += t[UVC_HEADER_SIZE:]
        if t[1] & 0x02:
            payloads.append(current)
            current = []
    expected = [sum([yuyv_model(line) for line in frame], []) for frame in src_frames]
    complete = [expected.index(p) for p in payloads if p in expected]
    assert complete and complete[-1] == len(src_frames) - 1

# UVC/UAC with LUNA -------------------------------------------------------------------------------

class _LUNAIsoBench(LiteXModule):
    """UVCVideo (LUNA mode) + the txact emulation of USBDeviceLUNA (requested -> finished)."""
    def __init__(self, frames):
        self.uvc       = uvc = UVCVideo(frames=frames, luna=True)
        self.requested = Signal()
        self.finished  = Signal()
        self.ready     = Signal()
        txact = Signal()
        self.sync += If(self.requested, txact.eq(1)).Elif(self.finished, txact.eq(0))
        self.comb += [uvc.txact.eq(txact), uvc.txpop.eq(self.ready)]
        # pop() helper interface.
        self.txpop = self.ready
        self.txdat = uvc.txdat

@pytest.mark.parametrize("frame_index, hbw", [(2, 0), (1, 1)])
def test_uvc_video_framing_luna(monkeypatch, frame_index, hbw):
    """UVC with LUNA: micro-frame length (next_len) latched at SOF, single transfer of up to 2048
    bytes (LUNA splits it in 1024-byte DATA1/DATA0 packets), header once per transfer; payloads are
    the YUYV conversion of the (upscaled) frames."""
    height = 4
    monkeypatch.setattr(usb_class, "UVC_HEIGHT", height)
    frames = [(2*UVC_WIDTH, 2*height), (UVC_WIDTH, height)]
    scale  = frames[frame_index - 1][0]//UVC_WIDTH
    dut    = _LUNAIsoBench(frames)
    dut.cd_video = ClockDomain(reset_less=True)
    uvc    = dut.uvc
    random.seed(0)
    src_frames = [[[tuple(random.randrange(64) for _ in range(3)) for _ in range(UVC_WIDTH)]
        for _ in range(height)] for _ in range(2)]
    transfers = []
    done      = []

    def video():
        for _ in range(20):
            yield
        for frame in src_frames:
            yield uvc.frame_valid.eq(1)
            for _ in range(10):
                yield
            for line in frame:
                for p in line:
                    yield uvc.data.eq(p[0] | (p[1] << 6) | (p[2] << 12))
                    yield uvc.enable.eq(1)
                    for _ in range(3):
                        yield
                yield uvc.enable.eq(0)
                for _ in range(200):
                    yield
            yield uvc.frame_valid.eq(0)
            for _ in range(600):
                yield
        done.append(1)

    def luna():
        yield uvc.frame_index.eq(frame_index)
        yield uvc.hbw.eq(hbw)
        yield uvc.reset.eq(1)
        for _ in range(8):
            yield
        yield uvc.reset.eq(0)
        extra = 16
        while extra:
            if done:
                extra -= 1
            # Micro-frame period: longer with high-bandwidth (the video must be produced faster
            # than a single transaction drains it for 2048-byte transfers to be used).
            for _ in range(6000 if hbw else 3000):
                yield
            # SOF: LUNA latches bytes_in_frame (next_len) in the SOF cycle.
            yield uvc.sof.eq(1)
            yield
            length = (yield uvc.next_len)
            yield uvc.sof.eq(0)
            for _ in range(8):
                yield
            yield dut.requested.eq(1)
            yield
            yield dut.requested.eq(0)
            for _ in range(4):
                yield
            data, remaining = [], length
            while remaining:
                n = min(remaining, UVC_PACKET_SIZE)
                data += yield from pop(dut, n)
                remaining -= n
                for _ in range(20): # Next packet (IN token) / end of transfer.
                    yield
            yield dut.finished.eq(1)
            yield
            yield dut.finished.eq(0)
            transfers.append(data)

    run_simulation(dut, {"sys": luna(), "video": video()}, clocks={"sys": 10, "video": 70})

    payloads, fids = [[]], []
    for t in transfers:
        assert t[0] == UVC_HEADER_SIZE and (t[1] & 0xfc) == 0x8c
        assert len(t) <= (2 if hbw else 1)*UVC_PACKET_SIZE
        if len(t) > UVC_HEADER_SIZE:
            payloads[-1] += t[UVC_HEADER_SIZE:]
            fids.append(t[1] & 0x01)
        if t[1] & 0x02:
            payloads.append([])
    assert len(payloads) == 3 and payloads[-1] == []
    for frame, payload in zip(src_frames, payloads):
        lines    = [[p for p in line for _ in range(scale)] for line in frame for _ in range(scale)]
        expected = sum([yuyv_model(line) for line in lines], [])
        assert payload == expected
    assert fids[0] == 0 and fids[-1] == 1
    # High-bandwidth transfers (2048 bytes) used when enabled.
    assert (2*UVC_PACKET_SIZE in [len(t) for t in transfers]) == bool(hbw)

def test_uac_endpoint_luna():
    """UAC with LUNA: complete samples (4 bytes, L/R little-endian) buffered, next_len is the number
    of buffered sample bytes (<= 24) and the data is popped in capture order."""
    dut = UACEndpoint(luna=True)
    dut.cd_audio = ClockDomain("audio", reset_less=True)
    lengths, data = [], []

    def gen():
        for i in range(60000):
            yield dut.left.eq(0x1234)
            yield dut.right.eq(0x5678)
            if i % 7500 == 7499:
                length = (yield dut.next_len)
                lengths.append(length)
                for _ in range(length):
                    data.append((yield dut.txdat))
                    yield dut.txpop.eq(1)
                    yield
                    yield dut.txpop.eq(0)
                    yield
            yield

    run_simulation(dut, gen(), clocks={"sys": 10, "audio": 70})
    assert all(l % 4 == 0 and l <= UACEndpoint.MAXBUFFER for l in lengths)
    assert set(lengths[2:]) <= {20, 24} and 20 in lengths[2:] and 24 in lengths[2:]
    samples = [data[i:i+4] for i in range(0, len(data), 4)]
    assert samples[-8:] == [[0x34, 0x12, 0x78, 0x56]]*8

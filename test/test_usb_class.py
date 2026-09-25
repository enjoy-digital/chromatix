#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

import os
import subprocess
import tempfile
from types import SimpleNamespace

import pytest

from migen import *

from litex.gen.sim import run_simulation

from chromatix.gateware.usb_class import *
from test.eqcheck import export_migen, eqcheck

# Helpers ------------------------------------------------------------------------------------------

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

# Original sources (from git history, so the checks keep working once they're removed from the tree).
ORIGINAL_REV  = "ae5fe05"
ORIGINAL_USB  = "chromatix/verilog/usb"
ORIGINAL_FILES = ["usbuvcuart_top.v", "usb_video/usb_defs.v", "usb_video/uvc_defs.v", "usb_video/uac_defs.v", "usb_video/uart_defs.v"]

def fetch_original(tmpdir):
    for f in ORIGINAL_FILES:
        path = os.path.join(tmpdir, f)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            data = subprocess.check_output(["git", "show", f"{ORIGINAL_REV}:{ORIGINAL_USB}/{f}"], cwd=ROOT, stderr=subprocess.DEVNULL)
        except Exception:
            pytest.skip("original sources not available (git history)")
        if f == "usbuvcuart_top.v":
            # Yosys doesn't support part-selects of concatenations: substitute the constant values.
            import re
            text = data.decode()
            text = text.replace("{`AUDIO_DATA_EP_NUM}[3:0]", "4'd5")
            text = re.sub(r"\{`UAC_FREQUENCY\}\[(\d+):(\d+)\]",
                lambda m: "8'd%d" % ((UAC_FREQUENCY >> int(m.group(2))) & 0xff), text)
            data = text.encode()
        with open(path, "wb") as fh:
            fh.write(data)
    return os.path.join(tmpdir, "usbuvcuart_top.v")

class _Wrapper(Module):
    """Expose a Migen port with the original module port names."""
    def __init__(self, clk_name="pClk"):
        self.ios = set()
        self.clock_domains.cd_sys = ClockDomain(reset_less=True)
        clk = self.i(clk_name)
        self.comb += self.cd_sys.clk.eq(clk)

    def i(self, name, width=1):
        s = Signal(width, name=name)
        self.ios.add(s)
        return s

    def o(self, name, sig):
        s = Signal(len(sig), name=name)
        self.ios.add(s)
        self.comb += s.eq(sig)

def setup_inputs(w):
    return SimpleNamespace(
        header_ready  = w.i("header_ready"),
        bmRequestType = w.i("bmRequestType", 8),
        bRequest      = w.i("bRequest", 8),
        wValue        = w.i("wValue", 16),
        wIndex        = w.i("wIndex", 16),
        wLength       = w.i("wLength", 16),
        cdata_ofs     = w.i("cdata_ofs", 16),
    )

def control_wrapper(cls, extra_outputs=[]):
    w     = _Wrapper()
    setup = setup_inputs(w)
    dut   = cls(setup)
    w.submodules.dut = dut
    w.comb += [
        dut.reset.eq(w.i("RESET_IN")),
        dut.rxdat.eq(w.i("usb_rxdat", 8)),
        dut.rxact.eq(w.i("usb_rxact")),
        dut.rxval.eq(w.i("usb_rxval")),
        dut.txpop.eq(w.i("usb_txpop")),
    ]
    w.o("usb_txval",     dut.txval)
    w.o("usb_txdat_len", dut.txdat_len)
    w.o("usb_txdat",     dut.txdat)
    for name, sig in extra_outputs:
        w.o(name, getattr(dut, sig))
    return w

def check(tmp_path, gold_top, gate, depth, ignore_outputs=[]):
    gold = fetch_original(str(tmp_path))
    gate_v = os.path.join(str(tmp_path), "gate.v")
    export_migen(gate, gate.ios, gold_top, gate_v)
    ok, log = eqcheck([gold], gold_top, [gate_v], gold_top, depth=depth,
        gold_includes=[os.path.dirname(gold)], ignore_outputs=ignore_outputs, workdir=str(tmp_path))
    assert ok, log[-2000:]

# Equivalence --------------------------------------------------------------------------------------

def test_setup_parser_equivalence(tmp_path):
    """USBSetupParser == setup header capture of usbuvcuart_top.v (checked through a gold wrapper)."""
    gold = fetch_original(str(tmp_path))
    # Gold wrapper: extract the setup parser always block into a module.
    src = open(gold).read()
    start = src.index("    reg  [ 7:0] bmRequestType;")
    end   = src.index("    ctrl_uart uart_if_ctrl(")
    body  = src[start:end]
    wrapper = os.path.join(str(tmp_path), "gold_setup.v")
    with open(wrapper, "w") as f:
        f.write("module setup_parser(input pClk, input RESET_IN, input setup_active, input [3:0] endpt_sel,\n"
                "  input [7:0] usb_rxdat, input usb_rxval, input usb_rxact, input usb_txact, input usb_txpop,\n"
                "  output header_ready_o, output [7:0] bmRequestType_o, output [7:0] bRequest_o,\n"
                "  output [15:0] wValue_o, output [15:0] wIndex_o, output [15:0] wLength_o, output [15:0] cdata_ofs_o);\n"
                "  localparam EP_CTRL = 4'd0;\n"
                + "\n".join(l for l in body.split("\n") if not any(k in l for k in ["s_ctl_sig", "s_dte1_rate", "s_char1_format",
                   "s_parity1_type", "s_data1_bits", "uart_dte_rate", "uart_char_format", "uart_parity_type", "uart_data_bits"])) +
                "\n  assign header_ready_o = header_ready; assign bmRequestType_o = bmRequestType; assign bRequest_o = bRequest;\n"
                "  assign wValue_o = wValue; assign wIndex_o = wIndex; assign wLength_o = wLength; assign cdata_ofs_o = cdata_ofs;\n"
                "endmodule\n")
    w   = _Wrapper()
    dut = USBSetupParser()
    w.submodules.dut = dut
    w.comb += [
        dut.reset.eq(w.i("RESET_IN")),
        dut.setup_active.eq(w.i("setup_active")),
        dut.endpt.eq(w.i("endpt_sel", 4)),
        dut.rxdat.eq(w.i("usb_rxdat", 8)),
        dut.rxval.eq(w.i("usb_rxval")),
        dut.rxact.eq(w.i("usb_rxact")),
        dut.txact.eq(w.i("usb_txact")),
        dut.txpop.eq(w.i("usb_txpop")),
    ]
    for name in ["header_ready", "bmRequestType", "bRequest", "wValue", "wIndex", "wLength", "cdata_ofs"]:
        w.o(name + "_o", getattr(dut, name))
    gate_v = os.path.join(str(tmp_path), "gate.v")
    export_migen(w, w.ios, "setup_parser", gate_v)
    ok, log = eqcheck([wrapper], "setup_parser", [gate_v], "setup_parser", depth=24, workdir=str(tmp_path))
    assert ok, log[-2000:]

def test_ctrl_uart_equivalence(tmp_path):
    """CDCACMControl == ctrl_uart."""
    w = control_wrapper(CDCACMControl, [("s_ctl_sig", "ctl_sig"), ("s_dte1_rate", "dte_rate"),
        ("s_char1_format", "char_format"), ("s_parity1_type", "parity_type"), ("s_data1_bits", "data_bits")])
    check(tmp_path, "ctrl_uart", w, depth=16)

def test_ctrl_uvc_equivalence(tmp_path):
    """UVCControl == ctrl_uvc (probe control outputs are constants and unused)."""
    w = control_wrapper(UVCControl)
    probe = ["bmHint", "bFormatIndex", "bFrameIndex", "dwFrameInterval", "wKeyFrameRate", "wPFrameRate",
        "wCompQuality", "wCompWindowSize", "wDelay", "dwMaxVideoFrameSize", "dwMaxPayloadTransferSize",
        "dwClockFrequency", "bmFramingInfo", "bPreferedVersion", "bMinVersion", "bMaxVersion"]
    check(tmp_path, "ctrl_uvc", w, depth=16, ignore_outputs=probe)

def test_ctrl_uac_equivalence(tmp_path):
    """UACControl == ctrl_uac."""
    w = control_wrapper(UACControl)
    check(tmp_path, "ctrl_uac", w, depth=16)

def test_interface_alt_select_equivalence(tmp_path):
    """InterfaceAltSelect == interface_alt_select."""
    w   = _Wrapper()
    dut = InterfaceAltSelect()
    w.submodules.dut = dut
    w.comb += [
        dut.reset.eq(w.i("RESET_IN")),
        dut.update.eq(w.i("interface_update")),
        dut.alt_i.eq(w.i("interface_alter_i", 8)),
    ]
    w.o("interface_alter_o", dut.alt_o)
    check(tmp_path, "interface_alt_select", w, depth=8)

# CDC UART -----------------------------------------------------------------------------------------

def test_cdc_uart_loopback():
    """Bytes sent on EP3 OUT come back on EP3 IN through a TX -> RX loopback at 1Mbaud."""
    dut   = CDCUART(sys_clk_freq=60e6)
    data  = [0x55, 0xa5, 0x00, 0xff, 0x12]
    rx    = []
    dut.comb += dut.rxd.eq(dut.txd)

    def gen():
        yield dut.baudrate.eq(1_000_000)
        for _ in range(4):
            yield
        for d in data:
            while not (yield dut.tx_ready):
                yield
            yield dut.tx_data.eq(d)
            yield dut.tx_valid.eq(1)
            yield
            yield dut.tx_valid.eq(0)
        for _ in range(60*10*(len(data) + 2)):
            if (yield dut.rx_valid):
                rx.append((yield dut.rx_data))
            yield

    run_simulation(dut, gen())
    assert rx == data

def test_uac_endpoint_equivalence(tmp_path):
    """UACEndpoint == usbuac_ep (bounded: the 44.1kHz accumulator is only partially covered)."""
    w   = _Wrapper()
    dut = UACEndpoint()
    w.submodules.dut = dut
    w.clock_domains.cd_audio = ClockDomain(reset_less=True)
    w.comb += [
        w.cd_audio.clk.eq(w.i("gClk")),
        dut.reset.eq(w.i("rst")),
        dut.sof_rise.eq(w.i("usb_sof_rise")),
        dut.left.eq(w.i("left", 16)),
        dut.right.eq(w.i("right", 16)),
        dut.txpop.eq(w.i("uac_txpop")),
        dut.txact.eq(w.i("uac_txact")),
    ]
    w.o("uac_txdat",     dut.txdat)
    w.o("uac_txdat_len", dut.txdat_len)
    w.o("uac_txcork",    dut.txcork)
    check(tmp_path, "usbuac_ep", w, depth=24)

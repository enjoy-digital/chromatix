#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
LUNA USB 2.0 device core integration: LUNADeviceCore (usb_luna_core.py, Amaranth) converted to
Verilog at build time with LiteX's Amaranth2VConverter, and the EP0 GET_DESCRIPTOR handler (Migen)
serving the descriptors ROM through the core's request bridge.
"""

from migen import *

from litex.gen import *

# Versions -----------------------------------------------------------------------------------------

LUNA_REQUIREMENTS = {"amaranth": "0.5.8", "luna-usb": "0.2.3", "usb-protocol": "0.9.2"}

def check_luna():
    """Checks that the pinned Amaranth/LUNA versions are installed (raises otherwise)."""
    from importlib.metadata import version, PackageNotFoundError
    for package, required in LUNA_REQUIREMENTS.items():
        try:
            installed = version(package)
        except PackageNotFoundError:
            raise RuntimeError(f"{package} not installed: pip3 install --user {package}=={required}")
        if not installed.startswith(required):
            raise RuntimeError(f"{package} {installed} installed, {required} required.")

# LUNA Controller ----------------------------------------------------------------------------------

class LUNAUSBController(LiteXModule):
    """LUNADeviceCore converted to Verilog; its ports are Migen Signals (attributes, same names)."""
    def __init__(self, platform, reset=None, **kwargs):
        check_luna()
        from litex.build.amaranth2v_converter import Amaranth2VConverter
        from chromatix.gateware.usb_luna_core import LUNADeviceCore

        self.core = core = LUNADeviceCore(**kwargs)
        ports = {
            "i_usb_clk"  : ClockSignal("phy"),
            "i_usb_rst"  : ResetSignal("phy") if reset is None else reset,
            "i_sync_clk" : ClockSignal("phy"),
            "i_sync_rst" : ResetSignal("phy") if reset is None else reset,
        }
        for s in core.ports:
            name = s.name
            sig  = Signal(len(s), name=name)
            setattr(self, name, sig)
            ports[f"{core.port_dirs[name]}_{name}"] = sig
        self.converter = Amaranth2VConverter(platform,
            name    = "luna_usb_device",
            module  = core,
            ports   = ports,
            domains = ["usb"],
        )

# Descriptors Request Handler ----------------------------------------------------------------------

class USBDescriptorRequest(LiteXModule):
    """
    GET_DESCRIPTOR from the descriptors ROM (USBDescriptors): device, configuration (same for FS/HS),
    device qualifier and strings; other types are not answered (STALL). The descriptor lookup is
    registered (setup fields are stable during the request), the ROM is read asynchronously at the
    descriptor address + cdata_ofs.
    """
    def __init__(self, setup, desc):
        self.reset     = Signal()
        self.txval     = Signal()
        self.txdat     = Signal(8)
        self.txdat_len = Signal(12)
        self.txpop     = Signal() # Unused (the ROM is addressed by cdata_ofs).

        # # #

        l = desc.layout
        s = setup
        addr   = Signal(16)
        length = Signal(16)
        found  = Signal()
        table  = [
            # (type, index, address, length).
            (1, 0, l.dev_addr,        l.dev_len),
            (2, 0, l.hscfg_addr,      l.hscfg_len),
            (6, 0, l.qual_addr,       l.qual_len),
            (3, 0, l.strlang_addr,    4),
            (3, 1, l.strvendor_addr,  l.strvendor_len),
            (3, 2, l.strproduct_addr, l.strproduct_len),
            (3, 3, l.strserial_addr,  l.strserial_len),
        ]
        cases = {"default": [addr.eq(0), length.eq(0), found.eq(0)]}
        for dtype, index, a, n in table:
            cases[(dtype << 8) | index] = [addr.eq(a), length.eq(n), found.eq(1)]
        is_get_descriptor = Signal()
        self.sync += [
            Case(s.wValue, cases),
            is_get_descriptor.eq(s.header_ready & (s.bmRequestType == 0x80) & (s.bRequest == 0x06)),
        ]
        self.comb += [
            self.txval.eq(s.header_ready & is_get_descriptor & found),
            self.txdat_len.eq(length),
            desc.descrom_raddr.eq(addr + s.cdata_ofs),
            self.txdat.eq(desc.descrom_rdat),
        ]

#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
USB descriptors ROM of the legacy USB (UVC/UAC/CDC) subsystem (usb_desc).

The descriptors (device, qualifier, UVC + UAC 2.0 + CDC-ACM configuration, other-speed hack and
strings) are built in Python. Only a few bytes are dynamic and follow the player number:

- idProduct low byte (byte 10) = playerNum (0 after reset, PRODUCTID[7:0] is never used).
- The "XX" of the product string = playerNum as 2 uppercase hex digits.

As in the original, these bytes are only updated when playerNum changes: after reset, they read
0x00/"XX" until playerNum differs from 0. The original reset is asynchronous: while reset is high,
the dynamic bytes read their reset values (reproduced combinationally here).
"""

from migen import *

from litex.gen import *

# Constants ----------------------------------------------------------------------------------------

# Descriptor types.
USB_DESCTYPE_DEVICE                = 0x01
USB_DESCTYPE_CONFIGURATION         = 0x02
USB_DESCTYPE_STRING                = 0x03
USB_DESCTYPE_INTERFACE             = 0x04
USB_DESCTYPE_ENDPOINT              = 0x05
USB_DESCTYPE_DEVICE_QUALIFIER      = 0x06
USB_DESCTYPE_INTERFACE_ASSOCIATION = 0x0B
USB_DESCTYPE_CS_INTERFACE          = 0x24
USB_DESCTYPE_CS_ENDPOINT           = 0x25

# Classes.
USB_CLASS_AUDIO          = 0x01
USB_CLASS_COMMUNICATIONS = 0x02
USB_CLASS_CDC_DATA       = 0x0A
USB_CLASS_VIDEO          = 0x0E

# UVC.
USB_VIDEO_CONTROL              = 0x01
USB_VIDEO_STREAMING            = 0x02
USB_VIDEO_INTERFACE_COLLECTION = 0x03
USB_VC_HEADER                  = 0x01
USB_VC_INPUT_TERMINAL          = 0x02
USB_VC_OUTPUT_TERMINAL         = 0x03
USB_VS_INPUT_HEADER            = 0x01
USB_VS_FORMAT_UNCOMPRESSED     = 0x04
USB_VS_FRAME_UNCOMPRESSED      = 0x05
USB_VS_COLORFORMAT             = 0x0D
UVC_VC_INTERFACE               = 0
UVC_VS_INTERFACE               = 1
VIDEO_STATUS_EP_NUM            = 1
VIDEO_DATA_EP_NUM              = 2
DEVICE_CLOCK_FREQUENCY         = 60_000_000
VIDEO_WIDTH                    = 160
VIDEO_HEIGHT                   = 144
VIDEO_BITS_PER_PIXEL           = 16
VIDEO_FPS_MIN                  = 1
VIDEO_FPS_MAX                  = 60
VIDEO_FPS                      = 60
VIDEO_MAX_FRAME_SIZE           = VIDEO_WIDTH*VIDEO_HEIGHT*VIDEO_BITS_PER_PIXEL//8
VIDEO_MIN_BIT_RATE             = VIDEO_MAX_FRAME_SIZE*VIDEO_FPS_MIN*8
VIDEO_MAX_BIT_RATE             = VIDEO_MAX_FRAME_SIZE*VIDEO_FPS_MAX*8
VIDEO_FRAME_INTERVAL           = 10_000_000//VIDEO_FPS # In 100ns units.
VIDEO_PACKET_SIZE              = 1024
VIDEO_ADDITIONAL_PACKET        = 0
YUY2_GUID                      = [0x59, 0x55, 0x59, 0x32, 0x00, 0x00, 0x10, 0x00,
                                  0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71]

# UAC 2.0.
USB_AUDIO_CONTROL             = 0x01
USB_AUDIO_STREAMING           = 0x02
AF_VERSION_02_00              = 0x20
UAC_HEADER                    = 0x01
UAC_INPUT_TERMINAL            = 0x02
UAC_OUTPUT_TERMINAL           = 0x03
UAC2_CLOCK_SOURCE             = 0x0A
UAC_AS_GENERAL                = 0x01
UAC_FORMAT_TYPE               = 0x02
UAC_FORMAT_TYPE_I             = 0x01
UAC_FORMAT_TYPE_I_PCM         = 0x00000001
UAC_EP_GENERAL                = 0x01
UAC_TERMINAL_STREAMING        = 0x0101
UAC_INPUT_TERMINAL_MICROPHONE = 0x0201
UAC_AC_INTERFACE              = 4
UAC_AS_INTERFACE              = 5
AUDIO_DATA_EP_NUM             = 5
UAC_PACKET_SIZE               = 24

# CDC-ACM.
UART_CTRL_IFACE = 2
UART_DATA_IFACE = 3

# Helpers ------------------------------------------------------------------------------------------

def le(value, n):
    """Little-endian bytes of value."""
    return [(value >> 8*i) & 0xff for i in range(n)]

def descriptor(dtype, *fields):
    """Descriptor with bLength/bDescriptorType header."""
    body = [b for f in fields for b in (f if isinstance(f, list) else [f])]
    return [2 + len(body), dtype] + body

def string_descriptor(s):
    """String descriptor (UTF-16LE, ASCII only)."""
    return descriptor(USB_DESCTYPE_STRING, *[[ord(c), 0x00] for c in s])

def ascii_hex(nibble):
    """ASCII (uppercase) hex digit of a 4-bit signal."""
    return Mux(nibble > 9, 0x37 + nibble, 0x30 + nibble)

# USB Descriptors Layout ---------------------------------------------------------------------------

class USBDescriptorsLayout:
    """Descriptor ROM content and layout (addresses/lengths), as computed by usb_desc."""
    def __init__(self, vendor_id, product_id, version_bcd, vendor_str, product_str, serial_str,
        hs_support, self_powered, player_str="XX"):
        # Device Descriptor (+2 bytes padding).
        dev = descriptor(USB_DESCTYPE_DEVICE,
            le(0x0200 if hs_support else 0x0110, 2), # bcdUSB.
            0xEF, 0x02, 0x01,                        # Misc Class / Common Class / IAD.
            0x40,                                    # bMaxPacketSize0.
            le(vendor_id, 2),                        # idVendor.
            0x00, product_id >> 8,                   # idProduct (low byte: playerNum).
            le(version_bcd, 2),                      # bcdDevice.
            1 if len(vendor_str)  else 0,            # iManufacturer.
            2 if len(product_str) else 0,            # iProduct.
            3 if len(serial_str)  else 0,            # iSerialNumber.
            0x01,                                    # bNumConfigurations.
        )

        # Device Qualifier Descriptor (+2 bytes padding, bNumConfigurations = 0).
        qual = descriptor(USB_DESCTYPE_DEVICE_QUALIFIER,
            le(0x0200, 2), 0x01, 0x00, 0x00, 0x40, 0x00, 0x00)

        # UVC Function.
        uvc_vc_header_len = 13
        uvc_vc_it_len     = 18
        uvc_vc_ot_len     = 9
        uvc_vs_header_len = 14
        uvc_vs_format_len = 27
        uvc_vs_frame_len  = 30
        uvc_vs_color_len  = 6
        uvc = [
            # Interface Association.
            *descriptor(USB_DESCTYPE_INTERFACE_ASSOCIATION, UVC_VC_INTERFACE, 2, USB_CLASS_VIDEO,
                USB_VIDEO_INTERFACE_COLLECTION, 0x00, 0x02),
            # VideoControl Interface.
            *descriptor(USB_DESCTYPE_INTERFACE, UVC_VC_INTERFACE, 0, 1, USB_CLASS_VIDEO,
                USB_VIDEO_CONTROL, 0x00, 0x02),
            # VC Header (UVC 1.1, wTotalLength till Output Terminal).
            *descriptor(USB_DESCTYPE_CS_INTERFACE, USB_VC_HEADER, le(0x0110, 2),
                le(uvc_vc_header_len + uvc_vc_it_len + uvc_vc_ot_len, 2),
                le(DEVICE_CLOCK_FREQUENCY, 4), 1, UVC_VS_INTERFACE),
            # VC Input Terminal (Camera, no controls).
            *descriptor(USB_DESCTYPE_CS_INTERFACE, USB_VC_INPUT_TERMINAL, 1, le(0x0201, 2), 0, 0,
                le(0, 2), le(0, 2), le(0, 2), 3, [0, 0, 0]),
            # VC Output Terminal (Streaming, source: Input Terminal).
            *descriptor(USB_DESCTYPE_CS_INTERFACE, USB_VC_OUTPUT_TERMINAL, 2, le(0x0101, 2), 0, 1,
                0),
            # VC Interrupt Endpoint.
            *descriptor(USB_DESCTYPE_ENDPOINT, 0x80 | VIDEO_STATUS_EP_NUM, 0x03, le(64, 2), 9),
            *descriptor(USB_DESCTYPE_CS_ENDPOINT, 0x03, le(64, 2)),
            # VideoStreaming Interface, Alt 0 (zero-bandwidth).
            *descriptor(USB_DESCTYPE_INTERFACE, UVC_VS_INTERFACE, 0, 0, USB_CLASS_VIDEO,
                USB_VIDEO_STREAMING, 0x00, 0x00),
            # VS Input Header.
            *descriptor(USB_DESCTYPE_CS_INTERFACE, USB_VS_INPUT_HEADER, 1,
                le(uvc_vs_header_len + uvc_vs_format_len + uvc_vs_frame_len + uvc_vs_color_len, 2),
                0x80 | VIDEO_DATA_EP_NUM, 0, 2, 1, 0, 0, 1, 0),
            # VS Format (Uncompressed YUY2).
            *descriptor(USB_DESCTYPE_CS_INTERFACE, USB_VS_FORMAT_UNCOMPRESSED, 1, 1, YUY2_GUID,
                VIDEO_BITS_PER_PIXEL, 1, 0, 0, 0, 0),
            # VS Frame.
            *descriptor(USB_DESCTYPE_CS_INTERFACE, USB_VS_FRAME_UNCOMPRESSED, 1, 1,
                le(VIDEO_WIDTH, 2), le(VIDEO_HEIGHT, 2),
                le(VIDEO_MIN_BIT_RATE, 4), le(VIDEO_MAX_BIT_RATE, 4),
                le(VIDEO_MAX_FRAME_SIZE, 4), le(VIDEO_FRAME_INTERVAL, 4),
                1, le(VIDEO_FRAME_INTERVAL, 4)),
            # VS Color Matching.
            *descriptor(USB_DESCTYPE_CS_INTERFACE, USB_VS_COLORFORMAT, 1, 1, 4),
            # VideoStreaming Interface, Alt 1.
            *descriptor(USB_DESCTYPE_INTERFACE, UVC_VS_INTERFACE, 1, 1, USB_CLASS_VIDEO,
                USB_VIDEO_STREAMING, 0x00, 0x00),
            # VS Isochronous Endpoint.
            *descriptor(USB_DESCTYPE_ENDPOINT, 0x80 | VIDEO_DATA_EP_NUM, 0x05,
                VIDEO_PACKET_SIZE & 0xff,
                ((VIDEO_ADDITIONAL_PACKET & 0x3) << 3) | ((VIDEO_PACKET_SIZE >> 8) & 0x7),
                1),
        ]

        # UAC 2.0 Function.
        uac_ac_if = descriptor(USB_DESCTYPE_INTERFACE, UAC_AC_INTERFACE, 0, 0, USB_CLASS_AUDIO,
            USB_AUDIO_CONTROL, AF_VERSION_02_00, 0x02)
        uac_ac_cs = [
            # Clock Source (internal fixed).
            *descriptor(USB_DESCTYPE_CS_INTERFACE, UAC2_CLOCK_SOURCE, 1, 0x01, 0x01, 2, 0),
            # Input Terminal (Microphone, stereo).
            *descriptor(USB_DESCTYPE_CS_INTERFACE, UAC_INPUT_TERMINAL, 2,
                le(UAC_INPUT_TERMINAL_MICROPHONE, 2), 0, 1, 2, le(0x3, 4), 0, le(0, 2), 0),
            # Output Terminal (Streaming).
            *descriptor(USB_DESCTYPE_CS_INTERFACE, UAC_OUTPUT_TERMINAL, 3,
                le(UAC_TERMINAL_STREAMING, 2), 0, 2, 1, le(0, 2), 0),
        ]
        uac_ac_header_len = 9
        # Note: wTotalLength also counts the standard AC Interface descriptor (as the original).
        uac_ac_total_len  = len(uac_ac_if) + uac_ac_header_len + len(uac_ac_cs)
        uac = [
            # Interface Association.
            *descriptor(USB_DESCTYPE_INTERFACE_ASSOCIATION, UAC_AC_INTERFACE, 2, USB_CLASS_AUDIO,
                0x00, AF_VERSION_02_00, 0x02),
            # AudioControl Interface.
            *uac_ac_if,
            # AC Header (ADC 2.0, category 0x0B).
            *descriptor(USB_DESCTYPE_CS_INTERFACE, UAC_HEADER, le(0x0200, 2), 0x0B,
                le(uac_ac_total_len, 2), 0x00),
            *uac_ac_cs,
            # AudioStreaming Interface, Alt 0 (no endpoint) and Alt 1.
            *descriptor(USB_DESCTYPE_INTERFACE, UAC_AS_INTERFACE, 0, 0, USB_CLASS_AUDIO,
                USB_AUDIO_STREAMING, AF_VERSION_02_00, 0x00),
            *descriptor(USB_DESCTYPE_INTERFACE, UAC_AS_INTERFACE, 1, 1, USB_CLASS_AUDIO,
                USB_AUDIO_STREAMING, AF_VERSION_02_00, 0x00),
            # AS General (PCM, stereo).
            *descriptor(USB_DESCTYPE_CS_INTERFACE, UAC_AS_GENERAL, 3, 0x00, UAC_FORMAT_TYPE_I,
                le(UAC_FORMAT_TYPE_I_PCM, 4), 2, le(0x3, 4), 0),
            # AS Format Type I (16-bit).
            *descriptor(USB_DESCTYPE_CS_INTERFACE, UAC_FORMAT_TYPE, UAC_FORMAT_TYPE_I, 2, 16),
            # AS Isochronous Endpoint.
            *descriptor(USB_DESCTYPE_ENDPOINT, 0x80 | AUDIO_DATA_EP_NUM, 0x05,
                UAC_PACKET_SIZE & 0xff, (UAC_PACKET_SIZE >> 8) & 0x7, 1),
            *descriptor(USB_DESCTYPE_CS_ENDPOINT, UAC_EP_GENERAL, 0, 0, 0, le(0, 2)),
        ]

        # CDC-ACM Function.
        cdc = [
            # Interface Association.
            *descriptor(USB_DESCTYPE_INTERFACE_ASSOCIATION, UART_CTRL_IFACE, 2,
                USB_CLASS_COMMUNICATIONS, 0x02, 0x00, 0x00),
            # Communication Interface.
            *descriptor(USB_DESCTYPE_INTERFACE, UART_CTRL_IFACE, 0, 1, USB_CLASS_COMMUNICATIONS,
                0x02, 0x00, 0x01),
            *descriptor(USB_DESCTYPE_CS_INTERFACE, 0x00, le(0x0120, 2)),                # Header.
            *descriptor(USB_DESCTYPE_CS_INTERFACE, 0x06, UART_CTRL_IFACE, UART_DATA_IFACE), # Union.
            *descriptor(USB_DESCTYPE_CS_INTERFACE, 0x01, 0x03, UART_DATA_IFACE),        # Call Mgmt.
            *descriptor(USB_DESCTYPE_CS_INTERFACE, 0x02, 0x03),                         # ACM.
            *descriptor(USB_DESCTYPE_ENDPOINT, 0x84, 0x03, le(8, 2), 7),                # Notify EP.
            # Data Interface + Bulk Endpoints.
            *descriptor(USB_DESCTYPE_INTERFACE, UART_DATA_IFACE, 0, 2, USB_CLASS_CDC_DATA,
                0x00, 0x00, 0x00),
            *descriptor(USB_DESCTYPE_ENDPOINT, 0x83, 0x02, le(512, 2), 0),
            *descriptor(USB_DESCTYPE_ENDPOINT, 0x03, 0x02, le(512, 2), 0),
        ]

        # Configuration Descriptor.
        cfg_len = 9 + len(uvc) + len(uac) + len(cdc)
        cfg     = descriptor(USB_DESCTYPE_CONFIGURATION,
            le(cfg_len, 2),                    # wTotalLength.
            6,                                 # bNumInterfaces.
            1,                                 # bConfigurationValue.
            0,                                 # iConfiguration.
            0xc0 if self_powered else 0x80,    # bmAttributes.
            0xFA,                              # bMaxPower = 500mA.
        ) + uvc + uac + cdc

        # Other Speed Configuration hack.
        oscfg = [0x07] + [0x00]*12

        # Strings.
        strlang    = descriptor(USB_DESCTYPE_STRING, le(0x0409, 2))
        strvendor  = string_descriptor(vendor_str)
        strproduct = string_descriptor(product_str)
        strserial  = string_descriptor(serial_str)

        # ROM Layout.
        self.have_strings = int(len(vendor_str) > 0 or len(product_str) > 0 or len(serial_str) > 0)
        self.rom = rom = []
        def add(data, align=1):
            addr = len(rom)
            rom.extend(data + [0x00]*(-len(data) % align))
            return addr, len(data)
        self.dev_addr,        self.dev_len        = add(dev,  align=4)
        self.qual_addr,       self.qual_len       = add(qual, align=4)
        self.fscfg_addr,      self.fscfg_len      = add(cfg)
        self.hscfg_addr,      self.hscfg_len      = self.fscfg_addr, self.fscfg_len
        self.oscfg_addr,      self.oscfg_len      = add(oscfg)
        self.strlang_addr,    _                   = add(strlang)
        self.strvendor_addr,  self.strvendor_len  = add(strvendor)
        self.strproduct_addr, self.strproduct_len = add(strproduct)
        self.strserial_addr,  self.strserial_len  = add(strserial)
        if not self.have_strings:
            del rom[self.strlang_addr if hs_support else self.oscfg_addr:]

        # Dynamic bytes.
        self.pid_lo_addr = self.dev_addr + 10
        if player_str in product_str:
            player_index = product_str.index(player_str)
            self.player_hi_addr = self.strproduct_addr + 2 + 2*(player_index + 0)
            self.player_lo_addr = self.strproduct_addr + 2 + 2*(player_index + 1)
        else:
            self.player_hi_addr = self.player_lo_addr = None

        self.addr_width = max(1, (len(rom) - 1).bit_length())

# USB Descriptors ----------------------------------------------------------------------------------

class USBDescriptors(LiteXModule):
    """USB descriptors ROM (asynchronous read) with player number in idProduct/product string."""
    def __init__(self,
        vendor_id    = 0x374E,
        product_id   = 0x013f,
        version_bcd  = 0x0200,
        vendor_str   = "ModRetro",
        product_str  = "Chromatic - Player XX",
        serial_str   = "012345678",
        hs_support   = True,
        self_powered = False):
        self.layout = layout = USBDescriptorsLayout(
            vendor_id    = vendor_id,
            product_id   = product_id,
            version_bcd  = version_bcd,
            vendor_str   = vendor_str,
            product_str  = product_str,
            serial_str   = serial_str,
            hs_support   = hs_support,
            self_powered = self_powered,
        )

        self.reset           = Signal()
        self.player_num      = Signal(8)

        self.descrom_raddr   = Signal(16)
        self.descrom_rdat    = Signal(8)

        self.dev_addr        = Signal(16)
        self.dev_len         = Signal(16)
        self.qual_addr       = Signal(16)
        self.qual_len        = Signal(16)
        self.fscfg_addr      = Signal(16)
        self.fscfg_len       = Signal(16)
        self.hscfg_addr      = Signal(16)
        self.hscfg_len       = Signal(16)
        self.oscfg_addr      = Signal(16)
        self.strlang_addr    = Signal(16)
        self.strvendor_addr  = Signal(16)
        self.strvendor_len   = Signal(16)
        self.strproduct_addr = Signal(16)
        self.strproduct_len  = Signal(16)
        self.strserial_addr  = Signal(16)
        self.strserial_len   = Signal(16)
        self.have_strings    = Signal()

        # # #

        # Layout.
        for name in ["dev_addr", "dev_len", "qual_addr", "qual_len", "fscfg_addr", "fscfg_len",
            "hscfg_addr", "hscfg_len", "oscfg_addr", "strlang_addr", "strvendor_addr",
            "strvendor_len", "strproduct_addr", "strproduct_len", "strserial_addr", "strserial_len",
            "have_strings"]:
            self.comb += getattr(self, name).eq(getattr(layout, name))

        # Player Number.
        # Dynamic bytes are rewritten when playerNum differs from its previous value: the written
        # value is then always the previous playerNum, so only the previous value and an "updated"
        # flag (strings still "XX" until the first change) are stored.
        player_num_prev    = Signal(8)
        player_num_updated = Signal()
        self.sync += [
            If(self.reset,
                player_num_prev.eq(0),
                player_num_updated.eq(0),
            ).Else(
                player_num_prev.eq(self.player_num),
                If(self.player_num != player_num_prev,
                    player_num_updated.eq(1),
                )
            )
        ]
        # Asynchronous reset of the original: reset values are visible while reset is high.
        player_num = Signal(8)
        updated    = Signal()
        self.comb += [
            player_num.eq(Mux(self.reset, 0, player_num_prev)),
            updated.eq(~self.reset & player_num_updated),
        ]

        # Descriptor ROM (asynchronous read). The address is decoded on addr_width bits: reads past
        # the ROM end return 0 up to 2**addr_width, then alias (undefined in the original).
        rom   = layout.rom
        raddr = self.descrom_raddr[:layout.addr_width]
        cases = {addr: self.descrom_rdat.eq(data) for addr, data in enumerate(rom)}
        cases[layout.pid_lo_addr] = self.descrom_rdat.eq(player_num)
        if layout.player_hi_addr is not None:
            cases[layout.player_hi_addr] = self.descrom_rdat.eq(
                Mux(updated, ascii_hex(player_num[4:8]), rom[layout.player_hi_addr]))
            cases[layout.player_lo_addr] = self.descrom_rdat.eq(
                Mux(updated, ascii_hex(player_num[0:4]), rom[layout.player_lo_addr]))
        cases["default"] = self.descrom_rdat.eq(0)
        self.comb += Case(raddr, cases)

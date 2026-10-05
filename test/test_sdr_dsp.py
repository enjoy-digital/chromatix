#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
SDR firmware DSP (firmware/sdr, built for the host): FFT, LTE cell search, BLE advertising, DECT
and IEEE 802.15.4 decoders, 2.4GHz signal classification, on synthetic ESP32 captures (16MS/s or
80MS/s 8-bit I/Q, ESP32 inverted spectrum, noise, frequency offsets).
"""

import os
import ctypes
import shutil
import tempfile
import subprocess
import unittest

try:
    import numpy as np
except ImportError:
    np = None

SDR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "firmware", "sdr")
SOURCES = ["dsp.c", "lte.c", "ble.c", "dect.c", "zigbee.c", "classify.c"]

# Library ------------------------------------------------------------------------------------------

_lib = None

def sdr_lib():
    global _lib
    if _lib is None:
        out = os.path.join(tempfile.mkdtemp(), "libsdr.so")
        subprocess.check_call(["cc", "-O2", "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror",
            "-Wno-unused-parameter", "-Wno-unused-const-variable", "-o", out] +
            [os.path.join(SDR_DIR, s) for s in SOURCES])
        _lib = ctypes.CDLL(out)
        _lib.ble_address.restype    = ctypes.c_uint64
        _lib.zb_network.restype     = ctypes.c_char_p
        _lib.sig_name.restype       = ctypes.c_char_p
    return _lib

def ptr(a):
    return a.ctypes.data_as(ctypes.c_void_p)

class LTECell(ctypes.Structure):
    _fields_ = [(n, ctypes.c_int) for n in "pci nid1 nid2 tdd subframe cfo_hz pss sss pos".split()]

class BLEPacket(ctypes.Structure):
    _fields_ = [("channel", ctypes.c_int), ("type", ctypes.c_int), ("txadd", ctypes.c_int),
        ("length", ctypes.c_int), ("pdu", ctypes.c_uint8*64), ("pos", ctypes.c_int),
        ("level_db4", ctypes.c_int)]

class DECTBurst(ctypes.Structure):
    _fields_ = [("rfp", ctypes.c_int), ("ta", ctypes.c_int), ("a", ctypes.c_uint8*8),
        ("rfpi", ctypes.c_uint64), ("pos", ctypes.c_int), ("level_db4", ctypes.c_int)]

class ZBFrame(ctypes.Structure):
    _fields_ = [("length", ctypes.c_int), ("psdu", ctypes.c_uint8*32), ("type", ctypes.c_int),
        ("seq", ctypes.c_int), ("pan", ctypes.c_int), ("dst", ctypes.c_uint64),
        ("src", ctypes.c_uint64),
        ("dst_mode", ctypes.c_int), ("src_mode", ctypes.c_int), ("pos", ctypes.c_int),
        ("level_db4", ctypes.c_int), ("errors", ctypes.c_int)]

class LTEMIB(ctypes.Structure):
    _fields_ = [(n, ctypes.c_int) for n in "ports rbs sfn phich_extended phich_ng".split()]

class Sig(ctypes.Structure):
    _fields_ = [(n, ctypes.c_int) for n in "cls khz bw_khz us level_db4 truncated".split()]

# Signals ------------------------------------------------------------------------------------------

def capture(x, offset_hz, fs=16e6, snr_db=20, n=16380, start=0, amp=40, seed=0):
    """Complex baseband -> ESP32 8-bit I/Q capture (Q negated: ESP32 inverted spectrum)."""
    rng = np.random.default_rng(seed)
    y = np.zeros(n, complex)
    m = min(len(x), n - start)
    y[start:start + m] = x[:m]
    y *= np.exp(2j*np.pi*offset_hz*np.arange(n)/fs)
    y += (rng.standard_normal(n) + 1j*rng.standard_normal(n))/np.sqrt(2)*10**(-snr_db/20)
    out = np.empty(2*n, np.int8)
    out[0::2] = np.clip(np.round(amp*y.real), -127, 127)
    out[1::2] = np.clip(np.round(-amp*y.imag), -127, 127)
    return out

def gfsk(bits, bitrate, fs=16e6, h=0.5, bt=0.5):
    """GFSK baseband (bit 1: positive frequency)."""
    sps = fs/bitrate
    t   = np.arange(int(len(bits)*sps))/sps
    m   = 2*np.asarray(bits)[np.minimum(t.astype(int), len(bits) - 1)] - 1.0
    sigma = np.sqrt(np.log(2))/(2*np.pi*bt)*sps
    k = np.arange(-int(4*sigma), int(4*sigma) + 1)
    g = np.exp(-k**2/(2*sigma**2))
    m = np.convolve(m, g/g.sum(), "same")
    return np.exp(1j*np.pi*h*np.cumsum(m)/sps)

def bits_lsb(data):
    return [(b >> k) & 1 for b in data for k in range(8)]

def bits_msb(data):
    return [(b >> (7 - k)) & 1 for b in data for k in range(8)]

# LTE: subframe 0 (PSS: slot 0 symbol 6, SSS: symbol 5, QPSK elsewhere) at 1.92MS/s, resampled.
def lte_pss(nid2):
    u = [25, 29, 34][nid2]
    n = np.arange(62)
    return np.where(n < 31, np.exp(-1j*np.pi*u*n*(n + 1)/63),
        np.exp(-1j*np.pi*u*(n + 1)*(n + 2)/63))

def lte_sss(nid1, nid2):
    def mseq(taps):
        x = [0, 0, 0, 0, 1]
        for i in range(26):
            x.append(sum(x[i + t] for t in taps) % 2)
        return 1 - 2*np.array(x)
    s, c, z = mseq((2, 0)), mseq((3, 0)), mseq((4, 2, 1, 0))
    q1 = nid1//30
    q  = (nid1 + q1*(q1 + 1)//2)//30
    mp = nid1 + q*(q + 1)//2
    m0 = mp % 31
    m1 = (m0 + mp//31 + 1) % 31
    n  = np.arange(31)
    d  = np.zeros(62)
    d[0::2] = s[(n + m0) % 31]*c[(n + nid2) % 31]
    d[1::2] = s[(n + m1) % 31]*c[(n + nid2 + 3) % 31]*z[(n + m0 % 8) % 31]
    return d

def gold(cinit, n):
    x1 = [1] + [0]*30
    x2 = [(cinit >> i) & 1 for i in range(31)]
    for i in range(n + 1600):
        x1.append(x1[i + 3] ^ x1[i])
        x2.append(x2[i + 3] ^ x2[i + 2] ^ x2[i + 1] ^ x2[i])
    return np.array([x1[i + 1600] ^ x2[i + 1600] for i in range(n)])

def lte_pbch(pci, mib, frame):
    """PBCH symbols (240 per frame, 2 ports: SFBC) of a MIB (24 bits): CRC (2 ports mask),
    tail-biting convolutional code, rate matching, scrambling, QPSK."""
    crc = 0
    for b in mib:
        fb  = ((crc >> 15) & 1) ^ b
        crc = (crc << 1) & 0xffff
        if fb:
            crc ^= 0x1021
    c = list(mib) + [((crc ^ 0xffff) >> (15 - i)) & 1 for i in range(16)]
    state = sum(c[39 - j] << (5 - j) for j in range(6))
    d = [[], [], []]
    for k in range(40):
        reg = (c[k] << 6) | state
        for i, g in enumerate((0o133, 0o171, 0o165)):
            d[i].append(bin(reg & g).count("1") & 1)
        state = (state >> 1) | (c[k] << 5)
    perm = [1, 17, 9, 25, 5, 21, 13, 29, 3, 19, 11, 27, 7, 23, 15, 31, 0, 16, 8, 24, 4, 20, 12, 28,
        2, 18, 10, 26, 6, 22, 14, 30]
    w = [d[s][r*32 + col - 24] for s in range(3) for col in perm for r in range(2)
        if r*32 + col >= 24]
    e = np.array([w[j % 120] for j in range(1920)])[480*frame:480*(frame + 1)]
    e ^= gold(pci, 1920)[480*frame:480*(frame + 1)]
    x = ((1 - 2.0*e[0::2]) + 1j*(1 - 2.0*e[1::2]))/np.sqrt(2)
    y0, y1 = x.copy(), np.zeros(240, complex)
    y1[0::2], y1[1::2] = -np.conj(x[1::2]), np.conj(x[0::2])
    return y0/np.sqrt(2), y1/np.sqrt(2)

def lte_signal(pci, seed=0, mib=None, frame=0):
    """2 subframes: PSS/SSS (subframe 0), PBCH (MIB, 2 ports: CRS/SFBC, port 1 channel: 0.5j)."""
    rng  = np.random.default_rng(seed)
    ks   = np.r_[np.arange(-36, 0), np.arange(1, 37)]
    syms = []
    if mib is not None:
        p0, p1 = lte_pbch(pci, mib, frame)
        pbch = p0 + 0.5j*p1
    for subframe in range(2):
        for slot in range(2):
            for l in range(7):
                X = np.zeros(128, complex)
                X[ks % 128] = (rng.choice([-1, 1], 72) + 1j*rng.choice([-1, 1], 72))/np.sqrt(2)
                if subframe == 0 and slot == 0 and l in (5, 6):
                    X[np.r_[np.arange(-31, 0), np.arange(1, 32)] % 128] = \
                        lte_sss(pci//3, pci % 3) if l == 5 else lte_pss(pci % 3)
                if mib is not None and subframe == 0 and slot == 1 and l < 4:
                    # PBCH REs (CRS positions of 4 ports excluded in symbols 0/1), CRS ports 0/1.
                    v = pci % 6
                    k = [k for k in range(72) if l >= 2 or (k - v) % 3]
                    n = sum(48 if j < 2 else 72 for j in range(l))
                    X[ks[k] % 128] = pbch[n:n + len(k)]
                    if l < 2:
                        X[ks[[k for k in range(72) if (k - v) % 3 == 0]] % 128] = 0
                    if l == 0:
                        cr = gold((1 << 10)*15*(2*pci + 1) + 2*pci + 1, 232)[208:]
                        r  = ((1 - 2.0*cr[0::2]) + 1j*(1 - 2.0*cr[1::2]))/np.sqrt(2)
                        X[ks[[6*m + v for m in range(12)]] % 128] = r
                        X[ks[[6*m + (3 + v) % 6 for m in range(12)]] % 128] = 0.5j*r
                x = np.fft.ifft(X)*np.sqrt(128)
                cp = 10 if l == 0 else 9
                syms.append(np.r_[x[-cp:], x])
    x = np.concatenate(syms)                        # 3840 samples (2ms).
    # 1.92MS/s -> 16MS/s (x25/3): spectrum zero padded.
    X = np.fft.fft(x)
    M = len(x)*25//3
    Y = np.zeros(M, complex)
    Y[:len(x)//2] = X[:len(x)//2]
    Y[-len(x)//2:] = X[-len(x)//2:]
    return np.fft.ifft(Y)*M/len(x)/2

# BLE advertising packet (LSB first, whitened PDU + CRC).
def ble_packet(channel, pdu):
    crc = 0x555555
    for b in bits_lsb(pdu):
        fb  = ((crc >> 23) & 1) ^ b
        crc = (crc << 1) & 0xffffff
        if fb:
            crc ^= 0x00065b
    bits = bits_lsb(pdu) + [(crc >> (23 - i)) & 1 for i in range(24)]
    lfsr = 0x40 | channel
    for i in range(len(bits)):
        w = lfsr & 1
        bits[i] ^= w
        lfsr = (lfsr >> 1) | (w << 6)
        if w:
            lfsr ^= 1 << 2
    return [0, 1]*4 + bits_lsb((0x8E89BED6).to_bytes(4, "little")) + bits

# DECT RFP burst: S-field, A-field (N_T tail: RFPI, R-CRC), B-field.
def dect_burst(rfpi, rfp=True, seed=0):
    a = bytes([(3 << 5) | 0x01]) + rfpi.to_bytes(5, "big")
    crc = 0
    for b in bits_msb(a):
        fb  = ((crc >> 15) & 1) ^ b
        crc = (crc << 1) & 0xffff
        if fb:
            crc ^= 0x0589
    a += (crc ^ 1).to_bytes(2, "big")
    s = (0xAAAAE98A if rfp else 0x55551675).to_bytes(4, "big")
    return bits_msb(s) + bits_msb(a) + list(np.random.default_rng(seed).integers(0, 2, 328))

# IEEE 802.15.4 O-QPSK (half-sine, I: even chips, Q: odd chips delayed by Tc).
ZB_CHIPS = [0xd9c3522e, 0xed9c3522, 0x2ed9c352, 0x22ed9c35, 0x522ed9c3, 0x3522ed9c, 0xc3522ed9,
    0x9c3522ed, 0x8c96077b, 0xb8c96077, 0x7b8c9607, 0x77b8c960, 0x077b8c96, 0x6077b8c9, 0x96077b8c,
    0xc96077b8]

def zigbee_signal(psdu, fs=16e6):
    fcs = 0
    for b in bits_lsb(psdu):
        fb  = (fcs ^ b) & 1
        fcs >>= 1
        if fb:
            fcs ^= 0x8408
    psdu = psdu + bytes([fcs & 0xff, fcs >> 8])
    ppdu = bytes(4) + bytes([0xa7, len(psdu)]) + psdu
    chips = [(ZB_CHIPS[s] >> (31 - k)) & 1 for b in ppdu for s in (b & 0xf, b >> 4)
        for k in range(32)]
    sps = fs/2e6
    t   = np.arange(int((len(chips) + 1)*sps))/sps
    x   = np.zeros(len(t), complex)
    for k, c in enumerate(chips):
        tk = t - k
        p  = np.where((tk >= 0) & (tk < 2), np.sin(np.pi*tk/2), 0)*(2*c - 1)
        x += p if k % 2 == 0 else 1j*p
    return x

# Tests --------------------------------------------------------------------------------------------

@unittest.skipUnless(np is not None and shutil.which("cc"), "numpy and a host C compiler needed")
class TestSDRDSP(unittest.TestCase):
    def test_fft(self):
        lib = sdr_lib()
        rng = np.random.default_rng(0)
        for log2n in (7, 11):
            n = 1 << log2n
            x = (rng.standard_normal(n) + 1j*rng.standard_normal(n))*2**20
            buf = np.empty(2*n, np.int32)
            buf[0::2], buf[1::2] = x.real, x.imag
            lib.dsp_fft(ptr(buf), log2n, 0)
            y = (buf[0::2] + 1j*buf[1::2])*n
            ref = np.fft.fft(np.round(x.real) + 1j*np.round(x.imag))
            self.assertLess(np.abs(y - ref).max()/np.abs(ref).max(), 1e-3)

    def test_lte(self):
        lib = sdr_lib()
        lib.lte_init()
        x = lte_signal(pci=388)
        for snr, cfo, wide in [(20, 0, 0), (10, 8000, 1), (5, -6000, 1)]:
            # Carrier 2MHz below the tuned frequency, PSS within the 1ms capture.
            cap  = capture(x[4000:], -2e6 + cfo, snr_db=snr, amp=30)
            cell = LTECell()
            self.assertEqual(lib.lte_search(ptr(cap), 16380, -2000000, wide, ctypes.byref(cell)), 1)
            self.assertEqual((cell.pci, cell.tdd), (388, 0))
            self.assertLess(abs(cell.cfo_hz - cfo), 1500) # 500Hz steps, PSS: 66.7us.

    def test_lte_mib(self):
        lib = sdr_lib()
        lib.lte_init()
        # MIB: 50 RBs (10MHz), normal PHICH duration, Ng 1, SFN 4*0x5a + frame.
        mib = [0, 1, 1, 0, 1, 0] + [int(b) for b in format(0x5a, "08b")] + [0]*10
        for frame, snr in [(0, 20), (3, 10)]:
            x    = lte_signal(pci=101, mib=mib, frame=frame)
            cap  = capture(x[1500:], -2e6, snr_db=snr, amp=30)
            cell = LTECell()
            self.assertEqual(lib.lte_search(ptr(cap), 16380, -2000000, 1, ctypes.byref(cell)), 1)
            self.assertEqual((cell.pci, cell.subframe), (101, 0))
            m = LTEMIB()
            self.assertEqual(lib.lte_mib(ctypes.byref(cell), ctypes.byref(m)), 1)
            self.assertEqual((m.ports, m.rbs, m.phich_ng, m.sfn), (2, 50, 2, 4*0x5a + frame))

    def test_lte_noise(self):
        lib = sdr_lib()
        lib.lte_init()
        cell = LTECell()
        for seed in range(4):
            cap = capture(np.zeros(1), 0, snr_db=-20, seed=seed, amp=2)
            self.assertEqual(lib.lte_search(ptr(cap), 16380, -2000000, 1, ctypes.byref(cell)), 0)

    def test_ble(self):
        lib = sdr_lib()
        # ADV_NONCONN_IND, random address, flags + Apple Find My (separated: 25 bytes).
        adv = bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0xc6])
        ad  = bytes([2, 0x01, 0x06, 27, 0xff, 0x4c, 0x00, 0x12, 25]) + bytes(range(25))
        pdu = bytes([0x42, len(adv + ad)]) + adv + ad
        x   = gfsk(ble_packet(37, pdu), 1e6)
        pkts = (BLEPacket*4)()
        for snr in (20, 10):
            cap = capture(x, -2e6, snr_db=snr, start=1000)
            self.assertEqual(lib.ble_decode(ptr(cap), 16380, -2000000, 37, pkts, 4), 1)
            self.assertEqual(lib.ble_address(ctypes.byref(pkts[0])), 0xc65544332211)
            text = ctypes.create_string_buffer(32)
            self.assertEqual(lib.ble_describe(ctypes.byref(pkts[0]), text, 32), 1)
            self.assertEqual(text.value, b"FINDMY SEPARATED")

    def test_dect(self):
        lib = sdr_lib()
        bursts = (DECTBurst*4)()
        for snr, off, rfp in [(20, -864000, True), (8, 864000, True), (10, 864000, False)]:
            x   = gfsk(dect_burst(0x0123456789, rfp=rfp), 1.152e6)
            cap = capture(x, off + 20000, snr_db=snr, start=2000)
            self.assertEqual(lib.dect_decode(ptr(cap), 16380, off, bursts, 4), 1)
            self.assertEqual(bursts[0].rfp, int(rfp))
            self.assertEqual(bursts[0].rfpi, 0x0123456789 if rfp else 0)

    def test_zigbee(self):
        lib = sdr_lib()
        frames = (ZBFrame*4)()
        # Data frame: PAN 0x1a62, 0x0001 -> 0xffff, Zigbee NWK frame control.
        data = bytes([0x41, 0x88, 0x17, 0x62, 0x1a, 0xff, 0xff, 0x01, 0x00, 0x08, 0x00, 0xfd, 0xff])
        for psdu, snr, off in [(data, 15, 2500000), (bytes([0x02, 0x00, 0x42]), 8, -2500000)]:
            cap = capture(zigbee_signal(psdu), off + 30000, snr_db=snr, start=500)
            self.assertEqual(lib.zb_decode(ptr(cap), 16380, off, frames, 4), 1)
            self.assertEqual(frames[0].seq, psdu[2])
            if psdu is data:
                self.assertEqual((frames[0].type, frames[0].pan, frames[0].src), (1, 0x1a62, 1))
                self.assertEqual(lib.zb_network(ctypes.byref(frames[0])), b"ZIGBEE")

    def test_classify(self):
        lib = sdr_lib()
        lib.sig_init()
        fs, n = 80e6, 16380
        rng = np.random.default_rng(0)
        # 80MS/s capture at 2420MHz: 18MHz wide burst at 2432MHz (Wi-Fi channel 5, 100us), BLE
        # advertising burst at 2402MHz (150us).
        wide = np.zeros(n, complex)
        X = np.zeros(2048, complex)
        X[np.r_[1:231, -230:0]] = np.exp(2j*np.pi*rng.random(460))
        burst = np.tile(np.fft.ifft(X)*2048/np.sqrt(460), 4)[:8000]
        wide[2000:10000] = burst*np.exp(2j*np.pi*12e6*np.arange(8000)/fs)
        ble = np.zeros(n, complex)
        g = gfsk(list(rng.integers(0, 2, 150)), 1e6, fs)
        ble[2000:2000 + len(g)] = g*np.exp(-2j*np.pi*18e6*np.arange(len(g))/fs)
        cap  = capture(wide + ble, 0, fs=fs, snr_db=15)
        sigs = (Sig*16)()
        count = lib.sig_detect(ptr(cap), n, 2420000, 0, sigs, 16, None)
        found = {lib.sig_name(sigs[i].cls).decode(): sigs[i] for i in range(count)}
        self.assertIn("WIFI", found)
        self.assertLess(abs(found["WIFI"].khz - 2432000), 1500)
        self.assertIn("BLE ADV", found)
        self.assertLess(abs(found["BLE ADV"].khz - 2402000), 700)

if __name__ == "__main__":
    unittest.main()

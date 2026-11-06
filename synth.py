"""Synthetic Meteor-M LRPT transmitter (test harness).

Builds MSU-MR segments -> CCSDS packets -> VCDUs -> RS(255,223)x4 -> randomiser -> ASM ->
NRZ-M -> K=7 r=1/2 convolutional code -> (80k: convolutional interleaver + 0x27 markers) ->
OQPSK with RRC(0.5) pulse shaping -> complex baseband with carrier offset and AWGN.
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from meteorrx import core, lrpt  # noqa: E402
from meteorrx.receiver import INT_BRANCHES, INT_DATA, INT_DELAY, INT_MARKER  # noqa: E402


def test_scene(lines: int, ch: int) -> np.ndarray:
    """Recognisable test pattern per channel: gradients, circles, channel-specific bars."""
    h = lines * 8
    y, x = np.mgrid[0:h, 0:lrpt.IMG_WIDTH].astype(np.float64)
    img = 40 + 120 * (x / lrpt.IMG_WIDTH) + 60 * np.sin(y / 23.0 + ch)
    for cx, cy, r in [(400, h * 0.4, 120), (1100, h * 0.6, 180), (800, h * 0.2, 60)]:
        img[(x - cx) ** 2 + (y - cy) ** 2 < r * r] = 230 - 40 * ch
    img[:, 1450 + ch * 30:1470 + ch * 30] = 10
    return np.clip(img, 0, 255).astype(np.uint8)


def build_packets(lines: int, apids=(64, 65, 66), qf: int = 77, seq0: int = 100):
    scenes = {a: test_scene(lines, a - 64) for a in apids}
    pkts = []
    seq = seq0
    for li in range(lines):
        for a in apids:
            for s in range(14):
                pix = scenes[a][li * 8:(li + 1) * 8, s * 112:(s + 1) * 112]
                body = lrpt.encode_segment_blocks(pix, qf)
                day, ms = 9500, (36000000 + li * 1200)  # arbitrary timestamp
                payload = (day.to_bytes(2, "big") + ms.to_bytes(4, "big") + b"\x00\x00"
                           + bytes([s * 14, 0, 0, 0xFF, 0xF0, qf]) + body)
                pkts.append(_packet(a, seq, payload))
                seq = (seq + 1) & 0x3FFF
        pkts.append(_packet(70, seq, bytes(8) + bytes(range(100))))  # telemetry
        seq = (seq + 1) & 0x3FFF
    return pkts, scenes


def _packet(apid: int, seq: int, payload: bytes) -> bytes:
    h = bytes([0x08 | (apid >> 8), apid & 0xFF, 0xC0 | (seq >> 8), seq & 0xFF,
               (len(payload) - 1) >> 8, (len(payload) - 1) & 0xFF])
    return h + payload


def build_cadus(pkts, scid: int = 0x9E, vcid: int = 5, counter0: int = 1000, lead_frames: int = 3):
    stream = b"".join(pkts)
    starts = []
    pos = 0
    for p in pkts:
        starts.append(pos)
        pos += len(p)
    cadus = []
    counter = counter0
    # idle lead-in frames (vcid 63) so the receiver can lock before imagery starts
    zones = [(None, bytes(lrpt.VCDU_DATA))] * lead_frames
    for off in range(0, len(stream), lrpt.VCDU_DATA):
        zone = stream[off:off + lrpt.VCDU_DATA].ljust(lrpt.VCDU_DATA, b"\x00")
        fhp = next((s - off for s in starts if off <= s < off + lrpt.VCDU_DATA), 0x7FF)
        zones.append((fhp, zone))
    for fhp, zone in zones:
        vc = 63 if fhp is None else vcid
        fh = 0x7FF if fhp is None else fhp
        vcdu = bytes([0x40 | (scid >> 2), ((scid & 3) << 6) | vc, (counter >> 16) & 0xFF, (counter >> 8) & 0xFF,
                      counter & 0xFF, 0, 0, 0, (fh >> 8) & 0x07, fh & 0xFF]) + zone
        assert len(vcdu) == 892
        blk = np.zeros(1020, dtype=np.uint8)
        v = np.frombuffer(vcdu, dtype=np.uint8)
        for k in range(4):
            d = v[k::4]
            blk[k:892:4] = d
            par = core.rs_encode(d)
            blk[892 + k::4] = par
        blk ^= lrpt.PN
        cadus.append(lrpt.ASM.to_bytes(4, "big") + blk.tobytes())
        counter = (counter + 1) & 0xFFFFFF
    return cadus


def coded_bits(cadus) -> np.ndarray:
    bits = np.unpackbits(np.frombuffer(b"".join(cadus), dtype=np.uint8))
    nrzm = np.bitwise_xor.accumulate(bits).astype(np.uint8)  # e[n] = d[n] ^ e[n-1]
    enc, _ = core.conv_encode(nrzm)
    return enc


def interleave_80k(enc: np.ndarray, rng) -> np.ndarray:
    """Transmit side: branch b delayed by b*D, sync marker 0x27 before every 72 data bits."""
    n = len(enc) + (INT_BRANCHES - 1) * INT_DELAY  # flush so all data is transmitted
    n = ((n + INT_DATA - 1) // INT_DATA) * INT_DATA
    k = np.arange(n, dtype=np.int64)
    src = k - (k % INT_BRANCHES) * INT_DELAY
    x = rng.integers(0, 2, n).astype(np.uint8)
    ok = (src >= 0) & (src < len(enc))
    x[ok] = enc[src[ok]]
    mk = np.unpackbits(np.array([INT_MARKER], dtype=np.uint8))
    rows = x.reshape(-1, INT_DATA)
    out = np.concatenate([np.tile(mk, (rows.shape[0], 1)), rows], axis=1)
    return out.reshape(-1)


def oqpsk_modulate(bits: np.ndarray, sym_rate: float, fs: float, alpha: float = 0.5) -> np.ndarray:
    """bits -> OQPSK baseband at fs. Q branch leads I by T/2 (matches SatDump alignment)."""
    from fractions import Fraction
    if len(bits) % 2:
        bits = bits[:-1]
    sy = (2.0 * bits.astype(np.float64) - 1.0).reshape(-1, 2)
    L = 10  # samples per symbol at the generation rate (even -> T/2 integer)
    gen_rate = sym_rate * L
    h = core.rrc_taps(L, alpha, 11 * L + 1).astype(np.float64)
    ns = len(sy)
    i_up = np.zeros(ns * L + L)
    q_up = np.zeros(ns * L + L)
    i_up[L // 2: L // 2 + ns * L: L] = sy[:, 0]  # I delayed by T/2
    q_up[0: ns * L: L] = sy[:, 1]
    i_bb = np.convolve(i_up, h, mode="same")
    q_bb = np.convolve(q_up, h, mode="same")
    x = i_bb + 1j * q_bb
    fr = Fraction(fs / gen_rate).limit_denominator(1000)
    from scipy.signal import resample_poly
    y = resample_poly(x, fr.numerator, fr.denominator)
    return (y / np.sqrt(np.mean(np.abs(y) ** 2))).astype(np.complex64)


def make_iq(mode: str = "72k", lines: int = 24, fs: float = 1.2e6, center: float = 137.5e6,
            freq: float = 137.9e6, offset_hz: float = 1200.0, snr_db: float = 12.0, seed: int = 7,
            pad_s: float = 1.0, alpha: float = 0.5):
    """Returns (iq complex64 at fs, scenes dict, expected number of CADUs)."""
    rng = np.random.default_rng(seed)
    pkts, scenes = build_packets(lines)
    cadus = build_cadus(pkts)
    enc = coded_bits(cadus)
    rate = 72000.0 if mode == "72k" else 80000.0
    if mode == "80k":
        enc = interleave_80k(enc, rng)
    bb = oqpsk_modulate(enc, rate, fs, alpha=alpha)
    pad = int(pad_s * fs)
    bb = np.concatenate([np.zeros(pad, np.complex64), bb, np.zeros(pad, np.complex64)])
    t = np.arange(len(bb)) / fs
    f_if = freq - center + offset_hz
    ph = 2 * np.pi * f_if * t
    sig = bb * (np.cos(ph) + 1j * np.sin(ph)).astype(np.complex64)
    # SNR defined in the signal bandwidth (~1.5*Rs)
    noise_bw_ratio = fs / (rate * 1.5)
    nvar = 10 ** (-snr_db / 10) * noise_bw_ratio
    noise = (rng.normal(0, np.sqrt(nvar / 2), len(sig)) + 1j * rng.normal(0, np.sqrt(nvar / 2), len(sig)))
    iq = (sig + noise) * 0.1
    return iq.astype(np.complex64), scenes, len(cadus)

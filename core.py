"""DSP / FEC front-end: filter design and stateful wrappers around the Numba kernels in dsp.py."""
from __future__ import annotations

import numpy as np

from . import dsp

VIT_BLOCK = dsp.VIT_BLOCK


# ----------------------------------------------------------------------------
# Filter design helpers (numpy only)
# ----------------------------------------------------------------------------
def lowpass_taps(fs: float, cutoff: float, transition: float) -> np.ndarray:
    """Windowed-sinc low-pass, Blackman-Harris window, unity DC gain."""
    n = int(np.ceil(4.0 * fs / transition)) | 1
    n = max(n, 31)
    t = np.arange(n) - (n - 1) / 2
    h = 2 * cutoff / fs * np.sinc(2 * cutoff / fs * t)
    a = [0.35875, 0.48829, 0.14128, 0.01168]
    w = (a[0] - a[1] * np.cos(2 * np.pi * np.arange(n) / (n - 1))
         + a[2] * np.cos(4 * np.pi * np.arange(n) / (n - 1))
         - a[3] * np.cos(6 * np.pi * np.arange(n) / (n - 1)))
    h *= w
    return (h / h.sum()).astype(np.float32)


def rrc_taps(sps: float, alpha: float, ntaps: int) -> np.ndarray:
    """Root-raised-cosine taps, unity peak-normalised energy."""
    t = (np.arange(ntaps) - (ntaps - 1) / 2) / sps
    h = np.zeros(ntaps)
    for i, ti in enumerate(t):
        if abs(ti) < 1e-9:
            h[i] = 1.0 - alpha + 4 * alpha / np.pi
        elif alpha > 0 and abs(abs(ti) - 1 / (4 * alpha)) < 1e-9:
            h[i] = (alpha / np.sqrt(2)) * ((1 + 2 / np.pi) * np.sin(np.pi / (4 * alpha))
                                           + (1 - 2 / np.pi) * np.cos(np.pi / (4 * alpha)))
        else:
            h[i] = (np.sin(np.pi * ti * (1 - alpha)) + 4 * alpha * ti * np.cos(np.pi * ti * (1 + alpha))) / (
                np.pi * ti * (1 - (4 * alpha * ti) ** 2))
    h /= np.sqrt(np.sum(h * h))
    return h.astype(np.float32)


# ----------------------------------------------------------------------------
# Object wrappers
# ----------------------------------------------------------------------------
class Channelizer:
    """NCO frequency shift + integer decimation."""

    def __init__(self, fs: float, decim: int, cutoff: float, transition: float):
        self.fs = fs
        self.decim = decim
        self.out_rate = fs / decim
        self.taps = lowpass_taps(fs, cutoff, transition).astype(np.float64)
        self._hist = np.zeros(2 * len(self.taps), np.complex128)
        self._st = np.zeros(4, np.float64)
        self.offset_hz = 0.0

    def set_offset(self, hz: float):
        self.offset_hz = hz
        self._st[1] = -2.0 * np.pi * hz / self.fs

    def process(self, iq: np.ndarray) -> np.ndarray:
        x = np.ascontiguousarray(iq, dtype=np.complex64)
        out = np.empty(len(x) // self.decim + 2, np.complex128)
        m = dsp.chan_process(x, self.taps, self._hist, self._st, self.decim, out)
        return out[:m].astype(np.complex64)


class Demodulator:
    """Resample to 2 sps, AGC, FLL, RRC, Costas, OQPSK alignment, M&M clock recovery."""

    def __init__(self, in_rate: float, sym_rate: float, oqpsk: bool = True, rrc_alpha: float = 0.5,
                 rrc_ntaps: int = 31, pll_bw: float = 0.005, clock_gain_mu: float = 8.7e-3,
                 clock_omega_limit: float = 0.005, fll_gain: float = 2e-5, costas_post: bool = True):
        self.in_rate = in_rate
        self.sym_rate = sym_rate
        self.rrc = rrc_taps(2.0, rrc_alpha, rrc_ntaps).astype(np.float64)
        self.st = dsp.demod_state(in_rate, sym_rate, pll_bw, clock_gain_mu, clock_gain_mu ** 2 / 4.0,
                                  clock_omega_limit, oqpsk, fll_gain, costas_post)
        self._rb = np.zeros(4, np.complex128)
        self._rh = np.zeros(2 * len(self.rrc), np.complex128)
        self._mb = np.zeros(1 << 20, np.complex128)

    def set_options(self, fll_gain: float, costas_post: bool):
        self.st[dsp.FLL_G] = fll_gain
        self.st[dsp.C_POST] = 1.0 if costas_post else 0.0

    def process(self, iq: np.ndarray):
        x = np.ascontiguousarray(iq, dtype=np.complex64)
        est = int(len(x) * 2 * self.sym_rate / self.in_rate) + 64
        soft = np.empty(2 * est, np.int8)
        syms = np.empty(est, np.complex64)
        wave = np.empty(2 * est + 64, np.complex64)
        strobes = np.empty(est, np.float64)
        k, nw = dsp.demod_process(x, self.st, self._rb, self.rrc, self._rh, self._mb, soft, syms, wave, strobes)
        return soft[:2 * k], syms[:k], wave[:nw], strobes[:k].astype(np.float32)

    @property
    def freq_hz(self) -> float:
        return float(self.st[dsp.C_FR] + self.st[dsp.FLL_FR]) * (2 * self.sym_rate) / (2 * np.pi)

    @property
    def omega(self) -> float:
        return float(self.st[dsp.MM_OM])

    def shift_freq(self, hz: float):
        """Remove hz from the carrier frequency registers (the channel NCO took it over)."""
        r = 2 * np.pi * hz / (2 * self.sym_rate)
        st = self.st
        if st[dsp.FLL_G] > 0.0:
            st[dsp.FLL_FR] += r
        else:
            st[dsp.C_FR] = float(np.clip(st[dsp.C_FR] + r, -st[dsp.C_MAXF], st[dsp.C_MAXF]))

    def reset_freq(self):
        self.st[dsp.C_FR] = self.st[dsp.FLL_FR] = 0.0

    def reset_loops(self):
        st = self.st
        st[dsp.C_FR] = st[dsp.C_PH] = st[dsp.FLL_FR] = 0.0
        st[dsp.MM_OM] = st[dsp.MM_OMID]
        st[dsp.MM_MU] = 0.5


class Viterbi:
    def __init__(self):
        self.reset()

    def reset(self):
        self._metric = np.zeros(64, np.int64)
        self._dec = np.zeros(dsp.VIT_CAP, np.uint64)
        self._hard = np.zeros(dsp.VIT_CAP, np.uint8)
        self._vs = np.zeros(4, np.int64)

    def process(self, soft: np.ndarray) -> np.ndarray:
        s = np.ascontiguousarray(soft, dtype=np.int8)
        s = s[: (len(s) // 2) * 2]
        out = np.empty(len(s) // 2 + VIT_BLOCK + 16, np.uint8)
        k = dsp.vit_process(s, self._metric, self._dec, self._hard, self._vs, dsp._VIT_OUT, dsp._VIT_MASK, out)
        return out[:k]

    def take_ber(self) -> float:
        e, t = self._vs[2], self._vs[3]
        self._vs[2] = self._vs[3] = 0
        return float(e) / float(t) if t else -1.0


def conv_encode(bits: np.ndarray, state: int = 0):
    b = np.ascontiguousarray(bits, dtype=np.uint8)
    out = np.empty(2 * len(b), np.uint8)
    st = dsp.conv_encode(state, b, out)
    return out, int(st)


def rs_encode(data223: np.ndarray) -> np.ndarray:
    par = np.zeros(32, np.uint8)
    dsp.rs_encode(np.ascontiguousarray(data223, dtype=np.uint8), par, dsp.ALPHA_TO, dsp.INDEX_OF, dsp.GENPOLY)
    return par


def rs_decode_interleaved(block: np.ndarray, depth: int = 4):
    """Decode in place; returns (ok, errs list)."""
    errs = np.zeros(depth, np.int64)
    ok = dsp.rs_decode_interleaved(block, depth, errs, dsp.ALPHA_TO, dsp.INDEX_OF)
    return bool(ok), [int(e) for e in errs]


def u8_to_c64(raw: np.ndarray) -> np.ndarray:
    """RTL-SDR interleaved uint8 I/Q -> complex64."""
    return ((raw.astype(np.float32) - 127.4) * (1.0 / 128.0)).view(np.complex64)

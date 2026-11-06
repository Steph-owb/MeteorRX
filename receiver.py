"""Signal path: wideband IQ -> channel -> OQPSK demod -> (80k deinterleave) -> Viterbi -> LRPT stack."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from . import core, lrpt

MODES = {"72k": 72000.0, "80k": 80000.0}

# Demodulator defaults (v1.5.1, from tools/bench_sensitivity.py): the AFC removes the carrier offset,
# so a narrow classic Costas loop is used without FLL (an FLL or a wide loop slips at low Es/N0).
DEFAULT_DSP = {"pll_bw": 0.0014, "fll_gain": 0.0, "costas_post": False, "clock_gain_mu": 0.002,
               "rrc_alpha": 0.5}

# 80k interleaving parameters (Meteor-M2-x "LRPT 80k")
INT_MARKER = 0x27
INT_STRIDE = 80
INT_DATA = 72
INT_BRANCHES = 36
INT_DELAY = 2048 * 36  # 73728


def rotate_soft(s: np.ndarray, quarter_turns: int) -> np.ndarray:
    """Rotate interleaved soft I/Q pairs by k*90 degrees."""
    k = quarter_turns % 4
    if k == 0:
        return s
    a = s.reshape(-1, 2).astype(np.int16)
    i, q = a[:, 0], a[:, 1]
    if k == 1:
        r = np.stack([-q, i], axis=1)
    elif k == 2:
        r = np.stack([-i, -q], axis=1)
    else:
        r = np.stack([q, -i], axis=1)
    return np.clip(r, -127, 127).astype(np.int8).reshape(-1)


class PairFixer:
    """Streaming soft-pair correction: optional 1-bit shift, optional I/Q swap (mirrored spectrum),
    k*90 deg rotation, then re-pairing (I_k, Q_{k+d}). On OQPSK a 90 deg carrier ambiguity also skews
    the two branches by one symbol, which plain QPSK rotations cannot undo.

    The I/Q swap is required for the real Meteor-M satellites: their downlink reaches the receiver
    mirrored with respect to the textbook convention (SatDump checks it too, Viterbi1_2 check_iq_swap).
    Rotations alone cannot produce a mirror image, so without it a real pass never locks."""

    def __init__(self, rot: int = 0, d: int = 0, shift: int = 0, swap: int = 0):
        self.rot, self.d, self.shift, self.swap = rot, d, shift, swap
        self.carry = np.zeros(0, dtype=np.int8)
        self.hold = np.zeros(0, dtype=np.int8)   # delayed branch samples
        self._first = True

    def push(self, soft: np.ndarray) -> np.ndarray:
        s = np.concatenate([self.carry, soft])
        if self._first and self.shift:
            s = s[1:]
        self._first = False
        n = (len(s) // 2) * 2
        self.carry = s[n:]
        s2 = s[:n]
        if self.swap:
            s2 = s2.reshape(-1, 2)[:, ::-1].reshape(-1)
        p = rotate_soft(s2, self.rot).reshape(-1, 2)
        if self.d == 0 or len(p) == 0:
            return p.reshape(-1)
        if len(self.hold) == 0:
            self.hold = np.zeros(1, dtype=np.int8)  # one erased symbol at start
        if self.d > 0:   # pair (I_{k-1}, Q_k): delay I by one symbol
            i = np.concatenate([self.hold, p[:, 0]])
            q = p[:, 1]
        else:            # pair (I_k, Q_{k-1}): delay Q by one symbol
            i = p[:, 0]
            q = np.concatenate([self.hold, p[:, 1]])
        if self.d > 0:
            self.hold, i = i[-1:], i[:-1]
        else:
            self.hold, q = q[-1:], q[:-1]
        return np.stack([i, q], axis=1).reshape(-1)


# (rotation, I/Q skew, bit shift, I/Q swap): 0/90 deg x skew -1/0/+1 x mirror. 180 deg is handled by NRZ-M.
HYPOTHESES = [(r, d, 0, sw) for sw in (1, 0) for r in (0, 1) for d in (0, -1, 1)]


class Deinterleaver80k:
    """80k LRPT: sync-marker tracking, rotation/pairing correction and convolutional de-interleaving.

    The marker grid (one 0x27 marker every 80 soft bits) is tracked in absolute stream position. When
    the markers are lost (carrier phase slip, fade) and found again, the number of 80-bit blocks elapsed
    is derived from the absolute positions and the missing blocks are fed to the de-interleaver as
    erasures, so the 36-branch delay lines stay aligned - a single misaligned block would otherwise
    corrupt the whole interleaver span (~16 s)."""

    MAX_BAD_RUN = 8

    def __init__(self):
        self.total = 0                          # soft bits received (absolute position counter)
        self.raw = np.zeros(0, dtype=np.int8)   # unsynchronised soft bits (marker search)
        self.raw_start = 0                      # absolute position of raw[0]
        self.buf = np.zeros(0, dtype=np.int8)   # pair-fixed soft bits, buf[0] = start of a marker
        self.buf_abs = 0                        # absolute position of buf[0]
        self.fixer: PairFixer | None = None
        self.synced = False
        self.ring = np.zeros(INT_BRANCHES * INT_DELAY, dtype=np.int8)
        self.k = 0                              # data bits written into the de-interleaver
        self.next_abs = None                    # absolute position of the next expected marker
        self.sync_count = 0
        self.good_run = 0
        self.erased_blocks = 0

    @staticmethod
    def _marker_bits():
        return np.unpackbits(np.array([INT_MARKER], dtype=np.uint8)).astype(bool)

    def _write_read(self, x: np.ndarray) -> np.ndarray:
        out = np.empty(len(x), dtype=np.int8)
        R = len(self.ring)
        pos = 0
        while pos < len(x):
            n = min(len(x) - pos, INT_DELAY)
            kk = self.k + np.arange(n, dtype=np.int64)
            self.ring[kk % R] = x[pos:pos + n]
            src = kk - (INT_BRANCHES - 1 - (kk % INT_BRANCHES)) * INT_DELAY
            vals = self.ring[src % R]
            vals[src < 0] = 0
            out[pos:pos + n] = vals
            self.k += n
            pos += n
        return out

    def _erase_blocks(self, m: int) -> np.ndarray:
        self.erased_blocks += m
        n = m * INT_DATA
        if n >= len(self.ring):          # gap longer than the interleaver span: start afresh
            self.ring[:] = 0
            self.k += n
            return np.zeros(0, dtype=np.int8)
        return self._write_read(np.zeros(n, dtype=np.int8))

    SYNC_SCORE = 0.75   # fraction of marker bits matching (random data ~0.5, BER 0.12 -> 0.88)

    def _find_sync(self):
        """Returns the erasure output produced to bridge a gap (possibly empty), or None if no sync."""
        need = INT_STRIDE * 48
        if len(self.raw) < need + 8:
            return None
        mk = self._marker_bits()
        best = (-1, None, 0)
        seg = self.raw[:need + 8]
        for swap in (1, 0):
            for shift in (0, 1):
                for rot in range(4):
                    for d in (0, -1, 1):
                        h = PairFixer(rot, d, shift, swap).push(seg)[:need] > 0
                        rows = h[: (len(h) // INT_STRIDE) * INT_STRIDE].reshape(-1, INT_STRIDE)
                        match = np.zeros(INT_STRIDE)
                        for off in range(INT_STRIDE - 8 + 1):
                            match[off] = np.count_nonzero(rows[:, off:off + 8] == mk)
                        off = int(np.argmax(match))
                        if match[off] > best[0]:
                            best = (match[off], (rot, d, shift, swap), off)
        score, (rot, d, shift, swap), off = best
        if score < self.SYNC_SCORE * 8 * (need // INT_STRIDE):
            keep = INT_STRIDE * 8
            self.raw_start += len(self.raw) - keep
            self.raw = self.raw[-keep:]
            return None
        self.fixer = PairFixer(rot, d, shift, swap)
        fixed = self.fixer.push(self.raw)
        P = self.raw_start + shift + off
        self.buf = fixed[off:]
        self.buf_abs = P
        self.raw = np.zeros(0, dtype=np.int8)
        self.raw_start = self.total
        out = np.zeros(0, dtype=np.int8)
        if self.next_abs is not None:
            m = int(round((P - self.next_abs) / INT_STRIDE))
            if m < 0:                      # these blocks were already written: skip them
                drop = min(-m * INT_STRIDE, len(self.buf) // INT_STRIDE * INT_STRIDE)
                self.buf = self.buf[drop:]
                self.buf_abs += drop
            elif m > 0:
                out = self._erase_blocks(m)
        self.synced = True
        self.good_run = 0
        self.sync_count += 1
        return out

    def push(self, soft: np.ndarray) -> np.ndarray:
        self.total += len(soft)
        if self.synced:
            self.buf = np.concatenate([self.buf, self.fixer.push(soft)])
        else:
            self.raw = np.concatenate([self.raw, soft])
        outs = []
        mk = self._marker_bits()
        while True:
            if not self.synced:
                r = self._find_sync()
                if r is None:
                    break
                if len(r):
                    outs.append(r)
            nblk = len(self.buf) // INT_STRIDE
            if nblk == 0:
                break
            blk = self.buf[:nblk * INT_STRIDE].reshape(nblk, INT_STRIDE)
            good = np.count_nonzero((blk[:, :8] > 0) != mk, axis=1) <= 2
            cut = nblk
            run = 0
            for i, g in enumerate(good):
                if g:
                    run = 0
                    self.good_run += 1
                else:
                    run += 1
                    self.good_run = 0
                if run >= self.MAX_BAD_RUN:
                    cut = i - self.MAX_BAD_RUN + 1
                    break
            if cut > 0:
                outs.append(self._write_read(blk[:cut, 8:].reshape(-1)))
                self.buf = self.buf[cut * INT_STRIDE:]
                self.buf_abs += cut * INT_STRIDE
                self.next_abs = self.buf_abs
            if cut < nblk:                 # markers lost: search again on the following samples
                self.synced = False
                self.buf = np.zeros(0, dtype=np.int8)
                self.raw = np.zeros(0, dtype=np.int8)
                self.raw_start = self.total
                continue
            break
        return np.concatenate(outs) if outs else np.zeros(0, dtype=np.int8)


def dsp_from_config(cfg) -> dict:
    """Demodulator options from [decoder]; empty keys keep DEFAULT_DSP. Keys of v1.5.0 and older
    (pll_bw = 0.005, fll_gain) are ignored on purpose: those settings cost ~3.5 dB of sensitivity."""
    out = {}
    for key, name in (("carrier_loop_bw", "pll_bw"), ("clock_loop_gain", "clock_gain_mu"), ("rrc_alpha", "rrc_alpha")):
        v = (cfg.get("decoder", key, "") or "").strip()
        if v:
            try:
                out[name] = float(v)
            except ValueError:
                pass
    return out


class _Hypothesis:
    """One carrier/pairing hypothesis with its own Viterbi decoder, NRZ-M state and frame synchroniser.
    During acquisition all hypotheses run in parallel; the first one that delivers a CADU passing the
    Reed-Solomon check is the right one (no threshold guessing at low SNR, no frame lost at lock)."""

    def __init__(self, rot: int, d: int, shift: int, swap: int = 0):
        self.fixer = PairFixer(rot, d, shift, swap)
        self.vit = core.Viterbi()
        self.ber = 0.5
        self.nrzm_last = 0
        self.deframer = lrpt.Deframer()

    def push(self, soft: np.ndarray) -> list:
        """Returns the decoded CADUs: list of (cadu, (vcdu, ok, errs, fixed))."""
        bits = self.vit.process(self.fixer.push(soft))
        b = self.vit.take_ber()
        if b >= 0:
            self.ber = b
        if len(bits) == 0:
            return []
        dec, self.nrzm_last = lrpt.nrzm_decode(bits, self.nrzm_last)
        return [(f, lrpt.cadu_to_vcdu(f)) for f in self.deframer.push(dec)]


@dataclass
class ChainStats:
    mode: str = ""
    sym_count: int = 0
    vit_ber: float = 0.5
    vit_locked: bool = False
    frames_ok: int = 0
    frames_bad: int = 0
    deframer_locked: bool = False
    packets: int = 0
    costas_hz: float = 0.0
    deint_synced: bool = False
    last_ok_time: float = 0.0


class LrptChain:
    """Complete decoding chain for one symbol-rate mode."""

    # Viterbi re-encode BER (= channel bit error rate): pure noise gives ~0.165, a wrong orientation of a
    # strong signal (e.g. after a 90 deg Costas slip) 0.12-0.13, the decoding threshold (Es/N0 ~ 2 dB)
    # ~0.105. Lock is decided by a Reed-Solomon-valid CADU; a very low BER (strong signal) locks at once.
    # Unlock above 0.118 for 4 windows (~0.45 s): a slip is caught in half a second instead of waiting
    # NOFRAME_TIMEOUT, while a weak but decodable signal (BER <= 0.11) is kept.
    STRONG_BER = 0.06
    UNLOCK_BER = 0.118
    UNLOCK_WINDOWS = 4
    NOFRAME_TIMEOUT = 4.0  # s of lock without a single good CADU -> relock
    FULL_S = 2.0           # acquisition: all hypotheses for the first 2 s (the 4 s sample ring absorbs the CPU peak),
    GROUP_S = 0.35         # then mirrored / textbook groups alternate every 0.35 s (3 CADUs) to halve the load
    last_swap = 1          # orientation of the last lock (station property): tried first at the next acquisition

    def __init__(self, in_rate: float, mode: str, imager: lrpt.MsuMrImager, cadu_sink=None, cfg=None):
        self.mode = mode
        cfg = cfg or {}
        self.demod = core.Demodulator(in_rate, MODES[mode], oqpsk=True,
                                      rrc_alpha=cfg.get("rrc_alpha", 0.5),
                                      pll_bw=cfg.get("pll_bw", DEFAULT_DSP["pll_bw"]),
                                      clock_gain_mu=cfg.get("clock_gain_mu", DEFAULT_DSP["clock_gain_mu"]),
                                      clock_omega_limit=cfg.get("clock_omega_limit", 0.005),
                                      fll_gain=cfg.get("fll_gain", DEFAULT_DSP["fll_gain"]),
                                      costas_post=cfg.get("costas_post", DEFAULT_DSP["costas_post"]))
        self.costas_post = cfg.get("costas_post", DEFAULT_DSP["costas_post"])
        self.fll_track = cfg.get("fll_gain", DEFAULT_DSP["fll_gain"])
        self.fll_acq = 2.5 * self.fll_track       # faster pull-in until the decoder locks
        self._fll_now = None
        self._set_fll(self.fll_acq)
        self.deint = Deinterleaver80k() if mode == "80k" else None
        # 80k: the marker search resolves rotation/pairing, a single decoder suffices
        self._hyp_set = [(0, 0, 0, 0)] if mode == "80k" else HYPOTHESES
        self._groups = sorted({h[3] for h in self._hyp_set}, reverse=True)   # swap 1 first (real satellites)
        self._group_i = 0
        self._group_left = 0
        self._group_len = int(2 * MODES[mode] * self.GROUP_S)                 # soft values per window
        self._full_len = int(2 * MODES[mode] * self.FULL_S)
        self.hyps = self._start_acquisition()
        self.active: _Hypothesis | None = None
        self.bad_windows = 0
        self.demux = lrpt.Demuxer()
        self.imager = imager
        self.cadu_sink = cadu_sink
        self.stats = ChainStats(mode=mode)
        self.enabled = True
        self.time_scale = 1.0   # <1 when replaying files faster than real time
        self._lock_t = 0.0
        self._lock_frames = 0

    def process(self, iq: np.ndarray, run_fec: bool = True):
        """Returns (symbols for constellation, wave, strobes).

        run_fec=False keeps the demodulator running (display) but skips de-interleaving and
        Viterbi decoding while there is clearly no signal, to save CPU before AOS."""
        soft, syms, wave, strobes = self.demod.process(iq)
        if not run_fec and self.active is None:
            self.stats.sym_count += len(soft) // 2
            self.stats.vit_locked = False
            return syms, wave, strobes
        st = self.stats
        st.sym_count += len(soft) // 2
        st.costas_hz = self.demod.freq_hz
        if self.deint is not None:
            soft = self.deint.push(soft)
            st.deint_synced = self.deint.synced
        if len(soft) == 0:
            return syms, wave, strobes
        if self.active is None:
            self._full_left -= len(soft)
            self._group_left -= len(soft)
            if self._full_left < 0 and self._group_left <= 0 and len(self._groups) > 1:  # next group, fresh decoders
                self._group_i = (self._group_i + 1) % len(self._groups)
                self.hyps = self._new_group()
            best, frames = None, []
            for h in self.hyps:
                fr = h.push(soft)
                if best is None and any(r[1] for _, r in fr):
                    best, frames = h, fr
            if best is None:
                h = min(self.hyps, key=lambda x: x.ber)
                if h.ber < self.STRONG_BER:
                    best = h
            st.vit_ber = (best or min(self.hyps, key=lambda x: x.ber)).ber
            if best is not None:
                self.active = best
                fx = self.deint.fixer if self.deint is not None and self.deint.fixer is not None else best.fixer
                LrptChain.last_swap = fx.swap
                self._group_i = 0
                self.bad_windows = 0
                self._lock_t = time.monotonic()
                self._lock_frames = st.frames_ok
                self._frames(frames)
        else:
            frames = self.active.push(soft)
            st.vit_ber = self.active.ber
            self._frames(frames)
            if self.active.ber > self.UNLOCK_BER:
                self.bad_windows += 1
            else:
                self.bad_windows = 0
            if st.frames_ok > self._lock_frames:
                self._lock_frames = st.frames_ok
                self._lock_t = time.monotonic()
            if self.bad_windows >= self.UNLOCK_WINDOWS or \
                    time.monotonic() - self._lock_t > self.NOFRAME_TIMEOUT * self.time_scale:
                self._relock()
        st.deframer_locked = self.active is not None and self.active.deframer.locked
        st.vit_locked = self.active is not None
        carrier_ok = st.vit_locked or (self.deint is not None and self.deint.synced and self.deint.good_run > 300)
        self._set_fll(self.fll_track if carrier_ok else self.fll_acq)
        return syms, wave, strobes

    def _new_group(self) -> list:
        g = sorted({h[3] for h in self._hyp_set}, reverse=True)
        self._groups = sorted(g, key=lambda x: x != LrptChain.last_swap)
        sw = self._groups[self._group_i % len(self._groups)]
        self._group_left = self._group_len
        return [_Hypothesis(*h) for h in self._hyp_set if h[3] == sw]

    def _start_acquisition(self) -> list:
        """All hypotheses during the first FULL_S seconds, then one orientation group at a time."""
        self._group_i = -1                    # first switch -> group 0 = orientation of the last lock
        self._new_group()
        self._full_left = self._full_len
        self._group_left = 0
        return [_Hypothesis(*h) for h in self._hyp_set]

    def _relock(self):
        self.active = None
        self.hyps = self._start_acquisition()

    def _set_fll(self, g: float):
        if g != self._fll_now:
            self.demod.set_options(g, self.costas_post)
            self._fll_now = g

    def _frames(self, frames: list):
        for f, (vc, ok, _, fixed) in frames:
            if not ok:
                self.stats.frames_bad += 1
                continue
            self.stats.frames_ok += 1
            self.stats.last_ok_time = time.time()
            if self.cadu_sink is not None:
                self.cadu_sink(fixed)
            v = lrpt.Vcdu(vc)
            if v.vcid != 5:
                continue
            for pkt in self.demux.work(v):
                self.imager.work(pkt)
                self.stats.packets += 1


@dataclass
class SignalReport:
    freq_hz: float
    snr_db: float
    centroid_hz: float
    band_dbfs: float = -999.0      # signal power in the channel, noise removed (S-meter)
    n0_dbfs_hz: float = -999.0     # noise density next to the channel


class SignalMonitor:
    """Wideband PSD averaging, per-frequency SNR estimate and carrier centroid."""

    def __init__(self, fs: float, nfft: int = 4096, avg: float = 0.2):
        self.fs = fs
        self.nfft = nfft
        self.avg = avg
        self.psd = None
        self.win = np.blackman(nfft).astype(np.float32)
        self.freqs = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / fs))
        # psd / norm = power per bin, same scale as calib.power_spectrum (0 dBFS = full-scale sine)
        self.norm = float(np.sum(self.win.astype(np.float64) ** 2)) * nfft

    def push(self, iq: np.ndarray):
        n = (len(iq) // self.nfft) * self.nfft
        if n == 0:
            return
        blk = iq[-min(n, self.nfft * 8):].reshape(-1, self.nfft) * self.win
        p = np.mean(np.abs(np.fft.fftshift(np.fft.fft(blk, axis=1), axes=1)) ** 2, axis=0)
        self.psd = p if self.psd is None else (1 - self.avg) * self.psd + self.avg * p

    def report(self, offset_hz: float, half_bw: float = 55e3) -> SignalReport | None:
        if self.psd is None:
            return None
        f = self.freqs
        sig = (f > offset_hz - half_bw) & (f < offset_hz + half_bw)
        ref = ((np.abs(f - offset_hz) > 1.6 * half_bw) & (np.abs(f - offset_hz) < 3.2 * half_bw)
               & (np.abs(f) > 10e3) & (np.abs(f) < 0.45 * self.fs))
        if not sig.any() or not ref.any():
            return None
        noise = float(np.median(self.psd[ref]))
        ps = self.psd[sig]
        s = float(np.mean(ps))
        snr = 10 * np.log10(max(s / noise - 1.0, 1e-3))
        w = np.clip(ps - noise, 0, None)
        cen = float(np.sum(w * f[sig]) / np.sum(w)) if w.sum() > 0 else offset_hz
        r = self.psd[ref]
        nb = float(np.mean(r[r < 5 * np.median(r)])) / self.norm      # robust mean, power per bin
        band = float(np.sum(ps)) / self.norm - nb * len(ps)
        return SignalReport(offset_hz, snr, cen,
                            band_dbfs=10 * np.log10(band) if band > 0 else -999.0,
                            n0_dbfs_hz=10 * np.log10(nb / (self.fs / self.nfft)) if nb > 0 else -999.0)

    def spectrum_db(self) -> np.ndarray | None:
        if self.psd is None:
            return None
        return 10 * np.log10(self.psd + 1e-20)


class CarrierAfc:
    """Non-data-aided carrier frequency estimator for (O)QPSK: the 4th power of the signal removes the
    modulation and leaves a spectral line at 4 x the carrier offset. Detects the carrier well below the
    level where the spectrum shows the signal and measures the offset to a few Hz, so the Costas loop can
    stay narrow (no cycle slips) and never has to pull in on its own.

    Spectra are averaged non-coherently along several drift hypotheses (each spectrum shifted by the
    distance the line would have moved): a Doppler-drifting carrier (no TLE pre-correction) stays one
    sharp line, and the estimate is the frequency *now*, not the mean over the averaging window."""

    NFFT = 1 << 14
    AVG = 8                 # spectra averaged (~0.45 s at 290 kS/s)
    DRIFTS = tuple(range(-120, 121, 20))   # Hz/s hypotheses (LEO Doppler rate at 137 MHz <= ~70 Hz/s)
    THRESH = 5.8            # line / mean noise bin: false alarm < 1e-10 per bin and hypothesis
    SYM_RATES = (72000.0, 80000.0)

    def __init__(self, rate: float, max_offset_hz: float = 15000.0):
        self.rate = rate
        self.df = rate / self.NFFT
        self.tblk = self.NFFT / rate
        self.buf = np.zeros(0, np.complex64)
        self.specs: list[np.ndarray] = []
        self.specs2: list[np.ndarray] = []
        self.win = np.hanning(self.NFFT).astype(np.float32)
        f = np.fft.fftfreq(self.NFFT, 1 / rate)
        self.band = np.nonzero(np.abs(f) <= min(4 * max_offset_hz, 0.45 * rate))[0]
        self.f = f
        self.estimate: float | None = None     # Hz, residual offset at the end of the last block
        self.drift = 0.0                       # Hz/s of the last detection
        self.level_db = 0.0                    # line / noise (dB) of the last estimate
        self.last_detect = -1e9
        self.blocks = 0

    def reset(self):
        """Forget the averaged spectra (after the frequency correction was changed)."""
        self.specs = []
        self.specs2 = []
        self.estimate = None

    def _aligned(self, specs, mult: float, drift: float) -> np.ndarray:
        n = len(specs)
        acc = np.zeros(self.NFFT)
        for k, sp in enumerate(specs):
            age = n - 1 - k
            sh = int(round(mult * drift * age * self.tblk / self.df))
            acc += np.roll(sp, sh) if sh else sp
        return acc / n

    def push(self, y: np.ndarray) -> float | None:
        """Feed channel samples; returns a new offset estimate (Hz) when one is available."""
        self.buf = np.concatenate([self.buf, y.astype(np.complex64)])
        out = None
        while len(self.buf) >= self.NFFT:
            seg = self.buf[:self.NFFT].astype(np.complex128)
            self.buf = self.buf[self.NFFT:]
            x2 = seg * seg
            x4 = x2 * x2
            self.specs.append(np.abs(np.fft.fft(x4 * self.win)) ** 2)
            self.specs2.append(np.abs(np.fft.fft(x2 * self.win)) ** 2)
            self.specs = self.specs[-self.AVG:]
            self.specs2 = self.specs2[-self.AVG:]
            self.blocks += 1
            if len(self.specs) < self.AVG:
                continue
            best = None
            for d in self.DRIFTS:
                ps = self._aligned(self.specs, 4.0, d)
                b = ps[self.band]
                noise = float(np.mean(b))
                k = int(np.argmax(b))
                r = float(b[k]) / noise
                if best is None or r > best[0]:
                    best = (r, d, ps, k)
            r, d, ps, k = best
            self.level_db = 10 * np.log10(max(r, 1e-9))
            if r < self.THRESH:
                self.estimate = None
                continue
            i = self.band[k]
            # parabolic interpolation on the log spectrum
            a, pk, c = ps[(i - 1) % self.NFFT], ps[i], ps[(i + 1) % self.NFFT]
            la, lb, lc = np.log(a + 1e-30), np.log(pk + 1e-30), np.log(c + 1e-30)
            den = la - 2 * lb + lc
            frac = 0.5 * (la - lc) / den if den < 0 else 0.0
            est = (self.f[i] + frac * self.df) / 4.0 + d * self.tblk / 2   # centre of last block -> its end
            if not self._is_oqpsk(est, d):
                self.estimate = None
                continue
            self.estimate, self.drift = est, float(d)
            self.last_detect = time.monotonic()
            out = self.estimate
        return out

    def _is_oqpsk(self, est: float, drift: float) -> bool:
        """An unmodulated carrier (spur, CW) or a BPSK signal also gives a 4th-power line. Squaring tells
        them apart: a carrier / BPSK shows one line at 2f; OQPSK shows two lines at 2f +/- Rs and none at
        2f; plain QPSK shows no line at all. Accepted: OQPSK pair present, or no line at 2f (QPSK)."""
        p2 = self._aligned(self.specs2, 2.0, drift)
        noise2 = float(np.mean(p2[self.band]))

        def pw(fr):
            k = int(round(fr / self.df)) % self.NFFT
            return float(max(p2[(k + j) % self.NFFT] for j in (-2, -1, 0, 1, 2)))
        centre = pw(2 * est)
        pair = max(min(pw(2 * est + r), pw(2 * est - r)) for r in self.SYM_RATES
                   if abs(2 * est) + r < 0.5 * self.rate)
        return pair > 2.0 * centre or centre < 4.0 * noise2

    def detected(self, within_s: float = 2.0, time_scale: float = 1.0) -> bool:
        return time.monotonic() - self.last_detect < within_s * time_scale


@dataclass
class ReceiverConfig:
    sample_rate: float
    center_hz: float
    freqs_hz: list = field(default_factory=lambda: [137.9e6, 137.1e6])
    mode: str = "auto"            # auto | 72k | 80k
    trigger_snr_db: float = 3.0
    park_snr_db: float = 1.0
    ppm: float = 0.0
    dsp: dict = field(default_factory=dict)
    afc: bool = True
    afc_max_hz: float = 15000.0


def warm_up(rc: "ReceiverConfig"):
    """Run a throw-away receiver on 0.3 s of noise so every DSP kernel is loaded/compiled before the SDR
    starts streaming (otherwise the first blocks take seconds and the stream buffer overflows)."""
    import dataclasses
    r = Receiver(dataclasses.replace(rc))
    rng = np.random.default_rng(0)
    n = int(rc.sample_rate * 0.1)
    for _ in range(3):
        r.process((rng.normal(0, 0.01, n) + 1j * rng.normal(0, 0.01, n)).astype(np.complex64))


class Receiver:
    """Stateful processing of wideband IQ blocks. Not thread-aware: call from one thread."""

    def __init__(self, rc: ReceiverConfig, cadu_sink=None):
        self.rc = rc
        self.fs = rc.sample_rate
        self.decim = max(1, int(self.fs // 280e3))
        self.chan_rate = self.fs / self.decim
        self.monitor = SignalMonitor(self.fs)
        self.imager = lrpt.MsuMrImager()
        self.cadu_sink = cadu_sink
        self.doppler_ratio = 0.0   # -range_rate / c
        self.channels = []
        for f in rc.freqs_hz:
            ch = core.Channelizer(self.fs, self.decim, cutoff=95e3, transition=50e3)
            self.channels.append((f, ch))
        self.active_idx = 0
        self.afc_hz = 0.0          # carrier offset removed by the AFC (receiver ppm error + residual Doppler)
        self.afc = CarrierAfc(self.chan_rate, rc.afc_max_hz) if rc.afc else None
        self.time_scale = 1.0
        self._retune()
        modes = ["72k", "80k"] if rc.mode == "auto" else [rc.mode]
        self.chains = [LrptChain(self.chan_rate, m, self.imager, cadu_sink, rc.dsp) for m in modes]
        self.signal_present = False
        self.snr_db = -99.0
        self.reports: list[SignalReport] = []
        self.last_syms = np.zeros(0, np.complex64)
        self.last_wave = np.zeros(0, np.complex64)
        self.last_strobes = np.zeros(0, np.float32)
        self.baseband_sink = None   # callable(complex64 array) for raw channel IQ recording
        self.audio_sink = None      # callable(complex64 array, rate) for the audio monitor

    # -- tuning --------------------------------------------------------------
    def _retune(self):
        ppm_hz = -self.rc.ppm * 1e-6 * self.rc.center_hz
        for f, ch in self.channels:
            ch.set_offset(f - self.rc.center_hz + self.doppler_ratio * f + ppm_hz + self.afc_hz)

    def set_range_rate(self, rr_km_s: float | None):
        """Doppler pre-correction from the TLE range-rate (None = no correction)."""
        self.doppler_ratio = 0.0 if rr_km_s is None else -rr_km_s * 1000.0 / 299792458.0
        self._retune()

    @property
    def doppler_hz(self) -> float:
        return self.doppler_ratio * self.active_freq

    def total_offset_hz(self) -> float:
        """Carrier offset from the nominal (Doppler-corrected) frequency: AFC + demodulator loop (Hz).
        Mostly the receiver reference error: ~ -ppm x 137.9 Hz."""
        c = self.locked_chain or (self.chains[0] if self.chains else None)
        return self.afc_hz + (c.demod.freq_hz if c else 0.0)

    @property
    def carrier_detected(self) -> bool:
        return self.afc is not None and self.afc.detected(time_scale=self.time_scale)

    @property
    def locked_chain(self) -> LrptChain | None:
        for c in self.chains:
            if c.stats.frames_ok > 0 and c.stats.vit_locked:
                return c
        return None

    @property
    def active_freq(self) -> float:
        return self.channels[self.active_idx][0]

    # -- processing ----------------------------------------------------------
    def process(self, iq: np.ndarray):
        iq = np.ascontiguousarray(iq, dtype=np.complex64)
        self.monitor.push(iq)
        self.reports = []
        for f, ch in self.channels:
            r = self.monitor.report(ch.offset_hz)
            if r is not None:
                r.freq_hz = f
                self.reports.append(r)
        # pick the strongest candidate frequency while nothing is locked
        lc = self.locked_chain
        if lc is None and self.reports:
            best = max(range(len(self.reports)), key=lambda i: self.reports[i].snr_db)
            if best != self.active_idx and self.reports[best].snr_db > self.reports[self.active_idx].snr_db + 3:
                self.active_idx = best
                for c in self.chains:
                    c.demod.reset_loops()
        if self.reports:
            self.snr_db = self.reports[self.active_idx].snr_db
        carrier = self.afc is not None and self.afc.detected(time_scale=self.time_scale)
        self.signal_present = self.snr_db >= self.rc.trigger_snr_db or lc is not None or carrier
        # keep the carrier loops parked at zero while there is no signal, so they
        # cannot random-walk away in pure noise before AOS
        if self.snr_db < self.rc.park_snr_db and not carrier and not any(c.stats.vit_locked for c in self.chains):
            for c in self.chains:
                c.demod.reset_loops()
        # channelize all candidates (keeps NCO/filter states continuous), demod the active one
        base = None
        for i, (f, ch) in enumerate(self.channels):
            y = ch.process(iq)
            if i == self.active_idx:
                base = y
        if base is None or len(base) == 0:
            return
        if self.baseband_sink is not None:
            self.baseband_sink(base)
        if self.audio_sink is not None:
            try:
                self.audio_sink(base, self.chan_rate)
            except Exception:  # noqa: BLE001  (the monitor must never stop the reception)
                self.audio_sink = None
        if lc is not None:
            for c in self.chains:
                c.enabled = c is lc
        disp = None
        run_fec = self.snr_db >= self.rc.park_snr_db or lc is not None or carrier or \
            any(c.active is not None for c in self.chains)
        for c in self.chains:
            if not c.enabled:
                continue
            out = c.process(base, run_fec)
            if disp is None or c is lc or (lc is None and c.stats.vit_ber < (disp[0].stats.vit_ber)):
                disp = (c, out)
        # AFC, applied between blocks (the block just demodulated was made with the previous NCO setting).
        # Locked: the NCO takes over the frequency the Costas loop is tracking (Doppler drift, oscillator
        # drift), so the loop works around 0 Hz and the 4th-power line stays sharp. Acquiring: the NCO
        # jumps to the carrier measured by the 4th-power estimator.
        if self.afc is not None:
            est = self.afc.push(base)
            locked = [c for c in self.chains if c.enabled and c.active is not None]
            if locked:
                m = locked[0].demod.freq_hz
                # only while the decoder is healthy: a loop wandering in noise must not drag the NCO
                if abs(m) > 3.0 and locked[0].stats.vit_ber < LrptChain.UNLOCK_BER:
                    self._afc_step(float(np.clip(m, -15.0, 15.0)), reset=False)   # <= 150 Hz/s
            elif est is not None:
                if abs(est) > 2.0:
                    self._afc_step(est)
        if disp is not None:
            syms, wave, strobes = disp[1]
            self.last_syms = syms[-2000:]
            self.last_wave = wave
            self.last_strobes = strobes

    def _afc_step(self, est_hz: float, reset: bool = True):
        """Move the channel NCO onto the carrier and take the same amount out of the demodulator
        frequency registers, so the total correction is continuous (a locked loop is not disturbed)."""
        new = float(np.clip(self.afc_hz + est_hz, -self.rc.afc_max_hz, self.rc.afc_max_hz))
        step = new - self.afc_hz
        if step == 0.0:
            return
        self.afc_hz = new
        self._retune()
        for c in self.chains:
            if c.active is not None:          # locked: its loop holds the offset, keep the sum continuous
                c.demod.shift_freq(-step)
            else:                             # acquiring: the NCO now does the job, start the loop from 0
                c.demod.reset_freq()
        if reset:
            self.afc.reset()

    def chain_stats(self) -> list[ChainStats]:
        return [c.stats for c in self.chains]

    def eye_traces(self, n_traces: int = 80, span_sym: float = 2.0, upsample: int = 8):
        """Eye diagram traces of the I branch, centred on recovered symbol strobes.

        Returns (t, traces) with t in symbol periods from -span/2 to +span/2."""
        w = self.last_wave
        s = self.last_strobes
        if len(w) < 64 or len(s) < 8:
            return None
        x = np.real(w).astype(np.float64)
        half = span_sym  # in samples (2 sps): span_sym symbols = 2*span_sym samples; half-span = span_sym samples
        t_rel = np.linspace(-half, half, int(2 * half * upsample) + 1)
        valid = s[(s > half + 2) & (s < len(x) - half - 3)]
        if len(valid) == 0:
            return None
        valid = valid[-n_traces:]
        idx = valid[:, None] + t_rel[None, :]
        # cubic (Catmull-Rom) interpolation
        i0 = np.floor(idx).astype(np.int64)
        mu = idx - i0
        p0, p1, p2, p3 = x[i0 - 1], x[i0], x[i0 + 1], x[i0 + 2]
        tr = p1 + 0.5 * mu * (p2 - p0 + mu * (2 * p0 - 5 * p1 + 4 * p2 - p3 + mu * (3 * (p1 - p2) + p3 - p0)))
        scale = np.median(np.abs(x[np.clip(np.round(valid).astype(int), 0, len(x) - 1)])) or 1.0
        return t_rel / 2.0, tr / scale

"""Audio monitor: FM discriminator on the 137 MHz channel, played on the default Windows sound device
(winmm waveOut through ctypes: no extra library, nothing to install). Only for listening to what the
receiver gets - noise hiss, the LRPT "rushing" sound, interference - it plays no part in decoding."""
from __future__ import annotations

import ctypes as C
import os
import threading

import numpy as np

AUDIO_RATE = 48000


class FmDiscriminator:
    """Complex channel samples -> FM audio at AUDIO_RATE (int16), stateful across blocks."""

    AUDIO_BW = 12000.0

    def __init__(self, in_rate: float):
        self.in_rate = float(in_rate)
        self.prev = 1 + 0j
        self.step = self.in_rate / AUDIO_RATE           # input samples per output sample
        n = max(1, int(round(self.in_rate / (2 * self.AUDIO_BW))))
        self.lp = np.ones(n) / n                         # boxcar anti-alias before decimation
        self.hist = np.zeros(n - 1)
        self.pos = 0.0                                   # position of the next output sample in the stream
        self.tail = np.zeros(0)
        self.dc = 0.0

    def process(self, x: np.ndarray, volume: float = 0.5) -> np.ndarray:
        if len(x) == 0:
            return np.zeros(0, np.int16)
        x = x.astype(np.complex128)
        d = np.angle(x * np.conj(np.concatenate([[self.prev], x[:-1]]))) / np.pi
        self.prev = x[-1]
        y = np.convolve(np.concatenate([self.hist, d]), self.lp, mode="valid")
        self.hist = d[-(len(self.lp) - 1):] if len(self.lp) > 1 else self.hist
        s = np.concatenate([self.tail, y])
        idx = np.arange(self.pos, len(s) - 1, self.step)
        out = np.interp(idx, np.arange(len(s)), s) if len(idx) else np.zeros(0)
        nxt = self.pos + len(idx) * self.step
        keep = int(np.floor(nxt))
        self.tail = s[keep:]
        self.pos = nxt - keep
        if len(out):
            self.dc += 0.01 * (float(np.mean(out)) - self.dc)
            out = out - self.dc
        return np.clip(out * (32767.0 * 1.5 * volume), -32767, 32767).astype(np.int16)


# ----------------------------------------------------------------------------
# Windows waveOut
# ----------------------------------------------------------------------------
class _WaveFormat(C.Structure):
    _fields_ = [("wFormatTag", C.c_ushort), ("nChannels", C.c_ushort), ("nSamplesPerSec", C.c_uint32),
                ("nAvgBytesPerSec", C.c_uint32), ("nBlockAlign", C.c_ushort), ("wBitsPerSample", C.c_ushort),
                ("cbSize", C.c_ushort)]


class _WaveHdr(C.Structure):
    _fields_ = [("lpData", C.c_void_p), ("dwBufferLength", C.c_uint32), ("dwBytesRecorded", C.c_uint32),
                ("dwUser", C.c_size_t), ("dwFlags", C.c_uint32), ("dwLoops", C.c_uint32),
                ("lpNext", C.c_void_p), ("reserved", C.c_size_t)]


WHDR_DONE = 0x1
WAVE_MAPPER = 0xFFFFFFFF


class WaveOut:
    """Minimal non-blocking PCM player. Chunks are dropped when more than max_latency is queued
    (file replay faster than real time), so the sound never lags behind the display."""

    def __init__(self, rate: int = AUDIO_RATE, max_latency: float = 0.4):
        self.rate = rate
        self.max_bytes = int(max_latency * rate) * 2
        self.h = C.c_void_p()
        self.pending: list = []
        w = C.WinDLL("winmm")
        self.w = w
        w.waveOutOpen.argtypes = [C.POINTER(C.c_void_p), C.c_uint, C.POINTER(_WaveFormat), C.c_size_t, C.c_size_t,
                                  C.c_uint32]
        for fn in ("waveOutPrepareHeader", "waveOutUnprepareHeader", "waveOutWrite"):
            getattr(w, fn).argtypes = [C.c_void_p, C.POINTER(_WaveHdr), C.c_uint]
        w.waveOutReset.argtypes = [C.c_void_p]
        w.waveOutClose.argtypes = [C.c_void_p]
        fmt = _WaveFormat(1, 1, rate, rate * 2, 2, 16, 0)
        r = w.waveOutOpen(C.byref(self.h), WAVE_MAPPER, C.byref(fmt), 0, 0, 0)
        if r != 0:
            raise OSError(f"waveOutOpen failed ({r}): no sound output device?")

    def _reclaim(self):
        keep = []
        for hdr, buf in self.pending:
            if hdr.dwFlags & WHDR_DONE:
                self.w.waveOutUnprepareHeader(self.h, C.byref(hdr), C.sizeof(_WaveHdr))
            else:
                keep.append((hdr, buf))
        self.pending = keep

    def write(self, pcm: np.ndarray):
        self._reclaim()
        if len(pcm) == 0 or sum(len(b) for _, b in self.pending) > self.max_bytes:
            return
        buf = C.create_string_buffer(pcm.astype("<i2").tobytes())
        hdr = _WaveHdr(C.cast(buf, C.c_void_p), pcm.nbytes, 0, 0, 0, 0, None, 0)
        if self.w.waveOutPrepareHeader(self.h, C.byref(hdr), C.sizeof(_WaveHdr)) == 0:
            if self.w.waveOutWrite(self.h, C.byref(hdr), C.sizeof(_WaveHdr)) == 0:
                self.pending.append((hdr, buf))
            else:
                self.w.waveOutUnprepareHeader(self.h, C.byref(hdr), C.sizeof(_WaveHdr))

    def close(self):
        try:
            self.w.waveOutReset(self.h)
            self._reclaim()
            self.w.waveOutClose(self.h)
        except Exception:  # noqa: BLE001
            pass


class AudioMonitor:
    """Called from the receiver thread with channel samples; muted by default."""

    def __init__(self, log=None):
        self.enabled = False
        self.volume = 0.5
        self._fm: FmDiscriminator | None = None
        self._out = None
        self._lock = threading.Lock()
        self._log = log or (lambda m: None)
        self.error = ""
        self.player_factory = WaveOut if os.name == "nt" else None   # tests replace it

    def set(self, enabled: bool, volume: float | None = None):
        with self._lock:
            if volume is not None:
                self.volume = float(np.clip(volume, 0.0, 1.0))
            self.enabled = bool(enabled)
            if not self.enabled:
                self._close()

    def _close(self):
        if self._out is not None:
            self._out.close()
            self._out = None

    def push(self, x: np.ndarray, rate: float):
        if not self.enabled:
            return
        with self._lock:
            if not self.enabled:
                return
            if self._fm is None or self._fm.in_rate != float(rate):
                self._fm = FmDiscriminator(rate)
            pcm = self._fm.process(x, self.volume)
            if self._out is None:
                if self.player_factory is None:
                    self.error = "no audio output on this system"
                    return
                try:
                    self._out = self.player_factory()
                    self.error = ""
                except OSError as e:
                    self.error = str(e)
                    self._log(f"Audio: {e}")
                    self.enabled = False
                    return
            self._out.write(pcm)

    def close(self):
        with self._lock:
            self.enabled = False
            self._close()

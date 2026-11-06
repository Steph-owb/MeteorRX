"""RTL-SDR source: bulk-read length rules (WinUSB rejects lengths that are not a multiple of 512)."""
import ctypes as C
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from meteorrx import sdr  # noqa: E402


class FakeWinUsbLib:
    """Mimics librtlsdr over WinUSB: bulk read length must be a multiple of 512, else -1."""

    def __init__(self, fail_first=0):
        self.lengths = []
        self.resets = 0
        self.fail_first = fail_first

    def rtlsdr_read_sync(self, dev, buf, n, n_read):
        self.lengths.append(n)
        if self.fail_first:
            self.fail_first -= 1
            return -1
        if n % 512:
            return -1
        C.memset(buf, 128, n)
        C.cast(n_read, C.POINTER(C.c_int))[0] = n
        return 0

    def rtlsdr_reset_buffer(self, dev):
        self.resets += 1


def make(lib):
    """Source for the synchronous fall-back reader (no streaming thread)."""
    s = object.__new__(sdr.RtlSdrSource)
    s.lib, s.dev, s.sample_rate = lib, None, 1.2e6
    s.buf = np.empty(1 << 18, dtype=np.uint8)
    s._errors = 0
    s.read = s._read_sync
    return s


@pytest.mark.parametrize("fs", [1.2e6, 2.048e6, 2.4e6, 250e3, 960e3, 1.024e6])
def test_read_length_is_bulk_compatible(fs):
    n = sdr.RtlSdrSource.read_length(int(fs * 0.1))
    assert n % 512 == 0 and 16384 <= n <= 1 << 18


def test_read_with_winusb_rules():
    lib = FakeWinUsbLib()
    s = make(lib)
    x = s.read(120000)                       # 0.1 s at 1.2 MS/s - used to request 240000 bytes (error -1)
    assert len(x) > 0 and all(n % 512 == 0 for n in lib.lengths)
    assert np.allclose(x, (128 - 127.4) / 128 * (1 + 1j), atol=1e-6)


def test_transient_error_retried_then_reported():
    lib = FakeWinUsbLib(fail_first=2)
    s = make(lib)
    assert len(s.read(120000)) == 0 and len(s.read(120000)) == 0      # flushed and retried
    assert len(s.read(120000)) > 0 and lib.resets >= 2
    lib.fail_first = 10
    with pytest.raises(sdr.SdrError, match="I/O error"):
        for _ in range(4):
            s.read(120000)


# ---------------------------------------------------------------------------------------------------------
# Continuity: a model of the RTL2832U, whose small USB FIFO overflows whenever nobody is reading
# ---------------------------------------------------------------------------------------------------------
import threading  # noqa: E402
import time  # noqa: E402


class FakeDongle:
    """Produces a counter ramp at `fs` in real time into a 64 KiB FIFO (the RTL2832U buffers ~ that much
    while no USB transfer is pending); what does not fit is lost. Implements read_sync and read_async."""

    FIFO = 1 << 16

    def __init__(self, fs=1.2e6):
        self.fs = fs
        self.t0 = time.monotonic()
        self.produced = 0          # bytes produced so far (2 per sample)
        self.fifo = bytearray()
        self.lost = 0
        self.lock = threading.Lock()
        self.cancel = False
        self.next = 0              # value of the next byte (ramp mod 256 -> lets the test see gaps)

    def _advance(self):
        n = int((time.monotonic() - self.t0) * self.fs * 2) - self.produced
        if n <= 0:
            return
        self.produced += n
        data = ((self.next + np.arange(n)) % 256).astype(np.uint8).tobytes()
        self.next = (self.next + n) % 256
        room = self.FIFO - len(self.fifo)
        self.fifo += data[:room]
        self.lost += max(0, n - room)

    def rtlsdr_read_sync(self, dev, buf, n, n_read):
        """Takes what the FIFO kept since the last call, then streams (losslessly) until n bytes."""
        with self.lock:
            self._advance()
            out = bytearray(self.fifo[:n])
            del self.fifo[:n]
        need = n - len(out)
        if need > 0:
            time.sleep(need / (2 * self.fs))
            with self.lock:
                out += ((self.next + np.arange(need)) % 256).astype(np.uint8).tobytes()
                self.next = (self.next + need) % 256
                self.produced += need
        C.memmove(buf, bytes(out), n)
        C.cast(n_read, C.POINTER(C.c_int))[0] = n
        return 0

    def rtlsdr_read_async(self, dev, cb, ctx, nbuf, blen):
        # with transfers queued the device streams without loss: data are taken as soon as produced
        blen = blen or 262144
        pending = bytearray()
        sent = 0
        while not self.cancel:
            want = int((time.monotonic() - self.t0) * self.fs * 2) - sent
            if want > 0:
                pending += ((sent + np.arange(want)) % 256).astype(np.uint8).tobytes()
                sent += want
            while len(pending) >= blen:
                b = (C.c_ubyte * blen).from_buffer_copy(bytes(pending[:blen]))
                del pending[:blen]
                cb(C.cast(b, C.POINTER(C.c_ubyte)), blen, None)
            time.sleep(0.005)
        return 0

    def rtlsdr_cancel_async(self, dev):
        self.cancel = True
        return 0

    def rtlsdr_reset_buffer(self, dev):
        with self.lock:
            self.fifo.clear()


def _process_like_receiver(src, seconds, work=0.035):
    """Read 0.1 s blocks and spend `work` seconds 'processing' each, like the receiver thread."""
    got = []
    t_end = time.monotonic() + seconds
    while time.monotonic() < t_end:
        x = src.read(int(1.2e6 * 0.1))
        got.append(x)
        time.sleep(work)
    return np.concatenate(got)


def _gaps(x):
    """Discontinuities of the byte ramp (I and Q bytes are consecutive ramp values)."""
    raw = np.rint(np.stack([x.real, x.imag], 1).reshape(-1) * 128 + 127.4).astype(np.int64) % 256
    return int(np.count_nonzero((np.diff(raw) % 256) != 1))


def test_sync_reading_between_blocks_loses_samples():
    """What v1.5.1 did: one synchronous read per processed block -> the FIFO overflows while processing."""
    d = FakeDongle()
    s = make(d)
    x = _process_like_receiver(s, 2.0)
    assert d.lost > 0.05 * d.produced and _gaps(x) > 5


def test_async_streaming_is_continuous():
    d = FakeDongle()
    s = object.__new__(sdr.RtlSdrSource)
    s.lib, s.dev, s.sample_rate, s.name = d, None, 1.2e6, "RTL-SDR test"
    s.buf = np.empty(1 << 18, dtype=np.uint8)
    s._bias = False
    s._init_stream()
    s.mode = ""
    s._errors = 0
    s._cb = sdr._RTL_CB(s._on_async)
    s._start_thread(s._stream, "test")
    x = _process_like_receiver(s, 7.0)
    h = s.health()
    s._stop_evt.set()
    d.cancel = True
    s._join()
    assert s.mode == "async" and _gaps(x) == 0
    assert h["ratio"] is not None and h["ratio"] > 0.97 and h["dropped"] == 0


def test_ring_reports_drops_and_rate():
    r = sdr.SampleRing(1000)
    r.put(np.ones(800, np.complex64))
    r.put(np.ones(800, np.complex64))           # does not fit: dropped and counted
    assert r.dropped == 800 and len(r.get(5000, timeout=0.01)) == 800

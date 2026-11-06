"""SDR sources: RTL-SDR (librtlsdr), SDRplay RSP (all models, SDRplay API 3.x), HackRF One (libhackrf),
PlutoSDR (IIOD network protocol, no library) and IQ file playback.

All hardware sources stream continuously from their own thread into a ring buffer (StreamSource): the
receiver never stops the USB/network flow while it processes a block. Throughput is measured, so lost
samples (USB, CPU) are reported instead of silently chopping the signal.
All sources deliver complex64 blocks; tuning is fixed for the whole session (137.5 MHz covers both Meteor
frequencies), Doppler and ppm corrections are done in software.
"""
from __future__ import annotations

import ctypes as C
import os
import struct
import threading
import time

import numpy as np

from . import core


class SdrError(RuntimeError):
    def __init__(self, msg: str, usb_driver: bool = False):
        super().__init__(msg)
        self.usb_driver = usb_driver   # True when the fix is to install WinUSB with Zadig


def load_rtlsdr(extra_dir: str = ""):
    """Select (installed if suitable, else bundled) and load librtlsdr. Returns (CDLL, path, report)."""
    from . import drivers
    rep = drivers.select_library(extra_dir)
    if rep.chosen is None:
        raise SdrError("No suitable RTL-SDR driver library.\n" + "\n".join(rep.lines))
    try:
        return drivers.load_library(rep), rep.chosen.path, rep
    except OSError as e:
        raise SdrError(f"Cannot load {rep.chosen.path}: {e}") from e


class Source:
    name = "source"
    sample_rate = 0.0
    center_hz = 0.0

    def start(self):
        pass

    def read(self, max_samples: int) -> np.ndarray | None:
        raise NotImplementedError

    def stop(self):
        pass

    @property
    def is_file(self) -> bool:
        return False

    def health(self) -> dict | None:
        """Stream health (hardware sources): delivered/nominal sample rate, samples dropped."""
        return None


class SampleRing:
    """Thread-safe complex64 FIFO between a device thread (put) and the receiver thread (get), with
    throughput accounting: the delivered sample rate over the last seconds and the samples dropped
    because the receiver did not keep up."""

    def __init__(self, cap: int):
        self.buf = np.zeros(max(cap, 1024), np.complex64)
        self.cap = len(self.buf)
        self.w = 0
        self.r = 0
        self.dropped = 0
        self.total = 0
        self.cond = threading.Condition()
        self.hist: list = []           # (monotonic time, total) every ~0.5 s for the rate estimate

    def put(self, x: np.ndarray):
        n = len(x)
        if n == 0:
            return
        now = time.monotonic()
        with self.cond:
            self.total += n
            if not self.hist or now - self.hist[-1][0] >= 0.5:
                self.hist.append((now, self.total))
                if len(self.hist) > 60:
                    del self.hist[0]
            if self.w - self.r + n > self.cap:
                self.dropped += n
                return
            a = self.w % self.cap
            k = min(n, self.cap - a)
            self.buf[a:a + k] = x[:k]
            if k < n:
                self.buf[:n - k] = x[k:]
            self.w += n
            self.cond.notify()

    def available(self) -> int:
        with self.cond:
            return self.w - self.r

    def get(self, maxn: int, timeout: float = 1.0, want: int | None = None) -> np.ndarray:
        """Up to maxn samples; waits until `want` (default maxn/2) are there or the timeout expires."""
        want = max(1, min(maxn, want if want is not None else maxn // 2))
        end = time.monotonic() + timeout
        with self.cond:
            while self.w - self.r < want:
                left = end - time.monotonic()
                if left <= 0:
                    break
                self.cond.wait(left)
            n = min(maxn, self.w - self.r)
            a = self.r % self.cap
            k = min(n, self.cap - a)
            out = np.concatenate([self.buf[a:a + k], self.buf[:n - k]]) if k < n else self.buf[a:a + n].copy()
            self.r += n
        return out

    def rate(self, window: float = 10.0) -> float | None:
        """Samples per second actually delivered by the device over the last `window` seconds."""
        with self.cond:
            h = list(self.hist)
            tot = self.total
        if len(h) < 2:
            return None
        now = time.monotonic()
        old = [e for e in h if now - e[0] <= window + 0.5] or h
        t0, n0 = old[0]
        dt = now - t0
        return (tot - n0) / dt if dt > 1.0 else None


class StreamSource(Source):
    """Base of the hardware sources: a device thread fills a SampleRing (4 s), read() takes from it."""

    RING_SECONDS = 4.0

    def _init_stream(self):
        self.ring = SampleRing(int(self.sample_rate * self.RING_SECONDS))
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self.error: str | None = None
        self._t_start = None
        self._no_data_since = None

    def _start_thread(self, target, name: str):
        self._t_start = time.monotonic()
        self._thread = threading.Thread(target=self._guard(target), name=name, daemon=True)
        self._thread.start()

    def _guard(self, fn):
        def run():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                self.error = str(e) or e.__class__.__name__
        return run

    def read(self, max_samples: int) -> np.ndarray | None:
        if not getattr(self, "_reading", False):
            # first read: whatever piled up while the receiver was starting (DSP cache loading) is stale;
            # discard it and do not count it as a loss
            self._reading = True
            with self.ring.cond:
                self.ring.r = self.ring.w
                self.ring.dropped = 0
        x = self.ring.get(int(max_samples), timeout=0.5)
        if len(x):
            self._no_data_since = None
            return x
        if self.error:
            raise SdrError(f"{self.name}: {self.error}")
        if self._thread is not None and not self._thread.is_alive():
            raise SdrError(f"{self.name}: the device stream stopped")
        now = time.monotonic()
        if self._no_data_since is None:
            self._no_data_since = now
        elif now - self._no_data_since > 4.0:
            raise SdrError(f"{self.name}: no samples for 4 s (device removed?)")
        return np.zeros(0, np.complex64)

    def health(self) -> dict | None:
        r = self.ring.rate()
        if r is None or self._t_start is None or time.monotonic() - self._t_start < 6.0:
            return {"ratio": None, "rate": r, "dropped": self.ring.dropped, "nominal": self.sample_rate}
        return {"ratio": r / self.sample_rate, "rate": r, "dropped": self.ring.dropped,
                "nominal": self.sample_rate}

    def _join(self, timeout: float = 3.0):
        self._stop_evt.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout)


# ============================================================================
# RTL-SDR
# ============================================================================
RTL_TUNERS = {0: "", 1: "E4000", 2: "FC0012", 3: "FC0013", 4: "FC2580", 5: "R820T", 6: "R828D"}


def _rtl_prototypes(L):
    L.rtlsdr_get_device_count.restype = C.c_uint32
    L.rtlsdr_get_device_name.restype = C.c_char_p
    L.rtlsdr_get_device_name.argtypes = [C.c_uint32]
    L.rtlsdr_get_device_usb_strings.restype = C.c_int
    L.rtlsdr_get_device_usb_strings.argtypes = [C.c_uint32, C.c_char_p, C.c_char_p, C.c_char_p]
    L.rtlsdr_get_index_by_serial.restype = C.c_int
    L.rtlsdr_get_index_by_serial.argtypes = [C.c_char_p]
    L.rtlsdr_get_tuner_type.restype = C.c_int
    L.rtlsdr_get_tuner_type.argtypes = [C.c_void_p]


def _rtl_strings(L, index: int) -> tuple[str, str, str]:
    m, p, sn = (C.create_string_buffer(256) for _ in range(3))
    if L.rtlsdr_get_device_usb_strings(index, m, p, sn) != 0:
        return "", "", ""
    return tuple(b.value.decode(errors="replace").strip() for b in (m, p, sn))


def enumerate_rtl(dll_dir: str = "") -> list:
    """Connected RTL-SDR receivers with their USB identity (does not claim the devices)."""
    from .profiles import DeviceId
    try:
        L, _, _ = load_rtlsdr(dll_dir)
    except SdrError:
        return []
    _rtl_prototypes(L)
    out = []
    for i in range(L.rtlsdr_get_device_count()):
        man, prod, sn = _rtl_strings(L, i)
        if not (man or prod):
            prod = (L.rtlsdr_get_device_name(i) or b"").decode(errors="replace")
        out.append(DeviceId("rtlsdr", man, prod, sn, index=i))
    return out


_RTL_CB = C.CFUNCTYPE(None, C.POINTER(C.c_ubyte), C.c_uint32, C.c_void_p)


class RtlSdrSource(StreamSource):
    """librtlsdr in asynchronous mode (rtlsdr_read_async, 15 x 256 KiB USB transfers queued) on its own
    thread, as rtl_sdr / SDR# / SatDump do. Up to v1.5.1 MeteorRX read synchronously between two processing
    blocks: the dongle FIFO overflowed during the processing and ~1/3 of the samples were lost in short gaps
    (chopped audio, carrier/timing loops restarting, never a complete frame)."""

    ASYNC_BUFS = 15
    ASYNC_LEN = 16 * 32 * 512          # 262144 bytes = 131072 samples per transfer (librtlsdr default)

    def __init__(self, index: int, sample_rate: float, center_hz: float, gain_db: str | float,
                 bias_tee: bool = False, dll_dir: str = "", serial: str = ""):
        from .profiles import DeviceId
        self.name = "RTL-SDR"
        self.warning = ""
        self.sample_rate = float(sample_rate)
        self.center_hz = float(center_hz)
        self.lib, self.lib_path, self.report = load_rtlsdr(dll_dir)
        L = self.lib
        _rtl_prototypes(L)
        L.rtlsdr_open.argtypes = [C.POINTER(C.c_void_p), C.c_uint32]
        L.rtlsdr_close.argtypes = [C.c_void_p]
        L.rtlsdr_set_center_freq.argtypes = [C.c_void_p, C.c_uint32]
        L.rtlsdr_set_sample_rate.argtypes = [C.c_void_p, C.c_uint32]
        L.rtlsdr_set_tuner_gain_mode.argtypes = [C.c_void_p, C.c_int]
        L.rtlsdr_get_tuner_gains.argtypes = [C.c_void_p, C.POINTER(C.c_int)]
        L.rtlsdr_set_tuner_gain.argtypes = [C.c_void_p, C.c_int]
        L.rtlsdr_set_agc_mode.argtypes = [C.c_void_p, C.c_int]
        L.rtlsdr_reset_buffer.argtypes = [C.c_void_p]
        L.rtlsdr_read_sync.argtypes = [C.c_void_p, C.c_void_p, C.c_int, C.POINTER(C.c_int)]
        if hasattr(L, "rtlsdr_read_async"):
            L.rtlsdr_read_async.argtypes = [C.c_void_p, _RTL_CB, C.c_void_p, C.c_uint32, C.c_uint32]
            L.rtlsdr_read_async.restype = C.c_int
            L.rtlsdr_cancel_async.argtypes = [C.c_void_p]
            L.rtlsdr_cancel_async.restype = C.c_int
        n = L.rtlsdr_get_device_count()
        if n == 0:
            hint = ""
            if self.report.usb_problem:
                hint = " The receiver is plugged in but its USB driver is not WinUSB: use the menu " \
                       "'Drivers > Install the USB driver (Zadig)'."
            elif not self.report.dongles:
                hint = " No RTL2832U device is visible on USB."
            raise SdrError("No usable RTL-SDR found." + hint, usb_driver=self.report.usb_problem)
        if serial:                                  # receiver profile: pick the dongle by its serial number
            i = L.rtlsdr_get_index_by_serial(serial.encode())
            if i >= 0:
                index = i
            else:
                found = ", ".join(_rtl_strings(L, k)[2] or "?" for k in range(n))
                self.warning = (f"RTL-SDR serial {serial} (active profile) not connected - using device "
                                f"{min(index, n - 1)} (serial {found})")
                index = min(index, n - 1)
        if index >= n:
            raise SdrError(f"RTL-SDR index {index} not present ({n} device(s))")
        man, prod, sn = _rtl_strings(L, index)
        self.dev = C.c_void_p()
        r = L.rtlsdr_open(C.byref(self.dev), index)
        if r != 0:
            raise SdrError(f"rtlsdr_open failed ({r}): device used by another program, or USB driver "
                           "not WinUSB (menu Drivers)", usb_driver=self.report.usb_problem)
        self.name = "RTL-SDR: " + (L.rtlsdr_get_device_name(index) or b"").decode(errors="replace") + \
            f" ({self.lib_path})"
        tuner = RTL_TUNERS.get(L.rtlsdr_get_tuner_type(self.dev), "")
        self.identity = DeviceId("rtlsdr", man, prod, sn, tuner, index)
        # frequency first: setting the rate while the R820T is still at its power-on frequency gives the
        # harmless but confusing "[R82XX] PLL not locked!" message
        L.rtlsdr_set_center_freq(self.dev, int(round(self.center_hz)))
        L.rtlsdr_set_sample_rate(self.dev, int(self.sample_rate))
        L.rtlsdr_set_center_freq(self.dev, int(round(self.center_hz)))
        L.rtlsdr_set_agc_mode(self.dev, 0)
        if str(gain_db).strip().lower() == "auto":
            L.rtlsdr_set_tuner_gain_mode(self.dev, 0)
        else:
            L.rtlsdr_set_tuner_gain_mode(self.dev, 1)
            gains = (C.c_int * 64)()
            ng = L.rtlsdr_get_tuner_gains(self.dev, gains)
            want = int(round(float(gain_db) * 10))
            if ng > 0:
                want = min(list(gains)[:ng], key=lambda g: abs(g - want))
            L.rtlsdr_set_tuner_gain(self.dev, want)
            self.gain_db = want / 10.0
        if bias_tee and hasattr(L, "rtlsdr_set_bias_tee"):
            L.rtlsdr_set_bias_tee.argtypes = [C.c_void_p, C.c_int]
            L.rtlsdr_set_bias_tee(self.dev, 1)
        self._bias = bias_tee
        self.buf = np.empty(1 << 18, dtype=np.uint8)
        self.mode = ""
        self._init_stream()

    # librtlsdr transfers are USB bulk reads: the length must be a multiple of 512 bytes (USB 2.0
    # packet), otherwise WinUSB fails the transfer (LIBUSB_ERROR_IO = -1). rtl_sdr uses 16 x 16384.
    BLOCK = 16384
    MAX_READ = 16 * 16384
    LIBUSB_ERRORS = {-1: "I/O error", -2: "invalid parameter", -3: "access denied (driver not WinUSB?)",
                     -4: "device disconnected", -5: "not found", -6: "busy (used by another program)",
                     -7: "timeout", -8: "overflow", -9: "pipe error", -11: "out of memory", -12: "not supported"}

    def start(self):
        self.lib.rtlsdr_reset_buffer(self.dev)
        self._errors = 0
        self._cb = _RTL_CB(self._on_async)
        self._start_thread(self._stream, "rtlsdr-stream")

    def _on_async(self, buf, n, ctx):
        try:
            if self._stop_evt.is_set():
                self.lib.rtlsdr_cancel_async(self.dev)
                return
            a = np.ctypeslib.as_array(buf, shape=(int(n),))
            self.ring.put(core.u8_to_c64(a[: int(n) & ~1]))
        except Exception:  # noqa: BLE001  (never raise into the librtlsdr thread)
            pass

    def _stream(self):
        L = self.lib
        if hasattr(L, "rtlsdr_read_async"):
            self.mode = "async"
            t0 = time.monotonic()
            r = L.rtlsdr_read_async(self.dev, self._cb, None, self.ASYNC_BUFS, self.ASYNC_LEN)
            if self._stop_evt.is_set():
                return
            if r != 0 and self.ring.total == 0 and time.monotonic() - t0 < 2.0:
                L.rtlsdr_reset_buffer(self.dev)       # async refused at once: fall back to a sync reader
            else:
                raise SdrError(f"rtlsdr_read_async ended ({r}: {self.LIBUSB_ERRORS.get(r, 'libusb error')})")
        self.mode = "sync thread"
        while not self._stop_evt.is_set():
            x = self._read_sync(self.MAX_READ // 2)
            self.ring.put(x)

    @classmethod
    def read_length(cls, max_samples: int) -> int:
        """Bytes to request: multiple of 16384 (hence of 512), between 16384 and 262144."""
        want = 2 * int(max_samples)
        return max(cls.BLOCK, min(cls.MAX_READ, (want // cls.BLOCK) * cls.BLOCK))

    def _read_sync(self, max_samples: int) -> np.ndarray:
        nbytes = self.read_length(max_samples)
        got = C.c_int(0)
        r = self.lib.rtlsdr_read_sync(self.dev, self.buf.ctypes.data, nbytes, C.byref(got))
        if r < 0:
            self._errors = getattr(self, "_errors", 0) + 1
            if self._errors <= 2 and r not in (-4, -3):
                self.lib.rtlsdr_reset_buffer(self.dev)     # transient error: flush and retry
                return np.zeros(0, np.complex64)
            raise SdrError(f"rtlsdr_read_sync error {r} ({self.LIBUSB_ERRORS.get(r, 'libusb error')})",
                           usb_driver=(r == -3))
        self._errors = 0
        n = got.value & ~1
        return core.u8_to_c64(self.buf[:n])

    def stop(self):
        self._stop_evt.set()
        try:
            if self.mode == "async" and hasattr(self.lib, "rtlsdr_cancel_async"):
                self.lib.rtlsdr_cancel_async(self.dev)
        except Exception:  # noqa: BLE001
            pass
        self._join(3.0)
        self._close()

    def _close(self):
        try:
            if self._bias and hasattr(self.lib, "rtlsdr_set_bias_tee"):
                self.lib.rtlsdr_set_bias_tee(self.dev, 0)
            self.lib.rtlsdr_close(self.dev)
        except Exception:
            pass


# ============================================================================
# SDRplay API 3.x (RSP1A / RSP1B / RSPdx in single tuner mode)
# Structure layouts from sdrplay_api*.h v3.15 (only the leading members we use).
# ============================================================================
class _GainValues(C.Structure):
    _fields_ = [("curr", C.c_float), ("max", C.c_float), ("min", C.c_float)]


class _Gain(C.Structure):
    _fields_ = [("gRdB", C.c_int), ("LNAstate", C.c_ubyte), ("syncUpdate", C.c_ubyte), ("minGr", C.c_int),
                ("gainVals", _GainValues)]


class _RfFreq(C.Structure):
    _fields_ = [("rfHz", C.c_double), ("syncUpdate", C.c_ubyte)]


class _DcOffsetTuner(C.Structure):
    _fields_ = [("dcCal", C.c_ubyte), ("speedUp", C.c_ubyte), ("trackTime", C.c_int), ("refreshRateTime", C.c_int)]


class _TunerParams(C.Structure):
    _fields_ = [("bwType", C.c_int), ("ifType", C.c_int), ("loMode", C.c_int), ("gain", _Gain),
                ("rfFreq", _RfFreq), ("dcOffsetTuner", _DcOffsetTuner)]


class _DcOffset(C.Structure):
    _fields_ = [("DCenable", C.c_ubyte), ("IQenable", C.c_ubyte)]


class _Decimation(C.Structure):
    _fields_ = [("enable", C.c_ubyte), ("decimationFactor", C.c_ubyte), ("wideBandSignal", C.c_ubyte)]


class _Agc(C.Structure):
    _fields_ = [("enable", C.c_int), ("setPoint_dBfs", C.c_int), ("attack_ms", C.c_ushort), ("decay_ms", C.c_ushort),
                ("decay_delay_ms", C.c_ushort), ("decay_threshold_dB", C.c_ushort), ("syncUpdate", C.c_int)]


class _ControlParams(C.Structure):
    _fields_ = [("dcOffset", _DcOffset), ("decimation", _Decimation), ("agc", _Agc), ("adsbMode", C.c_int)]


class _Rsp1aTuner(C.Structure):
    _fields_ = [("biasTEnable", C.c_ubyte)]


class _Rsp2Tuner(C.Structure):
    _fields_ = [("biasTEnable", C.c_ubyte), ("amPortSel", C.c_int), ("antennaSel", C.c_int),
                ("rfNotchEnable", C.c_ubyte)]


class _RspDuoResetSlave(C.Structure):
    _fields_ = [("resetGainUpdate", C.c_ubyte), ("resetRfUpdate", C.c_ubyte)]


class _RspDuoTuner(C.Structure):
    _fields_ = [("biasTEnable", C.c_ubyte), ("tuner1AmPortSel", C.c_int), ("tuner1AmNotchEnable", C.c_ubyte),
                ("rfNotchEnable", C.c_ubyte), ("rfDabNotchEnable", C.c_ubyte), ("resetSlaveFlags", _RspDuoResetSlave)]


class _RspDxTuner(C.Structure):
    _fields_ = [("hdrBw", C.c_int)]


class _RxChannelParams(C.Structure):  # sdrplay_api_rx_channel.h (API 3.15)
    _fields_ = [("tunerParams", _TunerParams), ("ctrlParams", _ControlParams), ("rsp1aTunerParams", _Rsp1aTuner),
                ("rsp2TunerParams", _Rsp2Tuner), ("rspDuoTunerParams", _RspDuoTuner),
                ("rspDxTunerParams", _RspDxTuner)]


class _FsFreq(C.Structure):
    _fields_ = [("fsHz", C.c_double), ("syncUpdate", C.c_ubyte), ("reCal", C.c_ubyte)]


class _SyncUpdate(C.Structure):
    _fields_ = [("sampleNum", C.c_uint), ("period", C.c_uint)]


class _ResetFlags(C.Structure):
    _fields_ = [("resetGainUpdate", C.c_ubyte), ("resetRfUpdate", C.c_ubyte), ("resetFsUpdate", C.c_ubyte)]


class _Rsp1aParams(C.Structure):
    _fields_ = [("rfNotchEnable", C.c_ubyte), ("rfDabNotchEnable", C.c_ubyte)]


class _Rsp2Params(C.Structure):
    _fields_ = [("extRefOutputEn", C.c_ubyte)]


class _RspDuoParams(C.Structure):
    _fields_ = [("extRefOutputEn", C.c_int)]


class _RspDxParams(C.Structure):
    _fields_ = [("hdrEnable", C.c_ubyte), ("biasTEnable", C.c_ubyte), ("antennaSel", C.c_int),
                ("rfNotchEnable", C.c_ubyte), ("rfDabNotchEnable", C.c_ubyte)]


class _DevParams(C.Structure):  # sdrplay_api_dev.h (API 3.15)
    _fields_ = [("ppm", C.c_double), ("fsFreq", _FsFreq), ("syncUpdate", _SyncUpdate), ("resetFlags", _ResetFlags),
                ("mode", C.c_int), ("samplesPerPkt", C.c_uint), ("rsp1aParams", _Rsp1aParams),
                ("rsp2Params", _Rsp2Params), ("rspDuoParams", _RspDuoParams), ("rspDxParams", _RspDxParams)]


class _DeviceParams(C.Structure):
    _fields_ = [("devParams", C.POINTER(_DevParams)), ("rxChannelA", C.POINTER(_RxChannelParams)),
                ("rxChannelB", C.POINTER(_RxChannelParams))]


class _Device(C.Structure):
    _fields_ = [("SerNo", C.c_char * 64), ("hwVer", C.c_ubyte), ("tuner", C.c_int), ("rspDuoMode", C.c_int),
                ("valid", C.c_ubyte), ("rspDuoSampleFreq", C.c_double), ("dev", C.c_void_p)]


class _CallbackFns(C.Structure):
    _fields_ = [("StreamACbFn", C.c_void_p), ("StreamBCbFn", C.c_void_p), ("EventCbFn", C.c_void_p)]


_STREAM_CB = C.CFUNCTYPE(None, C.POINTER(C.c_short), C.POINTER(C.c_short), C.c_void_p, C.c_uint, C.c_uint,
                         C.c_void_p)
_EVENT_CB = C.CFUNCTYPE(None, C.c_int, C.c_int, C.c_void_p, C.c_void_p)


_HW_NAMES = {1: "RSP1", 255: "RSP1A", 2: "RSP2", 3: "RSPduo", 4: "RSPdx", 6: "RSP1B", 7: "RSPdx-R2"}
# LNA states available at 137 MHz (60-250 MHz band) per model; gain reduction 20..59 dB on all
SDRPLAY_LNA_MAX = {1: 3, 255: 9, 6: 9, 2: 8, 3: 9, 4: 26, 7: 26}
SDRPLAY_ANTENNAS = {2: ("A", "B"), 4: ("A", "B", "C"), 7: ("A", "B", "C")}
_SDRPLAY_HINT = ("the SDRplay API service is not running, or another program (SDRuno, SDR#, SDR++, SatDump) "
                 "is using the receiver: close it, or restart the service 'SDRplay API Service' (services.msc)")


def load_sdrplay(dll_path: str = ""):
    cands = [dll_path] if dll_path else []
    if os.name == "nt":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        cands += [os.path.join(pf, "SDRplay", "API", "x64", "sdrplay_api.dll"), "sdrplay_api.dll"]
    else:
        cands += ["/usr/local/lib/libsdrplay_api.so.3", "libsdrplay_api.so.3", "libsdrplay_api.so"]
    L = None
    for p in cands:
        try:
            L = C.CDLL(p)
            break
        except OSError:
            continue
    if L is None:
        raise SdrError("SDRplay API not found: install 'SDRplay API 3.x' from sdrplay.com")
    for fn in ("sdrplay_api_Open", "sdrplay_api_Close", "sdrplay_api_LockDeviceApi", "sdrplay_api_UnlockDeviceApi"):
        getattr(L, fn).restype = C.c_int
    L.sdrplay_api_ApiVersion.argtypes = [C.POINTER(C.c_float)]
    L.sdrplay_api_GetDevices.argtypes = [C.POINTER(_Device), C.POINTER(C.c_uint), C.c_uint]
    L.sdrplay_api_SelectDevice.argtypes = [C.POINTER(_Device)]
    L.sdrplay_api_ReleaseDevice.argtypes = [C.POINTER(_Device)]
    L.sdrplay_api_GetDeviceParams.argtypes = [C.c_void_p, C.POINTER(C.POINTER(_DeviceParams))]
    L.sdrplay_api_Init.argtypes = [C.c_void_p, C.POINTER(_CallbackFns), C.c_void_p]
    L.sdrplay_api_Uninit.argtypes = [C.c_void_p]
    L.sdrplay_api_GetErrorString.restype = C.c_char_p
    L.sdrplay_api_GetErrorString.argtypes = [C.c_int]
    return L


def enumerate_sdrplay(dll_path: str = "") -> list:
    """Connected SDRplay RSPs (serial number, model). Empty when the API is not installed / not running."""
    from .profiles import DeviceId, SDRPLAY_HW
    try:
        L = load_sdrplay(dll_path)
    except (SdrError, AttributeError):
        return []
    out = []
    if L.sdrplay_api_Open() != 0:
        return []
    try:
        if L.sdrplay_api_LockDeviceApi() != 0:
            return []
        try:
            devs = (_Device * 16)()
            nd = C.c_uint(0)
            if L.sdrplay_api_GetDevices(devs, C.byref(nd), 16) == 0:
                for i in range(nd.value):
                    out.append(DeviceId("sdrplay", "SDRplay", SDRPLAY_HW.get(devs[i].hwVer, str(devs[i].hwVer)),
                                        devs[i].SerNo.decode(errors="replace"), "", i))
        finally:
            L.sdrplay_api_UnlockDeviceApi()
    finally:
        L.sdrplay_api_Close()
    return out


def detect_receivers(cfg) -> list:
    """All connected receivers (RTL-SDR, SDRplay, HackRF, LimeSDR, PlutoSDR at the configured address)."""
    out = enumerate_rtl((cfg.get("sdr", "rtl_dll_dir", "") or "").strip())
    out += enumerate_sdrplay(cfg.get("sdr", "sdrplay_api_dll", "") or "")
    try:
        from .hackrf import enumerate_hackrf
        out += enumerate_hackrf((cfg.get("sdr", "hackrf_dll_dir", "") or "").strip())
    except Exception:  # noqa: BLE001
        pass
    try:
        from .lime import enumerate_lime
        out += enumerate_lime((cfg.get("sdr", "lime_dll_dir", "") or "").strip())
    except Exception:  # noqa: BLE001
        pass
    try:
        from .pluto import enumerate_pluto
        out += enumerate_pluto((cfg.get("sdr", "pluto_uri", "ip:192.168.2.1") or "").strip())
    except Exception:  # noqa: BLE001
        pass
    return out


class SdrPlaySource(StreamSource):
    """Every RSP of the SDRplay API 3.x: RSP1, RSP1A, RSP1B, RSP2, RSPduo (single tuner A), RSPdx, RSPdx-R2.
    Model-specific settings (Bias-T, notch, antenna port) go to the structure of that model."""

    TUNER_A = 1

    def __init__(self, sample_rate: float, center_hz: float, grdb: int, lna_state: int, agc: bool,
                 bias_tee: bool, fm_notch: bool, serial: str = "", dll_path: str = "", antenna: str = "A"):
        from .profiles import DeviceId, SDRPLAY_HW
        self.warning = ""
        self.notes: list[str] = []
        self.sample_rate = float(sample_rate)
        self.center_hz = float(center_hz)
        self.name = "SDRplay"
        self.lib = L = load_sdrplay(dll_path)
        self.running = False

        def chk(r, what):
            if r != 0:
                msg = (L.sdrplay_api_GetErrorString(r) or b"?").decode(errors="replace")
                raise SdrError(f"SDRplay {what}: {msg} ({r})")

        r = L.sdrplay_api_Open()
        if r != 0:
            msg = (L.sdrplay_api_GetErrorString(r) or b"?").decode(errors="replace")
            raise SdrError(f"SDRplay API Open: {msg} ({r}) - {_SDRPLAY_HINT}")
        ver = C.c_float(0)
        L.sdrplay_api_ApiVersion(C.byref(ver))
        self.api_version = round(ver.value, 2)
        chk(L.sdrplay_api_LockDeviceApi(), "LockDeviceApi")
        devs = (_Device * 16)()
        nd = C.c_uint(0)
        try:
            chk(L.sdrplay_api_GetDevices(devs, C.byref(nd), 16), "GetDevices")
            if nd.value == 0:
                L.sdrplay_api_UnlockDeviceApi()
                L.sdrplay_api_Close()
                raise SdrError("No SDRplay receiver found (plugged in? used by another program?)")
            sel = None
            for i in range(nd.value):
                sn = devs[i].SerNo.decode(errors="replace")
                if not serial or sn == serial:
                    sel = i
                    break
            if sel is None:                         # profile's RSP absent: use the one that is there
                self.warning = (f"SDRplay serial {serial} (active profile) not connected - using "
                                f"{devs[0].SerNo.decode(errors='replace')}")
                sel = 0
            self.device = devs[sel]
            hw = self.device.hwVer
            if hw == 3:                              # RSPduo: single tuner, tuner A (50 ohm port)
                if not (self.device.rspDuoMode & 1):
                    raise SdrError("RSPduo already used in dual-tuner / master-slave mode by another program")
                self.device.tuner = self.TUNER_A
                self.device.rspDuoMode = 1
                self.device.rspDuoSampleFreq = 0.0
            chk(L.sdrplay_api_SelectDevice(C.byref(self.device)), "SelectDevice")
        except SdrError:
            try:
                L.sdrplay_api_UnlockDeviceApi()
                L.sdrplay_api_Close()
            except Exception:  # noqa: BLE001
                pass
            raise
        L.sdrplay_api_UnlockDeviceApi()
        model = SDRPLAY_HW.get(hw, str(hw))
        self.model = model
        self.identity = DeviceId("sdrplay", "SDRplay", model, self.device.SerNo.decode(errors="replace"), "", sel)
        self.name = f"SDRplay {_HW_NAMES.get(hw, hw)} {self.device.SerNo.decode(errors='replace')} " \
                    f"(API {self.api_version})"
        pp = C.POINTER(_DeviceParams)()
        chk(L.sdrplay_api_GetDeviceParams(self.device.dev, C.byref(pp)), "GetDeviceParams")
        dp = pp.contents
        dev = dp.devParams.contents
        dev.fsFreq.fsHz = self.sample_rate
        ch = dp.rxChannelA.contents
        ch.tunerParams.bwType = 1536
        ch.tunerParams.ifType = 0
        ch.tunerParams.rfFreq.rfHz = self.center_hz
        ch.tunerParams.gain.gRdB = int(min(59, max(20, grdb)))
        lmax = SDRPLAY_LNA_MAX.get(hw, 9)
        if int(lna_state) > lmax:
            self.notes.append(f"LNA state {lna_state} > {lmax} for the {model}: using {lmax}")
        ch.tunerParams.gain.LNAstate = int(min(max(0, lna_state), lmax))
        ch.ctrlParams.decimation.enable = 0
        ch.ctrlParams.decimation.decimationFactor = 1
        ch.ctrlParams.agc.enable = 3 if agc else 0  # 5 Hz AGC or disabled
        ch.ctrlParams.dcOffset.DCenable = 1
        ch.ctrlParams.dcOffset.IQenable = 1
        b, notch = 1 if bias_tee else 0, 1 if fm_notch else 0
        ant = (antenna or "A").strip().upper()[:1]
        if hw in (255, 6):                          # RSP1A / RSP1B
            ch.rsp1aTunerParams.biasTEnable = b
            dev.rsp1aParams.rfNotchEnable = notch
        elif hw == 2:                               # RSP2: antenna A/B, AM port 2 (= 50 ohm ports)
            ch.rsp2TunerParams.biasTEnable = b
            ch.rsp2TunerParams.rfNotchEnable = notch
            ch.rsp2TunerParams.amPortSel = 0
            ch.rsp2TunerParams.antennaSel = 6 if ant == "B" else 5
            self.notes.append(f"RSP2 antenna {'B' if ant == 'B' else 'A'}")
        elif hw == 3:                               # RSPduo tuner A: no Bias-T on tuner A
            ch.rspDuoTunerParams.rfNotchEnable = notch
            ch.rspDuoTunerParams.tuner1AmPortSel = 0
            if b:
                self.notes.append("RSPduo: Bias-T exists on tuner 2 only, tuner 1 is used: Bias-T not applied")
        elif hw in (4, 7):                          # RSPdx / RSPdx-R2: antenna A/B/C, HDR off
            dev.rspDxParams.hdrEnable = 0
            dev.rspDxParams.biasTEnable = b
            dev.rspDxParams.rfNotchEnable = notch
            dev.rspDxParams.antennaSel = {"A": 0, "B": 1, "C": 2}.get(ant, 0)
            self.notes.append(f"{model} antenna {ant if ant in 'ABC' else 'A'}")
        elif b:
            self.notes.append(f"{model}: no Bias-T on this model")
        self._chk = chk
        self._init_stream()
        self._stream_cb = _STREAM_CB(self._on_stream)
        self._event_cb = _EVENT_CB(lambda *a: None)
        self._cb = _CallbackFns(C.cast(self._stream_cb, C.c_void_p).value, C.cast(self._stream_cb, C.c_void_p).value,
                                C.cast(self._event_cb, C.c_void_p).value)

    def _on_stream(self, xi, xq, params, n, reset, ctx):
        try:
            i = np.ctypeslib.as_array(xi, shape=(n,))
            q = np.ctypeslib.as_array(xq, shape=(n,))
            self.ring.put(((i.astype(np.float32) + 1j * q.astype(np.float32)) * np.float32(1.0 / 32768.0))
                          .astype(np.complex64))
        except Exception:  # noqa: BLE001  (never let an exception cross into the SDRplay thread)
            pass

    def start(self):
        self._t_start = time.monotonic()
        self._chk(self.lib.sdrplay_api_Init(self.device.dev, C.byref(self._cb), None), "Init")
        self.running = True

    def stop(self):
        L = self.lib
        try:
            if self.running:
                L.sdrplay_api_Uninit(self.device.dev)
            L.sdrplay_api_ReleaseDevice(C.byref(self.device))
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                L.sdrplay_api_Close()
            except Exception:  # noqa: BLE001
                pass
        self.running = False


# ============================================================================
# File playback
# ============================================================================
class FileSource(Source):
    def __init__(self, path: str, sample_rate: float, center_hz: float, realtime: bool):
        if not os.path.isfile(path):
            raise SdrError(f"IQ file not found: {path}")
        self.path = path
        self.name = "File: " + os.path.basename(path)
        self.center_hz = float(center_hz)
        self.realtime = realtime
        self.f = open(path, "rb")
        ext = os.path.splitext(path)[1].lower()
        self.offset = 0
        self.length = os.path.getsize(path)
        if ext == ".wav":
            self._parse_wav()
        else:
            self.sample_rate = float(sample_rate)
            self.fmt = {".cu8": "u8", ".u8": "u8", ".cs8": "s8", ".s8": "s8", ".cs16": "s16", ".s16": "s16",
                        ".raw": "s16", ".cf32": "f32", ".f32": "f32", ".fc32": "f32", ".iq": "u8"}.get(ext, "s16")
        self.bps = {"u8": 2, "s8": 2, "s16": 4, "f32": 8}[self.fmt]
        self.f.seek(self.offset)
        self._t0 = None
        self._sent = 0

    def _parse_wav(self):
        f = self.f
        hdr = f.read(12)
        if hdr[:4] != b"RIFF" or hdr[8:12] != b"WAVE":
            raise SdrError("Not a RIFF/WAVE file")
        fmt = None
        while True:
            ch = f.read(8)
            if len(ch) < 8:
                raise SdrError("WAV: no data chunk")
            cid, size = ch[:4], struct.unpack("<I", ch[4:])[0]
            if cid == b"fmt ":
                body = f.read(size)
                tag, nch, rate, _, _, bits = struct.unpack("<HHIIHH", body[:16])
                if tag == 0xFFFE and len(body) >= 26:
                    tag = struct.unpack("<H", body[24:26])[0]
                if nch != 2:
                    raise SdrError("WAV must be 2-channel IQ")
                fmt = (tag, rate, bits)
            elif cid == b"data":
                self.offset = f.tell()
                self.length = self.offset + (size if size not in (0, 0xFFFFFFFF) else
                                             os.path.getsize(self.path) - self.offset)
                break
            else:
                f.seek(size + (size & 1), 1)
        if fmt is None:
            raise SdrError("WAV: no fmt chunk")
        tag, rate, bits = fmt
        self.sample_rate = float(rate)
        if tag == 3 and bits == 32:
            self.fmt = "f32"
        elif bits == 16:
            self.fmt = "s16"
        elif bits == 8:
            self.fmt = "u8"
        else:
            raise SdrError(f"WAV format not supported (tag {tag}, {bits} bits)")

    @property
    def is_file(self) -> bool:
        return True

    @property
    def progress(self) -> float:
        span = max(1, self.length - self.offset)
        return min(1.0, (self.f.tell() - self.offset) / span)

    def read(self, max_samples: int) -> np.ndarray | None:
        remaining = (self.length - self.f.tell()) // self.bps
        n = int(min(max_samples, remaining))
        if n <= 0:
            return None
        raw = self.f.read(n * self.bps)
        if self.fmt == "u8":
            x = (np.frombuffer(raw, np.uint8).astype(np.float32) - 127.4) / 128.0
        elif self.fmt == "s8":
            x = np.frombuffer(raw, np.int8).astype(np.float32) / 128.0
        elif self.fmt == "s16":
            x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
        else:
            x = np.frombuffer(raw, "<f4").astype(np.float32)
        out = x.view(np.complex64)
        if self.realtime:
            if self._t0 is None:
                self._t0 = time.monotonic()
            self._sent += len(out)
            ahead = self._sent / self.sample_rate - (time.monotonic() - self._t0)
            if ahead > 0:
                time.sleep(ahead)
        return out

    def stop(self):
        try:
            self.f.close()
        except Exception:
            pass


def open_source(cfg) -> Source:
    dev = (cfg.get("sdr", "device", "rtlsdr") or "rtlsdr").strip().lower()
    center = cfg.getf("sdr", "center_mhz", 137.5) * 1e6
    if dev == "rtlsdr":
        return RtlSdrSource(cfg.geti("sdr", "rtl_index", 0), cfg.getf("sdr", "rtl_sample_rate", 1.2e6), center,
                            cfg.get("sdr", "rtl_gain_db", "38.6"), cfg.getb("sdr", "rtl_bias_tee", False),
                            (cfg.get("sdr", "rtl_dll_dir", "") or "").strip(),
                            (cfg.get("sdr", "rtl_serial", "") or "").strip())
    if dev == "sdrplay":
        return SdrPlaySource(cfg.getf("sdr", "sdrplay_sample_rate", 2e6), center, cfg.geti("sdr", "sdrplay_grdb", 40),
                             cfg.geti("sdr", "sdrplay_lna_state", 3), cfg.getb("sdr", "sdrplay_agc", False),
                             cfg.getb("sdr", "sdrplay_bias_tee", False), cfg.getb("sdr", "sdrplay_fm_notch", True),
                             cfg.get("sdr", "sdrplay_serial", "") or "", cfg.get("sdr", "sdrplay_api_dll", "") or "",
                             cfg.get("sdr", "sdrplay_antenna", "A") or "A")
    if dev == "hackrf":
        from .hackrf import HackRfSource
        return HackRfSource(cfg.getf("sdr", "hackrf_sample_rate", 2.4e6), center, cfg.geti("sdr", "hackrf_lna_gain", 24),
                            cfg.geti("sdr", "hackrf_vga_gain", 30), cfg.getb("sdr", "hackrf_amp", False),
                            cfg.getb("sdr", "hackrf_bias_tee", False), (cfg.get("sdr", "hackrf_serial", "") or "").strip(),
                            (cfg.get("sdr", "hackrf_dll_dir", "") or "").strip())
    if dev == "lime":
        from .lime import LimeSource
        return LimeSource(cfg.getf("sdr", "lime_sample_rate", 2.4e6), center, cfg.geti("sdr", "lime_gain_db", 40),
                          cfg.get("sdr", "lime_antenna", "auto") or "auto", (cfg.get("sdr", "lime_serial", "") or "").strip(),
                          (cfg.get("sdr", "lime_dll_dir", "") or "").strip(), cfg.get("sdr", "lime_format", "f32") or "f32")
    if dev == "pluto":
        from .pluto import PlutoSource
        return PlutoSource((cfg.get("sdr", "pluto_uri", "ip:192.168.2.1") or "ip:192.168.2.1").strip(),
                           cfg.getf("sdr", "pluto_sample_rate", 2.16e6), center,
                           cfg.get("sdr", "pluto_gain_db", "50") or "50")
    if dev == "file":
        fc = cfg.getf("sdr", "file_center_mhz", 0.0) * 1e6 or center
        return FileSource(cfg.get("sdr", "file_path", ""), cfg.getf("sdr", "file_sample_rate", 1e6), fc,
                          cfg.getb("sdr", "file_realtime", False))
    raise SdrError(f"Unknown device '{dev}' (rtlsdr | sdrplay | hackrf | lime | pluto | file)")

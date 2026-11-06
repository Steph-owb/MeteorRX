"""LimeSDR (Mini 1.x / Mini 2.0 / USB) through the LimeSuite LMS API (LimeSuite.dll) with ctypes.

The library is not shipped with MeteorRX: the one of an installed SDR program is used (SatDump 1.2 ships
LimeSuite 23.11, which knows the Mini 2.0) or the folder given by [sdr] lime_dll_dir. LimeSuite versions older
than 22.09 (e.g. PothosSDR 2021) treat a Mini 2.0 as a Mini 1.x and do not work with it, so a library that
knows the Mini 2.0 is preferred. The Mini (FT601 USB 3 chip) needs the FTDI D3XX driver on Windows.

Receive path: RX channel 0, LO 137.5 MHz, 2.4 MS/s (both Meteor channels at +/-400 kHz, away from the DC
residue), analog low-pass = sample rate, gain 0..73 dB (call sequence of SatDump 1.2), float32 stream read by a
thread into the sample ring."""
from __future__ import annotations

import ctypes as C
import os
import re
import threading

import numpy as np

from . import drivers
from .sdr import SdrError, StreamSource

LIB_NAMES = ["LimeSuite.dll"] if os.name == "nt" else ["libLimeSuite.so", "libLimeSuite.so.23.11-1"]
REQUIRED = ("LMS_GetDeviceList", "LMS_Open", "LMS_Close", "LMS_Init", "LMS_EnableChannel", "LMS_SetSampleRate",
            "LMS_SetLOFrequency", "LMS_SetGaindB", "LMS_SetupStream", "LMS_StartStream", "LMS_RecvStream",
            "LMS_StopStream", "LMS_DestroyStream")
EXTRA_DIRS = [r"C:\Program Files\LimeSuite\bin", r"C:\Program Files\PothosSDR\bin", r"C:\PothosSDR\bin"]
LMS_CH_RX = False
GAIN_MAX = 73
STREAM_CHUNK = 1 << 15          # samples per LMS_RecvStream call (13.6 ms at 2.4 MS/s)

_info_str = C.c_char * 256
_name_str = C.c_char * 16


class _Stream(C.Structure):
    _fields_ = [("handle", C.c_size_t), ("isTx", C.c_bool), ("channel", C.c_uint32), ("fifoSize", C.c_uint32),
                ("throughputVsLatency", C.c_float), ("dataFmt", C.c_int), ("linkFmt", C.c_int)]


class _Meta(C.Structure):
    _fields_ = [("timestamp", C.c_uint64), ("waitForTimestamp", C.c_bool), ("flushPartialPacket", C.c_bool)]


class _Status(C.Structure):
    _fields_ = [("active", C.c_bool), ("fifoFilledCount", C.c_uint32), ("fifoSize", C.c_uint32),
                ("underrun", C.c_uint32), ("overrun", C.c_uint32), ("droppedPackets", C.c_uint32),
                ("sampleRate", C.c_double), ("linkRate", C.c_double), ("timestamp", C.c_uint64)]


class _DevInfo(C.Structure):
    _fields_ = [("deviceName", C.c_char * 32), ("expansionName", C.c_char * 32), ("firmwareVersion", C.c_char * 16),
                ("hardwareVersion", C.c_char * 16), ("protocolVersion", C.c_char * 16),
                ("boardSerialNumber", C.c_uint64), ("gatewareVersion", C.c_char * 16),
                ("gatewareTargetBoard", C.c_char * 32)]


_LOG_CB = C.CFUNCTYPE(None, C.c_int, C.c_char_p)
_lib = None
_lib_path = ""
_lock = threading.Lock()
_log: list[str] = []


def _on_log(level, msg):                    # keep errors/warnings (0 critical, 1 error, 2 warning), drop the rest
    try:
        if level <= 2 and msg:
            _log.append(msg.decode(errors="replace").strip())
            del _log[:-20]
    except Exception:  # noqa: BLE001
        pass


_log_cb = _LOG_CB(_on_log)


def find_library(configured_dir: str = "") -> tuple[str | None, list[str]]:
    """Usable LimeSuite library (64-bit, all functions, dependencies present), preferring one that knows the
    Mini 2.0. Returns (path, report)."""
    dirs = []
    if configured_dir:
        dirs.append((configured_dir, "configured (lime_dll_dir)"))
    dirs += [(d, f"installed: {lab}") for d, lab in drivers.installed_dirs()]
    dirs += [(d, "installed") for d in EXTRA_DIRS]
    if os.name != "nt":
        dirs += [(d, "system") for d in ("/usr/local/lib", "/usr/lib", "/usr/lib/x86_64-linux-gnu", "/opt/homebrew/lib")]
    lines, seen, ok_old = [], set(), None
    for d, origin in dirs:
        for n in LIB_NAMES:
            p = os.path.join(d, n)
            key = os.path.normcase(os.path.abspath(p))
            if key in seen or not os.path.isfile(p):
                continue
            seen.add(key)
            ok, why, mini2 = check_library(p)
            lines.append(f"{p} [{origin}]: {why}")
            if ok and mini2:
                return p, lines
            if ok and ok_old is None:
                ok_old = p
    if ok_old:
        lines.append(f"using {ok_old}: older LimeSuite (no LimeSDR Mini 2.0 support)")
        return ok_old, lines
    lines.append("no usable LimeSuite library: install SatDump 1.2 (it includes LimeSuite.dll 23.11), "
                 "or set [sdr] lime_dll_dir")
    return None, lines


def check_library(path: str) -> tuple[bool, str, bool]:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return False, f"unreadable ({e})", False
    mini2 = b"Mini_v2" in data
    if not path.lower().endswith(".dll"):
        return True, "OK", mini2
    try:
        pe = drivers.parse_pe(data)
    except ValueError as e:
        return False, f"unreadable ({e})", False
    if pe.machine != 0x8664:
        return False, "not a 64-bit DLL", False
    missing = [e for e in REQUIRED if e not in pe.exports]
    if missing:
        return False, "missing functions: " + ", ".join(missing[:3]), False
    unres = [i for i in pe.imports if not drivers._import_resolvable(i, os.path.dirname(path))]
    if unres:
        return False, "missing DLL(s) next to it: " + ", ".join(unres), False
    m = re.search(rb"(\d{2}\.\d{1,2}\.\d+)-g[0-9a-f]{6,}", data)
    ver = m.group(1).decode() if m else "?"
    return True, f"OK (LimeSuite {ver}{', Mini 2.0 support' if mini2 else ''})", mini2


def load(configured_dir: str = ""):
    global _lib, _lib_path
    with _lock:
        if _lib is not None:
            return _lib, _lib_path
        path, lines = find_library(configured_dir)
        if path is None:
            raise SdrError("LimeSDR: " + lines[-1] + ("\n" + "\n".join(lines[:-1]) if len(lines) > 1 else ""))
        if hasattr(os, "add_dll_directory") and os.path.dirname(path):
            try:
                os.add_dll_directory(os.path.dirname(path))
            except OSError:
                pass
        try:
            L = C.CDLL(path)
        except OSError as e:
            raise SdrError(f"LimeSDR: cannot load {path}: {e}") from e
        vp, b, sz, d = C.c_void_p, C.c_bool, C.c_size_t, C.c_double
        L.LMS_GetDeviceList.argtypes = [C.c_void_p]
        L.LMS_Open.argtypes = [C.POINTER(vp), C.c_char_p, vp]
        L.LMS_Close.argtypes = [vp]
        L.LMS_Init.argtypes = [vp]
        L.LMS_EnableChannel.argtypes = [vp, b, sz, b]
        L.LMS_SetSampleRate.argtypes = [vp, d, sz]
        L.LMS_GetSampleRate.argtypes = [vp, b, sz, C.POINTER(d), C.POINTER(d)]
        L.LMS_SetLOFrequency.argtypes = [vp, b, sz, d]
        L.LMS_GetAntennaList.argtypes = [vp, b, sz, C.c_void_p]
        L.LMS_SetAntenna.argtypes = [vp, b, sz, sz]
        L.LMS_SetLPFBW.argtypes = [vp, b, sz, d]
        L.LMS_SetGaindB.argtypes = [vp, b, sz, C.c_uint]
        L.LMS_SetLPF.argtypes = [vp, b, sz, b]
        L.LMS_Calibrate.argtypes = [vp, b, sz, d, C.c_uint]
        L.LMS_SetupStream.argtypes = [vp, C.POINTER(_Stream)]
        L.LMS_DestroyStream.argtypes = [vp, C.POINTER(_Stream)]
        L.LMS_StartStream.argtypes = [C.POINTER(_Stream)]
        L.LMS_StopStream.argtypes = [C.POINTER(_Stream)]
        L.LMS_RecvStream.argtypes = [C.POINTER(_Stream), C.c_void_p, sz, C.POINTER(_Meta), C.c_uint]
        try:
            L.LMS_GetStreamStatus.argtypes = [C.POINTER(_Stream), C.POINTER(_Status)]
        except AttributeError:
            pass
        L.LMS_GetDeviceInfo.argtypes = [vp]
        L.LMS_GetDeviceInfo.restype = C.POINTER(_DevInfo)
        L.LMS_GetLastErrorMessage.restype = C.c_char_p
        L.LMS_GetLastErrorMessage.argtypes = []
        L.LMS_GetLibraryVersion.restype = C.c_char_p
        L.LMS_GetLibraryVersion.argtypes = []
        try:
            L.LMS_RegisterLogHandler.argtypes = [_LOG_CB]
            L.LMS_RegisterLogHandler(_log_cb)
        except AttributeError:
            pass
        _lib, _lib_path = L, path
        return L, path


def _err(L) -> str:
    try:
        m = (L.LMS_GetLastErrorMessage() or b"").decode(errors="replace").strip()
    except Exception:  # noqa: BLE001
        m = ""
    return m or (_log[-1] if _log else "error")


def parse_info(s: str) -> dict:
    """'LimeSDR Mini, media=USB 3.0, module=FT601, addr=24607:1027, serial=1D4C...' -> fields."""
    parts = [p.strip() for p in s.split(",")]
    d = {"name": parts[0] if parts else "LimeSDR"}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def _list(L) -> list[str]:
    n = L.LMS_GetDeviceList(None)
    if n <= 0:
        return []
    arr = (_info_str * n)()
    n = L.LMS_GetDeviceList(arr)
    return [arr[i].value.decode(errors="replace") for i in range(max(0, n))]


def enumerate_lime(configured_dir: str = "") -> list:
    from .profiles import DeviceId
    try:
        L, _ = load(configured_dir)
        infos = _list(L)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for i, s in enumerate(infos):
        d = parse_info(s)
        out.append(DeviceId("lime", "Lime Microsystems", d["name"], d.get("serial", "").lstrip("0") or "0", index=i))
    return out


def pick_antenna(names: list[str], wanted: str, freq_hz: float) -> int:
    """Index of the RX path: the configured name, else LNAW (wide, < 2 GHz) or LNAH above 2 GHz."""
    up = [n.upper() for n in names]
    w = (wanted or "auto").strip().upper()
    if w not in ("", "AUTO") and w in up:
        return up.index(w)
    pref = ("LNAW", "LNAL", "LNAH") if freq_hz < 2e9 else ("LNAH", "LNAW")
    for p in pref:
        if p in up:
            return up.index(p)
    return 1 if len(names) > 1 else 0


class LimeSource(StreamSource):
    NO_DATA_S = 3.0

    def __init__(self, sample_rate: float, center_hz: float, gain_db: int, antenna: str = "auto", serial: str = "",
                 dll_dir: str = "", fmt: str = "f32"):
        from .profiles import DeviceId
        self.warning = ""
        self.notes: list[str] = []
        self.sample_rate = float(sample_rate)
        self.center_hz = float(center_hz)
        self.name = "LimeSDR"
        self.fmt = "i16" if str(fmt).strip().lower() == "i16" else "f32"
        L, self.lib_path = load(dll_dir)
        self.lib = L
        infos = _list(L)
        if not infos:
            raise SdrError("No LimeSDR found (plugged in? FTDI D3XX driver installed? used by another program?)",
                           usb_driver=True)
        parsed = [parse_info(s) for s in infos]
        idx = 0
        if serial:
            m = [i for i, d in enumerate(parsed) if d.get("serial", "").lstrip("0") == serial.lstrip("0")]
            if m:
                idx = m[0]
            else:
                self.warning = f"LimeSDR serial {serial} (active profile) not connected - using {parsed[0].get('serial', '?')}"
        info = parsed[idx]
        self.dev = C.c_void_p()
        self._info = infos[idx].encode()
        if L.LMS_Open(C.byref(self.dev), self._info, None) != 0 or not self.dev:
            raise SdrError(f"LimeSDR open failed: {_err(L)} - used by another program, or driver missing")
        self.stream = None
        try:
            self._configure(L, gain_db, antenna)
        except SdrError:
            L.LMS_Close(self.dev)
            raise
        try:
            di = L.LMS_GetDeviceInfo(self.dev).contents
            dec = lambda b: b.decode(errors="replace")  # noqa: E731
            hw = f"HW {dec(di.hardwareVersion)}, GW {dec(di.gatewareVersion)}"
            self.notes.append(f"board {dec(di.deviceName)}, gateware target {dec(di.gatewareTargetBoard)}, "
                              f"firmware {dec(di.firmwareVersion)}, protocol {dec(di.protocolVersion)}")
        except Exception:  # noqa: BLE001
            hw = ""
        for m in dict.fromkeys(_log):           # unique, without the harmless LPF clamp notice
            if "LPF min bandwidth" not in m:
                self.notes.append(f"LimeSuite: {m}")
        sn = info.get("serial", "").lstrip("0") or "0"
        prod = info["name"]
        self.identity = DeviceId("lime", "Lime Microsystems", prod, sn, "", idx)
        self.name = f"{prod} {sn[-8:]}" + (f" ({hw})" if hw else "") + f" [{self.lib_path}]"
        try:
            ver = (L.LMS_GetLibraryVersion() or b"").decode()
            self.notes.insert(0, f"LimeSuite {ver}")
        except Exception:  # noqa: BLE001
            pass
        self._init_stream()

    def _configure(self, L, gain_db, antenna):
        """Same sequence as SatDump 1.2 (known to stream with a Mini 2.0): init (retried once - LimeSuite bug),
        RX channel, antenna, rate, LO, gain, low-pass (again after the stream start). No LMS_Calibrate (SatDump
        does not either); the DC residue sits at the centre, 400 kHz away from both Meteor channels."""
        dev = self.dev
        _log.clear()

        def chk(what, r):
            if r != 0:
                raise SdrError(f"LimeSDR {what}: {_err(L)}")
        if L.LMS_Init(dev) != 0:
            L.LMS_Close(dev)
            self.dev = dev = C.c_void_p()
            if L.LMS_Open(C.byref(dev), self._info, None) != 0 or not dev:
                raise SdrError(f"LimeSDR re-open failed: {_err(L)}")
            chk("init", L.LMS_Init(dev))
        chk("RX channel", L.LMS_EnableChannel(dev, LMS_CH_RX, 0, True))
        names = (_name_str * 16)()
        na = L.LMS_GetAntennaList(dev, LMS_CH_RX, 0, names)
        alist = [names[i].value.decode(errors="replace") for i in range(max(0, min(na, 16)))]
        if alist:
            ai = pick_antenna(alist, antenna, self.center_hz)
            chk("antenna", L.LMS_SetAntenna(dev, LMS_CH_RX, 0, ai))
            self.antenna = alist[ai]
            self.notes.append(f"RX path {alist[ai]} (available: {', '.join(a for a in alist if a != 'NONE')})")
        chk("sample rate", L.LMS_SetSampleRate(dev, self.sample_rate, 0))
        host, rf = C.c_double(), C.c_double()
        if L.LMS_GetSampleRate(dev, LMS_CH_RX, 0, C.byref(host), C.byref(rf)) == 0 and host.value > 0:
            if abs(host.value - self.sample_rate) > 1:
                self.notes.append(f"sample rate {host.value:.0f} S/s (asked {self.sample_rate:.0f})")
            self.sample_rate = host.value
        chk("frequency", L.LMS_SetLOFrequency(dev, LMS_CH_RX, 0, self.center_hz))
        g = int(min(GAIN_MAX, max(0, int(gain_db))))
        chk("gain", L.LMS_SetGaindB(dev, LMS_CH_RX, 0, g))
        self.gain_desc = f"{g} dB"
        self._set_lpf()

    def _set_lpf(self):
        L = self.lib
        try:
            if L.LMS_SetLPFBW(self.dev, LMS_CH_RX, 0, self.sample_rate) != 0:
                self.notes.append(f"low-pass filter not set ({_err(L)})")
            L.LMS_SetLPF(self.dev, LMS_CH_RX, 0, True)
        except Exception:  # noqa: BLE001
            pass

    def start(self):
        L = self.lib
        _log.clear()
        st = _Stream(0, False, 0, 8192 * 10, 0.5, 1 if self.fmt == "i16" else 0, 0)   # as SatDump
        if L.LMS_SetupStream(self.dev, C.byref(st)) != 0:
            raise SdrError(f"LimeSDR stream setup: {_err(L)}")
        self.stream = st
        if L.LMS_StartStream(C.byref(st)) != 0:
            raise SdrError(f"LimeSDR stream start: {_err(L)}")
        self._start_thread(self._run, "lime-stream")
        self._set_lpf()                         # again after start: LimeSDR Mini 2.0 (SatDump does the same)

    def status(self) -> str:
        s = _Status()
        try:
            if self.stream is None or self.lib.LMS_GetStreamStatus(C.byref(self.stream), C.byref(s)) != 0:
                return "status n/a"
        except Exception:  # noqa: BLE001
            return "status n/a"
        return (f"active {s.active}, FIFO {s.fifoFilledCount}/{s.fifoSize}, overrun {s.overrun}, underrun "
                f"{s.underrun}, dropped packets {s.droppedPackets}, rate {s.sampleRate:.0f} S/s, link {s.linkRate / 1e6:.2f} MB/s")

    def _run(self):
        import time
        L, st = self.lib, self.stream
        i16 = self.fmt == "i16"
        buf = np.empty(2 * STREAM_CHUNK, np.int16) if i16 else np.empty(STREAM_CHUNK, np.complex64)
        meta = _Meta()
        ptr = buf.ctypes.data_as(C.c_void_p)
        fails = calls = empty = got = 0
        t_last = time.monotonic()
        while not self._stop_evt.is_set():
            n = L.LMS_RecvStream(C.byref(st), ptr, STREAM_CHUNK, C.byref(meta), 500)
            calls += 1
            if n < 0:
                fails += 1
                if fails > 5:
                    raise SdrError(f"stream read failed: {_err(L)} ({self.status()})")
                continue
            fails = 0
            if n:
                got += n
                t_last = time.monotonic()
                if i16:
                    self.ring.put((buf[:2 * n].astype(np.float32) * np.float32(1 / 32768)).view(np.complex64))
                else:
                    self.ring.put(buf[:n].copy())
            else:
                empty += 1
                if time.monotonic() - t_last > self.NO_DATA_S:
                    raise SdrError(f"no samples from the LimeSDR: {calls} reads ({empty} empty, {got} samples); "
                                   f"{self.status()}" + (f"; LimeSuite: {' | '.join(_log[-4:])}" if _log else ""))

    def stop(self):
        L = self.lib
        self._join(2.0)
        try:
            if self.stream is not None:
                L.LMS_StopStream(C.byref(self.stream))
                L.LMS_DestroyStream(self.dev, C.byref(self.stream))
                self.stream = None
            L.LMS_EnableChannel(self.dev, LMS_CH_RX, 0, False)
            L.LMS_Close(self.dev)
        except Exception:  # noqa: BLE001
            pass

"""HackRF One through libhackrf (hackrf.dll / libhackrf.so) with ctypes.

The library is not shipped with MeteorRX: the one of an installed SDR program is used (SatDump, SDR++,
PothosSDR...) or the folder given by [sdr] hackrf_dll_dir. Like the RTL-SDR, the HackRF needs the WinUSB
driver (Zadig) on Windows. Samples are 8-bit; MeteorRX runs it at 2.4 MS/s (2 MS/s is the HackRF minimum),
which the 137.5 MHz tuning turns into both Meteor channels at +/-400 kHz, away from the DC spike."""
from __future__ import annotations

import ctypes as C
import os
import threading

import numpy as np

from . import drivers
from .sdr import SdrError, StreamSource

LIB_NAMES = ["hackrf.dll", "libhackrf.dll"] if os.name == "nt" else ["libhackrf.so.0", "libhackrf.so"]
REQUIRED = ("hackrf_init", "hackrf_exit", "hackrf_open", "hackrf_close", "hackrf_set_sample_rate",
            "hackrf_set_freq", "hackrf_set_lna_gain", "hackrf_set_vga_gain", "hackrf_set_amp_enable",
            "hackrf_start_rx", "hackrf_stop_rx", "hackrf_device_list", "hackrf_device_list_open")
EXTRA_DIRS = [r"C:\Program Files\PothosSDR\bin", r"C:\PothosSDR\bin", r"C:\Program Files\HackRF\bin"]


class _Transfer(C.Structure):
    _fields_ = [("device", C.c_void_p), ("buffer", C.POINTER(C.c_ubyte)), ("buffer_length", C.c_int),
                ("valid_length", C.c_int), ("rx_ctx", C.c_void_p), ("tx_ctx", C.c_void_p)]


class _DeviceList(C.Structure):
    _fields_ = [("serial_numbers", C.POINTER(C.c_char_p)), ("usb_board_ids", C.POINTER(C.c_int)),
                ("usb_device_index", C.POINTER(C.c_int)), ("devicecount", C.c_int),
                ("usb_devices", C.POINTER(C.c_void_p)), ("usb_devicecount", C.c_int)]


class _PartSerial(C.Structure):
    _fields_ = [("part_id", C.c_uint32 * 2), ("serial_no", C.c_uint32 * 4)]


_RX_CB = C.CFUNCTYPE(C.c_int, C.POINTER(_Transfer))
_lib = None
_lib_path = ""
_lock = threading.Lock()


def find_library(configured_dir: str = "") -> tuple[str | None, list[str]]:
    """First usable hackrf library (64-bit, all functions, dependencies present). Returns (path, report)."""
    dirs = []
    if configured_dir:
        dirs.append((configured_dir, "configured (hackrf_dll_dir)"))
    dirs += [(d, f"installed: {lab}") for d, lab in drivers.installed_dirs()]
    dirs += [(d, "installed") for d in EXTRA_DIRS]
    if os.name != "nt":
        dirs += [(d, "system") for d in ("/usr/local/lib", "/usr/lib", "/usr/lib/x86_64-linux-gnu", "/opt/homebrew/lib")]
    lines, seen = [], set()
    for d, origin in dirs:
        for n in LIB_NAMES:
            p = os.path.join(d, n)
            key = os.path.normcase(os.path.abspath(p))
            if key in seen or not os.path.isfile(p):
                continue
            seen.add(key)
            ok, why = check_library(p)
            lines.append(f"{p} [{origin}]: {why}")
            if ok:
                return p, lines
    lines.append("no usable HackRF library: install SatDump or SDR++ (they include hackrf.dll), "
                 "or set [sdr] hackrf_dll_dir")
    return None, lines


def check_library(path: str) -> tuple[bool, str]:
    if not path.lower().endswith(".dll"):
        return True, "OK"
    try:
        with open(path, "rb") as f:
            pe = drivers.parse_pe(f.read())
    except (OSError, ValueError) as e:
        return False, f"unreadable ({e})"
    if pe.machine != 0x8664:
        return False, "not a 64-bit DLL"
    missing = [e for e in REQUIRED if e not in pe.exports]
    if missing:
        return False, "missing functions: " + ", ".join(missing[:3])
    unres = [i for i in pe.imports if not drivers._import_resolvable(i, os.path.dirname(path))]
    if unres:
        return False, "missing DLL(s) next to it: " + ", ".join(unres)
    return True, "OK"


def load(configured_dir: str = ""):
    """Load and initialise libhackrf once per process (hackrf_init / hackrf_exit are process-wide)."""
    global _lib, _lib_path
    with _lock:
        if _lib is not None:
            return _lib, _lib_path
        path, lines = find_library(configured_dir)
        if path is None:
            raise SdrError("HackRF: " + lines[-1] + ("\n" + "\n".join(lines[:-1]) if len(lines) > 1 else ""))
        if hasattr(os, "add_dll_directory") and os.path.dirname(path):
            try:
                os.add_dll_directory(os.path.dirname(path))
            except OSError:
                pass
        try:
            L = C.CDLL(path)
        except OSError as e:
            raise SdrError(f"HackRF: cannot load {path}: {e}") from e
        L.hackrf_init.restype = C.c_int
        L.hackrf_exit.restype = C.c_int
        L.hackrf_error_name.restype = C.c_char_p
        L.hackrf_error_name.argtypes = [C.c_int]
        L.hackrf_device_list.restype = C.POINTER(_DeviceList)
        L.hackrf_device_list_free.argtypes = [C.POINTER(_DeviceList)]
        L.hackrf_device_list_open.argtypes = [C.POINTER(_DeviceList), C.c_int, C.POINTER(C.c_void_p)]
        L.hackrf_open_by_serial.argtypes = [C.c_char_p, C.POINTER(C.c_void_p)]
        L.hackrf_close.argtypes = [C.c_void_p]
        L.hackrf_set_sample_rate.argtypes = [C.c_void_p, C.c_double]
        L.hackrf_set_baseband_filter_bandwidth.argtypes = [C.c_void_p, C.c_uint32]
        L.hackrf_compute_baseband_filter_bw.argtypes = [C.c_uint32]
        L.hackrf_compute_baseband_filter_bw.restype = C.c_uint32
        L.hackrf_set_freq.argtypes = [C.c_void_p, C.c_uint64]
        L.hackrf_set_lna_gain.argtypes = [C.c_void_p, C.c_uint32]
        L.hackrf_set_vga_gain.argtypes = [C.c_void_p, C.c_uint32]
        L.hackrf_set_amp_enable.argtypes = [C.c_void_p, C.c_uint8]
        L.hackrf_set_antenna_enable.argtypes = [C.c_void_p, C.c_uint8]
        L.hackrf_start_rx.argtypes = [C.c_void_p, _RX_CB, C.c_void_p]
        L.hackrf_stop_rx.argtypes = [C.c_void_p]
        L.hackrf_board_partid_serialno_read.argtypes = [C.c_void_p, C.POINTER(_PartSerial)]
        L.hackrf_board_id_read.argtypes = [C.c_void_p, C.POINTER(C.c_uint8)]
        L.hackrf_board_id_name.argtypes = [C.c_int]
        L.hackrf_board_id_name.restype = C.c_char_p
        r = L.hackrf_init()
        if r != 0:
            raise SdrError(f"HackRF: hackrf_init failed ({_err(L, r)})")
        _lib, _lib_path = L, path
        return L, path


def _err(L, r: int) -> str:
    try:
        return f"{(L.hackrf_error_name(r) or b'?').decode()} ({r})"
    except Exception:  # noqa: BLE001
        return str(r)


def enumerate_hackrf(configured_dir: str = "") -> list:
    from .profiles import DeviceId
    try:
        L, _ = load(configured_dir)
    except SdrError:
        return []
    out = []
    lst = L.hackrf_device_list()
    if not lst:
        return out
    try:
        d = lst.contents
        for i in range(d.devicecount):
            sn = d.serial_numbers[i]
            out.append(DeviceId("hackrf", "Great Scott Gadgets", "HackRF One",
                                (sn or b"").decode(errors="replace").lstrip("0") or "0", index=i))
    finally:
        L.hackrf_device_list_free(lst)
    return out


class HackRfSource(StreamSource):
    LNA_STEP, VGA_STEP = 8, 2

    def __init__(self, sample_rate: float, center_hz: float, lna_gain: int, vga_gain: int, amp: bool,
                 bias_tee: bool, serial: str = "", dll_dir: str = ""):
        from .profiles import DeviceId
        self.warning = ""
        self.notes: list[str] = []
        self.sample_rate = max(2.0e6, float(sample_rate))
        self.center_hz = float(center_hz)
        self.name = "HackRF"
        L, self.lib_path = load(dll_dir)
        self.lib = L
        self.dev = C.c_void_p()
        lst = L.hackrf_device_list()
        if not lst or lst.contents.devicecount == 0:
            if lst:
                L.hackrf_device_list_free(lst)
            raise SdrError("No HackRF found (plugged in? WinUSB driver installed with Zadig? used by another program?)",
                           usb_driver=True)
        try:
            d = lst.contents
            serials = [(d.serial_numbers[i] or b"").decode(errors="replace") for i in range(d.devicecount)]
            idx = 0
            if serial:
                m = [i for i, s in enumerate(serials) if s.lstrip("0") == serial.lstrip("0")]
                if m:
                    idx = m[0]
                else:
                    self.warning = f"HackRF serial {serial} (active profile) not connected - using {serials[0]}"
            r = L.hackrf_device_list_open(lst, idx, C.byref(self.dev))
        finally:
            L.hackrf_device_list_free(lst)
        if r != 0:
            raise SdrError(f"HackRF open failed: {_err(L, r)} - used by another program, or USB driver not WinUSB")
        sn = serials[idx].lstrip("0") or "0"
        self.identity = DeviceId("hackrf", "Great Scott Gadgets", "HackRF One", sn, "", idx)
        self.name = f"HackRF One {sn[-8:]} ({self.lib_path})"
        lna = int(min(40, max(0, lna_gain)) // self.LNA_STEP * self.LNA_STEP)
        vga = int(min(62, max(0, vga_gain)) // self.VGA_STEP * self.VGA_STEP)
        self.gain_desc = f"LNA {lna} dB, VGA {vga} dB, amp {'on' if amp else 'off'}"
        for what, rr in (("sample rate", L.hackrf_set_sample_rate(self.dev, self.sample_rate)),
                         ("filter", L.hackrf_set_baseband_filter_bandwidth(
                             self.dev, L.hackrf_compute_baseband_filter_bw(int(self.sample_rate * 0.75)))),
                         ("frequency", L.hackrf_set_freq(self.dev, int(round(self.center_hz)))),
                         ("LNA gain", L.hackrf_set_lna_gain(self.dev, lna)),
                         ("VGA gain", L.hackrf_set_vga_gain(self.dev, vga)),
                         ("amplifier", L.hackrf_set_amp_enable(self.dev, 1 if amp else 0)),
                         ("Bias-T", L.hackrf_set_antenna_enable(self.dev, 1 if bias_tee else 0))):
            if rr != 0:
                L.hackrf_close(self.dev)
                raise SdrError(f"HackRF {what}: {_err(L, rr)}")
        self._bias = bias_tee
        self._init_stream()
        self._cb = _RX_CB(self._on_rx)
        self.running = False

    def _on_rx(self, tp):
        try:
            t = tp.contents
            n = int(t.valid_length) & ~1
            if n > 0 and not self._stop_evt.is_set():
                a = np.ctypeslib.as_array(t.buffer, shape=(n,)).view(np.int8)
                self.ring.put((a.astype(np.float32) * np.float32(1.0 / 128.0)).view(np.complex64))
        except Exception:  # noqa: BLE001
            pass
        return 0

    def start(self):
        import time
        self._t_start = time.monotonic()
        r = self.lib.hackrf_start_rx(self.dev, self._cb, None)
        if r != 0:
            raise SdrError(f"HackRF start: {_err(self.lib, r)}")
        self.running = True

    def stop(self):
        self._stop_evt.set()
        L = self.lib
        try:
            if self.running:
                L.hackrf_stop_rx(self.dev)
            if self._bias:
                L.hackrf_set_antenna_enable(self.dev, 0)
            L.hackrf_close(self.dev)
        except Exception:  # noqa: BLE001
            pass
        self.running = False

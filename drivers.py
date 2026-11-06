"""RTL-SDR driver management.

Two layers are needed for an RTL-SDR on Windows:
  1. the USB kernel driver bound to the dongle: must be WinUSB (or libusbK). Windows often binds
     its DVB-T driver (RTL2832UBDA) instead, or nothing at all  -> fixed with Zadig (bundled);
  2. the user-mode library rtlsdr.dll. MeteorRX looks for an existing installation (SatDump, SDR++,
     SDR#, PATH...) and uses it when it is suitable; otherwise it uses its bundled copy (official
     RTL-SDR Blog v1.3.6 x64, libusb statically linked, V4-capable).

A library is "suitable" when: it is a valid x64 PE, exports every function MeteorRX calls, all its
imported DLLs are resolvable, and - if an RTL-SDR Blog V4/V4L is connected - it contains the
RTL-SDR Blog V4 support (older osmocom builds tune a V4 incorrectly).
"""
from __future__ import annotations

import ctypes as C
import os
import struct
import sys
from dataclasses import dataclass, field

REQUIRED_EXPORTS = (
    "rtlsdr_get_device_count", "rtlsdr_get_device_name", "rtlsdr_open", "rtlsdr_close",
    "rtlsdr_set_center_freq", "rtlsdr_set_sample_rate", "rtlsdr_set_tuner_gain_mode",
    "rtlsdr_get_tuner_gains", "rtlsdr_set_tuner_gain", "rtlsdr_set_agc_mode", "rtlsdr_reset_buffer",
    "rtlsdr_read_sync",
)
OPTIONAL_EXPORTS = ("rtlsdr_set_bias_tee", "rtlsdr_get_device_usb_strings")
V4_MARKERS = (b"Blog V4", b"RTLSDRBlog")

# USB VID/PID of RTL2832U based receivers (from librtlsdr known_devices[])
RTL_IDS = {
    (0x0bda, 0x2832), (0x0bda, 0x2838), (0x0413, 0x6680), (0x0413, 0x6f0f), (0x0458, 0x707f),
    (0x0ccd, 0x00a9), (0x0ccd, 0x00b3), (0x0ccd, 0x00b4), (0x0ccd, 0x00b5), (0x0ccd, 0x00b7),
    (0x0ccd, 0x00b8), (0x0ccd, 0x00b9), (0x0ccd, 0x00c0), (0x0ccd, 0x00c6), (0x0ccd, 0x00d3),
    (0x0ccd, 0x00d7), (0x0ccd, 0x00e0), (0x1554, 0x5020), (0x15f4, 0x0131), (0x15f4, 0x0133),
    (0x185b, 0x0620), (0x185b, 0x0650), (0x185b, 0x0680), (0x1b80, 0xd393), (0x1b80, 0xd394),
    (0x1b80, 0xd395), (0x1b80, 0xd397), (0x1b80, 0xd398), (0x1b80, 0xd39d), (0x1b80, 0xd3a4),
    (0x1b80, 0xd3a8), (0x1b80, 0xd3af), (0x1b80, 0xd3b0), (0x1d19, 0x1101), (0x1d19, 0x1102),
    (0x1d19, 0x1103), (0x1d19, 0x1104), (0x1f4d, 0xa803), (0x1f4d, 0xb803), (0x1f4d, 0xc803),
    (0x1f4d, 0xd286), (0x1f4d, 0xd803),
}
GOOD_SERVICES = {"winusb": "WinUSB", "libusbk": "libusbK", "libusb0": "libusb0"}


# ============================================================================
# Minimal PE parser (exports, imports, machine) - no third-party dependency
# ============================================================================
@dataclass
class PeInfo:
    machine: int = 0
    exports: set = field(default_factory=set)
    imports: list = field(default_factory=list)
    signed: bool = False


def _rva_to_off(rva, sections):
    for va, vsize, raw, rsize in sections:
        if va <= rva < va + max(vsize, rsize):
            return rva - va + raw
    raise ValueError("RVA outside sections")


def _cstr(data, off, limit=512):
    end = data.find(b"\0", off, off + limit)
    return data[off:end if end >= 0 else off + limit].decode("ascii", "replace")


def parse_pe(data: bytes) -> PeInfo:
    if data[:2] != b"MZ":
        raise ValueError("not a PE file")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("bad PE signature")
    machine, nsec = struct.unpack_from("<HH", data, pe + 4)
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    dd = opt + (112 if magic == 0x20B else 96)
    ndd = struct.unpack_from("<I", data, dd - 4)[0]

    def ddir(i):
        return struct.unpack_from("<II", data, dd + 8 * i) if i < ndd else (0, 0)

    sec = opt + opt_size
    sections = []
    for i in range(nsec):
        vsize, va, rsize, raw = struct.unpack_from("<IIII", data, sec + 40 * i + 8)
        sections.append((va, vsize, raw, rsize))
    info = PeInfo(machine=machine)
    erva, esz = ddir(0)
    if erva and esz:
        e = _rva_to_off(erva, sections)
        nnames = struct.unpack_from("<I", data, e + 24)[0]
        names_rva = struct.unpack_from("<I", data, e + 32)[0]
        noff = _rva_to_off(names_rva, sections) if nnames else 0
        for i in range(nnames):
            n_rva = struct.unpack_from("<I", data, noff + 4 * i)[0]
            info.exports.add(_cstr(data, _rva_to_off(n_rva, sections)))
    irva, isz = ddir(1)
    if irva and isz:
        o = _rva_to_off(irva, sections)
        while True:
            name_rva = struct.unpack_from("<I", data, o + 12)[0]
            if name_rva == 0:
                break
            info.imports.append(_cstr(data, _rva_to_off(name_rva, sections)))
            o += 20
    info.signed = ddir(4)[1] > 0
    return info


# ============================================================================
# Library candidates
# ============================================================================
@dataclass
class LibCandidate:
    path: str
    origin: str
    usable: bool = False
    v4: bool = False
    reason: str = ""


def _system_dirs():
    w = os.environ.get("SystemRoot", r"C:\Windows")
    return [os.path.join(w, "System32"), w]


def _import_resolvable(name: str, folder: str) -> bool:
    low = name.lower()
    if low.startswith(("api-ms-win-", "ext-ms-win-")):
        return True
    for d in [folder] + _system_dirs():
        if os.path.isfile(os.path.join(d, name)):
            return True
    return False


def inspect_library(path: str, origin: str) -> LibCandidate:
    c = LibCandidate(path, origin)
    try:
        with open(path, "rb") as f:
            data = f.read()
        if not path.lower().endswith(".dll"):   # Linux/macOS shared object: no PE checks
            c.usable, c.v4 = True, any(m in data for m in V4_MARKERS)
            c.reason = "RTL-SDR Blog (V4-capable)" if c.v4 else "generic librtlsdr (no V4 support)"
            return c
        pe = parse_pe(data)
    except (OSError, ValueError, struct.error) as e:
        c.reason = f"unreadable ({e})"
        return c
    want_machine = 0x8664 if struct.calcsize("P") == 8 else 0x14C
    if pe.machine != want_machine:
        c.reason = "wrong architecture (32-bit DLL)" if pe.machine == 0x14C else f"wrong architecture ({pe.machine:#x})"
        return c
    missing = [e for e in REQUIRED_EXPORTS if e not in pe.exports]
    if missing:
        c.reason = "missing functions: " + ", ".join(missing[:3])
        return c
    if os.name == "nt":
        unres = [i for i in pe.imports if not _import_resolvable(i, os.path.dirname(path))]
        if unres:
            c.reason = "missing DLL(s) next to it: " + ", ".join(unres)
            return c
    c.v4 = any(m in data for m in V4_MARKERS)
    c.usable = True
    c.reason = "RTL-SDR Blog (V4-capable)" if c.v4 else "generic librtlsdr (no V4 support)"
    return c


def _install_dir() -> str:
    """Folder of MeteorRX.exe (drivers live there even when settings fall back to %LOCALAPPDATA%)."""
    from .config import _exe_dir
    return _exe_dir()


def bundled_dir() -> str:
    base = getattr(sys, "_MEIPASS", None)
    for d in (os.path.join(_install_dir(), "drivers", "rtlsdr"), os.path.join(base, "drivers", "rtlsdr") if base else ""):
        if d and os.path.isfile(os.path.join(d, "rtlsdr.dll")):
            return d
    return os.path.join(_install_dir(), "drivers", "rtlsdr")


def installed_dirs() -> list[tuple[str, str]]:
    """(folder, label) of SDR programs that commonly carry an rtlsdr.dll."""
    out = []
    if os.name != "nt":
        return out
    roots = [os.environ.get(k, "") for k in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")]
    roots.append(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"))
    keys = ("satdump", "sdr++", "sdrpp", "sdrsharp", "sdr#", "airspy", "rtl-sdr", "rtlsdr", "sdrangel", "gqrx")
    for r in filter(None, dict.fromkeys(roots)):
        try:
            names = sorted(os.listdir(r))
        except OSError:
            continue
        for n in names:
            if any(k in n.lower() for k in keys):
                for sub in ("", "x64", "bin"):
                    out.append((os.path.join(r, n, sub), n))
    for d in (r"C:\SatDump", r"C:\sdrpp", r"C:\SDRSharp", r"C:\rtl-sdr", r"C:\rtl-sdr\x64"):
        out.append((d, os.path.basename(d)))
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d:
            out.append((d, "PATH"))
    return out


LIB_NAMES = ["rtlsdr.dll", "librtlsdr.dll"] if os.name == "nt" else ["librtlsdr.so.0", "librtlsdr.so"]


def candidates(configured_dir: str = "") -> list[LibCandidate]:
    names = LIB_NAMES
    seen = set()
    out = []

    def add(folder, origin):
        for n in names:
            p = os.path.normcase(os.path.abspath(os.path.join(folder, n)))
            if p in seen or not os.path.isfile(p):
                continue
            seen.add(p)
            out.append(inspect_library(p, origin))

    if configured_dir:
        add(configured_dir, "configured (rtl_dll_dir)")
    for d, label in installed_dirs():
        add(d, f"installed: {label}")
    add(bundled_dir(), "bundled (RTL-SDR Blog v1.3.6)")
    if os.name != "nt":
        for d in ("/usr/local/lib", "/usr/lib", "/usr/lib/x86_64-linux-gnu", "/opt/homebrew/lib"):
            add(d, "system")
    return out


# ============================================================================
# USB devices (Windows SetupAPI)
# ============================================================================
@dataclass
class UsbDongle:
    vid: int
    pid: int
    instance: str
    service: str
    description: str

    @property
    def is_v4(self) -> bool:
        return "blog v4" in self.description.lower()

    @property
    def driver_ok(self) -> bool:
        return self.service.lower() in GOOD_SERVICES

    @property
    def status(self) -> str:
        s = self.service.lower()
        if s in GOOD_SERVICES:
            return f"{GOOD_SERVICES[s]} driver OK"
        if not s:
            return "no driver installed"
        if "rtl28" in s or "bda" in s or "dvb" in s:
            return f"Windows DVB-T driver ({self.service}) - must be replaced by WinUSB"
        return f"driver '{self.service}' - WinUSB expected"


def list_usb_dongles() -> list[UsbDongle]:
    """RTL2832U devices currently plugged in, with the kernel driver (service) bound to each."""
    if os.name != "nt":
        return []
    from ctypes import wintypes as W

    sa = C.WinDLL("setupapi", use_last_error=True)

    class GUID(C.Structure):
        _fields_ = [("d1", W.DWORD), ("d2", W.WORD), ("d3", W.WORD), ("d4", C.c_ubyte * 8)]

    class SP_DEVINFO_DATA(C.Structure):
        _fields_ = [("cbSize", W.DWORD), ("ClassGuid", GUID), ("DevInst", W.DWORD), ("Reserved", C.c_void_p)]

    class DEVPROPKEY(C.Structure):
        _fields_ = [("fmtid", GUID), ("pid", W.ULONG)]

    sa.SetupDiGetClassDevsW.restype = C.c_void_p
    sa.SetupDiGetClassDevsW.argtypes = [C.c_void_p, W.LPCWSTR, W.HWND, W.DWORD]
    sa.SetupDiEnumDeviceInfo.restype = W.BOOL
    sa.SetupDiEnumDeviceInfo.argtypes = [C.c_void_p, W.DWORD, C.POINTER(SP_DEVINFO_DATA)]
    sa.SetupDiGetDeviceInstanceIdW.restype = W.BOOL
    sa.SetupDiGetDeviceInstanceIdW.argtypes = [C.c_void_p, C.POINTER(SP_DEVINFO_DATA), W.LPWSTR, W.DWORD,
                                               C.POINTER(W.DWORD)]
    sa.SetupDiGetDeviceRegistryPropertyW.restype = W.BOOL
    sa.SetupDiGetDeviceRegistryPropertyW.argtypes = [C.c_void_p, C.POINTER(SP_DEVINFO_DATA), W.DWORD,
                                                     C.POINTER(W.DWORD), C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]
    sa.SetupDiGetDevicePropertyW.restype = W.BOOL
    sa.SetupDiGetDevicePropertyW.argtypes = [C.c_void_p, C.POINTER(SP_DEVINFO_DATA), C.POINTER(DEVPROPKEY),
                                             C.POINTER(W.ULONG), C.c_void_p, W.DWORD, C.POINTER(W.DWORD), W.DWORD]
    sa.SetupDiDestroyDeviceInfoList.argtypes = [C.c_void_p]

    DIGCF_PRESENT, DIGCF_ALLCLASSES = 0x2, 0x4
    SPDRP_DEVICEDESC, SPDRP_SERVICE = 0, 4
    bus_desc = DEVPROPKEY(GUID(0x540B947E, 0x8B40, 0x45BC, (C.c_ubyte * 8)(0xA8, 0xA2, 0x6A, 0x0B, 0x89, 0x4C,
                                                                           0xBD, 0xA2)), 4)
    h = sa.SetupDiGetClassDevsW(None, "USB", None, DIGCF_PRESENT | DIGCF_ALLCLASSES)
    if h is None or h == C.c_void_p(-1).value:
        return []
    out = []
    try:
        i = 0
        buf = C.create_unicode_buffer(512)
        while True:
            dd = SP_DEVINFO_DATA()
            dd.cbSize = C.sizeof(SP_DEVINFO_DATA)
            if not sa.SetupDiEnumDeviceInfo(h, i, C.byref(dd)):
                break
            i += 1
            if not sa.SetupDiGetDeviceInstanceIdW(h, C.byref(dd), buf, 512, None):
                continue
            inst = buf.value
            up = inst.upper()
            try:
                vid = int(up.split("VID_")[1][:4], 16)
                pid = int(up.split("PID_")[1][:4], 16)
            except (IndexError, ValueError):
                continue
            if (vid, pid) not in RTL_IDS:
                continue
            svc = ""
            if sa.SetupDiGetDeviceRegistryPropertyW(h, C.byref(dd), SPDRP_SERVICE, None, buf, C.sizeof(buf), None):
                svc = buf.value
            desc = ""
            ptype = W.ULONG(0)
            if sa.SetupDiGetDevicePropertyW(h, C.byref(dd), C.byref(bus_desc), C.byref(ptype), buf, C.sizeof(buf),
                                            None, 0):
                desc = buf.value
            elif sa.SetupDiGetDeviceRegistryPropertyW(h, C.byref(dd), SPDRP_DEVICEDESC, None, buf, C.sizeof(buf),
                                                      None):
                desc = buf.value
            out.append(UsbDongle(vid, pid, inst, svc, desc))
    finally:
        sa.SetupDiDestroyDeviceInfoList(h)
    return out


def summarize_dongles(devs: list[UsbDongle]) -> list[UsbDongle]:
    """One entry per receiver type (VID:PID). A composite dongle appears as a parent node (usbccgp)
    plus interface nodes (&MI_00...); the relevant one is the interface carrying WinUSB/DVB-T."""
    groups: dict = {}
    for d in devs:
        groups.setdefault((d.vid, d.pid), []).append(d)
    out = []
    for nodes in groups.values():
        real = [n for n in nodes if n.service.lower() != "usbccgp"] or nodes
        good = [n for n in real if n.driver_ok]
        mi0 = [n for n in real if "&MI_00" in n.instance.upper()]
        pick = (good or mi0 or real)[0]
        if not pick.description:
            pick.description = next((n.description for n in nodes if n.description), "")
        out.append(pick)
    return out


# ============================================================================
# Selection
# ============================================================================
@dataclass
class DriverReport:
    chosen: LibCandidate | None
    candidates: list
    dongles: list
    lines: list

    @property
    def usb_problem(self) -> bool:
        return any(not d.driver_ok for d in self.dongles)


def select_library(configured_dir: str = "", dongles: list | None = None) -> DriverReport:
    if dongles is None:
        try:
            dongles = summarize_dongles(list_usb_dongles())
        except Exception:  # noqa: BLE001
            dongles = []
    need_v4 = any(d.is_v4 for d in dongles)
    known_non_v4 = bool(dongles) and not need_v4
    cands = candidates(configured_dir)
    lines = []
    for d in dongles:
        lines.append(f"USB  {d.vid:04X}:{d.pid:04X} '{d.description}' - {d.status}")
    if os.name == "nt" and not dongles:
        lines.append("USB  no RTL2832U receiver detected (plugged in?)")
    chosen = None
    for c in cands:
        verdict = ""
        if not c.usable:
            verdict = "rejected: " + c.reason
        elif c.origin.startswith("bundled"):
            verdict = "OK"
        elif c.v4:
            verdict = "OK (RTL-SDR Blog V4-capable)"
        elif need_v4:
            verdict = "rejected: no RTL-SDR Blog V4 support and a V4 is connected"
        elif known_non_v4 or c.origin.startswith("configured"):
            verdict = "OK (generic, dongle is not a V4)"
        else:
            verdict = "skipped: generic build, V4 support not guaranteed - bundled driver preferred"
        ok = verdict.startswith("OK")
        if ok and chosen is None:
            chosen = c
            verdict += "  <- used"
        lines.append(f"DLL  {c.path} [{c.origin}] {c.reason}: {verdict}")
    if not cands:
        lines.append("DLL  no rtlsdr.dll found (bundled copy missing?)")
    return DriverReport(chosen, cands, dongles, lines)


def load_library(rep: DriverReport):
    if rep.chosen is None:
        raise OSError("no suitable rtlsdr library")
    p = rep.chosen.path
    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(os.path.dirname(p))
        except OSError:
            pass
    return C.CDLL(p)


# ============================================================================
# Zadig (WinUSB installer)
# ============================================================================
ZADIG_INI = """[general]
  advanced_mode = false
  exit_on_success = true
  log_level = 2
[device]
  list_all = true
  include_hubs = false
  trim_whitespaces = true
[driver]
  default_driver = 0
  extract_only = false
"""


def zadig_path() -> str:
    return os.path.join(_install_dir(), "drivers", "zadig", "zadig.exe")


def launch_zadig() -> str:
    """Start the bundled Zadig (UAC prompt). Returns an error message or ''."""
    exe = zadig_path()
    if not os.path.isfile(exe):
        return f"Zadig not found ({exe})"
    folder = os.path.dirname(exe)
    ini = os.path.join(folder, "zadig.ini")
    try:
        if not os.path.isfile(ini):
            with open(ini, "w", encoding="ascii") as f:
                f.write(ZADIG_INI)
    except OSError:
        pass
    if os.name != "nt":
        return "Zadig is a Windows program"
    r = C.windll.shell32.ShellExecuteW(None, "open", exe, None, folder, 1)
    return "" if r > 32 else f"could not start Zadig (code {r})"

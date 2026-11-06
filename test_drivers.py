"""RTL-SDR driver selection: PE inspection, version policy (V4), USB driver status."""
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from meteorrx import drivers as D  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
BUNDLED = os.path.join(ROOT, "drivers", "rtlsdr", "rtlsdr.dll")
V4 = [D.UsbDongle(0x0BDA, 0x2838, r"USB\VID_0BDA&PID_2838&MI_00\6&1", "WinUSB", "Blog V4")]
V3 = [D.UsbDongle(0x0BDA, 0x2838, r"USB\VID_0BDA&PID_2838&MI_00\6&1", "WinUSB", "RTL2838UHIDIR")]


def _stub(tmp_path, name, exports, cc="x86_64-w64-mingw32-gcc"):
    if not shutil.which(cc):
        pytest.skip("MinGW not available")
    d = tmp_path / name
    d.mkdir()
    (d / "s.c").write_text("".join(f"__declspec(dllexport) int {f}(void){{return 0;}}\n" for f in exports))
    subprocess.run([cc, "-shared", "-o", str(d / "rtlsdr.dll"), str(d / "s.c")], check=True)
    return str(d)


@pytest.fixture(autouse=True)
def dll_names(monkeypatch):
    monkeypatch.setattr(D, "LIB_NAMES", ["rtlsdr.dll"])
    monkeypatch.setattr(D, "installed_dirs", lambda: [])


@pytest.mark.skipif(not os.path.isfile(BUNDLED), reason="run tools/fetch_drivers.py first")
def test_bundled_library_is_v4_capable_x64_and_self_contained():
    c = D.inspect_library(BUNDLED, "bundled")
    assert c.usable and c.v4, c.reason
    pe = D.parse_pe(open(BUNDLED, "rb").read())
    assert pe.machine == 0x8664
    assert [i.upper() for i in pe.imports] == ["KERNEL32.DLL"]   # libusb linked statically
    assert set(D.REQUIRED_EXPORTS) <= pe.exports


@pytest.mark.skipif(not os.path.isfile(BUNDLED), reason="run tools/fetch_drivers.py first")
def test_version_policy(tmp_path):
    generic = _stub(tmp_path, "generic", D.REQUIRED_EXPORTS)
    assert D.select_library(generic, V3).chosen.origin.startswith("configured")
    assert D.select_library(generic, V4).chosen.origin.startswith("bundled")   # generic lib cannot drive a V4
    incomplete = _stub(tmp_path, "incomplete", D.REQUIRED_EXPORTS[:-1])
    rep = D.select_library(incomplete, V3)
    assert rep.chosen.origin.startswith("bundled") and any("missing functions" in x for x in rep.lines)


def test_32bit_library_rejected(tmp_path):
    d = _stub(tmp_path, "x86", D.REQUIRED_EXPORTS, cc="i686-w64-mingw32-gcc")
    c = D.inspect_library(os.path.join(d, "rtlsdr.dll"), "test")
    assert not c.usable and "32-bit" in c.reason


def test_usb_status_summary():
    nodes = [D.UsbDongle(0x0BDA, 0x2838, r"USB\VID_0BDA&PID_2838\00000001", "usbccgp", "Blog V4"),
             D.UsbDongle(0x0BDA, 0x2838, r"USB\VID_0BDA&PID_2838&MI_00\7&2", "RTL2832UBDA", ""),
             D.UsbDongle(0x0BDA, 0x2838, r"USB\VID_0BDA&PID_2838&MI_01\7&2", "", "")]
    s = D.summarize_dongles(nodes)
    assert len(s) == 1 and not s[0].driver_ok and "DVB-T" in s[0].status and s[0].is_v4
    nodes[1].service = "WinUSB"
    s = D.summarize_dongles(nodes)
    assert s[0].driver_ok


def test_list_usb_dongles_does_not_crash():
    assert isinstance(D.list_usb_dongles(), list)

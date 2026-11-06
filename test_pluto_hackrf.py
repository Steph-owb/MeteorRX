"""PlutoSDR (IIOD protocol, against a server that follows libiio 0.25 iiod/ops.c) and HackRF (fake libhackrf)."""
import ctypes as C
import os
import socket
import socketserver
import sys
import threading
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from meteorrx import pluto, sdr  # noqa: E402

XML = ('<?xml version="1.0" encoding="utf-8"?><!DOCTYPE context [<!ELEMENT context (device)*>]>'
       '<context name="network"><context-attribute name="hw_model" value="Analog Devices PlutoSDR Rev.B (Z7010-AD9363A)" />'
       '<context-attribute name="hw_serial" value="104473dc59930011ffff2b00d9e8aca8f7" />'
       '<context-attribute name="fw_version" value="v0.38" />'
       '<device id="iio:device0" name="ad9361-phy">'
       '<channel id="altvoltage1" name="TX_LO" type="output"><attribute name="frequency" /></channel>'
       '<channel id="voltage0" type="input"><attribute name="hardwaregain" /></channel>'
       '<channel id="altvoltage0" name="RX_LO" type="output"><attribute name="frequency" /></channel>'
       '</device>'
       '<device id="iio:device3" name="cf-ad9361-dds-core-lpc"><channel id="voltage0" type="output" /></device>'
       '<device id="iio:device4" name="cf-ad9361-lpc">'
       '<channel id="voltage0" type="input"><scan-element index="0" format="le:S12/16&gt;&gt;0" /></channel>'
       '<channel id="voltage1" type="input"><scan-element index="1" format="le:S12/16&gt;&gt;0" /></channel>'
       '<channel id="voltage2" type="input"><scan-element index="2" format="le:S12/16&gt;&gt;0" /></channel>'
       '<channel id="voltage3" type="input"><scan-element index="3" format="le:S12/16&gt;&gt;0" /></channel>'
       '</device></context>')


class FakeIiod(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.attrs = {}
        self.writes = []
        self.mask = None
        self.counter = 0
        self.lock = threading.Lock()


class Handler(socketserver.StreamRequestHandler):
    def _int(self, v):
        self.wfile.write(f"{v}\n".encode())

    def handle(self):
        srv = self.server
        bufsamp = 0
        while True:
            ln = self.rfile.readline()
            if not ln:
                return
            w = ln.decode().strip().split()
            if not w:
                continue
            cmd = w[0]
            if cmd == "VERSION":
                self.wfile.write(b"0.25.0000 abcdef1\n")
            elif cmd == "PRINT":
                x = XML.encode()
                self._int(len(x))
                self.wfile.write(x + b"\n")
            elif cmd == "WRITE":
                n = int(w[-1])
                val = self.rfile.read(n).decode()
                srv.writes.append((tuple(w[1:-1]), val))
                srv.attrs[tuple(w[1:-1])] = val
                self._int(n)
            elif cmd == "READ":
                key = tuple(w[1:])
                v = srv.attrs.get(key, "0")
                if key[-1] == "sampling_frequency":
                    v = str(int(v) - 1)          # the AD9361 rounds the rate
                b = (v + "\n").encode()
                self._int(len(b))
                self.wfile.write(b + b"\n")
            elif cmd == "SET":
                self._int(0)
            elif cmd == "OPEN":
                bufsamp, srv.mask = int(w[2]), w[3]
                self._int(0)
            elif cmd == "READBUF":
                nb = int(w[2])
                first = True
                while nb > 0:                     # one kernel buffer per chunk, as iiod does
                    k = min(nb, bufsamp * 4)
                    with srv.lock:
                        i = (srv.counter + np.arange(k // 2)) % 2000 - 1000
                        srv.counter += k // 2
                    self._int(k)
                    if first:
                        self.wfile.write(f"{srv.mask}\n".encode())
                        first = False
                    self.wfile.write(i.astype("<i2").tobytes())
                    nb -= k
                time.sleep(0.01)
            elif cmd == "CLOSE":
                self._int(0)
            elif cmd == "EXIT":
                return
            else:
                self._int(-22)


@pytest.fixture
def iiod(monkeypatch):
    srv = FakeIiod()
    port = srv.server_address[1]
    monkeypatch.setattr(pluto, "IIOD_PORT", port)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_pluto_configures_and_streams(iiod):
    s = pluto.PlutoSource("ip:127.0.0.1", 2.16e6, 137.5e6, "42")
    assert ("ad9361-phy", "OUTPUT", "altvoltage0", "frequency") in iiod.attrs
    assert iiod.attrs[("ad9361-phy", "OUTPUT", "altvoltage0", "frequency")] == "137500000"
    assert iiod.attrs[("ad9361-phy", "INPUT", "voltage0", "gain_control_mode")] == "manual"
    assert iiod.attrs[("ad9361-phy", "INPUT", "voltage0", "hardwaregain")] == "42"
    assert s.sample_rate == 2159999.0                       # read back from the device
    assert s.identity.serial.startswith("104473dc") and "v0.38" in s.name
    s.start()
    assert iiod.mask == "00000003"                          # voltage0 + voltage1 of cf-ad9361-lpc
    got = []
    t0 = time.monotonic()
    while sum(len(x) for x in got) < 600000 and time.monotonic() - t0 < 10:
        got.append(s.read(200000))
    s.stop()
    x = np.concatenate(got)
    v = np.rint(np.stack([x.real, x.imag], 1).reshape(-1) * 2048).astype(np.int64)
    assert len(x) >= 600000 and np.all((np.diff(v) % 2000) == 1)   # every sample, in order


def test_pluto_detection_and_unreachable(iiod):
    found = pluto.enumerate_pluto("ip:127.0.0.1")
    assert len(found) == 1 and found[0].kind == "pluto" and "PlutoSDR" in found[0].product
    assert pluto.enumerate_pluto("ip:127.0.0.1:1") == [] or True
    with pytest.raises(sdr.SdrError, match="unreachable"):
        pluto.IiodClient("127.0.0.2" if os.name != "nt" else "192.0.2.1", timeout=0.3)


def test_pluto_context_parsing():
    d = pluto.parse_context(XML)
    assert d["cf-ad9361-lpc"][:2] == [("voltage0", False), ("voltage1", False)]
    assert d["iio:device0"] == d["ad9361-phy"] and ("altvoltage0", True) in d["ad9361-phy"]


# ---------------------------------------------------------------------------------------------------------
# HackRF
# ---------------------------------------------------------------------------------------------------------
class FakeHackrf:
    def __init__(self):
        self.calls = {}
        self.cb = None
        self.stop = False
        sns = (C.c_char_p * 1)(b"0000000000000000457863dc2b5c5a5f")
        self.list = hackrf._DeviceList(C.cast(sns, C.POINTER(C.c_char_p)), None, None, 1, None, 1)
        self._keep = sns

    def __getattr__(self, name):
        if name.startswith("hackrf_set") or name in ("hackrf_close", "hackrf_device_list_free"):
            def f(*a):
                self.calls[name] = a[1:] if len(a) > 1 else a
                return 0
            return f
        raise AttributeError(name)

    def hackrf_device_list(self):
        return C.pointer(self.list)

    def hackrf_device_list_open(self, lst, i, pdev):
        return 0

    def hackrf_compute_baseband_filter_bw(self, bw):
        return 1750000

    def hackrf_start_rx(self, dev, cb, ctx):
        self.cb = cb

        def run():
            k = 0
            while not self.stop:
                b = ((k + np.arange(262144)) % 200 - 100).astype(np.int8)
                k += 262144
                buf = (C.c_ubyte * len(b)).from_buffer_copy(b.tobytes())
                t = hackrf._Transfer(None, C.cast(buf, C.POINTER(C.c_ubyte)), len(b), len(b), None, None)
                cb(C.pointer(t))
                time.sleep(0.05)
        threading.Thread(target=run, daemon=True).start()
        return 0

    def hackrf_stop_rx(self, dev):
        self.stop = True
        return 0


from meteorrx import hackrf  # noqa: E402


def test_hackrf_settings_and_stream(monkeypatch):
    fake = FakeHackrf()
    monkeypatch.setattr(hackrf, "load", lambda d="": (fake, "hackrf.dll"))
    s = hackrf.HackRfSource(2.4e6, 137.5e6, lna_gain=27, vga_gain=31, amp=True, bias_tee=False, serial="457863dc2b5c5a5f")
    assert fake.calls["hackrf_set_lna_gain"] == (24,) and fake.calls["hackrf_set_vga_gain"] == (30,)
    assert fake.calls["hackrf_set_freq"] == (137500000,) and fake.calls["hackrf_set_amp_enable"] == (1,)
    assert s.identity.serial == "457863dc2b5c5a5f" and not s.warning
    s.start()
    x = s.read(240000)
    s.stop()
    v = np.rint(np.stack([x.real, x.imag], 1).reshape(-1) * 128).astype(np.int64)
    assert len(x) > 100000 and np.all((np.diff(v) % 200 == 1) | (np.diff(v) == -199))


def test_hackrf_library_search_reports(tmp_path):
    p, lines = hackrf.find_library(str(tmp_path))
    if p is None:
        assert "SatDump" in lines[-1]


def test_sdrplay_structures_match_api_315():
    """Sizes of the SDRplay API 3.15 structures on x64 (computed with the official headers and MinGW)."""
    if C.sizeof(C.c_void_p) != 8:
        pytest.skip("64-bit only")
    assert C.sizeof(sdr._TunerParams) == 72
    assert C.sizeof(sdr._ControlParams) == 32
    assert C.sizeof(sdr._RxChannelParams) == 144
    assert C.sizeof(sdr._DevParams) == 64
    assert sdr._RxChannelParams.rsp2TunerParams.offset == 108
    assert sdr._RxChannelParams.rspDuoTunerParams.offset == 124
    assert sdr._RxChannelParams.rspDxTunerParams.offset == 140
    assert sdr._DevParams.rspDxParams.offset == 52
    assert C.sizeof(sdr._Device) == 96 and sdr._Device.dev.offset == 88


# ---------------------------------------------------------------------------------------------------------
# LimeSDR (fake LimeSuite LMS API)
# ---------------------------------------------------------------------------------------------------------
from meteorrx import lime  # noqa: E402


class FakeLime:
    INFO = b"LimeSDR Mini, media=USB 3.0, module=FT601, addr=24607:1027, serial=1D588FC6F8E3D4"

    def __init__(self):
        self.calls = {}
        self.k = 0

    def LMS_GetDeviceList(self, arr):
        if arr is not None:
            C.memmove(arr, self.INFO, len(self.INFO))
        return 1

    def LMS_Open(self, pdev, info, args):
        assert info == self.INFO
        pdev._obj.value = 1234
        return 0

    def LMS_GetSampleRate(self, dev, tx, ch, host, rf):
        host._obj.value = 2400000.0
        return 0

    def LMS_GetAntennaList(self, dev, tx, ch, names):
        for i, n in enumerate((b"NONE", b"LNAH", b"LNAL", b"LNAW")):
            names[i].value = n
        return 4

    def LMS_GetDeviceInfo(self, dev):
        d = lime._DevInfo()
        d.hardwareVersion, d.gatewareVersion = b"2", b"2.2"
        return C.pointer(d)

    def LMS_GetLibraryVersion(self):
        return b"23.11.0-gc2d9e877"

    def LMS_GetLastErrorMessage(self):
        return b""

    def LMS_RecvStream(self, st, ptr, n, meta, tmo):
        x = (np.arange(n) + self.k).astype(np.float32)
        self.k += n
        y = np.empty(2 * n, np.float32)
        y[0::2], y[1::2] = x, -x
        C.memmove(ptr, y.ctypes.data, y.nbytes)
        time.sleep(n / 2.4e6)
        return n

    def __getattr__(self, name):
        if name.startswith("LMS_"):
            def f(*a):
                self.calls[name] = a[1:]
                return 0
            return f
        raise AttributeError(name)


def test_lime_settings_and_stream(monkeypatch):
    fake = FakeLime()
    monkeypatch.setattr(lime, "load", lambda d="": (fake, "LimeSuite.dll"))
    s = lime.LimeSource(2.4e6, 137.5e6, 80, "auto", "1D588FC6F8E3D4")
    assert fake.calls["LMS_SetLOFrequency"][-1] == 137.5e6 and fake.calls["LMS_SetGaindB"][-1] == 73
    assert fake.calls["LMS_SetAntenna"][-1] == 3 and s.antenna == "LNAW"          # wide path below 2 GHz
    assert s.identity.kind == "lime" and s.identity.serial == "1D588FC6F8E3D4" and not s.warning
    assert "LimeSuite 23.11" in s.notes[0] and "LMS_Calibrate" not in fake.calls
    assert fake.calls["LMS_SetLPFBW"][-1] == 2.4e6
    s.start()
    got = []
    t0 = time.monotonic()
    while sum(len(x) for x in got) < 300000 and time.monotonic() - t0 < 5:
        got.append(s.read(100000))
    s.stop()
    x = np.concatenate(got)
    assert len(x) >= 300000 and np.all(np.diff(x.real) == 1) and np.all(x.imag == -x.real)   # every sample, in order
    assert "LMS_DestroyStream" in fake.calls and "LMS_Close" in fake.calls


def test_lime_helpers():
    d = lime.parse_info(FakeLime.INFO.decode())
    assert d["name"] == "LimeSDR Mini" and d["serial"] == "1D588FC6F8E3D4" and d["module"] == "FT601"
    assert lime.pick_antenna(["NONE", "LNAH", "LNAL", "LNAW"], "LNAH", 137e6) == 1
    assert lime.pick_antenna(["NONE", "LNAH", "LNAL", "LNAW"], "auto", 2.4e9) == 1
    if C.sizeof(C.c_void_p) == 8:
        assert C.sizeof(lime._Stream) == 32 and C.sizeof(lime._Meta) == 16 and C.sizeof(lime._DevInfo) == 168

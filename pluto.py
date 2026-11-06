"""ADALM-Pluto (PlutoSDR) over its native network interface: the IIOD protocol of libiio, spoken directly on
TCP port 30431. No libiio / driver to install: a Pluto on USB appears as a network adapter (192.168.2.1),
a Pluto on Ethernet/Wi-Fi at its own address ([sdr] pluto_uri = ip:<address>).

Receive path: ad9361-phy RX_LO = 137.5 MHz, manual gain, sample rate 2.16 MS/s (the AD9363 minimum without
FIR is 2.083 MS/s; 2.16 MS/s is also the rate of the MARL Meteor test-pattern generator, so a Pluto can
receive while it transmits the pattern: RX and TX share the sample clock). Samples: cf-ad9361-lpc
voltage0/voltage1, 12-bit in int16 little-endian."""
from __future__ import annotations

import re
import socket
import time

import numpy as np

from .sdr import SdrError, StreamSource

IIOD_PORT = 30431
PHY = "ad9361-phy"
RXDEV = "cf-ad9361-lpc"


def parse_uri(uri: str) -> str:
    u = (uri or "").strip()
    if u.lower().startswith("ip:"):
        u = u[3:]
    if u.lower().startswith("usb:"):
        raise SdrError("PlutoSDR: only the network interface is supported (pluto_uri = ip:192.168.2.1 for a Pluto "
                       "on USB, which is also a network adapter)")
    return u or "192.168.2.1"


class _Conn:
    def __init__(self, host: str, timeout: float = 3.0):
        try:
            self.s = socket.create_connection((host, IIOD_PORT), timeout=timeout)
        except OSError as e:
            raise SdrError(f"PlutoSDR {host}:{IIOD_PORT} unreachable ({e}). Pluto on USB: check that the "
                           "'PlutoSDR' network adapter is up (driver from Analog Devices) and that "
                           "192.168.2.1 answers") from e
        self.s.settimeout(5.0)
        self.s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            self.s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        except OSError:
            pass
        self.rbuf = bytearray()

    def send(self, data: bytes):
        self.s.sendall(data)

    def line(self) -> str:
        while True:
            i = self.rbuf.find(b"\n")
            if i >= 0:
                ln = bytes(self.rbuf[:i])
                del self.rbuf[:i + 1]
                return ln.decode(errors="replace").rstrip("\r")
            d = self.s.recv(65536)
            if not d:
                raise SdrError("PlutoSDR: connection closed")
            self.rbuf += d

    def exact(self, n: int) -> bytes:
        while len(self.rbuf) < n:
            d = self.s.recv(max(65536, n - len(self.rbuf)))
            if not d:
                raise SdrError("PlutoSDR: connection closed")
            self.rbuf += d
        out = bytes(self.rbuf[:n])
        del self.rbuf[:n]
        return out

    def integer(self) -> int:
        while True:
            ln = self.line().strip()
            if ln:
                try:
                    return int(ln)
                except ValueError as e:
                    raise SdrError(f"PlutoSDR: unexpected answer '{ln[:40]}'") from e

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


class IiodClient:
    """Minimal IIOD (text protocol) client, enough to configure the AD9361 and stream RX samples."""

    def __init__(self, host: str, timeout: float = 3.0):
        self.host = host
        self.ctl = _Conn(host, timeout)
        self.ctl.send(b"VERSION\r\n")
        self.version = self.ctl.line().strip()
        n = self._cmd("PRINT\r\n")
        if n <= 0:
            raise SdrError(f"PlutoSDR: cannot read the IIO context ({n})")
        self.xml = self.ctl.exact(n).decode(errors="replace")
        self.ctl.line()
        self.devices = parse_context(self.xml)
        self.attrs = dict(re.findall(r'<context-attribute name="([^"]+)" value="([^"]*)"', self.xml))
        self.data: _Conn | None = None

    def _cmd(self, cmd: str, conn: _Conn | None = None) -> int:
        c = conn or self.ctl
        c.send(cmd.encode())
        return c.integer()

    def write_chn(self, dev: str, output: bool, chn: str, attr: str, value) -> int:
        v = str(value).encode()
        self.ctl.send(f"WRITE {dev} {'OUTPUT' if output else 'INPUT'} {chn} {attr} {len(v)}\r\n".encode() + v)
        return self.ctl.integer()

    def read_chn(self, dev: str, output: bool, chn: str, attr: str) -> str:
        n = self._cmd(f"READ {dev} {'OUTPUT' if output else 'INPUT'} {chn} {attr}\r\n")
        if n < 0:
            raise SdrError(f"PlutoSDR: cannot read {dev}/{chn}/{attr} ({n})")
        v = self.ctl.exact(n).decode(errors="replace") if n else ""
        self.ctl.line()
        return v.rstrip("\x00\n")

    def open_rx(self, dev: str, channels: list[str], samples: int, kbufs: int = 4) -> int:
        """Open the RX buffer on a second connection. Returns the bytes per sample (all enabled channels)."""
        d = self.devices.get(dev)
        if d is None:
            raise SdrError(f"PlutoSDR: device {dev} not found (is it a Pluto?)")
        words = (len(d) + 31) // 32
        mask = [0] * words
        for c in channels:
            idx = [i for i, (cid, out) in enumerate(d) if cid == c and not out]
            if not idx:
                raise SdrError(f"PlutoSDR: channel {c} not found on {dev}")
            mask[idx[0] // 32] |= 1 << (idx[0] % 32)
        self.mask_hex = "".join(f"{m:08x}" for m in reversed(mask))
        self.mask_words = words
        self.data = _Conn(self.host)
        self._cmd(f"SET {dev} BUFFERS_COUNT {kbufs}\r\n", self.data)   # older iiod: error, harmless
        r = self._cmd(f"OPEN {dev} {samples} {self.mask_hex}\r\n", self.data)
        if r < 0:
            raise SdrError(f"PlutoSDR: OPEN {dev} failed ({r}) - another program may be receiving")
        self.rx_dev = dev
        return 2 * len(channels)

    def readbuf(self, nbytes: int) -> bytes:
        """READBUF: chunks of <length> [mask line once] <data> until nbytes."""
        c = self.data
        c.send(f"READBUF {self.rx_dev} {nbytes}\r\n".encode())
        out = []
        left = nbytes
        first = True
        while left > 0:
            n = c.integer()
            if n < 0:
                raise SdrError(f"PlutoSDR: READBUF error {n}")
            if n == 0:
                break
            if first:
                c.exact(self.mask_words * 8 + 1)
                first = False
            out.append(c.exact(n))
            left -= n
        return b"".join(out)

    def close(self):
        if self.data is not None:
            try:
                self._cmd(f"CLOSE {self.rx_dev}\r\n", self.data)
            except Exception:  # noqa: BLE001
                pass
            self.data.close()
            self.data = None
        try:
            self.ctl.send(b"EXIT\r\n")
        except OSError:
            pass
        self.ctl.close()


def parse_context(xml: str) -> dict:
    """{device name and id: [(channel id, is_output), ...]} in XML order (= libiio channel numbering)."""
    devs = {}
    for m in re.finditer(r"<device (.*?)>(.*?)</device>", xml, re.S):
        tag, body = m.group(1), m.group(2)
        did = re.search(r'id="([^"]*)"', tag)
        name = re.search(r'name="([^"]*)"', tag)
        chans = [(re.search(r'id="([^"]*)"', c).group(1), 'type="output"' in c)
                 for c in re.findall(r"<channel ([^>]*)>", body)]
        for k in (did.group(1) if did else None, name.group(1) if name else None):
            if k:
                devs[k] = chans
    return devs


def enumerate_pluto(uri: str) -> list:
    """The Pluto at the configured address, if it answers within 0.4 s."""
    from .profiles import DeviceId
    if not uri:
        return []
    host = parse_uri(uri)
    try:
        with socket.create_connection((host, IIOD_PORT), timeout=0.4):
            pass
        c = IiodClient(host, timeout=1.0)
    except (OSError, SdrError):
        return []
    try:
        sn = c.attrs.get("hw_serial", "") or host
        model = c.attrs.get("hw_model", "ADALM-PLUTO").split(" Rev")[0]
        return [DeviceId("pluto", "Analog Devices", model or "ADALM-PLUTO", sn)]
    finally:
        c.close()


class PlutoSource(StreamSource):
    BUF_SAMPLES = 1 << 17           # 131072 samples (60 ms at 2.16 MS/s) per network buffer

    def __init__(self, uri: str, sample_rate: float, center_hz: float, gain: str = "50"):
        from .profiles import DeviceId
        self.warning = ""
        self.notes: list[str] = []
        self.center_hz = float(center_hz)
        self.host = parse_uri(uri)
        self.cli = IiodClient(self.host)
        c = self.cli
        if PHY not in c.devices or RXDEV not in c.devices:
            c.close()
            raise SdrError(f"PlutoSDR {self.host}: no AD9361 in the IIO context (devices: {', '.join(c.devices)})")
        rate = int(round(max(2.084e6, float(sample_rate))))
        steps = [(PHY, True, "altvoltage0", "frequency", int(round(self.center_hz))),
                 (PHY, False, "voltage0", "sampling_frequency", rate),
                 (PHY, False, "voltage0", "rf_bandwidth", int(min(rate, 1500000)))]
        g = str(gain).strip().lower()
        if g in ("auto", "agc", "slow_attack"):
            steps.append((PHY, False, "voltage0", "gain_control_mode", "slow_attack"))
            self.gain_desc = "AGC (slow attack)"
        else:
            gv = min(71.0, max(0.0, float(g)))
            steps += [(PHY, False, "voltage0", "gain_control_mode", "manual"),
                      (PHY, False, "voltage0", "hardwaregain", f"{gv:.0f}")]
            self.gain_desc = f"{gv:.0f} dB"
        for dev, out, ch, attr, val in steps:
            r = c.write_chn(dev, out, ch, attr, val)
            if r < 0:
                c.close()
                raise SdrError(f"PlutoSDR: setting {ch}/{attr} = {val} failed ({r})")
        try:
            self.sample_rate = float(c.read_chn(PHY, False, "voltage0", "sampling_frequency").split()[0])
        except (SdrError, ValueError, IndexError):
            self.sample_rate = float(rate)
        if abs(self.sample_rate - rate) > 1:
            self.notes.append(f"Pluto sample rate {self.sample_rate:.0f} S/s (asked {rate})")
        sn = c.attrs.get("hw_serial", "") or self.host
        self.identity = DeviceId("pluto", "Analog Devices", "ADALM-PLUTO", sn, "", 0)
        fw = c.attrs.get("fw_version", "")
        self.name = f"PlutoSDR {self.host}" + (f" (fw {fw})" if fw else "")
        self._init_stream()

    def start(self):
        self.bps = self.cli.open_rx(RXDEV, ["voltage0", "voltage1"], self.BUF_SAMPLES)
        self._start_thread(self._stream, "pluto-stream")

    def _stream(self):
        nbytes = self.BUF_SAMPLES * self.bps
        scale = np.float32(1.0 / 2048.0)
        while not self._stop_evt.is_set():
            raw = self.cli.readbuf(nbytes)
            if not raw:
                continue
            x = np.frombuffer(raw[: len(raw) // 4 * 4], "<i2").astype(np.float32) * scale
            self.ring.put(x.view(np.complex64))

    def stop(self):
        self._stop_evt.set()
        t = self._thread
        if t is not None:
            t.join(2.0)
        self.cli.close()
        time.sleep(0.05)



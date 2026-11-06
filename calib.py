"""Receiver calibration and measurements with a signal generator.

Power scale: complex samples are normalised so that a full-scale complex sine has power 1 (0 dBFS).
The calibration constant K converts dBFS to dBm at the receiver input for one set of receiver
settings (device, gain, sample rate, tuning):  P[dBm] = P[dBFS] + K.

Measurements
  measure_tone()   CW carrier: integrated power (noise-corrected), frequency, noise density, clipping
  fit_points()     3-point fit: offset K, slope (linearity), residuals, verdict
  noise_figure()   receiver NF from the calibrated noise density with a 290 K (50 ohm) source
  preamp_result()  preamplifier gain and NF (Friis) from a measurement through the preamp
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, dataclass, field

import numpy as np

KT0_DBM_HZ = 10 * math.log10(1.380649e-23 * 290.0 * 1000.0)   # -173.98 dBm/Hz
S9_DBM_VHF = -93.0          # IARU Region 1 recommendation, frequencies above 30 MHz (5 uV / 50 ohm)
DB_PER_S = 6.0


# ============================================================================
# Spectral estimation
# ============================================================================
def _bh4(n: int) -> np.ndarray:
    a = (0.35875, 0.48829, 0.14128, 0.01168)
    k = np.arange(n) * 2 * np.pi / (n - 1)
    return a[0] - a[1] * np.cos(k) + a[2] * np.cos(2 * k) - a[3] * np.cos(3 * k)


def power_spectrum(iq: np.ndarray, nfft: int):
    """Averaged spectrum normalised to power per bin (sum over all bins = mean |x|^2)."""
    w = _bh4(nfft)
    nfr = len(iq) // nfft
    if nfr < 1:
        raise ValueError("not enough samples for one FFT frame")
    acc = np.zeros(nfft)
    for i in range(nfr):
        x = iq[i * nfft:(i + 1) * nfft].astype(np.complex128) * w
        acc += np.abs(np.fft.fft(x)) ** 2
    s = np.fft.fftshift(acc / nfr) / (np.sum(w * w) * nfft)
    return s, nfr


@dataclass
class ToneMeasurement:
    freq_nominal_hz: float          # absolute frequency expected
    freq_measured_hz: float         # absolute frequency found (before any ppm correction)
    power_dbfs: float               # tone power, noise contribution removed
    noise_dbfs_hz: float            # noise density next to the tone
    cn0_dbhz: float
    snr_in_bin_db: float
    peak_abs: float                 # max |I| or |Q| (1.0 = ADC full scale)
    clip_fraction: float
    frames: int
    rbw_hz: float
    valid: bool = True
    warning: str = ""

    @property
    def freq_error_hz(self) -> float:
        return self.freq_measured_hz - self.freq_nominal_hz

    @property
    def ppm(self) -> float:
        return self.freq_error_hz / self.freq_nominal_hz * 1e6


def measure_tone(iq: np.ndarray, fs: float, center_hz: float, tone_hz: float, search_hz: float = 30e3,
                 nfft: int = 65536) -> ToneMeasurement:
    iq = np.asarray(iq)
    s, nfr = power_spectrum(iq, nfft)
    f = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / fs))
    rbw = fs / nfft
    off = tone_hz - center_hz
    win = np.abs(f - off) <= search_hz
    if not win.any():
        raise ValueError("test frequency outside the receiver passband")
    idx = np.nonzero(win)[0]
    kp = int(idx[np.argmax(s[idx])])
    g = 6                                       # Blackman-Harris main lobe is +/-4 bins
    lo, hi = max(kp - g, 0), min(kp + g + 1, nfft)
    # noise: 5..40 kHz around the tone, away from DC and band edges, robust mean
    d = np.abs(f - f[kp])
    ann = (d > 5e3) & (d < 40e3) & (np.abs(f) > 3e3) & (np.abs(f) < 0.42 * fs)
    sa = s[ann]
    med = np.median(sa)
    noise_bin = float(np.mean(sa[sa < 5 * med])) if sa.size else 0.0
    total = float(np.sum(s[lo:hi]))
    tone = total - noise_bin * (hi - lo)
    frac = s[lo:hi]
    fm = float(np.sum(f[lo:hi] * frac) / np.sum(frac))
    comp = np.concatenate([iq.real, iq.imag]) if np.iscomplexobj(iq) else iq
    peak = float(np.max(np.abs(comp))) if comp.size else 0.0
    clip = float(np.mean(np.abs(comp) >= 0.99)) if comp.size else 0.0
    n0 = noise_bin / rbw
    valid = tone > 0 and noise_bin > 0
    warn = []
    if clip > 1e-4:
        warn.append(f"ADC clipping ({clip * 100:.2f}% of samples): reduce the level or the gain")
    snr_bin = 10 * math.log10(max(s[kp], 1e-30) / max(noise_bin, 1e-30))
    if snr_bin < 15:
        warn.append(f"tone only {snr_bin:.1f} dB above the noise in a {rbw:.0f} Hz bin")
    return ToneMeasurement(
        freq_nominal_hz=tone_hz, freq_measured_hz=center_hz + fm,
        power_dbfs=10 * math.log10(max(tone, 1e-30)), noise_dbfs_hz=10 * math.log10(max(n0, 1e-30)),
        cn0_dbhz=10 * math.log10(max(tone, 1e-30) / max(n0, 1e-30)), snr_in_bin_db=snr_bin,
        peak_abs=peak, clip_fraction=clip, frames=nfr, rbw_hz=rbw, valid=valid, warning="; ".join(warn))


def measure_noise(iq: np.ndarray, fs: float, center_hz: float, at_hz: float, half_bw: float = 40e3,
                  nfft: int = 65536) -> float:
    """Noise density (dBFS/Hz) around at_hz with no signal applied (robust mean)."""
    s, _ = power_spectrum(np.asarray(iq), nfft)
    f = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / fs))
    off = at_hz - center_hz
    sel = (np.abs(f - off) < half_bw) & (np.abs(f) > 3e3) & (np.abs(f) < 0.42 * fs)
    sa = s[sel]
    med = np.median(sa)
    nb = float(np.mean(sa[sa < 5 * med]))
    return 10 * math.log10(nb / (fs / nfft))


# ============================================================================
# Calibration fit and derived quantities
# ============================================================================
@dataclass
class CalPoint:
    level_dbm: float        # level at the receiver input (generator level - cable loss)
    meas: dict              # ToneMeasurement as dict


@dataclass
class Calibration:
    key: str
    k_db: float                     # dBm = dBFS + k
    slope: float
    residuals_db: list
    points: list
    noise_dbfs_hz: float | None = None
    nf_db: float | None = None
    ppm: float | None = None
    date: str = ""
    verdict: list = field(default_factory=list)
    ok: bool = True

    def dbm(self, dbfs: float) -> float:
        return dbfs + self.k_db


def fit_points(points: list[CalPoint], key: str) -> Calibration:
    x = np.array([p.level_dbm for p in points], float)
    y = np.array([p.meas["power_dbfs"] for p in points], float)
    if len(x) < 2:
        raise ValueError("at least two calibration points are needed")
    slope = float(np.polyfit(x, y, 1)[0])
    k = float(np.mean(x - y))
    res = [float(v) for v in (x - (y + k))]
    verdict = []
    ok = True
    if abs(slope - 1) > 0.05:
        ok = False
        verdict.append(f"linearity: slope {slope:.3f} dB/dB (expected 1.000 +/- 0.05)")
    else:
        verdict.append(f"linearity OK: slope {slope:.3f} dB/dB")
    order = np.argsort(x)
    worst = int(np.argmax(np.abs(res)))
    if max(abs(r) for r in res) > 0.5:
        ok = False
        if worst == order[-1] and res[worst] > 0:
            verdict.append(f"highest level reads {res[worst]:.2f} dB low: compression - lower the RF gain or the level")
        elif worst == order[0]:
            verdict.append(f"lowest level off by {res[worst]:+.2f} dB: leakage/pick-up or too close to the noise")
        else:
            verdict.append(f"residual {res[worst]:+.2f} dB at {x[worst]:.1f} dBm: check levels and cable loss")
    else:
        verdict.append(f"residuals within +/-{max(abs(r) for r in res):.2f} dB")
    for p in points:
        if p.meas.get("warning"):
            verdict.append(f"{p.level_dbm:.1f} dBm: {p.meas['warning']}")
            if "clipping" in p.meas["warning"]:
                ok = False
    ppms = [p.meas["freq_measured_hz"] / p.meas["freq_nominal_hz"] * 1e6 - 1e6 for p in points]
    return Calibration(key=key, k_db=k, slope=slope, residuals_db=res,
                       points=[asdict(p) if not isinstance(p, dict) else p for p in points],
                       ppm=float(np.median(ppms)), date=time.strftime("%Y-%m-%d %H:%M"), verdict=verdict, ok=ok)


def noise_figure(noise_dbfs_hz: float, k_db: float) -> float:
    """NF (dB) with a 290 K source: input-referred noise density minus kT0."""
    n_in = noise_dbfs_hz + k_db
    f = 10 ** ((n_in - KT0_DBM_HZ) / 10)
    return 10 * math.log10(max(f, 1.0))


@dataclass
class PreampResult:
    gain_db: float
    nf_system_db: float | None
    nf_preamp_db: float | None
    nf_note: str
    level_dbm: float
    lin_points: list


def preamp_result(cal: Calibration, level_dbm: float, meas_dbfs: float, noise_dbfs_hz: float | None,
                  extra: list | None = None) -> PreampResult:
    g = meas_dbfs + cal.k_db - level_dbm
    nf_sys = nf_pre = None
    note = ""
    if noise_dbfs_hz is not None and cal.nf_db is not None:
        n_in = noise_dbfs_hz + cal.k_db - g
        f_sys = 10 ** ((n_in - KT0_DBM_HZ) / 10)
        f_rx = 10 ** (cal.nf_db / 10)
        f_pre = f_sys - (f_rx - 1) / 10 ** (g / 10)
        nf_sys = 10 * math.log10(max(f_sys, 1.0))
        if f_pre > 1.0:
            nf_pre = 10 * math.log10(f_pre)
        else:
            nf_pre = 0.0
            note = "below the resolution of the method (< ~0.3 dB)"
        if g < 10:
            note = (note + "; " if note else "") + "low gain: NF of the preamp is less accurate (Friis correction large)"
    lin = []
    for lv, dbfs in (extra or []):
        lin.append((lv, dbfs + cal.k_db - lv))
    return PreampResult(g, nf_sys, nf_pre, note, level_dbm, lin)


def s_units(dbm: float) -> str:
    """IARU R1 S-meter scale above 30 MHz: S9 = -93 dBm, 6 dB per S unit."""
    if dbm >= S9_DBM_VHF:
        return f"S9+{dbm - S9_DBM_VHF:.0f}"
    s = 9 + (dbm - S9_DBM_VHF) / DB_PER_S
    return "S0" if s < 0.5 else f"S{int(round(s))}"


# ============================================================================
# Storage
# ============================================================================
def settings_key(cfg) -> str:
    dev = (cfg.get("sdr", "device", "rtlsdr") or "rtlsdr").lower()
    c = f"{cfg.getf('sdr', 'center_mhz', 137.5):.3f}"
    if dev == "rtlsdr":
        return f"rtlsdr|gain {cfg.get('sdr', 'rtl_gain_db', '38.6')}|{int(cfg.getf('sdr', 'rtl_sample_rate', 1.2e6))}|{c}"
    if dev == "sdrplay":
        return (f"sdrplay|grdb {cfg.geti('sdr', 'sdrplay_grdb', 40)} lna {cfg.geti('sdr', 'sdrplay_lna_state', 3)}"
                f" agc {cfg.get('sdr', 'sdrplay_agc', 'no')}|{int(cfg.getf('sdr', 'sdrplay_sample_rate', 2e6))}|{c}")
    if dev == "hackrf":
        return (f"hackrf|lna {cfg.geti('sdr', 'hackrf_lna_gain', 24)} vga {cfg.geti('sdr', 'hackrf_vga_gain', 30)}"
                f" amp {cfg.get('sdr', 'hackrf_amp', 'no')}|{int(cfg.getf('sdr', 'hackrf_sample_rate', 2.4e6))}|{c}")
    if dev == "lime":
        return (f"lime|gain {cfg.geti('sdr', 'lime_gain_db', 40)} {cfg.get('sdr', 'lime_antenna', 'auto')}"
                f"|{int(cfg.getf('sdr', 'lime_sample_rate', 2.4e6))}|{c}")
    if dev == "pluto":
        return f"pluto|gain {cfg.get('sdr', 'pluto_gain_db', '50')}|{int(cfg.getf('sdr', 'pluto_sample_rate', 2.16e6))}|{c}"
    return f"file|{cfg.get('sdr', 'file_path', '')}"


def cal_key(cfg, profile_id: str | None = None) -> str:
    """Calibration key: receiver profile + the settings that change the gain."""
    k = settings_key(cfg)
    return f"{profile_id}#{k}" if profile_id and not k.startswith("file|") else k


class CalStore:
    def __init__(self, path: str):
        self.path = path
        self.data = {"calibrations": {}, "preamps": []}
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.data.update(d)
        except (OSError, ValueError):
            pass

    def get(self, key: str) -> Calibration | None:
        d = self.data["calibrations"].get(key)
        if not d:
            return None
        try:
            return Calibration(**d)
        except TypeError:
            return None

    def get_for(self, cfg, profile_id: str | None) -> Calibration | None:
        """Calibration of this receiver with these settings. A calibration made before receiver profiles
        existed (key without profile) is adopted by the first profile that asks for it."""
        key = cal_key(cfg, profile_id)
        c = self.get(key)
        if c is not None or not profile_id:
            return c
        legacy = settings_key(cfg)
        d = self.data["calibrations"].get(legacy)
        if not d:
            return None
        d = dict(d, key=key)
        self.data["calibrations"][key] = d
        del self.data["calibrations"][legacy]
        try:
            self._save()
        except OSError:
            pass
        return self.get(key)

    def for_profile(self, profile_id: str) -> list[Calibration]:
        out = []
        for k in self.data["calibrations"]:
            if k.startswith(profile_id + "#"):
                c = self.get(k)
                if c is not None:
                    out.append(c)
        return out

    def put(self, cal: Calibration):
        self.data["calibrations"][cal.key] = asdict(cal)
        self._save()

    def add_preamp(self, name: str, res: PreampResult, cal_key: str):
        self.data["preamps"].append(dict(name=name, date=time.strftime("%Y-%m-%d %H:%M"), calibration=cal_key,
                                         **asdict(res)))
        self._save()

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)


def capture(source, seconds: float, settle: float = 0.4) -> np.ndarray:
    """Read 'seconds' of IQ after discarding 'settle' seconds (PLL / DC loops settling)."""
    fs = source.sample_rate
    need_skip = int(settle * fs)
    need = int(seconds * fs)
    chunks = []
    got = 0
    while got < need_skip + need:
        b = source.read(int(fs * 0.1))
        if b is None:
            break
        chunks.append(b)
        got += len(b)
    x = np.concatenate(chunks) if chunks else np.zeros(0, np.complex64)
    return x[need_skip:need_skip + need] if len(x) > need_skip + 65536 else x

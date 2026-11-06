"""Calibration / S-meter / preamp measurement accuracy, and the calibration dialog end to end."""
import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from meteorrx import calib as C  # noqa: E402

FS, FC = 1.2e6, 137.5e6


def tone_iq(p_dbfs, f_hz, n0_dbfs_hz, seconds=2.0, seed=1, clip=False):
    rng = np.random.default_rng(seed)
    n = int(FS * seconds)
    t = np.arange(n) / FS
    a = math.sqrt(10 ** (p_dbfs / 10))
    ph = 2 * np.pi * (f_hz - FC) * t
    sg = math.sqrt(10 ** (n0_dbfs_hz / 10) * FS / 2)
    x = a * (np.cos(ph) + 1j * np.sin(ph)) + rng.normal(0, sg, n) + 1j * rng.normal(0, sg, n)
    if clip:
        x = np.clip(x.real, -1, 1) + 1j * np.clip(x.imag, -1, 1)
    return x.astype(np.complex64)


@pytest.mark.parametrize("p,ferr", [(-20, 0.0), (-55, 1234.5), (-80, -9876.3)])
def test_tone_level_frequency_noise(p, ferr):
    m = C.measure_tone(tone_iq(p, 137.9e6 + ferr, -125.0), FS, FC, 137.9e6)
    assert abs(m.power_dbfs - p) < 0.1
    assert abs(m.noise_dbfs_hz + 125.0) < 0.2
    assert abs(m.freq_error_hz - ferr) < 2.0
    assert not m.warning


def test_clipping_is_reported():
    m = C.measure_tone(tone_iq(1.0, 137.9e6, -100.0, clip=True), FS, FC, 137.9e6)
    assert "clipping" in m.warning


def test_three_point_fit_and_compression():
    k = -38.0

    def pts(comp):
        return [C.CalPoint(lv, dict(power_dbfs=lv - k - (comp if lv == -70 else 0.0), freq_measured_hz=137.9e6 + 690,
                                    freq_nominal_hz=137.9e6, warning="")) for lv in (-70.0, -90.0, -110.0)]
    cal = C.fit_points(pts(0.0), "key")
    assert cal.ok and abs(cal.k_db - k) < 1e-9 and abs(cal.slope - 1) < 1e-9 and abs(cal.ppm - 5.0) < 0.01
    bad = C.fit_points(pts(1.5), "key")
    assert not bad.ok and any("compression" in v for v in bad.verdict)


def test_noise_figure_and_friis():
    k = -38.0
    cal = C.fit_points([C.CalPoint(lv, dict(power_dbfs=lv - k, freq_measured_hz=137.9e6, freq_nominal_hz=137.9e6,
                                            warning="")) for lv in (-70.0, -110.0)], "k")
    cal.nf_db = C.noise_figure(C.KT0_DBM_HZ + 6.0 - k, k)
    assert abs(cal.nf_db - 6.0) < 1e-6
    f_pre, g = 10 ** 0.08, 10 ** 2.2                     # 0.8 dB NF, 22 dB gain
    f_sys = f_pre + (10 ** 0.6 - 1) / g
    n_with = C.KT0_DBM_HZ + 10 * math.log10(f_sys) + 22.0 - k
    r = C.preamp_result(cal, -110.0, -110.0 + 22.0 - k, n_with, [(-80.0, -80.0 + 21.0 - k)])
    assert abs(r.gain_db - 22.0) < 1e-9 and abs(r.nf_preamp_db - 0.8) < 0.01
    assert abs(r.lin_points[0][1] - 21.0) < 1e-9


def test_s_units():
    assert C.s_units(-93.0) == "S9+0" and C.s_units(-73.0) == "S9+20"
    assert C.s_units(-99.0) == "S8" and C.s_units(-141.0) == "S1" and C.s_units(-160.0) == "S0"


def test_smeter_channel_power_on_lrpt():
    pytest.importorskip("scipy")
    from tests.synth import oqpsk_modulate
    from meteorrx.receiver import SignalMonitor
    rng = np.random.default_rng(2)
    bb = oqpsk_modulate(rng.integers(0, 2, 200000).astype(np.uint8), 72000, FS)
    t = np.arange(len(bb)) / FS
    x = bb * math.sqrt(1e-6) * (np.cos(2 * np.pi * 4e5 * t) + 1j * np.sin(2 * np.pi * 4e5 * t))
    sg = math.sqrt(10 ** (-125 / 10) * FS / 2)
    x = (x + rng.normal(0, sg, len(x)) + 1j * rng.normal(0, sg, len(x))).astype(np.complex64)
    m = SignalMonitor(FS)
    for i in range(0, len(x), 120000):
        m.push(x[i:i + 120000])
    r = m.report(4e5)
    assert abs(r.band_dbfs + 60.0) < 0.3 and abs(r.n0_dbfs_hz + 125.0) < 0.3


def test_calibration_dialog_end_to_end(tmp_path, monkeypatch):
    """Drive the dialog with a simulated receiver (K = -38 dB, NF 6 dB) and a 0.8 dB / 22 dB preamp."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    from meteorrx import caldialog, config, sdr
    _app = QApplication.instance() or QApplication([])  # noqa: F841
    ini = tmp_path / "m.ini"
    ini.write_text(config.DEFAULT_INI)
    cfg = config.load(str(ini))
    monkeypatch.setattr(type(cfg), "data_dir", property(lambda self: str(tmp_path)))
    K, NF = -38.0, 6.0
    state = {"dbm": None, "gain": 0.0, "f_pre": 1.0}

    class Sim(sdr.Source):
        sample_rate, center_hz = FS, FC

        def __init__(self):
            n0_in = C.KT0_DBM_HZ + 10 * math.log10(state["f_pre"] + (10 ** (NF / 10) - 1) / 10 ** (state["gain"] / 10))
            n0 = n0_in + state["gain"] - K
            p = -300.0 if state["dbm"] is None else state["dbm"] + state["gain"] - K
            self.x = tone_iq(p, 137.9e6 + 690.0, n0, seconds=1.0)
            self.i = 0

        def read(self, n):
            b = self.x[self.i:self.i + n]
            self.i += n
            return b if len(b) else None

    monkeypatch.setattr(sdr, "open_source", lambda c: Sim())
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: QMessageBox.Ok))
    monkeypatch.setattr(C, "capture", lambda src, secs, settle=0.4: src.x)
    d = caldialog.CalibrationDialog(cfg, "en")

    def run(fn, *a):
        fn(*a)
        d._job[1].join(30)
        d._poll()

    for n, lv in ((1, -70.0), (2, -90.0), (3, -110.0)):
        state["dbm"] = lv
        d.lv[n].setValue(lv)
        run(d._measure_point, n)
    state["dbm"] = None
    run(d._measure_noise_rx)
    d._fit()
    assert abs(d.cal.k_db - K) < 0.1 and abs(d.cal.nf_db - NF) < 0.3 and abs(d.cal.ppm - 5.0) < 0.05 and d.cal.ok
    assert C.CalStore(str(tmp_path / "calibration.json")).get(C.settings_key(cfg)) is not None
    state.update(gain=22.0, f_pre=10 ** 0.08, dbm=-110.0)
    run(d._measure_pre, "gain", -110.0)
    state["dbm"] = None
    run(d._measure_noise_pre)
    d._preamp()
    rep = d.report.toPlainText()
    g = float(rep.split("Gain at -110.0 dBm input: ")[1].split(" dB")[0])
    nf = float(rep.split("Preamp NF (Friis) ")[1].split(" dB")[0])
    assert abs(g - 22.0) < 0.15 and abs(nf - 0.8) < 0.3

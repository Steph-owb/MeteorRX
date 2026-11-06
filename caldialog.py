"""Calibration / preamplifier qualification dialog (signal generator, CW carrier)."""
from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import asdict

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (QDialog, QDoubleSpinBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout)

from . import calib, i18n, outputs, profiles, sdr

TXT = {code: i18n.texts(code, "calib") for code in i18n.LANGUAGES}   # texts: meteorrx/i18n.py


class CalibrationDialog(QDialog):
    def __init__(self, cfg, lang: str = "fr", parent=None, profile_id: str | None = None):
        super().__init__(parent)
        self.cfg = cfg
        self.t = TXT[i18n.normalize(lang)]
        self.store = calib.CalStore(os.path.join(cfg.data_dir, "calibration.json"))
        self.pstore = profiles.ProfileStore(cfg.data_dir)
        self.pid = profile_id if self.pstore.get(profile_id) else None
        self.prof = self.pstore.get(self.pid)
        self.key = calib.cal_key(cfg, self.pid)
        self.points = {}        # n -> CalPoint
        self.noise_rx = None
        self.pre = {}           # 'gain' / 'lin' -> (level, ToneMeasurement), 'noise' -> dBFS/Hz
        self.cal = self.store.get_for(cfg, self.pid)
        self._job = None
        self.setWindowTitle("MeteorRX – " + self.t["title"])
        scr = QGuiApplication.primaryScreen()
        h = min(860, scr.availableGeometry().height() - 60) if scr else 860
        self.resize(820, h)
        self._build()
        self._load_params()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(100)
        self._report_header()

    # ---------------------------------------------------------------- UI
    def _spin(self, v, lo, hi, dec=1, step=1.0):
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(dec)
        s.setSingleStep(step)
        s.setValue(v)
        return s

    def _build(self):
        t = self.t
        v = QVBoxLayout(self)
        intro = QLabel(t["intro"])
        intro.setWordWrap(True)
        v.addWidget(intro)
        g = QGridLayout()
        self.freq = self._spin(137.900, 137.0, 138.0, 4, 0.001)
        self.loss = self._spin(0.0, 0.0, 60.0, 1, 0.5)
        self.dur = self._spin(2.0, 0.5, 10.0, 1, 0.5)
        g.addWidget(QLabel(t["freq"]), 0, 0)
        g.addWidget(self.freq, 0, 1)
        g.addWidget(QLabel(t["loss"]), 0, 2)
        g.addWidget(self.loss, 0, 3)
        g.addWidget(QLabel(t["dur"]), 1, 0)
        g.addWidget(self.dur, 1, 1)
        who = (self.prof["name"] + " – ") if self.prof else ""
        lab = QLabel(t["settings"] + " : " + who + calib.settings_key(self.cfg))
        lab.setWordWrap(True)
        g.addWidget(lab, 1, 2, 1, 2)
        v.addLayout(g)

        b1 = QGroupBox(t["g1"])
        g1 = QGridLayout(b1)
        self.lv = {}
        self.res = {}
        for i, (n, lv) in enumerate(((1, -70.0), (2, -90.0), (3, -110.0))):
            self.lv[n] = self._spin(lv, -150.0, 0.0, 1, 1.0)
            btn = QPushButton(t["measure"])
            btn.clicked.connect(lambda _=False, n=n: self._measure_point(n))
            self.res[n] = QLabel("–")
            g1.addWidget(QLabel(t["point"].format(n=n)), i, 0)
            g1.addWidget(self.lv[n], i, 1)
            g1.addWidget(btn, i, 2)
            g1.addWidget(self.res[n], i, 3)
        bn = QPushButton(t["measure"])
        bn.clicked.connect(self._measure_noise_rx)
        self.res["noise"] = QLabel("–")
        g1.addWidget(QLabel(t["noise"]), 3, 0, 1, 2)
        g1.addWidget(bn, 3, 2)
        g1.addWidget(self.res["noise"], 3, 3)
        h = QHBoxLayout()
        self.btn_fit = QPushButton(t["fit"])
        self.btn_fit.clicked.connect(self._fit)
        self.btn_ppm = QPushButton(t["ppm"])
        self.btn_ppm.clicked.connect(self._apply_ppm)
        self.btn_ppm.setEnabled(False)
        h.addWidget(self.btn_fit)
        h.addWidget(self.btn_ppm)
        h.addStretch(1)
        g1.addLayout(h, 4, 0, 1, 4)
        g1.setColumnStretch(3, 1)
        v.addWidget(b1)

        b2 = QGroupBox(t["g2"])
        g2 = QGridLayout(b2)
        self.pname = QLineEdit("Preamp 137 MHz")
        g2.addWidget(QLabel(t["pname"]), 0, 0)
        g2.addWidget(self.pname, 0, 1, 1, 3)
        self.plv = self._spin(-110.0, -150.0, 0.0, 1, 1.0)
        self.plin = self._spin(-80.0, -150.0, 0.0, 1, 1.0)
        for row, (lab, sp, kind) in enumerate(((t["plevel"], self.plv, "gain"), (t["plin"], self.plin, "lin")), 1):
            b = QPushButton(t["measure"])
            b.clicked.connect(lambda _=False, k=kind, s=sp: self._measure_pre(k, s.value()))
            self.res["p" + kind] = QLabel("–")
            g2.addWidget(QLabel(lab), row, 0)
            g2.addWidget(sp, row, 1)
            g2.addWidget(b, row, 2)
            g2.addWidget(self.res["p" + kind], row, 3)
        bpn = QPushButton(t["measure"])
        bpn.clicked.connect(self._measure_noise_pre)
        self.res["pnoise"] = QLabel("–")
        g2.addWidget(QLabel(t["noise"]), 3, 0, 1, 2)
        g2.addWidget(bpn, 3, 2)
        g2.addWidget(self.res["pnoise"], 3, 3)
        bpc = QPushButton(t["pcalc"])
        bpc.clicked.connect(self._preamp)
        g2.addWidget(bpc, 4, 0)
        g2.setColumnStretch(3, 1)
        v.addWidget(b2)

        self.status = QLabel("")
        self.status.setStyleSheet("font-weight:600;color:#e0b040")
        v.addWidget(self.status)
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setFont(QFont("Consolas", 9))
        v.addWidget(self.report, 1)
        h = QHBoxLayout()
        for lab, fn in ((t["copy"], self._copy), (t["save"], self._save), (t["close"], self.accept)):
            b = QPushButton(lab)
            b.clicked.connect(fn)
            h.addWidget(b)
        h.addStretch(1)
        v.addLayout(h)
        self.buttons = self.findChildren(QPushButton)

    def _log(self, s: str):
        self.report.appendPlainText(s)

    def _report_header(self):
        self._log(f"MeteorRX receiver measurements - {time.strftime('%Y-%m-%d %H:%M')}")
        if self.prof:
            self._log(f"Receiver: {self.prof['name']}  [{self.prof['identity']['key']}]")
        self._log(f"Settings: {calib.settings_key(self.cfg)}")
        if self.cal:
            self._log(f"Existing calibration ({self.cal.date}): K = {self.cal.k_db:+.2f} dB, "
                      f"NF = {self.cal.nf_db if self.cal.nf_db is not None else float('nan'):.1f} dB")
        self._log("")

    # ---------------------------------------------------------------- measurement worker
    def _fixed_gain_ok(self) -> bool:
        dev = (self.cfg.get("sdr", "device", "rtlsdr") or "").lower()
        if dev == "rtlsdr" and str(self.cfg.get("sdr", "rtl_gain_db", "")).strip().lower() == "auto":
            return False
        if dev == "sdrplay" and self.cfg.getb("sdr", "sdrplay_agc", False):
            return False
        if dev == "pluto" and str(self.cfg.get("sdr", "pluto_gain_db", "")).strip().lower() in ("auto", "agc"):
            return False
        return True

    def _run(self, what: str, prompt: str, fn):
        if self._job is not None:
            return
        if not self._fixed_gain_ok():
            QMessageBox.warning(self, "MeteorRX", self.t["no_auto"])
            return
        if prompt and QMessageBox.information(self, "MeteorRX", prompt,
                                              QMessageBox.Ok | QMessageBox.Cancel) != QMessageBox.Ok:
            return
        self.status.setText(self.t["busy"])
        for b in self.buttons:
            b.setEnabled(False)
        box = {}
        f_hz = self.freq.value() * 1e6
        secs = self.dur.value()
        cfg = self.cfg

        def work():
            src = None
            try:
                src = sdr.open_source(cfg)
                src.start()
                x = calib.capture(src, secs)
                box["result"] = fn(x, src.sample_rate, src.center_hz, f_hz)
            except Exception as e:  # noqa: BLE001
                box["error"] = str(e)
            finally:
                if src is not None:
                    try:
                        src.stop()
                    except Exception:  # noqa: BLE001
                        pass

        th = threading.Thread(target=work, daemon=True)
        th.start()
        self._job = (what, th, box)

    def _poll(self):
        if self._job is None:
            return
        what, th, box = self._job
        if th.is_alive():
            return
        self._job = None
        self.status.setText("")
        for b in self.buttons:
            b.setEnabled(True)
        self.btn_ppm.setEnabled(self.cal is not None and self.cal.ppm is not None)
        if "error" in box:
            self._log(f"ERROR ({what}): {box['error']}")
            QMessageBox.warning(self, "MeteorRX", box["error"])
            return
        getattr(self, "_done_" + what.split(":")[0])(what, box["result"])

    @staticmethod
    def _tone(x, fs, fc, f):
        return calib.measure_tone(x, fs, fc, f)

    @staticmethod
    def _noise(x, fs, fc, f):
        return calib.measure_noise(x, fs, fc, f)

    # ---------------------------------------------------------------- part 1
    def _level_in(self, gen_dbm: float) -> float:
        return gen_dbm - self.loss.value()

    def _measure_point(self, n: int):
        lv = self.lv[n].value()
        self._run(f"point:{n}", self.t["set_gen"].format(lv=lv, f=self.freq.value()), self._tone)

    def _done_point(self, what, m: calib.ToneMeasurement):
        n = int(what.split(":")[1])
        lin = self._level_in(self.lv[n].value())
        self.points[n] = calib.CalPoint(lin, asdict(m))
        self.res[n].setText(f"{m.power_dbfs:7.2f} dBFS  K={lin - m.power_dbfs:+.2f} dB  "
                            f"Δf={m.freq_error_hz:+.0f} Hz  crête ADC {m.peak_abs * 100:.0f}%")
        self._log(f"Point {n}: input {lin:7.1f} dBm -> {m.power_dbfs:7.2f} dBFS, C/N0 {m.cn0_dbhz:5.1f} dBHz, "
                  f"f {m.freq_measured_hz / 1e6:.6f} MHz ({m.ppm:+.2f} ppm), ADC peak {m.peak_abs * 100:.0f}%"
                  + (f"  !! {m.warning}" if m.warning else ""))

    def _measure_noise_rx(self):
        self._run("noise_rx", self.t["gen_off"], self._noise)

    def _done_noise_rx(self, what, n0: float):
        self.noise_rx = n0
        txt = f"{n0:7.2f} dBFS/Hz"
        k = self.cal.k_db if self.cal else (self._k_from_points())
        if k is not None:
            txt += f"  ->  {n0 + k:7.2f} dBm/Hz, NF {calib.noise_figure(n0, k):.1f} dB"
        self.res["noise"].setText(txt)
        self._log(f"Noise (generator off): {txt}")

    def _k_from_points(self):
        if len(self.points) >= 2:
            return calib.fit_points(list(self.points.values()), self.key).k_db
        return None

    def _fit(self):
        if len(self.points) < 2:
            QMessageBox.warning(self, "MeteorRX", "≥ 2 points")
            return
        cal = calib.fit_points([self.points[k] for k in sorted(self.points)], self.key)
        if self.noise_rx is not None:
            cal.noise_dbfs_hz = self.noise_rx
            cal.nf_db = calib.noise_figure(self.noise_rx, cal.k_db)
        self.cal = cal
        self.store.put(cal)
        self._log("")
        self._log("=== Receiver calibration " + ("OK" if cal.ok else "WITH WARNINGS") + " ===")
        self._log(f"K = {cal.k_db:+.2f} dB   (P[dBm] = P[dBFS] + K)   slope {cal.slope:.3f} dB/dB")
        self._log("Residuals: " + ", ".join(f"{p['level_dbm']:.1f} dBm: {r:+.2f} dB"
                                             for p, r in zip(cal.points, cal.residuals_db)))
        if cal.nf_db is not None:
            self._log(f"Noise floor {cal.noise_dbfs_hz + cal.k_db:.2f} dBm/Hz -> receiver NF {cal.nf_db:.1f} dB")
            for bw, name in ((108e3, "LRPT 72k channel"), (2.4e3, "2.4 kHz")):
                self._log(f"  noise in {name}: {cal.noise_dbfs_hz + cal.k_db + 10 * math.log10(bw):.1f} dBm")
        self._log(f"Frequency error {cal.ppm:+.2f} ppm (ini: ppm = {self.cfg.getf('sdr', 'ppm', 0.0):+.2f})")
        for line in cal.verdict:
            self._log("  - " + line)
        self._log(f"Saved in {self.store.path}")
        if self.prof:
            self._log(self.t["saved_cal"].format(name=self.prof["name"], key=calib.settings_key(self.cfg)))
        self._save_params()
        self.btn_ppm.setEnabled(cal.ppm is not None)

    def _apply_ppm(self):
        if self.cal is None or self.cal.ppm is None:
            return
        self.cfg.set("sdr", "ppm", f"{self.cal.ppm:.2f}")
        self.pstore.update_settings(self.pid, self.cfg)     # ppm belongs to this receiver
        try:
            self.cfg.save()
            self._log(f"ppm = {self.cal.ppm:.2f} written to {self.cfg.path}")
        except OSError as e:
            self._log(f"ini not written: {e}")

    # ---------------------------------------------------------------- part 2
    def _measure_pre(self, kind: str, lv: float):
        if self.cal is None:
            QMessageBox.warning(self, "MeteorRX", self.t["need_cal"])
            return
        self._run(f"pre:{kind}:{lv}", self.t["set_gen"].format(lv=lv, f=self.freq.value()), self._tone)

    def _done_pre(self, what, m: calib.ToneMeasurement):
        _, kind, lv = what.split(":")
        lin = self._level_in(float(lv))
        self.pre[kind] = (lin, m)
        g = m.power_dbfs + self.cal.k_db - lin
        self.res["p" + kind].setText(f"{m.power_dbfs:7.2f} dBFS  ->  gain {g:+.2f} dB")
        self._log(f"Preamp {kind}: input {lin:.1f} dBm -> {m.power_dbfs:.2f} dBFS, gain {g:+.2f} dB"
                  + (f"  !! {m.warning}" if m.warning else ""))

    def _measure_noise_pre(self):
        if self.cal is None:
            QMessageBox.warning(self, "MeteorRX", self.t["need_cal"])
            return
        self._run("pnoise", self.t["gen_off"], self._noise)

    def _done_pnoise(self, what, n0: float):
        self.pre["noise"] = n0
        self.res["pnoise"].setText(f"{n0:7.2f} dBFS/Hz")
        self._log(f"Preamp noise (generator off): {n0:.2f} dBFS/Hz")

    def _preamp(self):
        if self.cal is None or "gain" not in self.pre:
            QMessageBox.warning(self, "MeteorRX", self.t["need_cal"])
            return
        lin, m = self.pre["gain"]
        extra = [(self.pre["lin"][0], self.pre["lin"][1].power_dbfs)] if "lin" in self.pre else []
        r = calib.preamp_result(self.cal, lin, m.power_dbfs, self.pre.get("noise"), extra)
        self.store.add_preamp(self.pname.text(), r, self.key)
        self._log("")
        self._log(f"=== Preamplifier '{self.pname.text()}' ===")
        self._log(f"Gain at {lin:.1f} dBm input: {r.gain_db:+.2f} dB")
        for lv, g in r.lin_points:
            self._log(f"Gain at {lv:.1f} dBm input: {g:+.2f} dB  (difference {g - r.gain_db:+.2f} dB"
                      + (": compression" if g - r.gain_db < -0.5 else ": linear") + ")")
        if r.nf_preamp_db is not None:
            self._log(f"System NF with preamp {r.nf_system_db:.2f} dB, receiver alone {self.cal.nf_db:.1f} dB")
            self._log(f"Preamp NF (Friis) {r.nf_preamp_db:.2f} dB" + (f"  ({r.nf_note})" if r.nf_note else ""))
        elif self.cal.nf_db is None:
            self._log("Preamp NF: needs the receiver noise measurement in part 1")
        self._log("Note: the gain method assumes a 290 K source (generator output off, attenuator at maximum).")
        self._save_params()
        if self.pid and QMessageBox.question(
                self, "MeteorRX", self.t["in_service"].format(name=self.pname.text(), g=r.gain_db)) == QMessageBox.Yes:
            self.pstore.set_preamp(self.pid, {"name": self.pname.text(), "gain_db": round(r.gain_db, 2),
                                              "nf_db": None if r.nf_preamp_db is None else round(r.nf_preamp_db, 2),
                                              "date": time.strftime("%Y-%m-%d %H:%M"), "in_service": True})
            self._log(f"Preamplifier '{self.pname.text()}' in service on '{self.prof['name']}'")

    # ---------------------------------------------------------------- remembered parameters
    def _params(self) -> dict:
        return {"freq_mhz": self.freq.value(), "loss_db": self.loss.value(), "duration_s": self.dur.value(),
                "levels_dbm": [self.lv[n].value() for n in (1, 2, 3)], "preamp_name": self.pname.text(),
                "preamp_level_dbm": self.plv.value(), "preamp_lin_dbm": self.plin.value()}

    def _load_params(self):
        p = (self.prof or {}).get("cal_params") or {}
        try:
            for w, k in ((self.freq, "freq_mhz"), (self.loss, "loss_db"), (self.dur, "duration_s"),
                         (self.plv, "preamp_level_dbm"), (self.plin, "preamp_lin_dbm")):
                if k in p:
                    w.setValue(float(p[k]))
            for n, v in zip((1, 2, 3), p.get("levels_dbm", [])):
                self.lv[n].setValue(float(v))
            if p.get("preamp_name"):
                self.pname.setText(str(p["preamp_name"]))
            elif (self.prof or {}).get("preamp"):
                self.pname.setText(self.prof["preamp"].get("name", ""))
        except (TypeError, ValueError):
            pass

    def _save_params(self):
        if self.pid:
            try:
                self.pstore = profiles.ProfileStore(self.cfg.data_dir)   # re-read: preamp may have changed
                self.pstore.set_cal_params(self.pid, self._params())
            except OSError:
                pass

    def done(self, r):
        self._save_params()
        super().done(r)

    # ---------------------------------------------------------------- report
    def _copy(self):
        QGuiApplication.clipboard().setText(self.report.toPlainText())

    def _save(self):
        p, _ = QFileDialog.getSaveFileName(self, self.t["save"],
                                           os.path.join(self.cfg.data_dir,
                                                        outputs.unique_path(os.path.join(self.cfg.data_dir,
                                                            time.strftime("measurements_%Y%m%d_%H%M%S.txt")))),
                                           "Text (*.txt)")
        if p:
            with open(p, "w", encoding="utf-8") as f:
                f.write(self.report.toPlainText())

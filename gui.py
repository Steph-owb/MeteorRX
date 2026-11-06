"""MeteorRX main window (PySide6). Left: controls + signal validation (eye diagram). Right: imagery."""
from __future__ import annotations

import json
import os
import queue
import time
import sys
import threading
from datetime import datetime, timezone

import numpy as np
from . import enhance, i18n
from PySide6.QtCore import QByteArray, QRectF, Qt, QTimer, QUrl
from PySide6.QtGui import (QAction, QColor, QDesktopServices, QFont, QGuiApplication, QImage, QKeySequence, QPainter,
                           QPalette, QPen, QPixmap, QShortcut)
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QFrame, QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QGridLayout,
                               QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
                               QSlider, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout,
                               QWidget)

from . import __version__, calib, config, geometry, imaging, lrpt, outputs, overlay, profiles, sdr
from .engine import Engine

# ----------------------------------------------------------------------------
# Translations
# ----------------------------------------------------------------------------
T = {code: i18n.texts(code, "gui") for code in i18n.LANGUAGES}   # all UI texts: meteorrx/i18n.py
DEV_KEYS = ["rtlsdr", "sdrplay", "hackrf", "lime", "pluto", "file"]
LIME_ANT = ["auto", "LNAW", "LNAH", "LNAL"]
BIAS_KEYS = {"rtlsdr": "rtl_bias_tee", "sdrplay": "sdrplay_bias_tee", "hackrf": "hackrf_bias_tee"}

ACCENT = QColor(80, 220, 140)


# ----------------------------------------------------------------------------
# Signal widgets
# ----------------------------------------------------------------------------
def _phosphor_lut() -> np.ndarray:
    x = np.linspace(0, 1, 256)
    r = np.clip(3 * x - 1.6, 0, 1)
    g = np.clip(1.6 * x, 0, 1)
    b = np.clip(2.2 * x - 1.2, 0, 1) * 0.8 + 0.08 * (1 - x)
    lut = (0xFF << 24) | ((r * 255).astype(np.uint32) << 16) | ((g * 255).astype(np.uint32) << 8) | \
          (b * 255 + 14).clip(0, 255).astype(np.uint32)
    return lut.astype(np.uint32)


_LUT = _phosphor_lut()


class DensityWidget(QWidget):
    """Persistence display base (eye diagram / constellation)."""

    def __init__(self, w=300, h=170, decay=0.80, parent=None):
        super().__init__(parent)
        self.W, self.H = w, h
        self.acc = np.zeros((h, w), np.float32)
        self.decay = decay
        self.setMinimumHeight(140)
        self._img = None
        self.title = ""
        self.overlay = ""

    def _render(self):
        a = self.acc
        m = np.percentile(a[a > 0], 99.7) if (a > 0).any() else 1.0
        idx = np.clip(np.log1p(4.0 * a / max(m, 1e-6)) / np.log1p(4.0) * 255, 0, 255).astype(np.uint8)
        buf = np.ascontiguousarray(_LUT[idx])
        self._buf = buf
        self._img = QImage(buf.data, self.W, self.H, self.W * 4, QImage.Format_ARGB32)

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(8, 12, 10))
        if self._img is not None:
            p.drawImage(QRectF(self.rect()), self._img)
        self.draw_grid(p)
        p.setPen(QColor(160, 200, 170))
        p.setFont(QFont("Consolas", 8))
        p.drawText(6, 13, self.title)
        if self.overlay:
            p.drawText(self.rect().adjusted(0, 0, -6, -4), Qt.AlignRight | Qt.AlignBottom, self.overlay)
        p.end()

    def draw_grid(self, p):
        pass


class EyeWidget(DensityWidget):
    def __init__(self, parent=None):
        super().__init__(320, 180, 0.70, parent)
        self.ylim = 1.8

    def update_traces(self, eye):
        self.acc *= self.decay
        if eye is not None:
            t, tr = eye
            # dense resampling along x for continuous traces
            xs = np.linspace(t[0], t[-1], self.W)
            for y in tr:
                yy = np.interp(xs, t, y)
                yi = np.clip(((self.ylim - yy) / (2 * self.ylim) * (self.H - 1)).astype(int), 0, self.H - 1)
                np.add.at(self.acc, (yi, np.arange(self.W)), 1.0)
            # eye quality at the decision instant: 20 log10(mean |y| / std |y|)
            c = np.abs(tr[:, tr.shape[1] // 2])
            q = 20 * np.log10(c.mean() / c.std()) if c.std() > 0 else 40.0
            self.overlay = f"Q {q:4.1f} dB"
        else:
            self.overlay = ""
        self._render()
        self.update()

    def draw_grid(self, p):
        r = self.rect()
        p.setPen(QPen(QColor(60, 90, 70), 1, Qt.DotLine))
        for k in (0.25, 0.5, 0.75):
            x = r.left() + k * r.width()
            p.drawLine(int(x), r.top(), int(x), r.bottom())
        for v in (-1, 0, 1):
            y = r.top() + (self.ylim - v) / (2 * self.ylim) * r.height()
            p.drawLine(r.left(), int(y), r.right(), int(y))


class ConstWidget(DensityWidget):
    def __init__(self, parent=None):
        super().__init__(160, 160, 0.6, parent)
        self.setMinimumHeight(140)

    def update_syms(self, s):
        self.acc *= self.decay
        if s is not None and len(s):
            lim = 1.8
            xi = np.clip(((s.real + lim) / (2 * lim) * (self.W - 1)).astype(int), 0, self.W - 1)
            yi = np.clip(((lim - s.imag) / (2 * lim) * (self.H - 1)).astype(int), 0, self.H - 1)
            np.add.at(self.acc, (yi, xi), 1.0)
        self._render()
        self.update()

    def draw_grid(self, p):
        r = self.rect()
        p.setPen(QPen(QColor(60, 90, 70), 1, Qt.DotLine))
        p.drawLine(r.center().x(), r.top(), r.center().x(), r.bottom())
        p.drawLine(r.left(), r.center().y(), r.right(), r.center().y())


class SpectrumWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(90)
        self.data = None
        self.smooth = None

    def update_spec(self, spec):
        if spec is None:
            return
        f, db, marks = spec
        if self.smooth is None or len(self.smooth) != len(db):
            self.smooth = db.copy()
        else:
            self.smooth = 0.7 * self.smooth + 0.3 * db
        self.data = (f, self.smooth, marks)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        r = self.rect()
        p.fillRect(r, QColor(8, 12, 10))
        if self.data is not None:
            f, db, marks = self.data
            n = r.width()
            idx = np.linspace(0, len(db) - 1, n).astype(int)
            v = db[idx]
            lo, hi = np.percentile(db, 5) - 3, db.max() + 3
            ys = r.bottom() - (v - lo) / max(hi - lo, 1) * (r.height() - 14)
            p.setPen(QPen(ACCENT, 1))
            pts = [(r.left() + i, int(y)) for i, y in enumerate(ys)]
            for i in range(1, len(pts)):
                p.drawLine(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1])
            p.setPen(QPen(QColor(230, 180, 60), 1, Qt.DashLine))
            for m in marks:
                x = r.left() + (m - f[0]) / (f[-1] - f[0]) * r.width()
                p.drawLine(int(x), r.top() + 12, int(x), r.bottom())
                p.drawText(int(x) + 3, r.top() + 11, f"{m / 1e6:.3f}")
        p.end()


class Led(QLabel):
    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.set(False)

    def set(self, on: bool, warn: bool = False):
        col = "#3ddc84" if on else ("#e0a030" if warn else "#3a4540")
        fg = "#06140c" if on else "#c8d4cc"
        self.setStyleSheet(f"QLabel{{background:{col};color:{fg};border-radius:9px;padding:2px 8px;"
                           f"font-weight:600;font-size:11px}}")


class SMeterWidget(QWidget):
    """Meteor channel power: calibrated dBm + IARU S-units, or dBFS when no calibration matches."""
    LO, HI = -141.0, -53.0          # S1 .. S9+40 (IARU R1 VHF scale)

    def __init__(self, uncal_text: str, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(38)
        self.uncal_text = uncal_text
        self.value = None
        self.cal = False
        self.line2 = ""

    def set_level(self, dbfs, n0_dbfs_hz, cal_k):
        if dbfs is None or dbfs < -900:
            self.value = None
            self.line2 = ""
        else:
            v = dbfs + (cal_k or 0.0)
            self.value = v if self.value is None else 0.7 * self.value + 0.3 * v
            self.cal = cal_k is not None
            cn0 = dbfs - n0_dbfs_hz if n0_dbfs_hz > -900 else float("nan")
            n0 = n0_dbfs_hz + (cal_k or 0.0)
            unit = "dBm/Hz" if self.cal else "dBFS/Hz"
            self.line2 = f"N0 {n0:.1f} {unit}  C/N0 {cn0:.1f} dBHz"
        self.update()

    def paintEvent(self, _):
        from . import calib
        p = QPainter(self)
        r = self.rect()
        p.fillRect(r, QColor(8, 12, 10))
        bar = r.adjusted(4, 14, -196, -4)
        p.setPen(QPen(QColor(60, 90, 70), 1))
        p.drawRect(bar)
        lo, hi = (self.LO, self.HI) if self.cal else (-110.0, -20.0)
        if self.value is not None:
            frac = min(max((self.value - lo) / (hi - lo), 0.0), 1.0)
            fill = bar.adjusted(1, 1, -bar.width() + int(frac * bar.width()), -1)
            col = ACCENT if (not self.cal or self.value < calib.S9_DBM_VHF) else QColor(230, 160, 60)
            p.fillRect(fill, col)
        p.setFont(QFont("Consolas", 7))
        p.setPen(QColor(150, 180, 160))
        if self.cal:
            for lab, dbm in (("1", -141), ("3", -129), ("5", -117), ("7", -105), ("9", -93), ("+20", -73),
                             ("+40", -53)):
                x = bar.left() + (dbm - lo) / (hi - lo) * bar.width()
                p.drawText(int(x) - (12 if lab == "+40" else 4), 11, lab)
        p.setFont(QFont("Consolas", 9))
        p.setPen(QColor(220, 235, 225))
        if self.value is None:
            txt = "–"
        elif self.cal:
            txt = f"{self.value:6.1f} dBm  {calib.s_units(self.value)}"
        else:
            txt = f"{self.value:6.1f} dBFS ({self.uncal_text})"
        p.drawText(r.adjusted(r.width() - 186, 0, 0, -16), Qt.AlignLeft | Qt.AlignVCenter, txt)
        p.setFont(QFont("Consolas", 7))
        p.drawText(r.adjusted(r.width() - 186, 18, 0, 0), Qt.AlignLeft | Qt.AlignVCenter, self.line2)
        p.end()


class ImageView(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.item = QGraphicsPixmapItem()
        self.item.setTransformationMode(Qt.SmoothTransformation)
        self.scene().addItem(self.item)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setBackgroundBrush(QColor(12, 14, 16))
        self.auto_fit = True
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)

    def show_array(self, img: np.ndarray):
        img = np.ascontiguousarray(img)
        h, w = img.shape[:2]
        if img.ndim == 2:
            q = QImage(img.data, w, h, w, QImage.Format_Grayscale8)
        else:
            q = QImage(img.data, w, h, 3 * w, QImage.Format_RGB888)
        self.item.setPixmap(QPixmap.fromImage(q.copy()))
        self.scene().setSceneRect(QRectF(0, 0, w, h))
        if self.auto_fit:
            self.fit_width()

    def fit_width(self):
        r = self.scene().sceneRect()
        if r.width() <= 0:
            return
        self.resetTransform()
        s = (self.viewport().width() - 4) / r.width()
        self.scale(s, s)
        self.auto_fit = True

    def wheelEvent(self, e):
        f = 1.25 if e.angleDelta().y() > 0 else 0.8
        self.scale(f, f)
        self.auto_fit = False

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.auto_fit:
            self.fit_width()


# ----------------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self, cfg: config.Config):
        super().__init__()
        self.cfg = cfg
        self.lang = i18n.normalize(cfg.get("gui", "language", "fr"))
        self.t = T[self.lang]
        self.setWindowTitle(f"MeteorRX {__version__} – Meteor-M LRPT – {cfg.get('station', 'callsign', '')}")
        self._fit_to_screen(1440, 900)
        self.logq: queue.Queue = queue.Queue()
        self.engine = Engine(cfg, log_cb=self.logq.put)
        self._preview_rev = -1
        self._last_view_update = 0
        self._build()
        self._connected: set = set()
        self._profiles_detect(startup=True)
        QShortcut(QKeySequence(Qt.Key_F11), self, self._toggle_fullscreen)
        QShortcut(QKeySequence(Qt.Key_Escape), self, self._esc)
        self.engine.start()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(100)
        self._tickn = 0

    # -- window placement -------------------------------------------------------
    def _fit_to_screen(self, w: int, h: int):
        """Never larger than the usable screen (laptop at 125-150 % scaling = ~1280x720 logical)."""
        scr = QGuiApplication.primaryScreen()
        if scr is not None:
            a = scr.availableGeometry()
            w, h = min(w, a.width() - 20), min(h, a.height() - 40)
            self.setGeometry(a.x() + (a.width() - w) // 2, a.y() + (a.height() - h) // 2, w, h)
        else:
            self.resize(w, h)

    def _state_path(self) -> str:
        return os.path.join(self.cfg.data_dir, "window.json")

    def show_initial(self):
        """[gui] window = maximized (default) | fullscreen | last."""
        mode = (self.cfg.get("gui", "window", "maximized") or "maximized").strip().lower()
        st = {}
        try:
            with open(self._state_path(), "r", encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            pass
        self.chk_mirror.setChecked(bool(st.get("mirror", False)))
        if isinstance(st.get("enh"), dict):
            for k, v in st["enh"].items():
                if k not in self.enh:
                    continue                                  # a key from a newer version: ignore it
                if k.endswith("_colour"):                     # a palette key, not a checkbox
                    self.enh[k] = v if v in overlay.PALETTE else self.enh[k]
                else:
                    self.enh[k] = bool(v)
        for k, sp in (("splitter", self.split), ("rsplitter", self.rsplit)):
            if st.get(k):
                try:
                    sp.restoreState(QByteArray.fromHex(st[k].encode()))
                except Exception:  # noqa: BLE001
                    pass
        if mode == "last" and st.get("geometry"):
            self.restoreGeometry(QByteArray.fromHex(st["geometry"].encode()))
            if not any(sc.availableGeometry().intersects(self.frameGeometry()) for sc in QGuiApplication.screens()):
                self._fit_to_screen(1440, 900)       # screen removed since last time
            if st.get("fullscreen"):
                self.showFullScreen()
            elif st.get("maximized", True):
                self.showMaximized()
            else:
                self.show()
        elif mode == "fullscreen":
            self.showFullScreen()
        else:
            self.showMaximized()

    def _save_window_state(self):
        try:
            os.makedirs(self.cfg.data_dir, exist_ok=True)
            st = {"geometry": bytes(self.saveGeometry().toHex()).decode(),
                  "splitter": bytes(self.split.saveState().toHex()).decode(),
                  "rsplitter": bytes(self.rsplit.saveState().toHex()).decode(),
                  "maximized": self.isMaximized(), "fullscreen": self.isFullScreen(),
                  "mirror": self.chk_mirror.isChecked(), "enh": self.enh}
            tmp = self._state_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f)
            os.replace(tmp, self._state_path())
        except OSError:
            pass

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showMaximized()
        else:
            self.showFullScreen()

    def _esc(self):
        if self.isFullScreen():
            self.showMaximized()

    # -- construction -----------------------------------------------------------
    def _build(self):
        t = self.t
        split = QSplitter(Qt.Horizontal, self)
        self.split = split
        self.setCentralWidget(split)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(8, 8, 8, 8)
        lv.setSpacing(8)

        # --- receiver
        g = QGroupBox(t["source"])
        fl = QFormLayout(g)
        ph = QHBoxLayout()
        ph.setContentsMargins(0, 0, 0, 0)
        self.prof_cb = QComboBox()
        self.prof_cb.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.prof_cb.setMinimumContentsLength(14)
        self.btn_detect = QPushButton(t["detect"])
        self.btn_detect.clicked.connect(lambda: self._profiles_detect(startup=False))
        pm = QToolButton()
        pm.setText("⋯")
        pm.setPopupMode(QToolButton.InstantPopup)
        mn = QMenu(pm)
        mn.addAction(t["rename"], self._profile_rename)
        mn.addAction(t["delete"], self._profile_delete)
        pm.setMenu(mn)
        ph.addWidget(self.prof_cb, 1)
        ph.addWidget(self.btn_detect)
        ph.addWidget(pm)
        prow = QWidget()
        prow.setLayout(ph)
        fl.addRow(t["profile"], prow)
        self.prof_lbl = QLabel("")
        self.prof_lbl.setWordWrap(True)
        self.prof_lbl.setStyleSheet("color:#9fb3a8;font-size:11px")
        fl.addRow(self.prof_lbl)
        self.dev = QComboBox()
        self.dev.addItems(t["devices"])
        cur = (self.cfg.get("sdr", "device", "rtlsdr") or "rtlsdr").lower()
        self.dev.setCurrentIndex(DEV_KEYS.index(cur) if cur in DEV_KEYS else 0)
        fl.addRow(t["device"], self.dev)
        self.rtl_gain = QDoubleSpinBox()
        self.rtl_gain.setRange(0, 50)
        self.rtl_gain.setDecimals(1)
        self.rtl_gain.setSingleStep(1)
        try:
            self.rtl_gain.setValue(float(self.cfg.get("sdr", "rtl_gain_db", "38.6")))
        except ValueError:
            self.rtl_gain.setValue(38.6)
        self.lbl_rtl_gain = QLabel(t["gain"])
        fl.addRow(self.lbl_rtl_gain, self.rtl_gain)
        self.sp_grdb = QSpinBox()
        self.sp_grdb.setRange(20, 59)
        self.sp_grdb.setValue(self.cfg.geti("sdr", "sdrplay_grdb", 40))
        self.sp_lna = QSpinBox()
        self.sp_lna.setRange(0, 9)
        self.sp_lna.setValue(self.cfg.geti("sdr", "sdrplay_lna_state", 3))
        self.lbl_grdb, self.lbl_lna = QLabel(t["grdb"]), QLabel(t["lna"])
        fl.addRow(self.lbl_grdb, self.sp_grdb)
        fl.addRow(self.lbl_lna, self.sp_lna)
        self.cb_ant = QComboBox()
        self.cb_ant.addItems(["A", "B", "C"])
        self.cb_ant.setCurrentText((self.cfg.get("sdr", "sdrplay_antenna", "A") or "A").upper()[:1])
        self.lbl_ant = QLabel(t["ant"])
        fl.addRow(self.lbl_ant, self.cb_ant)
        self.sp_hlna, self.sp_hvga = QSpinBox(), QSpinBox()
        self.sp_hlna.setRange(0, 40)
        self.sp_hlna.setSingleStep(8)
        self.sp_hvga.setRange(0, 62)
        self.sp_hvga.setSingleStep(2)
        self.sp_hlna.setValue(self.cfg.geti("sdr", "hackrf_lna_gain", 24))
        self.sp_hvga.setValue(self.cfg.geti("sdr", "hackrf_vga_gain", 30))
        self.lbl_hlna, self.lbl_hvga = QLabel(t["hlna"]), QLabel(t["hvga"])
        fl.addRow(self.lbl_hlna, self.sp_hlna)
        fl.addRow(self.lbl_hvga, self.sp_hvga)
        self.chk_hamp = QCheckBox(t["hamp"])
        self.chk_hamp.setChecked(self.cfg.getb("sdr", "hackrf_amp", False))
        fl.addRow("", self.chk_hamp)
        self.sp_lgain = QSpinBox()
        self.sp_lgain.setRange(0, 73)
        self.sp_lgain.setValue(self.cfg.geti("sdr", "lime_gain_db", 40))
        self.lbl_lgain = QLabel(t["lgain"])
        fl.addRow(self.lbl_lgain, self.sp_lgain)
        self.cb_lant = QComboBox()
        self.cb_lant.addItems(LIME_ANT)
        self._set_lant(self.cfg.get("sdr", "lime_antenna", "auto"))
        self.lbl_lant = QLabel(t["lant"])
        fl.addRow(self.lbl_lant, self.cb_lant)
        self.ed_puri = QLineEdit(self.cfg.get("sdr", "pluto_uri", "ip:192.168.2.1") or "ip:192.168.2.1")
        self.lbl_puri = QLabel(t["puri"])
        fl.addRow(self.lbl_puri, self.ed_puri)
        self.sp_pgain = QSpinBox()
        self.sp_pgain.setRange(0, 71)
        try:
            self.sp_pgain.setValue(int(float(self.cfg.get("sdr", "pluto_gain_db", "50"))))
        except ValueError:
            self.sp_pgain.setValue(50)
        self.lbl_pgain = QLabel(t["pgain"])
        fl.addRow(self.lbl_pgain, self.sp_pgain)
        self.bias = QCheckBox(t["bias"])
        fl.addRow("", self.bias)
        self.chk_preamp = QCheckBox("")
        self.chk_preamp.setVisible(False)
        self.chk_preamp.toggled.connect(self._preamp_toggled)
        fl.addRow(self.chk_preamp)
        fh = QHBoxLayout()
        self.file_edit = QLineEdit(self.cfg.get("sdr", "file_path", "") or "")
        bb = QToolButton()
        bb.setText(t["browse"])
        bb.clicked.connect(self._browse)
        fh.addWidget(self.file_edit)
        fh.addWidget(bb)
        self.lbl_file = QLabel(t["file"])
        self.file_row = QWidget()
        self.file_row.setLayout(fh)
        fh.setContentsMargins(0, 0, 0, 0)
        fl.addRow(self.lbl_file, self.file_row)
        self.dev.currentIndexChanged.connect(self._dev_changed)
        self.dev.activated.connect(self._dev_picked)
        for w in (self.rtl_gain, self.sp_grdb, self.sp_lna, self.sp_hlna, self.sp_hvga, self.sp_lgain, self.sp_pgain):
            w.valueChanged.connect(lambda *_: self._profile_status())
        self.prof_cb.activated.connect(self._profile_picked)
        lv.addWidget(g)

        # --- satellite / control
        g = QGroupBox(t["sat"])
        gl = QGridLayout(g)
        self.sat = QComboBox()
        for s in self.cfg.satellites:
            self.sat.addItem(s.name)
        self.sat.addItem(t["manual"])
        self.mode = QComboBox()
        self.mode.addItems(["auto", "72k", "80k"])
        self.mode.setCurrentText(self.cfg.get("decoder", "mode", "auto"))
        gl.addWidget(self.sat, 0, 0)
        gl.addWidget(QLabel(t["mode"]), 0, 1)
        gl.addWidget(self.mode, 0, 2)
        self.btn_start = QPushButton("▶  " + t["start"])
        self.btn_stop = QPushButton("■  " + t["stop"])
        self.btn_start.setMinimumHeight(30)
        self.btn_stop.setMinimumHeight(30)
        self.btn_start.setStyleSheet("QPushButton{background:#1f6f43;color:white;font-weight:600}")
        self.btn_stop.setStyleSheet("QPushButton{background:#7a2a2a;color:white;font-weight:600}")
        self.btn_start.clicked.connect(self._start)
        self.btn_stop.clicked.connect(lambda: self.engine.command("stop"))
        gl.addWidget(self.btn_start, 1, 0)
        gl.addWidget(self.btn_stop, 1, 1, 1, 2)
        self.chk_auto = QCheckBox(t["auto"])
        self.chk_auto.setChecked(self.engine.auto)
        self.chk_auto.toggled.connect(lambda on: self.engine.command("auto", enabled=on))
        gl.addWidget(self.chk_auto, 2, 0, 1, 3)
        self.state_lbl = QLabel("")
        self.state_lbl.setStyleSheet("font-size:14px;font-weight:700;padding:4px")
        gl.addWidget(self.state_lbl, 3, 0, 1, 3)
        self.prog = QProgressBar()
        self.prog.setVisible(False)
        gl.addWidget(self.prog, 4, 0, 1, 3)
        lv.addWidget(g)

        # --- signal validation
        g = QGroupBox(t["signal"])
        sv = QVBoxLayout(g)
        leds = QHBoxLayout()
        self.led_sig, self.led_vit = Led(t["led_sig"]), Led(t["led_vit"])
        self.led_frm, self.led_img, self.led_rec = Led(t["led_frm"]), Led(t["led_img"]), Led("● " + t["rec"])
        for w in (self.led_sig, self.led_vit, self.led_frm, self.led_img, self.led_rec):
            leds.addWidget(w)
        sv.addLayout(leds)
        self.smeter = SMeterWidget(t["uncal"])
        sv.addWidget(self.smeter)
        ah = QHBoxLayout()
        self.btn_audio = QPushButton(t["muted"])
        self.btn_audio.setCheckable(True)
        self.btn_audio.setToolTip(t["listen_tip"])
        self.btn_audio.toggled.connect(self._audio_toggled)
        self.sl_vol = QSlider(Qt.Horizontal)
        self.sl_vol.setRange(0, 100)
        self.sl_vol.setValue(self.cfg.geti("gui", "audio_volume", 50))
        self.sl_vol.setToolTip(t["volume"])
        self.sl_vol.valueChanged.connect(lambda v: self.engine.audio.set(self.btn_audio.isChecked(), v / 100.0))
        ah.addWidget(self.btn_audio)
        ah.addWidget(QLabel(t["volume"]))
        ah.addWidget(self.sl_vol, 1)
        sv.addLayout(ah)
        # compact: eye + constellation side by side, measurements in two columns underneath
        row = QHBoxLayout()
        self.eye = EyeWidget()
        self.eye.title = t["eye"]
        self.eye.setMinimumHeight(110)
        row.addWidget(self.eye, 1)
        self.const = ConstWidget()
        self.const.title = t["const"]
        self.const.setMinimumHeight(110)
        self.const.setFixedWidth(130)
        row.addWidget(self.const)
        sv.addLayout(row, 2)
        vrow = QHBoxLayout()
        self.vals, self.vals2 = QLabel(), QLabel()
        for lab in (self.vals, self.vals2):
            lab.setFont(QFont("Consolas", 9))
            lab.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            lab.setTextFormat(Qt.PlainText)
            vrow.addWidget(lab, 1)
        sv.addLayout(vrow)
        self.spec = SpectrumWidget()
        self.spec.setMinimumHeight(60)
        sv.addWidget(self.spec, 1)
        lv.addWidget(g, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(left)
        scroll.setMinimumWidth(420)
        scroll.setFrameShape(QFrame.NoFrame)
        split.addWidget(scroll)

        # --- right: result (top) + passes and log (bottom, always visible)
        self.rsplit = QSplitter(Qt.Vertical)
        right = QWidget()

        rv = QVBoxLayout(right)
        rv.setContentsMargins(6, 8, 8, 8)
        bar = QHBoxLayout()
        title = QLabel(t["result"])
        title.setStyleSheet("font-size:15px;font-weight:700")
        bar.addWidget(title)
        bar.addStretch(1)
        bar.addWidget(QLabel(t["view"]))
        self.view_sel = QComboBox()
        # The list follows [recording] composites, so a composite added to the ini can also be looked at,
        # not merely written to disk. Anything that is not three digits is ignored rather than offered.
        specs = [s.strip() for s in (self.cfg.get("recording", "composites", "") or "").split(",")
                 if len(s.strip()) == 3 and s.strip().isdigit()]
        self.view_sel.addItems([f"RGB {s}" for s in (specs or ["221"])]
                               + [f"{t['ch']} {i}" for i in range(1, 7)])
        self.view_sel.currentIndexChanged.connect(lambda _: self._refresh_view(force=True))
        bar.addWidget(self.view_sel)
        self.enh = {"colour": False, "infrared": False, "fill": True,
                    "resize": False, "coast": False, "cities": False,
                    "coast_colour": overlay.DEFAULT_COAST, "city_colour": overlay.DEFAULT_CITY}
        self.enh_dlg = None
        self._track_cache = (None, None)    # (imager id, (track, scan angle)) - SGP4 once per pass
        self._geo_warned = None   # the pass key already warned about, so each new pass warns once
        self._view_cost = 0.0     # seconds the last redraw took, used to space the automatic ones out
        self._last_view_t = 0.0
        b = QPushButton(t["enhance"])
        b.clicked.connect(self._open_enhance)
        bar.addWidget(b)
        self.chk_mirror = QPushButton(t["mirror"])
        self.chk_mirror.setCheckable(True)
        self.chk_mirror.setToolTip(t["mirror_tip"])
        self.chk_mirror.toggled.connect(lambda _: self._refresh_view(force=True))
        bar.addWidget(self.chk_mirror)
        b = QPushButton(t["fit"])
        b.clicked.connect(lambda: self.view.fit_width())
        bar.addWidget(b)
        b = QPushButton(t["cadu"])
        b.clicked.connect(self._open_cadu)
        bar.addWidget(b)
        b = QPushButton(t["open_folder"])
        b.clicked.connect(self._open_folder)
        bar.addWidget(b)
        rv.addLayout(bar)
        self.view = ImageView()
        rv.addWidget(self.view, 1)
        self.img_info = QLabel(t["no_image"])
        rv.addWidget(self.img_info)
        self.rsplit.addWidget(right)
        bottom = QWidget()
        bh = QHBoxLayout(bottom)
        bh.setContentsMargins(6, 0, 8, 4)

        # --- passes
        g = QGroupBox(t["passes"])
        pv = QVBoxLayout(g)
        pv.setContentsMargins(4, 4, 4, 4)
        self.next_lbl = QLabel("")
        pv.addWidget(self.next_lbl)
        self.tbl = QTableWidget(0, 5)
        self.tbl.setHorizontalHeaderLabels([t["col_sat"], t["col_aos"], t["col_el"], t["col_dir"], t["col_dur"]])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.verticalHeader().setDefaultSectionSize(20)
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl.setSelectionMode(QTableWidget.NoSelection)
        self.tbl.setMinimumHeight(60)
        pv.addWidget(self.tbl, 1)
        bh.addWidget(g, 1)

        # --- log
        g = QGroupBox(t["log"])
        gv = QVBoxLayout(g)
        gv.setContentsMargins(4, 4, 4, 4)
        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setMaximumBlockCount(500)
        self.logbox.setFont(QFont("Consolas", 8))
        gv.addWidget(self.logbox)
        bh.addWidget(g, 1)
        self.rsplit.addWidget(bottom)
        self.rsplit.setStretchFactor(0, 1)
        self.rsplit.setStretchFactor(1, 0)
        self.rsplit.setCollapsible(0, False)
        self.rsplit.setCollapsible(1, False)
        self.rsplit.setSizes([700, 190])
        split.addWidget(self.rsplit)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([460, 980])

        self.status = self.statusBar()
        self._dev_changed()

        m = self.menuBar().addMenu("TLE")
        act = QAction(t["reload_tle"], self)
        act.triggered.connect(lambda: self.engine.command("reload_tle"))
        m.addAction(act)
        act = QAction(t["import_tle"], self)
        act.triggered.connect(self._import_tle)
        m.addAction(act)
        m = self.menuBar().addMenu(t["drivers"])
        act = QAction(t["drv_diag"], self)
        act.triggered.connect(self._driver_diag)
        m.addAction(act)
        act = QAction(t["drv_zadig"], self)
        act.triggered.connect(self._zadig)
        m.addAction(act)
        m.addSeparator()
        mm = self.menuBar().addMenu(t["measures"])
        a2 = QAction(t["calib"], self)
        a2.triggered.connect(self._calibration)
        mm.addAction(a2)
        act = QAction(t["open_log"], self)
        act.triggered.connect(lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(os.path.join(self.cfg.data_dir, "meteorrx.log"))))
        m.addAction(act)

    # -- UI handlers --------------------------------------------------------------
    def _dev_changed(self):
        k = DEV_KEYS[self.dev.currentIndex()]
        for w in (self.rtl_gain, self.lbl_rtl_gain):
            w.setVisible(k == "rtlsdr")
        for w in (self.sp_grdb, self.sp_lna, self.lbl_grdb, self.lbl_lna, self.cb_ant, self.lbl_ant):
            w.setVisible(k == "sdrplay")
        for w in (self.sp_hlna, self.sp_hvga, self.lbl_hlna, self.lbl_hvga, self.chk_hamp):
            w.setVisible(k == "hackrf")
        for w in (self.sp_lgain, self.lbl_lgain, self.cb_lant, self.lbl_lant):
            w.setVisible(k == "lime")
        for w in (self.ed_puri, self.lbl_puri, self.sp_pgain, self.lbl_pgain):
            w.setVisible(k == "pluto")
        for w in (self.file_row, self.lbl_file):
            w.setVisible(k == "file")
        self.bias.setVisible(k in BIAS_KEYS)
        if k in BIAS_KEYS:
            self.bias.setChecked(self.cfg.getb("sdr", BIAS_KEYS[k], False))
        if hasattr(self, "_connected"):
            self._profile_status()

    def _browse(self):
        p, _ = QFileDialog.getOpenFileName(self, self.t["file"], os.path.dirname(self.file_edit.text()),
                                           "IQ (*.wav *.cu8 *.cs8 *.cs16 *.cf32 *.raw *.iq);;All (*.*)")
        if p:
            self.file_edit.setText(p)

    # -- receiver profiles -------------------------------------------------------
    def _pstore(self) -> profiles.ProfileStore:
        return profiles.ProfileStore(self.cfg.data_dir)

    def _busy(self) -> bool:
        return self.engine.snapshot().state in ("armed", "recording", "file", "saving")

    def _profiles_detect(self, startup: bool = False):
        """Identify the connected receivers, create profiles for new ones, select the active one."""
        try:
            found = sdr.detect_receivers(self.cfg) if not (not startup and self._busy()) else None
        except Exception as e:  # noqa: BLE001
            self.engine.log(f"Receiver detection failed: {e}")
            found = []
        if found is None:
            QMessageBox.information(self, "MeteorRX", self.t["busy_prof"])
            return
        self._connected = {d.key for d in found}
        st = self._pstore()
        file_mode = (self.cfg.get("sdr", "device", "rtlsdr") or "").lower() == "file"
        if not file_mode:
            self._sync_cfg()                             # keep what is on screen for the current profile
            st.update_settings(st.active_id, self.cfg)
        act, msgs, _new = profiles.reconcile(st, found, self.cfg)
        for m in msgs:
            self.engine.log(m)
        if not found:
            self.engine.log(self.t["no_rx"])
        if act is not None and not (startup and file_mode):
            profiles.ProfileStore.apply(act, self.cfg)
            self._save_ini()
            self._widgets_from_cfg()
            if found:
                self.engine.log(f"{self.t['prof_active']}: {act['name']}  [{act['identity']['key']}]")
        self._profiles_refresh()

    def _profiles_refresh(self):
        st = self._pstore()
        self.prof_cb.blockSignals(True)
        self.prof_cb.clear()
        for p in st.profiles:
            on = p["identity"]["key"] in self._connected
            self.prof_cb.addItem(("● " if on else "○ ") + p["name"], p["id"])
            self.prof_cb.setItemData(self.prof_cb.count() - 1,
                                     f"{p['identity']['key']}\n{self.t['connected'] if on else self.t['not_connected']}",
                                     Qt.ToolTipRole)
        i = self.prof_cb.findData(st.active_id)
        self.prof_cb.setCurrentIndex(max(i, 0))
        self.prof_cb.blockSignals(False)
        self._profile_status()

    def _profile_status(self):
        t = self.t
        st = self._pstore()
        p = st.active
        k = DEV_KEYS[self.dev.currentIndex()]
        if k == "file":
            self.prof_lbl.setText(t["file_mode"])
            self.chk_preamp.setVisible(False)
            return
        if p is None:
            self.prof_lbl.setText(t["no_rx"])
            self.chk_preamp.setVisible(False)
            return
        self._sync_cfg()
        on = p["identity"]["key"] in self._connected
        lines = [f"{p['identity']['kind'].upper()} · SN {p['identity'].get('serial') or '?'}"
                 + (f" · {p['identity']['tuner']}" if p["identity"].get("tuner") else "")
                 + f" · {'✔ ' + t['connected'] if on else '✖ ' + t['not_connected']}"]
        cs = calib.CalStore(os.path.join(self.cfg.data_dir, "calibration.json"))
        c = cs.get_for(self.cfg, p["id"]) if p["identity"]["kind"] == k else None
        if c is not None:
            nf = f", NF {c.nf_db:.1f} dB" if c.nf_db is not None else ""
            lines.append(("✔ " if c.ok else "⚠ ") + t["cal_ok"].format(date=c.date, k=c.k_db, nf=nf))
        else:
            lines.append("✖ " + t["cal_none"])
            others = sorted({x.key.split("#", 1)[-1].split("|")[1] for x in cs.for_profile(p["id"])})
            if others:
                lines.append("   " + t["cal_others"].format(lst=", ".join(others)))
        self.prof_lbl.setText("\n".join(lines))
        self.prof_lbl.setStyleSheet("font-size:11px;color:" + ("#9fb3a8" if on and c is not None else "#e0b040"))
        pa = p.get("preamp")
        self.chk_preamp.blockSignals(True)
        self.chk_preamp.setVisible(bool(pa))
        if pa:
            self.chk_preamp.setText(t["preamp_in"].format(name=pa.get("name", ""), g=float(pa.get("gain_db") or 0)))
            self.chk_preamp.setChecked(bool(pa.get("in_service")))
        self.chk_preamp.blockSignals(False)

    def _audio_toggled(self, on: bool):
        self.btn_audio.setText(self.t["listen"] if on else self.t["muted"])
        self.engine.audio.set(on, self.sl_vol.value() / 100.0)

    def _preamp_toggled(self, on: bool):
        st = self._pstore()
        st.set_preamp_in_service(st.active_id, on)
        self._profile_status()

    def _switch_profile(self, pid: str) -> bool:
        st = self._pstore()
        if pid == st.active_id:
            return True
        if self._busy():
            QMessageBox.information(self, "MeteorRX", self.t["busy_prof"])
            return False
        self._sync_cfg()
        st.update_settings(st.active_id, self.cfg)
        st.set_active(pid)
        p = st.get(pid)
        profiles.ProfileStore.apply(p, self.cfg)
        self._save_ini()
        self._widgets_from_cfg()
        self.engine.log(f"{self.t['prof_active']}: {p['name']}  [{p['identity']['key']}]")
        return True

    def _profile_picked(self, idx: int):
        pid = self.prof_cb.itemData(idx)
        if pid:
            self._switch_profile(pid)
        self._profiles_refresh()

    def _dev_picked(self, idx: int):
        """Device type chosen by hand: use the profile of a receiver of that type (connected first)."""
        k = DEV_KEYS[idx]
        st = self._pstore()
        act = st.active
        if k == "file" or (act and act["identity"]["kind"] == k):
            self._profile_status()
            return
        cands = [p for p in st.profiles if p["identity"]["kind"] == k]
        cands.sort(key=lambda p: p["identity"]["key"] not in self._connected)
        if cands:
            self._switch_profile(cands[0]["id"])
        self._profiles_refresh()

    def _profile_rename(self):
        st = self._pstore()
        p = st.active
        if not p:
            return
        name, ok = QInputDialog.getText(self, "MeteorRX", self.t["rename_q"], text=p["name"])
        if ok and name.strip():
            st.rename(p["id"], name)
            self._profiles_refresh()

    def _profile_delete(self):
        st = self._pstore()
        p = st.active
        if not p or self._busy():
            return
        if QMessageBox.question(self, "MeteorRX", self.t["delete_q"].format(name=p["name"])) == QMessageBox.Yes:
            st.delete(p["id"])
            if st.active:
                profiles.ProfileStore.apply(st.active, self.cfg)
                self._save_ini()
                self._widgets_from_cfg()
            self._profiles_refresh()

    def _widgets_from_cfg(self):
        c = self.cfg
        cur = (c.get("sdr", "device", "rtlsdr") or "rtlsdr").lower()
        ws = (self.dev, self.rtl_gain, self.sp_grdb, self.sp_lna, self.sp_hlna, self.sp_hvga, self.sp_lgain, self.sp_pgain)
        for w in ws:
            w.blockSignals(True)
        self.dev.setCurrentIndex(DEV_KEYS.index(cur) if cur in DEV_KEYS else 0)
        try:
            self.rtl_gain.setValue(float(c.get("sdr", "rtl_gain_db", "38.6")))
        except ValueError:
            pass
        self.sp_grdb.setValue(c.geti("sdr", "sdrplay_grdb", 40))
        self.sp_lna.setValue(c.geti("sdr", "sdrplay_lna_state", 3))
        self.cb_ant.setCurrentText((c.get("sdr", "sdrplay_antenna", "A") or "A").upper()[:1])
        self.sp_hlna.setValue(c.geti("sdr", "hackrf_lna_gain", 24))
        self.sp_hvga.setValue(c.geti("sdr", "hackrf_vga_gain", 30))
        self.chk_hamp.setChecked(c.getb("sdr", "hackrf_amp", False))
        self.sp_lgain.setValue(c.geti("sdr", "lime_gain_db", 40))
        self._set_lant(c.get("sdr", "lime_antenna", "auto"))
        self.ed_puri.setText(c.get("sdr", "pluto_uri", "ip:192.168.2.1") or "ip:192.168.2.1")
        try:
            self.sp_pgain.setValue(int(float(c.get("sdr", "pluto_gain_db", "50"))))
        except ValueError:
            pass
        for w in ws:
            w.blockSignals(False)
        self._dev_changed()

    def _set_lant(self, v):
        v = (v or "auto").strip()
        self.cb_lant.setCurrentText(next((a for a in LIME_ANT if a.lower() == v.lower()), "auto"))

    def _save_ini(self):
        try:
            self.cfg.save()
        except OSError as e:
            self.logq.put(f"INI save failed: {e}")

    def _sync_cfg(self):
        """Screen values -> cfg (memory only)."""
        c = self.cfg
        k = DEV_KEYS[self.dev.currentIndex()]
        c.set("sdr", "device", k)
        c.set("sdr", "rtl_gain_db", f"{self.rtl_gain.value():.1f}")
        c.set("sdr", "sdrplay_grdb", self.sp_grdb.value())
        c.set("sdr", "sdrplay_lna_state", self.sp_lna.value())
        c.set("sdr", "sdrplay_antenna", self.cb_ant.currentText())
        c.set("sdr", "hackrf_lna_gain", self.sp_hlna.value() // 8 * 8)
        c.set("sdr", "hackrf_vga_gain", self.sp_hvga.value() // 2 * 2)
        c.set("sdr", "hackrf_amp", "yes" if self.chk_hamp.isChecked() else "no")
        c.set("sdr", "lime_gain_db", self.sp_lgain.value())
        c.set("sdr", "lime_antenna", self.cb_lant.currentText())
        c.set("sdr", "pluto_uri", self.ed_puri.text().strip() or "ip:192.168.2.1")
        c.set("sdr", "pluto_gain_db", self.sp_pgain.value())
        if k in BIAS_KEYS:
            c.set("sdr", BIAS_KEYS[k], "yes" if self.bias.isChecked() else "no")

    def _apply_settings(self):
        c = self.cfg
        self._sync_cfg()
        st = self._pstore()
        st.update_settings(st.active_id, c)
        c.set("sdr", "file_path", self.file_edit.text())
        c.set("decoder", "mode", self.mode.currentText())
        c.set("gui", "auto_start", "yes" if self.chk_auto.isChecked() else "no")
        c.set("gui", "audio_volume", self.sl_vol.value())
        try:
            c.save()
        except OSError as e:
            self.logq.put(f"INI save failed: {e}")

    def _start(self):
        self._apply_settings()
        idx = self.sat.currentIndex()
        sat = self.cfg.satellites[idx].name if idx < len(self.cfg.satellites) else None
        self.engine.command("start", sat=sat, mode=self.mode.currentText())

    def _calibration(self):
        snap = self.engine.snapshot()
        if snap.state in ("armed", "recording", "file"):
            QMessageBox.information(self, "MeteorRX", self.t["busy_cal"])
            return
        self._apply_settings()
        was_auto = self.chk_auto.isChecked()
        self.engine.command("auto", enabled=False)       # no pass may grab the receiver meanwhile
        try:
            from .caldialog import CalibrationDialog
            CalibrationDialog(self.cfg, self.lang, self, profile_id=self._pstore().active_id).exec()
        finally:
            self.engine.command("auto", enabled=was_auto)
            self._profile_status()

    def _driver_diag(self):
        from . import drivers
        rep = drivers.select_library((self.cfg.get("sdr", "rtl_dll_dir", "") or "").strip())
        box = QMessageBox(self)
        box.setWindowTitle("MeteorRX – RTL-SDR")
        box.setText(self.t["drv_diag"].rstrip("…"))
        box.setDetailedText("\n".join(rep.lines))
        box.setInformativeText(
            ("✔ " + rep.chosen.path) if rep.chosen else "✖ no usable rtlsdr.dll")
        if rep.usb_problem:
            b = box.addButton(self.t["drv_zadig"].rstrip("…"), QMessageBox.ActionRole)
            b.clicked.connect(self._zadig)
        box.addButton(QMessageBox.Close)
        for line in rep.lines:
            self.logq.put("   " + line)
        box.exec()

    def _zadig(self):
        from . import drivers
        err = drivers.launch_zadig()
        if err:
            QMessageBox.warning(self, "Zadig", err)

    def _import_tle(self):
        p, _ = QFileDialog.getOpenFileName(self, self.t["import_tle"], os.path.expanduser("~"),
                                           "TLE (*.txt *.tle *.3le);;All (*.*)")
        if p:
            self.engine.command("import_tle", path=p)

    def _open_folder(self):
        snap = self.engine.snapshot()
        d = snap.out_folder if snap.out_folder and os.path.isdir(snap.out_folder) else self.cfg.output_dir
        os.makedirs(d, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(d))

    def _open_cadu(self):
        p, _ = QFileDialog.getOpenFileName(self, ".cadu", self.cfg.output_dir, "CADU (*.cadu);;All (*.*)")
        if not p:
            return
        im = lrpt.MsuMrImager()

        def work():
            n = lrpt.process_cadu_file(p, im)
            self.logq.put(f"{os.path.basename(p)}: {n} CADU, {im.packets} MSU-MR packets, "
                          f"channels {[c + 1 for c in im.channels_present]}")
            folder = outputs.unique_path(os.path.splitext(p)[0] + "_products")   # re-decoding never overwrites
            files = imaging.save_products(im, folder, os.path.basename(os.path.splitext(p)[0]),
                                          ["221", "123"], True, False, True)
            self.logq.put(f"Saved {len(files)} image(s) in {folder}")
            self.engine.last_imager = im
            self._force_view = True

        threading.Thread(target=work, daemon=True).start()

    # -- periodic refresh ------------------------------------------------------
    def _tick(self):
        self._tickn += 1
        while True:
            try:
                self.logbox.appendPlainText(self.logq.get_nowait())
            except queue.Empty:
                break
        snap = self.engine.snapshot()
        t = self.t
        st = snap.state
        if self.btn_audio.isChecked() and not self.engine.audio.enabled and self.engine.audio.error:
            self.btn_audio.setChecked(False)          # no sound device: back to muted, reason in the log
        if self.engine.usb_driver_alert:
            self.engine.usb_driver_alert = False
            if os.name == "nt" and QMessageBox.question(self, "MeteorRX – RTL-SDR", t["drv_alert"]) \
                    == QMessageBox.Yes:
                self._zadig()
        txt = t.get(f"state_{st}", st)
        if snap.sat and st in ("armed", "recording", "file"):
            txt += f" – {snap.sat}"
        col = {"recording": "#ff5a5a", "armed": "#e0b040", "file": "#60b0ff"}.get(st, "#9fb3a8")
        self.state_lbl.setText(txt)
        self.state_lbl.setStyleSheet(f"font-size:14px;font-weight:700;padding:4px;color:{col}")
        self.btn_start.setEnabled(st in ("idle", "waiting"))
        self.btn_stop.setEnabled(st in ("armed", "recording", "file"))
        if snap.file_progress is not None and st == "file":
            self.prog.setVisible(True)
            self.prog.setValue(int(snap.file_progress * 100))
        else:
            self.prog.setVisible(False)

        active = st in ("armed", "recording", "file")
        self.led_sig.set(active and snap.signal)
        self.led_vit.set(active and snap.vit_locked)
        self.led_frm.set(active and snap.frame_lock and snap.frames_ok > 0)
        self.led_img.set(active and snap.packets > 0)
        self.led_rec.set(snap.recording, warn=False)
        if active:
            self.smeter.set_level(snap.level_dbfs, snap.n0_dbfs_hz, snap.cal_k)
            self.eye.update_traces(snap.eye)
            self.const.update_syms(snap.syms)
            self.spec.update_spec(snap.spectrum)
            el = f"{snap.elevation:5.1f}°" if snap.elevation is not None else "  –  "
            self.vals.setText(
                f"{t['snr']:<13}{snap.snr_db:6.1f} dB\n"
                f"{t['ber']:<13}{snap.vit_ber:7.3f}\n"
                f"{t['frames']:<13}{snap.frames_ok}/{snap.frames_bad}\n"
                f"{t['packets']:<13}{snap.packets}\n"
                f"{'Mode':<13}{snap.lrpt_mode}\n"
                f"{t['stream']:<13}" + ("  –" if snap.stream_ratio is None else
                                        f"{100 * snap.stream_ratio:5.1f} %" + ("  ⚠" if snap.stream_ratio < 0.95
                                                                               or snap.stream_dropped else "")))
            self.vals2.setText(
                f"{t['freq']:<10}{snap.freq_hz / 1e6:.3f} MHz\n"
                f"{t['dopp']:<10}{snap.doppler_hz:+7.0f} Hz\n"
                f"{t['loop']:<10}{snap.loop_hz:+7.0f} Hz\n"
                f"{t['elev']:<10}{el}\n"
                f"{t['rec']:<10}{int(snap.rec_seconds) // 60:02d}:{int(snap.rec_seconds) % 60:02d}")
        elif self._tickn % 10 == 0:
            self.eye.update_traces(None)
            self.const.update_syms(None)

        if self._tickn % 10 == 1:
            self._update_passes(snap)
            now = datetime.now(timezone.utc)
            self.status.showMessage(
                f"{self.cfg.get('station', 'callsign', '')}  {self.cfg.getf('station', 'latitude'):.3f}, "
                f"{self.cfg.getf('station', 'longitude'):.3f}   |   UTC {now.strftime('%H:%M:%S')}   |   "
                f"{t['tle']} ({t['tle_age']}): {snap.tle_info}   |   {snap.source or ''}   |   {self.cfg.output_dir}")
        if snap.preview_rev != self._preview_rev or getattr(self, "_force_view", False):
            # Drawing the map costs about a second on a full pass, and the preview offers a new picture
            # every two seconds, so redrawing on every one of them would hold the Qt thread for half the
            # pass - on a slower machine, longer than the interval itself, and the receiver is sharing
            # that machine. Space the automatic redraws out in proportion to what the last one cost, so
            # the window stays responsive and the decoder keeps its CPU. Anything the operator does
            # (a checkbox, the view selector, Save) still redraws at once, through _refresh_view.
            if self._auto_redraw_due() or getattr(self, "_force_view", False):
                self._preview_rev = snap.preview_rev
                self._force_view = False
                self._refresh_view(force=True)

    def _update_passes(self, snap):
        t = self.t
        ps = snap.passes or []
        self.tbl.setRowCount(len(ps))
        for i, p in enumerate(ps):
            loc = p.aos.astimezone()
            vals = [p.sat.name, loc.strftime("%a %H:%M"), f"{p.max_el:.0f}°",
                    t["north"] if p.northbound else t["south"], f"{p.duration_s / 60:.0f} min"]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if p.max_el >= 40:
                    it.setForeground(ACCENT)
                self.tbl.setItem(i, j, it)
        if ps:
            dt = (ps[0].aos - datetime.now(timezone.utc)).total_seconds()
            if dt > 0:
                h, m = int(dt // 3600), int(dt % 3600 // 60)
                self.next_lbl.setText(f"{t['next_in']} {h} h {m:02d} min – {ps[0].sat.name}, "
                                      f"{ps[0].max_el:.0f}°")
            else:
                self.next_lbl.setText(f"{ps[0].sat.name} – AOS")
        else:
            self.next_lbl.setText("–")

    def _auto_redraw_due(self) -> bool:
        """Is it time to redraw the live preview by itself?

        Spaced out in proportion to what the last redraw cost, so an expensive one (the map overlay on
        a full pass is around a second) cannot hold the Qt thread for most of the pass on the same
        machine the receiver is running on. A cheap redraw is never delayed; a very slow one is capped
        so the picture still moves. Operator actions bypass this entirely."""
        return time.monotonic() - self._last_view_t >= min(20.0, 4.0 * self._view_cost)

    def _refresh_view(self, force=False):
        _t0 = time.monotonic()
        im = self.engine.last_imager
        if im is None:
            return
        sel = self.view_sel.currentText()
        chs = imaging.channel_images(im, fill=self.enh["fill"])
        if not chs:
            return
        img = None
        try:                                    # the selector is rebuilt as channels arrive: never trust it
            if sel.startswith("RGB"):
                img = imaging.composite(chs, sel.split()[1])
            else:
                img = chs.get(int(sel.split()[-1]))
        except (ValueError, IndexError):
            img = None
        label = sel
        if img is None:
            k = sorted(chs)[0]
            img, label = chs[k], f"{self.t['ch']} {k}"
        img = enhance.apply(img, self.enh["colour"], self.enh["infrared"])
        marks = [self.t[v].split(" (")[0].lower()           # "fill" is the normal default: not worth a mention
                 for k, v in (("colour", "enh_colour"), ("infrared", "enh_ir")) if self.enh[k]]
        # same orientation as the saved products: a northbound pass arrives south-first, so without the
        # half turn the picture - and any map drawn on it - would be upside down
        rotated = bool((self.engine.last_pass or {}).get("rotated"))
        if rotated:
            img = img[::-1, ::-1]
        src_w = img.shape[1]
        if self.enh["resize"]:
            img = geometry.correct(img)
            marks.append(self.t["enh_resize"].split(" (")[0].lower())
        mirrored = self.chk_mirror.isChecked()
        if mirrored:                         # display only: the saved pass images are never touched
            img = img[:, ::-1]
            marks.append(self.t["mirror"].lower())
        if self.enh["coast"] or self.enh["cities"]:
            img, drew = self._with_overlay(img, src_w, corrected=self.enh["resize"],
                                           rotated=rotated, mirror=mirrored)
            # Only claim an overlay that is really there. Saying "coastlines" over a bare picture sent
            # an OM looking for a bug in the save, when the pass simply could not be placed.
            marks += ([self.t[v].split(" (")[0].lower()
                       for k, v in (("coast", "enh_coast"), ("cities", "enh_cities")) if self.enh[k]]
                      if drew else [self.t["enh_geo_mark"]])
        if marks:
            label += " · " + ", ".join(marks)
        self._view_cost = time.monotonic() - _t0
        self._last_view_t = time.monotonic()
        self._shown = img
        self.view.show_array(img)
        self.img_info.setText(f"{label}  –  {img.shape[1]}×{img.shape[0]} px  –  "
                              f"{self.t['ch']}: {', '.join(str(k) for k in sorted(chs))}  –  {im.packets} packets")

    def _with_overlay(self, img, src_w, *, corrected, rotated, mirror):
        """Draw the map overlays, returning (picture, drawn?).

        The flag matters: when the pass cannot be placed the caller must not label the picture as
        carrying coastlines, and the saved copy must not be expected to show any."""
        im = self.engine.last_imager
        lp = self.engine.last_pass or {}
        chans = im.channels_present if im is not None else []
        ch = chans[0] if chans else None
        # The imager object lives for the whole pass and grows as frames arrive, so caching on its
        # identity alone would freeze the track at the first few rows and leave the rest of the image
        # without an overlay. Key on what the track actually depends on.
        extent = im._extent(ch) if ch is not None else None
        key = (id(im), ch, extent, tuple(lp.get("tle") or ()))
        if self._track_cache[0] != key:
            tr = None
            if ch is not None:
                tr = overlay.track_for(lp.get("satellite"), lp.get("tle"), im.row_times(ch))
            self._track_cache = (key, tr)
        tr = self._track_cache[1]
        if tr is None:
            # Once per pass, not once per run: the next pass deserves its own warning, and a user who
            # has scrolled the log past the first one is otherwise never told again. Keyed without the
            # extent, which grows with every strip - including it logged the warning on every refresh,
            # roughly every two seconds, for the whole pass.
            warn_key = (id(im), tuple(lp.get("tle") or ()))
            if self._geo_warned != warn_key:
                self._geo_warned = warn_key
                self.engine.log(self.t["enh_geo_none"])
            return img, False
        track, scan_deg = tr
        try:
            return overlay.draw(img, track, scan_deg, src_w, corrected=corrected, rotated=rotated,
                                mirror=mirror, coastlines=self.enh["coast"], cities=self.enh["cities"],
                                min_pop=self.cfg.geti("recording", "city_min_population", 200000),
                                coast_colour=self.enh["coast_colour"],
                                city_colour=self.enh["city_colour"]), True
        except Exception as e:  # noqa: BLE001  (an overlay must never cost the user the picture)
            self.engine.log(f"Overlay: {e}")
            return img, False

    def _open_enhance(self):
        """Small panel of image enhancements, applied to the view only (never to the saved files)."""
        from PySide6.QtWidgets import QDialog
        if self.enh_dlg is not None and self.enh_dlg.isVisible():
            self.enh_dlg.raise_()
            return
        t = self.t
        d = QDialog(self)
        d.setWindowTitle("MeteorRX - " + t["enh_title"])
        v = QVBoxLayout(d)
        for key, lab in (("resize", t["enh_resize"]), ("colour", t["enh_colour"]), ("infrared", t["enh_ir"]),
                         ("fill", t["enh_fill"]), ("coast", t["enh_coast"]), ("cities", t["enh_cities"])):
            cb = QCheckBox(lab)
            cb.setChecked(self.enh[key])
            cb.toggled.connect(lambda on, k=key: (self.enh.__setitem__(k, bool(on)),
                                                  self._refresh_view(force=True)))
            v.addWidget(cb)
        for key, lab in (("coast_colour", t["enh_coast_col"]), ("city_colour", t["enh_city_col"])):
            row = QHBoxLayout()
            row.addWidget(QLabel(lab))
            cbx = QComboBox()
            for name, rgb in overlay.PALETTE.items():
                cbx.addItem(t.get(f"col_{name}", name.title()), name)
                cbx.setItemData(cbx.count() - 1, QColor(*rgb), Qt.DecorationRole)   # a swatch, not just a word
            cbx.setCurrentIndex(max(0, cbx.findData(self.enh[key])))
            cbx.currentIndexChanged.connect(
                lambda _, c=cbx, k=key: (self.enh.__setitem__(k, c.currentData()),
                                         self._refresh_view(force=True)))
            row.addWidget(cbx, 1)
            v.addLayout(row)
        note = QLabel(t["enh_note"])
        note.setWordWrap(True)
        note.setStyleSheet("color:#9bb0a4")
        v.addWidget(note)
        credit = QLabel(t["enh_credit"])      # GeoNames is CC BY 4.0: the attribution has to be shown
        credit.setWordWrap(True)
        credit.setStyleSheet("color:#6f8378; font-size:10px")
        v.addWidget(credit)
        h = QHBoxLayout()
        bs = QPushButton(t["enh_save"])
        bs.clicked.connect(self._save_enhanced)
        h.addWidget(bs)
        bc = QPushButton(t["enh_close"])
        bc.clicked.connect(d.close)
        h.addWidget(bc)
        v.addLayout(h)
        self.enh_dlg = d
        d.show()

    def _save_enhanced(self):
        """Write the picture exactly as displayed, next to the pass it came from.

        The name is built from the pass prefix, so it carries the pass index and sorts with the rest of
        the session, and `unique_path` numbers each further save: ..._enhanced.png, ..._enhanced_2.png.
        The decoded images and the CADU recording are never touched."""
        img = getattr(self, "_shown", None)
        if img is None:
            return
        lp = self.engine.last_pass or {}
        folder, prefix = lp.get("folder"), lp.get("prefix")
        if not folder or not prefix:        # nothing saved for this pass yet: let the user place it
            p, _ = QFileDialog.getSaveFileName(self, self.t["enh_save"], "meteorrx_enhanced.png", "PNG (*.png)")
            if not p:
                return
            path = p if p.lower().endswith(".png") else p + ".png"
        else:
            path = outputs.unique_path(os.path.join(folder, f"{prefix}_enhanced.png"))
        try:
            imaging.write_png(path, np.ascontiguousarray(img))
            self.engine.log(f"{self.t['enh_save'].rstrip('… .')}: {os.path.basename(path)}")
        except OSError as e:
            QMessageBox.warning(self, "MeteorRX", str(e))

    def closeEvent(self, e):
        snap = self.engine.snapshot()
        if snap.state in ("armed", "recording", "file"):
            if QMessageBox.question(self, "MeteorRX", self.t["quit_q"]) != QMessageBox.Yes:
                e.ignore()
                return
        self._apply_settings()
        self._save_window_state()
        self.engine.quit()
        e.accept()


def apply_dark(app: QApplication):
    app.setStyle("Fusion")
    p = QPalette()
    base, alt, text = QColor(24, 28, 30), QColor(34, 39, 42), QColor(220, 228, 224)
    p.setColor(QPalette.Window, base)
    p.setColor(QPalette.WindowText, text)
    p.setColor(QPalette.Base, QColor(18, 21, 23))
    p.setColor(QPalette.AlternateBase, alt)
    p.setColor(QPalette.Text, text)
    p.setColor(QPalette.Button, alt)
    p.setColor(QPalette.ButtonText, text)
    p.setColor(QPalette.Highlight, QColor(40, 140, 90))
    p.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    p.setColor(QPalette.ToolTipBase, alt)
    p.setColor(QPalette.ToolTipText, text)
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(110, 118, 114))
    p.setColor(QPalette.Disabled, QPalette.Text, QColor(110, 118, 114))
    app.setPalette(p)
    app.setStyleSheet("QGroupBox{font-weight:600;border:1px solid #3a4440;border-radius:6px;margin-top:10px;"
                      "padding-top:8px} QGroupBox::title{subcontrol-origin:margin;left:8px;padding:0 4px;"
                      "color:#8fe0b0}")


def _warm_dsp(app: QApplication, lang: str):
    """Compile/load the Numba DSP kernels, with a small notice when it takes time (first start)."""
    from . import dsp
    res = {}
    th = threading.Thread(target=lambda: res.update(t=dsp.warmup()), daemon=True)
    th.start()
    th.join(1.0)
    if not th.is_alive():
        return
    msg = i18n.texts(lang, "gui")["warmup"]
    lbl = QLabel(f"  MeteorRX {__version__}\n\n  {msg}  ")
    lbl.setWindowFlags(Qt.SplashScreen | Qt.WindowStaysOnTopHint)
    lbl.setStyleSheet("QLabel{background:#18201c;color:#cfe8d8;font-size:13px;padding:18px;"
                      "border:1px solid #3ddc84}")
    lbl.show()
    while th.is_alive():
        app.processEvents()
        th.join(0.05)
    lbl.close()


def main(argv=None):
    cfg = config.load()
    if os.name == "nt":
        try:  # own taskbar entry and icon instead of "Python"
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MARL.MeteorRX")
        except Exception:  # noqa: BLE001
            pass
    app = QApplication(argv or sys.argv)
    app.setApplicationName("MeteorRX")
    ico = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "meteorrx.png")
    if os.path.isfile(ico):
        from PySide6.QtGui import QIcon
        app.setWindowIcon(QIcon(ico))
    apply_dark(app)
    _warm_dsp(app, i18n.normalize(cfg.get("gui", "language", "fr")))
    w = MainWindow(cfg)
    w.show_initial()
    auto = os.environ.get("METEORRX_AUTOTEST")   # unattended test: start, run N s, screenshot, quit
    if auto:
        secs = float(auto)
        QTimer.singleShot(1500, w._start)
        QTimer.singleShot(int(secs * 1000), lambda: (
            w.grab().save(os.path.join(cfg.data_dir, "autotest.png")), w.engine.command("stop")))
        QTimer.singleShot(int(secs * 1000) + 4000, lambda: (
            w.engine.quit(), app.exit(0)))
    return app.exec()

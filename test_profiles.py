"""Receiver profiles: identity, per-receiver settings / calibrations, migration, selection by serial number."""
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from meteorrx import calib, config, profiles, sdr  # noqa: E402
from meteorrx.profiles import DeviceId, ProfileStore  # noqa: E402

V4 = DeviceId("rtlsdr", "RTLSDRBlog", "Blog V4", "00000001")
V3 = DeviceId("rtlsdr", "Realtek", "RTL2838UHIDIR", "SN-V3-2")
RSP = DeviceId("sdrplay", "SDRplay", "RSP1A", "1907AB1234")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    ini = tmp_path / "m.ini"
    ini.write_text(config.DEFAULT_INI)
    c = config.load(str(ini))
    monkeypatch.setattr(type(c), "data_dir", property(lambda self: str(tmp_path / "data")))
    return c


def fake_rtl(devs, opened):
    """librtlsdr stand-in: plain functions (ctypes attributes can be assigned on them)."""
    def f(fn):
        return fn
    L = types.SimpleNamespace()
    L.rtlsdr_get_device_count = f(lambda: len(devs))
    L.rtlsdr_get_device_name = f(lambda i: b"Generic RTL2832U OEM")

    def usb_strings(i, m, p, s):
        m.value, p.value, s.value = (x.encode() for x in devs[i])
        return 0
    L.rtlsdr_get_device_usb_strings = f(usb_strings)

    def by_serial(sn):
        sn = sn.decode()
        return next((i for i, d in enumerate(devs) if d[2] == sn), -3)
    L.rtlsdr_get_index_by_serial = f(by_serial)

    def op(pdev, i):
        opened.append(i)
        return 0
    L.rtlsdr_open = f(op)
    L.rtlsdr_get_tuner_type = f(lambda d: 6)
    for name in ("close", "set_center_freq", "set_sample_rate", "set_tuner_gain_mode", "get_tuner_gains",
                 "set_tuner_gain", "set_agc_mode", "reset_buffer", "read_sync", "set_bias_tee"):
        setattr(L, "rtlsdr_" + name, f(lambda *a: 0))
    return L


def test_identity_keys_and_labels():
    assert V4.key == "rtlsdr|RTLSDRBlog|Blog V4|00000001"
    assert RSP.key == "sdrplay|RSP1A|1907AB1234"
    assert "Blog V4" in V4.label and V4.generic_serial and not V3.generic_serial


def test_new_receivers_get_profiles_with_current_settings(cfg):
    cfg.set("sdr", "rtl_gain_db", "29.7")
    st = ProfileStore(cfg.data_dir)
    act, msgs, new = profiles.reconcile(st, [V4], cfg)
    assert act["identity"]["key"] == V4.key and act["settings"]["rtl_gain_db"] == "29.7"
    assert new == [V4] and any("rtl_eeprom" in m for m in msgs)       # generic serial noted
    # persisted, and found again on the next start without creating a duplicate
    st2 = ProfileStore(cfg.data_dir)
    act2, msgs2, new2 = profiles.reconcile(st2, [V4], cfg)
    assert act2["id"] == act["id"] and not new2 and len(st2.profiles) == 1


def test_switch_to_connected_receiver_and_keep_each_settings(cfg):
    st = ProfileStore(cfg.data_dir)
    profiles.reconcile(st, [V4, RSP], cfg)
    p4, prsp = st.find(V4), st.find(RSP)
    assert st.active_id == p4["id"]
    ProfileStore.apply(p4, cfg)
    cfg.set("sdr", "rtl_gain_db", "40.2")
    cfg.set("sdr", "ppm", "1.30")
    st.update_settings(p4["id"], cfg)
    st.set_active(prsp["id"])
    ProfileStore.apply(prsp, cfg)
    assert cfg.get("sdr", "device") == "sdrplay" and cfg.get("sdr", "sdrplay_serial") == RSP.serial
    cfg.set("sdr", "sdrplay_grdb", "35")
    st.update_settings(prsp["id"], cfg)
    st.update_settings(p4["id"], cfg)                  # device type differs: RTL profile untouched
    # only the RSP is plugged in at next start: it is selected; the V4 keeps its own values
    st = ProfileStore(cfg.data_dir)
    st.set_active(p4["id"])
    act, msgs, _ = profiles.reconcile(st, [RSP], cfg)
    assert act["id"] == prsp["id"] and any("switching" in m for m in msgs)
    v4 = st.get(p4["id"])["settings"]
    assert v4["rtl_gain_db"] == "40.2" and v4["ppm"] == "1.30" and v4["device"] == "rtlsdr"
    ProfileStore.apply(st.get(p4["id"]), cfg)
    assert cfg.get("sdr", "rtl_gain_db") == "40.2" and cfg.get("sdr", "rtl_serial") == V4.serial


def test_identical_dongles_are_reported(cfg):
    st = ProfileStore(cfg.data_dir)
    _, msgs, _ = profiles.reconcile(st, [V4, DeviceId("rtlsdr", "RTLSDRBlog", "Blog V4", "00000001", index=1)], cfg)
    assert any("same identity" in m for m in msgs) and len(st.profiles) == 1


def test_not_connected_is_reported(cfg):
    st = ProfileStore(cfg.data_dir)
    profiles.reconcile(st, [V4], cfg)
    act, msgs, _ = profiles.reconcile(st, [], cfg)
    assert act is not None and any("not connected" in m for m in msgs)


def test_calibrations_are_per_receiver_and_legacy_is_adopted(cfg):
    path = os.path.join(cfg.data_dir, "calibration.json")
    os.makedirs(cfg.data_dir, exist_ok=True)
    legacy = calib.settings_key(cfg)
    cal = calib.Calibration(key=legacy, k_db=-38.2, slope=1.0, residuals_db=[0, 0], points=[], nf_db=6.1,
                            date="2026-10-26 18:00")
    with open(path, "w") as f:
        json.dump({"calibrations": {legacy: cal.__dict__}, "preamps": []}, f)
    st = ProfileStore(cfg.data_dir)
    profiles.reconcile(st, [V4, V3], cfg)
    p4, p3 = st.find(V4), st.find(V3)
    cs = calib.CalStore(path)
    c = cs.get_for(cfg, p4["id"])                       # adopted by the first receiver that asks
    assert c is not None and c.k_db == -38.2 and c.key == f"{p4['id']}#{legacy}"
    assert calib.CalStore(path).get_for(cfg, p3["id"]) is None     # not shared with another dongle
    assert legacy not in json.load(open(path))["calibrations"]
    assert [x.k_db for x in calib.CalStore(path).for_profile(p4["id"])] == [-38.2]


def test_rtl_enumeration_and_selection_by_serial(monkeypatch):
    devs = [("Realtek", "RTL2838UHIDIR", "SN-V3-2"), ("RTLSDRBlog", "Blog V4", "00000001")]
    opened = []
    L = fake_rtl(devs, opened)
    rep = types.SimpleNamespace(usb_problem=False, dongles=[1])
    monkeypatch.setattr(sdr, "load_rtlsdr", lambda d="": (L, "fake.dll", rep))
    found = sdr.enumerate_rtl()
    assert [d.key for d in found] == ["rtlsdr|Realtek|RTL2838UHIDIR|SN-V3-2", V4.key]
    s = sdr.RtlSdrSource(0, 1.2e6, 137.5e6, "38.6", serial="00000001")
    assert opened == [1] and s.identity.key == V4.key and s.identity.tuner == "R828D" and not s.warning
    s = sdr.RtlSdrSource(0, 1.2e6, 137.5e6, "38.6", serial="GONE")      # unplugged: keep receiving
    assert opened[-1] == 0 and "GONE" in s.warning


def test_session_uses_calibration_of_the_receiver_in_use(cfg, monkeypatch):
    from meteorrx import engine
    st = ProfileStore(cfg.data_dir)
    profiles.reconcile(st, [V4, V3], cfg)
    p4, p3 = st.find(V4), st.find(V3)
    st.set_active(p4["id"])
    cs = calib.CalStore(os.path.join(cfg.data_dir, "calibration.json"))
    for p, k in ((p4, -38.0), (p3, -41.0)):
        cs.put(calib.Calibration(key=calib.cal_key(cfg, p["id"]), k_db=k, slope=1.0, residuals_db=[], points=[]))
    st.set_preamp(p4["id"], {"name": "LNA4ALL", "gain_db": 20.0, "in_service": True})

    class Src(sdr.Source):
        sample_rate, center_hz, warning = 1.2e6, 137.5e6, ""

        def __init__(self, ident):
            self.identity = ident

        def read(self, n):
            return None
    logs = []
    eng = types.SimpleNamespace(cfg=cfg, log=logs.append, observer=None)
    for ident, want in ((V4, -58.0), (V3, -41.0), (DeviceId("rtlsdr", "X", "Y", "Z"), None)):
        monkeypatch.setattr(sdr, "open_source", lambda c, i=ident: Src(i))
        s = engine.Session(eng, None, None, "72k", manual=True)
        assert s.cal_k == want
    assert any("preamplifier input" in m for m in logs) and any("no profile" in m for m in logs)


def test_calibration_dialog_remembers_parameters_and_preamp(cfg, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    from meteorrx import caldialog
    _app = QApplication.instance() or QApplication([])  # noqa: F841
    st = ProfileStore(cfg.data_dir)
    profiles.reconcile(st, [V4], cfg)
    pid = st.active_id
    d = caldialog.CalibrationDialog(cfg, "fr", profile_id=pid)
    d.loss.setValue(6.5)
    d.lv[3].setValue(-113.0)
    d.pname.setText("LNA4ALL 137")
    d.accept()
    d2 = caldialog.CalibrationDialog(cfg, "en", profile_id=pid)
    assert d2.loss.value() == 6.5 and d2.lv[3].value() == -113.0 and d2.pname.text() == "LNA4ALL 137"
    assert d2.key.startswith(pid + "#")
    # preamp result -> put in service on this receiver
    d2.cal = calib.Calibration(key=d2.key, k_db=-38.0, slope=1.0, residuals_db=[], points=[], nf_db=6.0)
    m = types.SimpleNamespace(power_dbfs=-110.0 + 20.0 + 38.0, warning="")      # 20 dB preamp, K = -38
    d2.pre["gain"] = (-110.0, m)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    d2._preamp()
    pa = ProfileStore(cfg.data_dir).get(pid)["preamp"]
    assert pa["name"] == "LNA4ALL 137" and pa["in_service"] and abs(pa["gain_db"] - 20.0) < 0.01
    d2.reject()
    assert ProfileStore(cfg.data_dir).get(pid)["preamp"]["in_service"]      # not lost by the params save


def test_main_window_profiles(cfg, monkeypatch, tmp_path):
    """Start with two known receivers, only the RSP plugged in: it is selected, its settings shown."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from meteorrx import gui
    app = QApplication.instance() or QApplication([])
    cfg.set("recording", "output_dir", str(tmp_path / "out"))
    st = ProfileStore(cfg.data_dir)
    profiles.reconcile(st, [V4, RSP], cfg)
    p4, prsp = st.find(V4), st.find(RSP)
    p4["settings"]["rtl_gain_db"] = "33.8"
    prsp["settings"]["sdrplay_grdb"] = "44"
    st.set_active(p4["id"])
    cs = calib.CalStore(os.path.join(cfg.data_dir, "calibration.json"))
    ProfileStore.apply(prsp, cfg)
    cs.put(calib.Calibration(key=calib.cal_key(cfg, prsp["id"]), k_db=-30.5, slope=1.0, residuals_db=[], points=[],
                             nf_db=4.2, date="2026-10-27 09:00"))
    monkeypatch.setattr(sdr, "detect_receivers", lambda c: [RSP])
    monkeypatch.setattr(gui.Engine, "start", lambda self: None)
    monkeypatch.setattr(gui.Engine, "quit", lambda self: None)
    w = gui.MainWindow(cfg)
    assert ProfileStore(cfg.data_dir).active_id == prsp["id"]
    assert gui.DEV_KEYS[w.dev.currentIndex()] == "sdrplay" and w.sp_grdb.value() == 44
    assert "K -30.5 dB" in w.prof_lbl.text() and "NF 4.2" in w.prof_lbl.text()
    assert w.prof_cb.count() == 2 and w.prof_cb.currentText().startswith("● ")
    # pick the (unplugged) V4 by hand: its own gain comes back, not calibrated
    w._profile_picked(w.prof_cb.findData(p4["id"]))
    assert gui.DEV_KEYS[w.dev.currentIndex()] == "rtlsdr" and abs(w.rtl_gain.value() - 33.8) < 0.01
    assert "✖" in w.prof_lbl.text()
    w.rtl_gain.setValue(40.2)
    w._apply_settings()
    assert ProfileStore(cfg.data_dir).get(p4["id"])["settings"]["rtl_gain_db"] == "40.2"
    assert ProfileStore(cfg.data_dir).get(prsp["id"])["settings"]["sdrplay_grdb"] == "44"
    w.resize(1280, 720)
    w.show()
    app.processEvents()
    w.grab().save(str(tmp_path / "gui.png"))
    os.environ.get("METEORRX_SHOT") and w.grab().save(os.environ["METEORRX_SHOT"])
    w.close()

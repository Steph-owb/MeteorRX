"""Nothing is ever overwritten: session sequence numbers, unique products, INI save, window placement."""
import json
import os
import sys
import types
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from meteorrx import config, outputs  # noqa: E402


def test_sequence_is_persistent_and_monotonic(tmp_path):
    d = str(tmp_path / "data")
    assert [outputs.next_sequence(d) for _ in range(3)] == [1, 2, 3]
    assert json.load(open(os.path.join(d, "sequence.json")))["next"] == 4   # survives a restart
    assert outputs.next_sequence(d) == 4
    assert not os.path.exists(os.path.join(d, "sequence.json.lock"))


def test_stale_lock_is_recovered(tmp_path):
    d = str(tmp_path)
    open(os.path.join(d, "sequence.json.lock"), "w").close()   # left by a crash
    assert outputs.next_sequence(d) == 1


def test_unique_path_and_open_new_never_truncate(tmp_path):
    p = str(tmp_path / "a.cadu")
    with outputs.open_new(p) as f:
        f.write(b"first reception")
    with outputs.open_new(p) as f:
        f.write(b"second")
    assert open(p, "rb").read() == b"first reception"
    assert open(str(tmp_path / "a_2.cadu"), "rb").read() == b"second"
    os.makedirs(tmp_path / "x_products")
    assert outputs.unique_path(str(tmp_path / "x_products")) == str(tmp_path / "x_products_2")


def test_session_dirs_are_distinct_for_the_same_pass(tmp_path):
    out, data = str(tmp_path / "out"), str(tmp_path / "data")
    a = outputs.new_session_dir(out, data, "20261027_0912", "METEOR-M2-4")
    b = outputs.new_session_dir(out, data, "20261027_0912", "METEOR-M2-4")
    assert a[2] == 1 and b[2] == 2
    assert os.path.basename(a[0]) == a[1] == "0001_20261027_0912_METEOR-M2-4"
    assert os.path.basename(b[0]) == "0002_20261027_0912_METEOR-M2-4"
    os.remove(os.path.join(data, "sequence.json"))          # counter lost: folder still unique
    c = outputs.new_session_dir(out, data, "20261027_0912", "METEOR-M2-4")
    assert c[0].endswith("0001_20261027_0912_METEOR-M2-4_2")


def test_restarted_reception_keeps_both_recordings(tmp_path, monkeypatch):
    """Two sessions of the same pass (same AOS minute): two folders, two complete .cadu, no overwrite."""
    from tests.synth import make_iq
    from meteorrx import engine, sdr
    iq, _, ncadu = make_iq("72k", lines=16, snr_db=12.0)
    raw = str(tmp_path / "pass.cf32")
    iq.astype(np.complex64).tofile(raw)
    ini = tmp_path / "m.ini"
    ini.write_text(config.DEFAULT_INI)
    cfg = config.load(str(ini))
    cfg.set("recording", "output_dir", str(tmp_path / "out"))
    monkeypatch.setattr(type(cfg), "data_dir", property(lambda self: str(tmp_path / "data")))
    monkeypatch.setattr(sdr, "open_source", lambda c: sdr.FileSource(raw, 1.2e6, 137.5e6, realtime=False))
    fixed = datetime(2026, 10, 27, 9, 12, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(engine, "utcnow", lambda: fixed)
    logs = []
    eng = types.SimpleNamespace(cfg=cfg, log=logs.append, observer=None)
    folders = []
    for _ in range(2):
        s = engine.Session(eng, None, None, "72k", manual=True)
        for c in s.rx.chains:
            c.time_scale = 0.25
        while s.step():
            pass
        files = s.finish()
        assert files, "no image saved"
        folders.append(s.folder)
    assert [os.path.basename(f) for f in folders] == ["0001_20261027_0912_METEOR", "0002_20261027_0912_METEOR"]
    sizes = []
    for f in folders:
        name = os.path.basename(f)
        cadu = os.path.join(f, name + ".cadu")
        sizes.append(os.path.getsize(cadu))
        info = json.load(open(os.path.join(f, name + "_info.json")))
        assert info["session"] == int(name[:4])
        assert any(x.endswith(".png") for x in os.listdir(f))
    assert sizes[0] == sizes[1] >= (ncadu - 13) * 1024


def test_session_without_signal_creates_nothing(tmp_path, monkeypatch):
    from meteorrx import engine, sdr
    raw = str(tmp_path / "noise.cf32")
    (np.random.default_rng(1).normal(0, 0.01, 2 * 600000).astype(np.float32)).tofile(raw)
    ini = tmp_path / "m.ini"
    ini.write_text(config.DEFAULT_INI)
    cfg = config.load(str(ini))
    cfg.set("recording", "output_dir", str(tmp_path / "out"))
    monkeypatch.setattr(type(cfg), "data_dir", property(lambda self: str(tmp_path / "data")))
    monkeypatch.setattr(sdr, "open_source", lambda c: sdr.FileSource(raw, 1.2e6, 137.5e6, realtime=False))
    s = engine.Session(types.SimpleNamespace(cfg=cfg, log=lambda m: None, observer=None), None, None, "72k", True)
    while s.step():
        pass
    assert s.finish() == [] and s.folder is None
    assert not os.path.exists(tmp_path / "out")
    assert not os.path.exists(tmp_path / "data" / "sequence.json")   # no number burnt


def test_ini_save_appends_new_keys_and_keeps_comments(tmp_path):
    old = config.DEFAULT_INI.replace("window          = maximized\n", "")   # INI from v1.4
    ini = tmp_path / "m.ini"
    ini.write_text(old)
    cfg = config.load(str(ini))
    cfg.set("gui", "window", "last")
    cfg.set("profiles", "active", "rtl-1")
    cfg.save()
    txt = ini.read_text()
    assert "; Start the automatic scheduler at launch" in txt
    again = config.load(str(ini))
    assert again.get("gui", "window") == "last" and again.get("profiles", "active") == "rtl-1"
    assert again.get("gui", "language") == "fr"


def test_window_starts_maximized_and_restores(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from meteorrx import gui
    _app = QApplication.instance() or QApplication([])  # noqa: F841
    ini = tmp_path / "m.ini"
    ini.write_text(config.DEFAULT_INI)
    cfg = config.load(str(ini))
    cfg.set("recording", "output_dir", str(tmp_path / "out"))
    cfg.set("gui", "auto_start", "no")
    monkeypatch.setattr(type(cfg), "data_dir", property(lambda self: str(tmp_path / "data")))
    monkeypatch.setattr(gui.Engine, "start", lambda self: None)
    monkeypatch.setattr(gui.Engine, "quit", lambda self: None)
    w = gui.MainWindow(cfg)
    w.show_initial()
    _app.processEvents()
    assert w.isMaximized()
    w._toggle_fullscreen()
    _app.processEvents()
    assert w.isFullScreen()
    w._esc()
    _app.processEvents()
    assert not w.isFullScreen()
    w.close()
    st = json.load(open(tmp_path / "data" / "window.json"))
    assert st["geometry"] and "maximized" in st

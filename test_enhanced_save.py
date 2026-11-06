"""The enhanced copy must contain everything the window was showing - and say so honestly.

An OM reported city names and coastlines missing from his saved copies. The save itself turned out to
be exact; what was wrong is that his recordings were started by hand, so no satellite and no TLE were
attached to the picture, the overlay could not be placed at all, and the caption under the image
claimed "coastlines, cities" over a bare picture. These tests pin all three ends of that: a manual
recording still gets a satellite, the caption never claims an overlay that is not there, and the file
on disk is pixel for pixel what was on screen.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from meteorrx import config, engine as E, i18n, imaging  # noqa: E402

# METEOR-M2 3, epoch 2026-309. Over Malta at 08:05 UTC on 2026-11-06 at 46 deg, and nothing is up at 02:00.
TLE = ("1 57166U 23091A   26309.95755911 -.00000019  00000+0  10410-4 0  9992",
       "2 57166  98.5912   1.9044 0005004 113.2114 246.9592 14.24055539174693")
OVERHEAD = datetime(2026, 11, 6, 8, 5, tzinfo=timezone.utc)
EVENING = datetime(2026, 11, 6, 19, 25, tzinfo=timezone.utc)
EMPTY_SKY = datetime(2026, 11, 6, 2, 0, tzinfo=timezone.utc)


class _Source:
    center_hz = 137.9e6
    is_file = False
    path = None


class _Session:
    """Only what _note_pass reads of a live session started by hand."""
    def __init__(self):
        self.source = _Source()
        self.sat = None          # nobody told the engine which satellite: that is the whole point
        self.pas = None
        self.folder = None
        self.prefix = None
        self.seq = 1
        self.rx = mock.Mock(imager=mock.Mock(packets=42), active_freq=137.9e6)


def isolate(tmp_path, monkeypatch):
    """Point every path the application writes to at the test's own folder, and keep the engine inert.

    data_dir is derived from app_dir(), not from the ini, so meteorrx.log and window.json would
    otherwise land in the repository; Engine.start() would download TLEs and probe for hardware."""
    monkeypatch.setattr(config, "app_dir", lambda: str(tmp_path))
    monkeypatch.setattr(E.Engine, "start", lambda self: None)
    monkeypatch.setattr(E.Engine, "quit", lambda self: None)     # nothing was ever started
    return isolated_cfg(tmp_path)


def isolated_cfg(tmp_path):
    """A config of our own, writing nowhere near the operator's real Documents folder.

    config.load() with no path reads the installed meteorrx.ini and may rewrite it, and Engine then
    cleans and purges the real output folder - a test must never touch either."""
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    ini = tmp_path / "meteorrx.ini"
    ini.write_text("\n".join(
        f"output_dir          = {out}" if l.startswith("output_dir") else l
        for l in config.DEFAULT_INI.split("\n")), encoding="utf-8")
    return config.load(str(ini))


@pytest.fixture
def eng(tmp_path, monkeypatch):
    e = E.Engine(isolate(tmp_path, monkeypatch))
    for s in e.sats:
        if s.name == "METEOR-M2 3":
            s.load(*TLE)
    return e


def at(when):
    return mock.patch.object(E, "datetime", mock.Mock(now=lambda tz=None: when))


def test_a_hand_started_recording_still_gets_a_satellite(eng):
    """Without this the picture carries no TLE and the overlays are silently dropped."""
    with at(OVERHEAD):
        sat, north = eng._identify(_Session())
    assert sat is not None and sat.name == "METEOR-M2 3"
    assert north is False                      # the morning pass over Malta is descending


def test_the_evening_pass_is_recognised_as_northbound(eng):
    """The half turn applied to the saved products hangs on this flag."""
    with at(EVENING):
        sat, north = eng._identify(_Session())
    assert sat is not None and north is True


def test_nothing_is_claimed_when_nothing_is_overhead(eng):
    """A guess would put the coastline of the wrong hemisphere on the picture."""
    with at(EMPTY_SKY):
        sat, north = eng._identify(_Session())
    assert sat is None and north is None


def test_note_pass_attaches_the_tle_it_found(eng):
    s = _Session()
    with at(OVERHEAD):
        eng._note_pass(s)
    assert eng.last_pass["satellite"] == "METEOR-M2 3"
    assert eng.last_pass["tle"] == list(TLE)


def test_the_satellite_is_resolved_once_and_kept(eng):
    """_note_pass runs on every tick; a satellite swapped halfway would re-project one picture twice."""
    s = _Session()
    with at(OVERHEAD):
        eng._note_pass(s)
    first = eng.last_pass["satellite"]
    with at(EMPTY_SKY):                        # the sky empties, the picture on screen does not change
        eng._note_pass(s)
    assert eng.last_pass["satellite"] == first


def test_an_empty_sky_does_not_freeze_the_search(eng):
    """Operators start a minute early: a first miss must not stop it finding the bird when it rises."""
    s = _Session()
    with at(EMPTY_SKY):
        eng._note_pass(s)
    assert eng.last_pass["satellite"] is None
    with at(OVERHEAD):
        eng._note_pass(s)
    assert eng.last_pass["satellite"] == "METEOR-M2 3"


def test_the_frequency_field_is_in_megahertz():
    """orbit.Satellite.freqs_mhz was once called freqs_hz while holding MHz, and the mismatch silently
    disabled the frequency match in _sat_overhead. Pin the unit so the name cannot drift back."""
    from meteorrx import orbit
    assert "freqs_mhz" in orbit.Satellite.__dataclass_fields__
    assert "freqs_hz" not in orbit.Satellite.__dataclass_fields__
    s = orbit.Satellite("x", 1, [137.9, 137.1])
    assert all(100.0 < f < 200.0 for f in s.freqs_mhz)


def test_every_language_can_say_the_overlay_was_not_placed():
    """The caption falls back to this when the pass cannot be geolocated."""
    for code in i18n.LANGUAGES:
        assert i18n.texts(code, "gui").get("enh_geo_mark")


# --- the window itself -------------------------------------------------------------------------

class _StubImager:
    """Only what the view and the overlay ask of a live imager."""
    def __init__(self, h=400, w=1568, chans=(1,)):
        assert h % 8 == 0, "the imager works in 8-line strips"
        rng = np.random.default_rng(0)
        self._chs = {c - 1: rng.integers(0, 255, (h, w), dtype=np.uint8) for c in chans}
        t0 = OVERHEAD - timedelta(seconds=h * 0.1539)
        self._rt = {c - 1: np.array([t0.timestamp() + i * 0.1539 for i in range(h)]) for c in chans}
        self.packets = 1

    @property
    def channels_present(self):
        return sorted(self._chs)

    def get_channel(self, ch):
        return self._chs[ch]

    def row_times(self, ch):
        return self._rt[ch]

    def _extent(self, ch):
        return (0, self._chs[ch].shape[0])


@pytest.fixture
def win(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from meteorrx import gui
    cfg = isolate(tmp_path, monkeypatch)
    QApplication.instance() or QApplication([])
    w = gui.MainWindow(cfg)
    w.engine.last_imager = _StubImager()
    w.view_sel.clear()
    w.view_sel.addItems([f"{w.t['ch']} 1"])
    w.enh.update(coast=True, cities=True, fill=True, resize=False)
    yield w
    w.engine.quit()          # otherwise the engine thread outlives the test and Qt complains


def _placeable(folder):
    return {"satellite": "METEOR-M2 3", "tle": list(TLE), "rotated": False,
            "folder": str(folder), "prefix": "0001_test"}


def test_the_saved_copy_is_exactly_what_was_on_screen(win, tmp_path):
    """The whole report started here: whatever the window shows must reach the file untouched."""
    win.engine.last_pass = _placeable(tmp_path)
    win._refresh_view(force=True)
    shown = win._shown
    assert shown.ndim == 3, "an overlay was expected, so the picture must be in colour"
    assert int((shown.max(axis=2).astype(int) - shown.min(axis=2)).sum()) > 0, "no overlay was drawn"
    win._save_enhanced()
    files = sorted(p for p in os.listdir(tmp_path) if p.endswith("_enhanced.png"))
    assert files, "no enhanced copy was written"
    back = imaging.read_png(os.path.join(tmp_path, files[-1]))
    assert back.shape == shown.shape
    assert np.array_equal(back, shown)


def test_the_caption_does_not_claim_an_overlay_that_is_not_there(win):
    """A manual recording with no TLE used to be captioned "coastlines, cities" over a bare picture,
    which sent an OM looking for a bug in the save instead of at the missing TLE."""
    win.engine.last_pass = {"satellite": None, "tle": None, "rotated": False,
                            "folder": None, "prefix": None}
    win._refresh_view(force=True)
    caption = win.img_info.text()
    assert win.t["enh_geo_mark"] in caption
    assert win.t["enh_coast"].split(" (")[0].lower() not in caption
    assert win._shown.ndim == 2, "nothing should have been drawn on the picture"


def test_the_caption_names_the_overlays_when_they_are_drawn(win, tmp_path):
    win.engine.last_pass = _placeable(tmp_path)
    win._refresh_view(force=True)
    caption = win.img_info.text()
    assert win.t["enh_coast"].split(" (")[0].lower() in caption
    assert win.t["enh_geo_mark"] not in caption


def test_each_pass_warns_once_but_a_new_pass_warns_again(win):
    """The warning used to fire once per run, so every later pass failed in silence."""
    win.engine.last_pass = {"satellite": None, "tle": None, "rotated": False}
    logged = []
    win.engine.log = logged.append
    win._refresh_view(force=True)
    win._refresh_view(force=True)
    assert sum(win.t["enh_geo_none"] in m for m in logged) == 1, "it should not repeat for one pass"
    win.engine.last_imager = _StubImager(h=504)       # a different pass arrives
    win._refresh_view(force=True)
    assert sum(win.t["enh_geo_none"] in m for m in logged) == 2, "a new pass deserves its own warning"


def test_a_growing_live_image_does_not_repeat_the_warning(win):
    """The key once included the image extent, which grows with every strip, so an un-geolocatable
    live pass logged the warning on every refresh - roughly every two seconds for the whole pass."""
    win.engine.last_pass = {"satellite": None, "tle": None, "rotated": False}
    logged = []
    win.engine.log = logged.append
    im = win.engine.last_imager
    for rows in range(8, 200, 8):                 # the imager grows, as it does during reception
        im._chs[0] = im._chs[0][:rows] if rows <= im._chs[0].shape[0] else im._chs[0]
        win._refresh_view(force=True)
    assert sum(win.t["enh_geo_none"] in m for m in logged) == 1, logged[:4]


# --- replaying a recording ---------------------------------------------------------------------

class _FileSource(_Source):
    is_file = True

    def __init__(self, path):
        self.path = str(path)


def _file_session(path):
    s = _Session()
    s.source = _FileSource(path)
    return s


def test_a_replay_never_guesses_from_todays_sky(eng, tmp_path):
    """The sky now says nothing about a recording made last week. Drawing a coastline from it would
    be confidently wrong - worse than no coastline - and could even attach the other satellite."""
    cadu = tmp_path / "0001_test.cadu"
    cadu.write_bytes(b"")
    with at(OVERHEAD):                       # a satellite IS overhead right now: it must be ignored
        sat, north = eng._identify(_file_session(cadu))
    assert sat is None and north is None


def test_a_replay_uses_the_elements_recorded_with_the_file(eng, tmp_path):
    """This is why the TLE is written into _info.json when the pass is received."""
    import json
    (tmp_path / "0001_test.cadu").write_bytes(b"")
    (tmp_path / "0001_test_info.json").write_text(json.dumps(
        {"satellite": "METEOR-M2 3", "tle": list(TLE), "northbound": True}), encoding="utf-8")
    with at(EMPTY_SKY):                      # nothing overhead now, and it does not matter
        sat, north = eng._identify(_file_session(tmp_path / "0001_test.cadu"))
    assert sat is not None and sat.name == "METEOR-M2 3" and north is True
    assert list(sat.tle) == list(TLE)


def test_a_replay_says_so_once_and_does_not_repeat_itself(eng, tmp_path):
    """A replay asks its own files once; the answer cannot change, so it must not log on every tick."""
    (tmp_path / "0002_test.cadu").write_bytes(b"")
    logged = []
    eng.log = logged.append
    s = _file_session(tmp_path / "0002_test.cadu")
    with at(OVERHEAD):
        for _ in range(20):
            eng._note_pass(s)
    assert len(logged) == 1, logged[:3]
    assert eng.last_pass["tle"] is None


def test_a_corrupt_stored_tle_does_not_stop_the_replay(eng, tmp_path):
    import json
    (tmp_path / "0003_test.cadu").write_bytes(b"")
    (tmp_path / "0003_test_info.json").write_text(json.dumps(
        {"satellite": "METEOR-M2 3", "tle": ["not a tle", "nor this"]}), encoding="utf-8")
    with at(OVERHEAD):
        sat, _ = eng._identify(_file_session(tmp_path / "0003_test.cadu"))
    assert sat is None


def test_identification_waits_for_the_first_decoded_frame(eng):
    """A decoded frame proves a Meteor is really transmitting; geometry alone does not."""
    s = _Session()
    s.rx = mock.Mock(imager=mock.Mock(packets=0), active_freq=137.9e6)
    with at(OVERHEAD):
        assert eng._identify(s) == (None, None)
    s.rx.imager.packets = 1
    with at(OVERHEAD):
        assert eng._identify(s)[0] is not None


# --- keeping the receiver's CPU ------------------------------------------------------------------

def test_a_cheap_redraw_is_never_delayed(win):
    """With no overlay a redraw costs milliseconds; the preview must keep its normal cadence."""
    import time as _t
    win.enh.update(coast=False, cities=False)
    win._refresh_view(force=True)
    assert win._view_cost < 0.5, "a view with no overlay should cost milliseconds"
    win._last_view_t = _t.monotonic() - 2.0      # the preview offers a new picture every two seconds
    assert win._auto_redraw_due(), "a cheap view must not be held back at the normal cadence"


def test_an_expensive_redraw_is_spaced_out(win):
    """Drawing the map takes about a second on a full pass while the preview offers a new picture
    every two: redrawing on every one would hold the Qt thread for half the pass, on the same machine
    that is demodulating the signal."""
    import time as _t
    win._view_cost = 1.0
    win._last_view_t = _t.monotonic()
    assert not win._auto_redraw_due()
    win._last_view_t = _t.monotonic() - 3.9
    assert not win._auto_redraw_due()
    win._last_view_t = _t.monotonic() - 4.1
    assert win._auto_redraw_due()


def test_a_pathologically_slow_redraw_still_lets_the_picture_move(win):
    """The spacing is capped: a very slow machine must not stop updating the preview altogether."""
    import time as _t
    win._view_cost = 60.0
    win._last_view_t = _t.monotonic() - 21.0
    assert win._auto_redraw_due()


def test_the_cost_is_measured_on_every_redraw(win, tmp_path):
    win.engine.last_pass = _placeable(tmp_path)
    win._refresh_view(force=True)
    assert win._view_cost > 0.0 and win._last_view_t > 0.0


# --- the TLE saved beside the recording ----------------------------------------------------------

def _tle_file(folder, stem, name="METEOR-M2 3"):
    (folder / f"{stem}.tle").write_text(f"{name}\n{TLE[0]}\n{TLE[1]}\n", encoding="utf-8")


def test_a_replay_prefers_the_plain_tle_beside_the_recording(eng, tmp_path):
    """A three-line TLE is the universal form: SatDump, Gpredict and a human all read it, and it
    survives someone copying away only the recording and this file."""
    (tmp_path / "0001_test.cadu").write_bytes(b"")
    _tle_file(tmp_path, "0001_test")
    with at(EMPTY_SKY):
        sat, _ = eng._identify(_file_session(tmp_path / "0001_test.cadu"))
    assert sat is not None and sat.name == "METEOR-M2 3"
    assert list(sat.tle) == list(TLE)


def test_the_direction_still_comes_from_our_own_file(eng, tmp_path):
    """A plain TLE carries no direction of travel, and the half turn for a northbound pass needs it."""
    import json
    (tmp_path / "0001_test.cadu").write_bytes(b"")
    _tle_file(tmp_path, "0001_test")
    (tmp_path / "0001_test_info.json").write_text(json.dumps(
        {"satellite": "METEOR-M2 3", "tle": list(TLE), "northbound": True}), encoding="utf-8")
    with at(EMPTY_SKY):
        sat, north = eng._identify(_file_session(tmp_path / "0001_test.cadu"))
    assert sat is not None and north is True


def test_a_tle_dropped_in_by_hand_is_found(eng, tmp_path):
    """A recording that arrived from another operator without its elements can be rescued by putting
    any .tle in the folder - the name need not match."""
    (tmp_path / "from_tony.cadu").write_bytes(b"")
    _tle_file(tmp_path, "whatever-i-called-it")
    with at(EMPTY_SKY):
        sat, _ = eng._identify(_file_session(tmp_path / "from_tony.cadu"))
    assert sat is not None


def test_a_two_line_tle_without_a_name_is_accepted(eng, tmp_path):
    (tmp_path / "0001_test.cadu").write_bytes(b"")
    (tmp_path / "0001_test.tle").write_text(f"{TLE[0]}\n{TLE[1]}\n", encoding="utf-8")
    with at(EMPTY_SKY):
        sat, _ = eng._identify(_file_session(tmp_path / "0001_test.cadu"))
    assert sat is not None and list(sat.tle) == list(TLE)


def test_a_file_that_is_not_a_tle_is_ignored(eng, tmp_path):
    (tmp_path / "0001_test.cadu").write_bytes(b"")
    (tmp_path / "notes.tle").write_text("these are my notes, not elements\n", encoding="utf-8")
    with at(EMPTY_SKY):
        assert eng._identify(_file_session(tmp_path / "0001_test.cadu")) == (None, None)

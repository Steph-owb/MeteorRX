"""Panoramic (earth-curvature) correction and the housekeeping of the output folder."""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from meteorrx import geometry, imaging, lrpt, outputs  # noqa: E402


def test_mapping_matches_satdump_geometry():
    cols = geometry.source_columns(lrpt.IMG_WIDTH)
    assert cols.size == 2800                                   # swath 2800 km at 1 km/pixel
    assert cols[0] == 0.0 and abs(cols[-1] - (lrpt.IMG_WIDTH - 1)) < 1.0
    d = np.diff(cols)
    assert (d >= 0).all()                                      # never goes backwards
    # The output grid reaches a hair beyond the outermost input sample, so the first and last output
    # columns sit on the clip and repeat their neighbour. Everything between them must advance.
    flat = np.flatnonzero(d == 0)
    assert flat.size <= 4 and set(flat) <= {0, 1, len(d) - 2, len(d) - 1}, flat
    assert (d[3:-3] > 0).all()
    # the swath edge is seen at ~55 deg from nadir: SatDump's scan_angle is 110.1 deg for M2-3 / M2-4
    r = geometry.EARTH_RADIUS_KM + geometry.ALTITUDE_KM
    edge = np.degrees(geometry._nadir_angle(geometry.SWATH_KM / geometry.EARTH_RADIUS_KM / 2, r))
    assert 54.5 < edge < 55.5
    ratio, mag = geometry.scale_factors()
    assert 1.78 < ratio < 1.79 and 5.0 < mag < 6.0             # edge pixels stretched ~5.4x
    # the centre of the swath must stay the centre, and the mapping must be exactly symmetric: anything
    # less and a corrected image is not the mirror of the mirrored one, which the overlay would contradict
    assert abs(cols[1400] - (lrpt.IMG_WIDTH - 1) / 2) < 1.0
    assert np.abs(cols - ((lrpt.IMG_WIDTH - 1) - cols)[::-1]).max() < 1e-9


def test_correct_preserves_content_and_black():
    rng = np.random.default_rng(3)
    for shape in ((40, lrpt.IMG_WIDTH), (12, lrpt.IMG_WIDTH, 3)):
        img = rng.integers(40, 256, shape, dtype=np.uint8)
        out = geometry.correct(img)
        assert out.shape[0] == img.shape[0] and out.shape[1] == 2800 and out.dtype == np.uint8
        assert out.ndim == img.ndim and (out.shape[2:] == img.shape[2:])
        assert img.shape == shape                               # the input is never modified
        assert out.min() > 0                                    # nothing invented black
        # a vertical edge keeps its ground position: a step at the centre stays at the centre
    step = np.zeros((4, lrpt.IMG_WIDTH), np.uint8)
    step[:, lrpt.IMG_WIDTH // 2:] = 200
    o = geometry.correct(step)
    assert abs(int(np.argmax(o[0] > 100)) - 1400) <= 2
    # missing (black) pixels do not bleed into their neighbours
    gap = np.full((2, lrpt.IMG_WIDTH), 255, np.uint8)
    gap[:, 700:760] = 0
    og = geometry.correct(gap)
    assert set(np.unique(og)) <= {0, 255}


def test_correct_is_reversible_in_position():
    """A feature at a known input column lands where the geometry says it should."""
    cols = geometry.source_columns(lrpt.IMG_WIDTH)
    for src in (200, 784, 1300):
        out_col = int(np.argmin(np.abs(cols - src)))
        img = np.zeros((2, lrpt.IMG_WIDTH), np.uint8)
        img[:, src] = 255
        o = geometry.correct(img)
        assert abs(int(np.argmax(o[0])) - out_col) <= 2


def test_save_products_writes_corrected_copies(tmp_path):
    class Fake:
        channels_present = [0, 1]
        packets = 10

        def get_channel(self, c):
            return np.full((30, lrpt.IMG_WIDTH), 100 + c, np.uint8)

    files = imaging.save_products(Fake(), str(tmp_path), "0001_x", [], fill=False, rotate=False,
                                  ir_inverted=False, correct=True)
    names = sorted(os.path.basename(f) for f in files)
    assert names == ["0001_x_ch1.png", "0001_x_ch1_corr.png", "0001_x_ch2.png", "0001_x_ch2_corr.png"]
    assert imaging.read_png(str(tmp_path / "0001_x_ch1.png")).shape[1] == lrpt.IMG_WIDTH     # original kept
    assert imaging.read_png(str(tmp_path / "0001_x_ch1_corr.png")).shape[1] == 2800
    off = imaging.save_products(Fake(), str(tmp_path), "0002_x", [], fill=False, rotate=False,
                                ir_inverted=False, correct=False)
    assert not any("_corr" in os.path.basename(f) for f in off)


def test_png_roundtrip_grey_and_rgb(tmp_path):
    rng = np.random.default_rng(0)
    for shape in ((17, 40), (9, 25, 3)):
        a = rng.integers(0, 256, shape, dtype=np.uint8)
        p = str(tmp_path / "t.png")
        imaging.write_png(p, a)
        assert np.array_equal(imaging.read_png(p), a)


def _session(root, name, files=()):
    d = os.path.join(root, name)
    os.makedirs(d)
    for fn, size in files:
        with open(os.path.join(d, fn), "wb") as f:
            f.write(b"\0" * size)
    return d


def test_clean_empty_sessions_only_removes_what_is_empty(tmp_path):
    root = str(tmp_path)
    empty = _session(root, "0001_20261104_0912_METEOR-M2-3")
    zero = _session(root, "0002_20261104_0930_METEOR-M2-3", [("0002.cadu", 0)])
    kept = _session(root, "0003_20261104_0940_METEOR-M2-3", [("0003.cadu", 1024)])
    notsession = _session(root, "my own pictures")
    sub = _session(root, "0004_20261104_0950_METEOR-M2-3")
    os.makedirs(os.path.join(sub, "inner"))
    removed = outputs.clean_empty_sessions(root)
    assert sorted(removed) == sorted([empty, zero])
    assert not os.path.exists(empty) and not os.path.exists(zero)
    assert os.path.isdir(kept) and os.path.isdir(notsession) and os.path.isdir(sub)


def test_purge_old_sessions_uses_the_folder_date(tmp_path):
    root = str(tmp_path)
    old = time.strftime("%Y%m%d_%H%M", time.localtime(time.time() - 40 * 86400))
    recent = time.strftime("%Y%m%d_%H%M", time.localtime(time.time() - 3 * 86400))
    a = _session(root, f"0001_{old}_METEOR-M2-3", [("img.png", 50)])
    b = _session(root, f"0002_{recent}_METEOR-M2-3", [("img.png", 50)])
    c = _session(root, "not a session", [("img.png", 50)])
    assert outputs.purge_old_sessions(root, 0) == []            # 0 = never delete anything
    assert os.path.isdir(a)
    assert outputs.purge_old_sessions(root, 30) == [a]
    assert not os.path.exists(a) and os.path.isdir(b) and os.path.isdir(c)


def test_colour_enhancement_keeps_hue_and_black():
    rng = np.random.default_rng(5)
    rgb = rng.integers(60, 200, (60, 300, 3), dtype=np.uint8)
    rgb[:, :20] = 0
    from meteorrx import enhance
    out = enhance.colour(rgb)
    assert out.shape == rgb.shape and out.dtype == np.uint8
    assert (out[:, :20] == 0).all()                                   # missing stays missing
    assert out[:, 20:].std() > rgb[:, 20:].std()                      # contrast increased
    # the hue ordering of the channels is preserved on a synthetic ramp
    ramp = np.zeros((4, 300, 3), np.uint8)
    ramp[:, :, 0] = 200
    ramp[:, :, 1] = 150
    ramp[:, :, 2] = 100
    o = enhance.colour(ramp)
    assert o[0, 150, 0] >= o[0, 150, 1] >= o[0, 150, 2]


def test_infrared_clahe():
    from meteorrx import enhance
    rng = np.random.default_rng(6)
    img = np.clip(rng.normal(120, 8, (120, 400)), 0, 255).astype(np.uint8)   # low-contrast IR scene
    img[:, :30] = 0
    out = enhance.infrared(img)
    assert out.shape == img.shape and out.dtype == np.uint8
    assert (out[:, :30] == 0).all()
    assert out[:, 30:].std() > 2.5 * img[:, 30:].std()                # local contrast strongly increased
    assert enhance.infrared(np.zeros((40, 40), np.uint8)).max() == 0   # nothing invented from nothing


def test_enhance_apply_dispatch():
    from meteorrx import enhance
    g = np.full((40, 40), 100, np.uint8)
    c = np.full((40, 40, 3), 100, np.uint8)
    assert enhance.apply(g, True, False) is g                          # colour does not touch a grey image
    assert enhance.apply(c, False, True) is c                          # infrared does not touch an RGB image
    assert enhance.apply(g, False, False) is g and enhance.apply(c, False, False) is c


def test_composite_does_not_invent_colour_where_data_is_missing():
    """A lost frame rarely costs every channel the same pixels.

    The channels' segments are interleaved, so a gap often leaves one channel black while the others
    still carry data. Stacked naively that becomes a band of saturated blue or yellow across the
    picture - it reads as corruption rather than as absence, and it covered 1.2 % of a real night pass.
    A pixel missing from any contributing channel must come out black."""
    h, w = 12, 32
    chs = {1: np.full((h, w), 100, np.uint8),
           2: np.full((h, w), 200, np.uint8),
           4: np.full((h, w), 150, np.uint8)}
    chs[2][3] = 0                      # one channel loses a line
    chs[1][7] = 0                      # another channel loses a different line
    chs[2][9] = 0                      # and a line where both are gone
    chs[1][9] = 0
    out = imaging.composite(chs, "221")
    assert out is not None and out.shape == (h, w, 3)
    assert (out[3] == 0).all(), "a line missing from channel 2 must not come out blue"
    assert (out[7] == 0).all(), "a line missing from channel 1 must not come out yellow"
    assert (out[9] == 0).all()
    good = np.ones(h, bool)
    good[[3, 7, 9]] = False
    assert (out[good] > 0).all(), "lines with full data must be untouched"
    np.testing.assert_array_equal(out[0], np.dstack([np.full((1, w), 200, np.uint8),
                                                     np.full((1, w), 200, np.uint8),
                                                     np.full((1, w), 100, np.uint8)])[0])


def test_composite_rejects_a_bad_spec():
    chs = {1: np.full((4, 8), 90, np.uint8), 2: np.full((4, 8), 90, np.uint8)}
    assert imaging.composite(chs, "22") is None
    assert imaging.composite(chs, "2x1") is None
    assert imaging.composite(chs, "229") is None          # channel 9 was never received
    assert imaging.composite(chs, "221") is not None


def test_default_composites_include_the_satdump_false_colour():
    """124 is SatDump's MSU-MR false colour (ch1, ch2, ch4) and the reference the testers compare against.

    It must be in the shipped defaults, and the list must survive a round trip through the ini parser,
    because the picture window builds its view list from this same setting."""
    import tempfile
    from meteorrx import config
    cfg = config.load(os.path.join(tempfile.mkdtemp(), "meteorrx.ini"))
    raw = cfg.get("recording", "composites", "")
    specs = [s.strip() for s in raw.split(",") if s.strip()]
    assert "124" in specs, raw
    assert "221" in specs, raw
    assert all(len(s) == 3 and s.isdigit() for s in specs), raw


def test_false_colour_124_maps_channels_to_red_green_blue():
    """R from channel 1, G from channel 2, B from channel 4 - in that order, not re-sorted."""
    h, w = 6, 10
    chs = {1: np.full((h, w), 30, np.uint8),
           2: np.full((h, w), 120, np.uint8),
           4: np.full((h, w), 210, np.uint8)}
    out = imaging.composite(chs, "124")
    assert out is not None
    assert out[0, 0, 0] == 30 and out[0, 0, 1] == 120 and out[0, 0, 2] == 210
    # and it is a different picture from the natural-colour one, not an accidental duplicate
    assert not np.array_equal(out, imaging.composite(chs, "221"))


def test_a_composite_needing_an_absent_channel_is_skipped_not_faked():
    """Meteor-M2-3 and M2-4 send channels 1, 2 and 4; a 123 in the list must simply be passed over."""
    chs = {1: np.full((4, 8), 50, np.uint8), 2: np.full((4, 8), 60, np.uint8),
           4: np.full((4, 8), 70, np.uint8)}
    assert imaging.composite(chs, "123") is None
    assert imaging.composite(chs, "124") is not None

"""Geolocation and map overlays.

The orientation constants in `geo` were established against a real Meteor-M2-3 pass (see the comment on
`geo.EAST_ON_LEFT`); what is checked here is that the geometry is self-consistent, that it agrees with
the independent pass-prediction code in `orbit`, and that the overlay survives the resampling and the
flips the viewer can apply.
"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pytest

from meteorrx import geo, geometry, imaging, orbit, overlay

# METEOR-M2 3, epoch 2026-11-02, as received at a tester's station
TLE1 = "1 57166U 23091A   26306.86603644 -.00000034  00000+0  40461-5 0  9991"
TLE2 = "2 57166  98.5916 358.8831 0004931 120.1936 239.9732 14.24055337174258"
T0 = 1793737736.0          # 2026-11-03 20:28:56 UTC, the first line of that pass
WIDTH = 1568
SCAN, ROLL = geo.scan_params("METEOR-M2 3")


@pytest.fixture(scope="module")
def track():
    from sgp4.api import Satrec
    # 2400 rows at the measured MSU-MR line period: 6 minutes of the pass, from the Sahara up across
    # the Mediterranean, so the swath really does contain coastlines and labelled cities
    t = T0 + np.arange(2400) * 0.1539
    return geo.Track(Satrec.twoline2rv(TLE1, TLE2), t, ROLL)


def test_scan_params_known_and_unknown():
    assert geo.scan_params("METEOR-M2 3") == (110.1, -0.31)
    assert geo.scan_params("meteor-m2-3") == (110.1, -0.31)      # spelling-tolerant
    assert geo.scan_params("METEOR-M2") == (110.6, 2.3)
    assert geo.scan_params(None) == geo.DEFAULT_SCAN
    assert geo.scan_params("NOAA 19") == geo.DEFAULT_SCAN


def test_track_matches_the_pass_predictor(track):
    """geo.Track and orbit.Observer are separate code paths; they must see the same satellite."""
    sat = orbit.Satellite(name="METEOR-M2 3", norad=57166, freqs_mhz=[137.9])
    sat.load(TLE1, TLE2)
    obs = orbit.Observer(45.4170, 8.7747, 0.120)
    for i in (0, 1200, 2399):
        when = dt.datetime.fromtimestamp(float(track.t[i]), dt.timezone.utc)
        _, el, rng, _, _ = obs.look(sat, when)
        p = geo._geodetic_to_ecef(np.array([45.4170]), np.array([8.7747]), 0.12)[0]
        d = track.r[i] - p
        la, lo = math.radians(45.4170), math.radians(8.7747)
        up = np.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])
        mine = math.degrees(math.asin(float(np.dot(d, up) / np.linalg.norm(d))))
        assert abs(float(np.linalg.norm(d)) - rng) < 1.0          # km
        assert abs(mine - el) < 0.05                              # degrees


def test_body_frame_is_orthonormal(track):
    for v in (track.nadir, track.along, track.cross):
        assert np.allclose(np.linalg.norm(v, axis=1), 1.0, atol=1e-9)
    assert np.allclose(np.sum(track.nadir * track.along, axis=1), 0.0, atol=1e-9)
    assert np.allclose(np.sum(track.nadir * track.cross, axis=1), 0.0, atol=1e-9)
    assert np.allclose(np.sum(track.along * track.cross, axis=1), 0.0, atol=1e-9)
    # nadir points at the Earth, along-track points the way the satellite is going
    assert np.all(np.sum(track.nadir * track.r, axis=1) < 0)
    assert np.all(np.sum(track.along * track.v, axis=1) > 0)


def test_forward_and_inverse_agree(track):
    """look_latlon and pixel_of must be each other's inverse, to well under a pixel."""
    rows = np.array([5, 600, 1200, 1800, 2394])
    cols = np.array([0, 200, 784, 1300, WIDTH - 1])
    ang = geo.scan_angles(WIDTH, SCAN)[cols]
    lat, lon = geo.look_latlon(track, ang, rows)
    assert np.isfinite(lat).all(), "the whole swath must hit the Earth"
    r, c = geo.pixel_of(track, lat.ravel(), lon.ravel(), WIDTH, SCAN)
    r = r.reshape(lat.shape)
    c = c.reshape(lat.shape)
    assert np.isfinite(r).all() and np.isfinite(c).all()
    assert np.abs(r - rows[:, None]).max() < 0.5
    assert np.abs(c - cols[None, :]).max() < 0.5


def test_swath_is_about_2800_km_wide(track):
    ang = geo.scan_angles(WIDTH, SCAN)
    lat, lon = geo.look_latlon(track, ang[[0, WIDTH - 1]], np.array([1200]))
    a = geo._geodetic_to_ecef(lat[0, :1], lon[0, :1])[0]
    b = geo._geodetic_to_ecef(lat[0, 1:], lon[0, 1:])[0]
    chord = float(np.linalg.norm(a - b))
    arc = 2 * geo.WGS84_A * math.asin(chord / (2 * geo.WGS84_A))
    assert 2500 < arc < 3000, arc                 # the published MSU-MR swath is 2800 km


def test_east_is_where_the_constant_says(track):
    """A point east of the ground track must land on the side EAST_ON_LEFT claims."""
    lat0, lon0 = geo.look_latlon(track, np.array([0.0]), np.array([1200]))
    r, c = geo.pixel_of(track, lat0[0, 0], lon0[0, 0] + 6.0, WIDTH, SCAN)   # 6 deg east of nadir
    assert np.isfinite(c[0])
    assert bool(c[0] < WIDTH / 2) is geo.EAST_ON_LEFT
    r2, c2 = geo.pixel_of(track, lat0[0, 0], lon0[0, 0] + 6.0, WIDTH, SCAN, east_left=not geo.EAST_ON_LEFT)
    assert abs((c[0] - (WIDTH - 1) / 2) + (c2[0] - (WIDTH - 1) / 2)) < 1e-6  # the two are mirror images


def test_points_behind_the_earth_are_rejected(track):
    """A place on the far side of the globe can fall inside the scan cone by direction alone; it must
    still be dropped, or it gets drawn across the picture (this was a real bug)."""
    lat0, lon0 = geo.look_latlon(track, np.array([0.0]), np.array([1200]))
    far_lon = (float(lon0[0, 0]) + 180.0 + 180.0) % 360.0 - 180.0
    r, c = geo.pixel_of(track, -float(lat0[0, 0]), far_lon, WIDTH, SCAN)
    assert not np.isfinite(r[0]) and not np.isfinite(c[0])
    # and nothing anywhere on the far hemisphere gets through
    la, lo = np.meshgrid(np.arange(-80, 81, 10.0), np.arange(-180, 180, 10.0))
    r, c = geo.pixel_of(track, la.ravel(), lo.ravel(), WIDTH, SCAN)
    ok = np.isfinite(r) & np.isfinite(c)
    if ok.any():
        p = geo._geodetic_to_ecef(la.ravel()[ok], lo.ravel()[ok])
        i = np.nan_to_num(r[ok]).astype(int)
        up = p / np.linalg.norm(p, axis=1, keepdims=True)
        assert np.all(np.sum((track.r[i] - p) * up, axis=1) > 0)


def test_pixel_of_chunking_changes_nothing(track):
    lat = np.linspace(20, 50, 500)
    lon = np.linspace(-10, 20, 500)
    a = geo.pixel_of(track, lat, lon, WIDTH, SCAN, chunk=10_000)
    b = geo.pixel_of(track, lat, lon, WIDTH, SCAN, chunk=37)
    np.testing.assert_array_equal(np.isnan(a[0]), np.isnan(b[0]))
    np.testing.assert_allclose(np.nan_to_num(a[0]), np.nan_to_num(b[0]))
    np.testing.assert_allclose(np.nan_to_num(a[1]), np.nan_to_num(b[1]))


# -- overlay ------------------------------------------------------------------------------------

def test_bundled_data_loads():
    parts = overlay.load_coastline()
    assert len(parts) > 1000
    pts = np.vstack(parts)
    assert pts.shape[1] == 2
    assert -180.1 <= pts[:, 0].min() and pts[:, 0].max() <= 180.1
    assert -90.1 <= pts[:, 1].min() and pts[:, 1].max() <= 90.1

    cities = overlay.load_cities()
    assert len(cities) > 5000
    assert all(c[4] >= 50_000 for c in cities)
    assert [c[4] for c in cities] == sorted((c[4] for c in cities), reverse=True)
    names = {c[0] for c in cities}
    assert {"Milano", "Madrid", "Berlin"} & names or {"Milan", "Madrid"} & names


def test_font_renders_accents():
    f = overlay.font()
    assert f.height > 8 and 0 < f.ascent < f.height
    plain = f.mask("Zurich")
    accent = f.mask("Zürich")
    assert plain.any() and accent.any()
    assert accent.sum() > plain.sum()            # the diaeresis adds ink
    assert f.mask("").size == 0


def test_track_for_rejects_bad_input():
    assert overlay.track_for("METEOR-M2 3", None, [1.0, 2.0]) is None
    assert overlay.track_for("METEOR-M2 3", [TLE1, TLE2], None) is None
    assert overlay.track_for("METEOR-M2 3", [TLE1], [1.0, 2.0]) is None
    assert overlay.track_for("METEOR-M2 3", ["nonsense", "rubbish"], [1.0, 2.0]) is None
    got = overlay.track_for("METEOR-M2 3", [TLE1, TLE2], T0 + np.arange(20) * 0.1539)
    assert got is not None and got[1] == SCAN


def test_draw_marks_the_picture_and_leaves_the_original_alone(track):
    img = np.full((2400, WIDTH), 80, np.uint8)
    before = img.copy()
    out = overlay.draw(img, track, SCAN, WIDTH, coastlines=True, cities=True, min_pop=300_000)
    np.testing.assert_array_equal(img, before)              # the caller's image is untouched
    assert out.shape == (2400, WIDTH, 3)
    drawn = (out != 80).any(axis=2)
    assert drawn.sum() > 200, "the pass crosses land: something should have been drawn"
    assert (out == 0).any(), "labels are drawn with a dark outline over a light colour"


def test_draw_without_options_is_just_a_grey_to_rgb_copy(track):
    img = np.full((2400, WIDTH), 80, np.uint8)
    out = overlay.draw(img, track, SCAN, WIDTH, coastlines=False, cities=False)
    np.testing.assert_array_equal(out[:, :, 0], img)
    np.testing.assert_array_equal(out[:, :, 1], img)


def test_overlay_follows_the_geometric_correction(track):
    """The same place must come out at the same spot before and after the panoramic correction."""
    lat0, lon0 = geo.look_latlon(track, np.array([0.0]), np.array([1200]))
    lat, lon = float(lat0[0, 0]), float(lon0[0, 0]) + 5.0
    _, c_src = geo.pixel_of(track, lat, lon, WIDTH, SCAN)
    assert np.isfinite(c_src[0])
    cmap = overlay._column_map(WIDTH, 2800, corrected=True)
    c_out = float(np.asarray(cmap(c_src))[0])
    # going back through the correction's own mapping has to land on the source column again
    back = float(geometry.source_columns(WIDTH)[int(round(c_out))])
    assert abs(back - c_src[0]) < 2.0


def test_overlay_respects_rotation_and_mirror(track):
    img = np.full((2400, WIDTH), 80, np.uint8)
    plain = overlay.draw(img, track, SCAN, WIDTH, coastlines=True)
    mirrored = overlay.draw(img, track, SCAN, WIDTH, coastlines=True, mirror=True)
    rotated = overlay.draw(img, track, SCAN, WIDTH, coastlines=True, rotated=True)
    np.testing.assert_array_equal(plain[:, ::-1], mirrored)
    np.testing.assert_array_equal(plain[::-1, ::-1], rotated)


def test_coastline_is_not_joined_across_the_swath_edge(track):
    """A vertex that fell outside must break the line; joining past it drew streaks across the picture."""
    img = np.zeros((2400, WIDTH), np.uint8)
    out = overlay.draw(img, track, SCAN, WIDTH, coastlines=True, cities=False)
    runs = (out != 0).any(axis=2).sum(axis=0)    # drawn pixels per column
    assert runs.max() < 0.5 * out.shape[0], "no column should be a solid red streak"


def test_draw_on_a_corrected_image_is_the_right_size(track):
    img = np.full((2400, WIDTH), 80, np.uint8)
    corr = geometry.correct(img)
    out = overlay.draw(corr, track, SCAN, WIDTH, corrected=True, coastlines=True, cities=True)
    assert out.shape == (2400, 2800, 3)
    assert (out != 80).any(axis=2).sum() > 200


def test_png_roundtrip_of_an_overlaid_image(tmp_path, track):
    img = np.full((120, WIDTH), 90, np.uint8)
    out = overlay.draw(img, track, SCAN, WIDTH, coastlines=True)
    p = str(tmp_path / "ov.png")
    imaging.write_png(p, out)
    np.testing.assert_array_equal(imaging.read_png(p), out)


def test_colour_choice_reaches_the_picture(track):
    """The palette is what the interface offers; a chosen colour must be the one drawn."""
    img = np.full((2400, WIDTH), 80, np.uint8)
    for key in ("cyan", "green", "magenta"):
        out = overlay.draw(img, track, SCAN, WIDTH, coastlines=True, cities=False, coast_colour=key)
        want = overlay.PALETTE[key]
        hit = (out[:, :, 0] == want[0]) & (out[:, :, 1] == want[1]) & (out[:, :, 2] == want[2])
        assert hit.sum() > 200, key
    # a ready-made triple works too, and an unknown key falls back instead of crashing
    out = overlay.draw(img, track, SCAN, WIDTH, coastlines=True, coast_colour=(1, 2, 3))
    assert ((out[:, :, 0] == 1) & (out[:, :, 1] == 2) & (out[:, :, 2] == 3)).sum() > 200
    assert overlay.colour("not-a-colour") == overlay.PALETTE[overlay.DEFAULT_COAST]
    assert overlay.colour(None, "green") == overlay.PALETTE["green"]


def test_label_outline_follows_the_colour():
    """A dark label needs a light halo, a light one a dark halo, or it vanishes on its background."""
    assert overlay._outline(overlay.PALETTE["black"]) == (255, 255, 255)
    assert overlay._outline(overlay.PALETTE["white"]) == (0, 0, 0)
    assert overlay._outline(overlay.PALETTE["yellow"]) == (0, 0, 0)


def test_every_palette_entry_has_a_name_in_every_language():
    from meteorrx import i18n
    for code in i18n.LANGUAGES:
        t = i18n.texts(code, "gui")
        for key in overlay.PALETTE:
            assert t.get(f"col_{key}"), (code, key)
    assert overlay.DEFAULT_COAST in overlay.PALETTE
    assert overlay.DEFAULT_CITY in overlay.PALETTE


# -- regression against a real pass ---------------------------------------------------------------
# tests/data/geo_pass_m2_3.npz holds a window of the thermal channel of a real Meteor-M2-3 pass (northern
# Italy, 2026-11-03 20:29 UTC) over Sardinia and Corsica, with the line times and the TLE it was received
# against. The geometry was validated by eye at 1:1 on those islands - a statistical measure on this scene
# cannot do it, the night infrared is too cluttered with cloud to separate a 1-degree roll from noise.
#
# So this is a REGRESSION PIN, not an independent proof: it records where the validated geometry puts
# known landmarks, and fails if anything moves them. That is what it is for. Every change to the body
# frame that was proposed and measured to be worse - an inertial velocity vector, a different roll bias -
# moves these landmarks by 4 to 39 pixels, far outside the tolerance, so the pin does catch them.

import os

FIXTURE = os.path.join(os.path.dirname(__file__), "data", "geo_pass_m2_3.npz")

# (latitude, longitude) -> (row, column) in the decoded 1568-pixel grid of that pass
LANDMARKS = {
    "Capo Testa":     ((41.242, 9.139),  (2316.6, 209.6)),
    "Capo Teulada":   ((38.868, 8.638),  (2066.5, 254.4)),
    "Cap Corse":      ((43.005, 9.455),  (2504.5, 183.4)),
    "Capo Carbonara": ((39.102, 9.516),  (2079.7, 212.0)),
    "Alghero":        ((40.558, 8.317),  (2253.4, 252.4)),
    "Marseille":      ((43.296, 5.370),  (2590.2, 372.9)),
    "Barcelona":      ((41.385, 2.173),  (2440.9, 629.8)),
    "Palma":          ((39.570, 2.650),  (2240.6, 636.0)),
    "Algiers":        ((36.753, 3.060),  (1936.0, 670.8)),
    "Valencia":       ((39.470, -0.376), (2291.5, 888.3)),
}
TOL = 3.0


@pytest.fixture(scope="module")
def real_pass():
    if not os.path.exists(FIXTURE):
        pytest.skip("real-pass fixture not present")
    return np.load(FIXTURE, allow_pickle=False)


@pytest.fixture(scope="module")
def real_track(real_pass):
    from meteorrx import overlay
    got = overlay.track_for(str(real_pass["satellite"]), [str(x) for x in real_pass["tle"]],
                            real_pass["full_row_times"])
    assert got is not None
    assert len(got[0]) == int(real_pass["nrows"])
    return got


def _landmark_pixels(track, scan, width):
    out = {}
    for name, ((la, lo), _) in LANDMARKS.items():
        r, c = geo.pixel_of(track, la, lo, width, scan)
        out[name] = (float(r[0]), float(c[0]))
    return out


def test_real_pass_landmarks_have_not_moved(real_pass, real_track):
    """The validated geometry must keep putting known places where it put them."""
    track, scan = real_track
    got = _landmark_pixels(track, scan, int(real_pass["width"]))
    worst = 0.0
    for name, (_, (er, ec)) in LANDMARKS.items():
        r, c = got[name]
        assert np.isfinite(r) and np.isfinite(c), f"{name} fell outside the swath"
        worst = max(worst, abs(r - er), abs(c - ec))
        assert abs(r - er) < TOL and abs(c - ec) < TOL, \
            f"{name}: expected ({er}, {ec}), got ({r:.1f}, {c:.1f})"
    assert worst < TOL


def test_the_landmark_pin_is_sensitive_to_the_body_frame(real_pass, real_track):
    """A pin that nothing can break is worthless: each rejected frame change must trip it.

    These are the three changes an independent review proposed; each was measured against the real
    islands and each made the fit worse, so the pin has to reject all of them."""
    track, scan = real_track
    width = int(real_pass["width"])

    def rebuilt(roll=0.0, inertial=False, geodetic=False):
        t2 = geo.Track.__new__(geo.Track)
        t2.t, t2.v, t2.r = track.t, track.v, track.r
        vel = track.v + np.cross(np.array([0.0, 0.0, geo.OMEGA_E]), track.r) if inertial else track.v
        if geodetic:
            la, lo = geo._ecef_to_geodetic(track.r)
            cl, sl = np.cos(np.radians(la)), np.sin(np.radians(la))
            nd = -np.stack([cl * np.cos(np.radians(lo)), cl * np.sin(np.radians(lo)), sl], axis=-1)
        else:
            nd = -track.r / np.linalg.norm(track.r, axis=1, keepdims=True)
        vv = vel - nd * np.sum(vel * nd, axis=1, keepdims=True)
        t2.along = vv / np.linalg.norm(vv, axis=1, keepdims=True)
        t2.nadir, t2.cross = nd, np.cross(nd, t2.along)
        if roll:
            a = math.radians(roll)
            n2 = t2.nadir * math.cos(a) + t2.cross * math.sin(a)
            t2.cross = t2.cross * math.cos(a) - t2.nadir * math.sin(a)
            t2.nadir = n2
        return t2

    for label, kw in (("roll +1 deg", dict(roll=1.0)), ("roll -1 deg", dict(roll=-1.0)),
                      ("inertial velocity", dict(inertial=True)), ("geodetic nadir", dict(geodetic=True))):
        got = _landmark_pixels(rebuilt(**kw), scan, width)
        moved = max(max(abs(got[n][0] - er), abs(got[n][1] - ec))
                    for n, (_, (er, ec)) in LANDMARKS.items() if np.isfinite(got[n][0]))
        assert moved > TOL, f"{label} moved the landmarks by only {moved:.1f} px - the pin would miss it"


def test_row_times_are_per_channel():
    """Every channel's line stamps must come from its own segment ids.

    The segment ids of the channels below the lowest-offset one are shifted by 14, so one shared table
    hands them a time a whole line period early - eight rows, about 8 km on the ground."""
    from meteorrx import lrpt
    from tests import synth
    im = lrpt.MsuMrImager()
    pkts, _ = synth.build_packets(40, seq0=115)
    for raw in pkts:
        im.work(lrpt.Packet(raw))
    chans = im.channels_present
    assert len(chans) >= 2, "the synthetic stream should carry several channels"
    assert isinstance(im.line_time, list) and len(im.line_time) == 6
    times = {c: im.row_times(c) for c in chans}
    for c, tt in times.items():
        assert tt.size == im.get_channel(c).shape[0], c
    ref = times[chans[0]]
    for c in chans[1:]:
        other = times[c]
        n = min(ref.size, other.size)
        # the channels are read out together: their row times must agree to well under a line period
        assert n and np.abs(ref[:n] - other[:n]).max() < 0.5, c


def test_bracket_search_matches_the_exhaustive_one(real_track, real_pass):
    """pixel_of brackets the crossing on a coarse grid for speed; it must give the same answer."""
    from meteorrx import overlay
    track, scan = real_track
    width = int(real_pass["width"])
    pts = np.vstack(overlay.load_coastline()[:400])
    lat, lon = pts[:, 1].astype(float), pts[:, 0].astype(float)
    fast = geo.pixel_of(track, lat, lon, width, scan)
    old = geo._STRIDE
    try:
        geo._STRIDE = 10 ** 9                      # forces the row-by-row path
        slow = geo.pixel_of(track, lat, lon, width, scan)
    finally:
        geo._STRIDE = old
    np.testing.assert_array_equal(np.isnan(fast[0]), np.isnan(slow[0]))
    k = ~np.isnan(fast[0])
    assert k.sum() > 1000
    np.testing.assert_allclose(fast[0][k], slow[0][k], atol=1e-6)
    np.testing.assert_allclose(fast[1][k], slow[1][k], atol=1e-6)


def test_correction_commutes_with_a_mirror():
    """The resampling grid must be symmetric, or a corrected image is not the mirror of the mirrored one
    and the overlay - which maps columns before the viewer's flips - disagrees with it at the edges."""
    cols = geometry.source_columns(1568)
    assert abs(cols[0]) < 1e-6 and abs(cols[-1] - 1567) < 1e-6
    assert np.abs(cols - (1567 - cols)[::-1]).max() < 1e-6
    rng = np.random.default_rng(3)
    img = rng.integers(1, 255, (4, 1568), dtype=np.uint8)
    np.testing.assert_array_equal(geometry.correct(img[:, ::-1]), geometry.correct(img)[:, ::-1])


def test_labels_hanging_off_any_edge_do_not_break_the_overlay():
    """A city near an edge puts its label partly outside the picture.

    This must clip, not raise: the caller treats a failed overlay as "draw nothing", so one such label
    removed the whole overlay on every refresh - which is what a tester's log showed, fifty times over."""
    from meteorrx import overlay
    f = overlay.font()
    mask = f.mask("Roma")
    drawn = 0
    for y in (-50, -5, -1, 0, 3, 100, 190, 195, 199, 260):
        for x in (-80, -5, -1, 0, 7, 150, 290, 299, 400):
            rgb = np.zeros((200, 300, 3), np.uint8)            # fresh each time, or a repeat looks blank
            overlay._blit(rgb, mask, y, x, (255, 70, 70))      # must not raise
            hit = bool(rgb.any())
            # wholly outside must draw nothing; wholly inside must draw something. In between it may
            # land on a blank column of the glyph box, so only "did not raise" is required there.
            if y + mask.shape[0] <= 0 or y >= 200 or x + mask.shape[1] <= 0 or x >= 300:
                assert not hit, f"({y}, {x}) is outside the picture but drew"
            elif 0 <= y and y + mask.shape[0] <= 200 and 0 <= x and x + mask.shape[1] <= 300:
                assert hit, f"({y}, {x}) fits entirely but drew nothing"
            drawn += hit
    assert drawn > 30


def test_cities_at_the_edges_of_a_small_image_are_drawn(track):
    """The same thing through the public entry point, on an image small enough that labels overflow."""
    from meteorrx import overlay
    img = np.full((2400, WIDTH), 80, np.uint8)
    out = overlay.draw(img, track, SCAN, WIDTH, coastlines=False, cities=True,
                       min_pop=100_000, label_gap=10, max_cities=400)
    assert (out != 80).any(axis=2).sum() > 500, "labels should survive near the edges"


# -- second satellite: METEOR-M2-4 -----------------------------------------------------------------
# tests/data/geo_pass_m2_4.npz holds a window of the near-infrared channel of a real Meteor-M2-4 pass
# (Baltic, 2026-11-04 12:31 UTC) over Oland and Gotland. M2-4 matters separately because its roll bias
# started life as a value copied from SatDump; it was then measured on this pass by tracking the offset
# between Oland's bright centroid and its projected polygon as the roll was swept:
#
#     roll   -1.40  -0.90  -0.40  +0.10  +0.60  +1.10   (degrees)
#     dcol    +4.6   +2.1   -0.5   -3.2   -5.3   -8.1   (pixels across track)
#
# The crossing is at about -0.45 deg, against the -0.40 in SCAN, so the inherited value is right to
# within a tenth of a degree. Oland is a thin island in open sea, which is what makes the measurement
# clean; Gotland's box caught cloud and was discarded.

M24_FIXTURE = os.path.join(os.path.dirname(__file__), "data", "geo_pass_m2_4.npz")

M24_LANDMARKS = {
    "Oland N":    ((57.360, 17.070), (2449.8, 892.3)),
    "Oland S":    ((56.200, 16.400), (2345.9, 971.3)),
    "Gotland N":  ((57.930, 19.000), (2471.2, 764.8)),
    "Gotland S":  ((56.920, 18.150), (2383.5, 848.3)),
    "Copenhagen": ((55.676, 12.568), (2381.4, 1174.4)),
    "Stockholm":  ((59.330, 18.070), (2632.1, 766.0)),
    "Gdansk":     ((54.350, 18.650), (2110.5, 910.4)),
    "Riga":       ((56.950, 24.110), (2284.7, 515.1)),
    "Helsinki":   ((60.170, 24.940), (2615.6, 407.6)),
    "Warsaw":     ((52.230, 21.010), (1844.8, 836.4)),
}


@pytest.fixture(scope="module")
def m24_pass():
    if not os.path.exists(M24_FIXTURE):
        pytest.skip("Meteor-M2-4 fixture not present")
    return np.load(M24_FIXTURE, allow_pickle=False)


@pytest.fixture(scope="module")
def m24_track(m24_pass):
    from meteorrx import overlay
    got = overlay.track_for(str(m24_pass["satellite"]), [str(x) for x in m24_pass["tle"]],
                            m24_pass["full_row_times"])
    assert got is not None
    assert len(got[0]) == int(m24_pass["nrows"])
    return got


def test_m24_uses_its_own_scan_parameters():
    """M2-4 and M2-3 do not share a roll bias; a lookup that fell back to the default would be wrong."""
    assert geo.scan_params("METEOR-M2 4") == (110.1, -0.40)
    assert geo.scan_params("METEOR-M2 4") != geo.scan_params("METEOR-M2 3")


def test_m24_landmarks_have_not_moved(m24_pass, m24_track):
    track, scan = m24_track
    width = int(m24_pass["width"])
    worst = 0.0
    for name, ((la, lo), (er, ec)) in M24_LANDMARKS.items():
        r, c = geo.pixel_of(track, la, lo, width, scan)
        assert np.isfinite(r[0]) and np.isfinite(c[0]), f"{name} fell outside the swath"
        worst = max(worst, abs(r[0] - er), abs(c[0] - ec))
        assert abs(r[0] - er) < TOL and abs(c[0] - ec) < TOL, \
            f"{name}: expected ({er}, {ec}), got ({r[0]:.1f}, {c[0]:.1f})"
    assert worst < TOL


def test_m24_roll_bias_is_the_measured_one(m24_pass, m24_track):
    """Changing M2-4's roll by a quarter of a degree must move the landmarks more than the tolerance.

    The bias was measured to about a tenth of a degree, so the pin has to be tighter than that is loose."""
    track, scan = m24_track
    width = int(m24_pass["width"])
    for d in (-0.25, 0.25):
        a = math.radians(d)
        t2 = geo.Track.__new__(geo.Track)
        t2.t, t2.v, t2.r, t2.along = track.t, track.v, track.r, track.along
        t2.nadir = track.nadir * math.cos(a) + track.cross * math.sin(a)
        t2.cross = track.cross * math.cos(a) - track.nadir * math.sin(a)
        moved = 0.0
        for name, ((la, lo), (er, ec)) in M24_LANDMARKS.items():
            r, c = geo.pixel_of(t2, la, lo, width, scan)
            if np.isfinite(r[0]):
                moved = max(moved, abs(r[0] - er), abs(c[0] - ec))
        assert moved > TOL, f"a {d:+.2f} deg roll moved the landmarks by only {moved:.1f} px"

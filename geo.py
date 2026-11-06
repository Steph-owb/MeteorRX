"""Geolocation of an MSU-MR image: which point of the Earth each pixel looks at, and where a given
latitude / longitude falls in the picture.

The instrument sweeps a mirror across track at a constant angular rate while the satellite moves, so a
pixel is fixed by two things: the **time** of its image row (the MSU-MR stamps every scan line, see
`lrpt.MsuMrImager.row_times`) and its **scan angle**, linear in the column across a 110.1 deg swath.

For a row at time t the satellite state comes from the TLE through SGP4, in Earth-fixed coordinates. The
body frame is built nadir-down, x along the ground track; the look direction for scan angle a is

    d = cos(a) * nadir + sin(a) * cross_track

and the pixel sees the first intersection of that ray with the WGS84 ellipsoid.

The inverse (latitude / longitude -> pixel), used to place coastlines and city labels, exploits the fact
that the scan is perpendicular to the ground track: the row is the one whose scan plane contains the
target, i.e. where the along-track component of (target - satellite) changes sign. That is a single
monotonic crossing, so one interpolation per target rather than a search over the whole image.

Scan parameters are the published ones for MSU-MR LRPT (SatDump's projection settings): 110.1 deg of scan
for Meteor-M2-3 and M2-4, 110.6 deg for Meteor-M2, with a small roll bias per satellite.
"""
from __future__ import annotations

import math

import numpy as np

from .orbit import OMEGA_E, WGS84_A, WGS84_F, _gmst

WGS84_B = WGS84_A * (1.0 - WGS84_F)

# scan angle (deg, full width) and roll bias (deg) per satellite, from SatDump's projection settings
SCAN = {
    "METEOR-M2": (110.6, 2.3),
    "METEOR-M2 2": (110.6, 2.3),
    "METEOR-M2 3": (110.1, -0.31),
    "METEOR-M2 4": (110.1, -0.40),
}
DEFAULT_SCAN = (110.1, 0.0)

# Which end of a decoded line is east. Determined from real passes, not assumed: on Antonio Mazzoni's
# Meteor-M2-3 pass of 2026-11-03 20:29 UTC the correlation between "this pixel is land" and the
# high-passed thermal channel (land cools below the sea at night, so the correct alignment is the one
# with the strongest NEGATIVE correlation) gives r = -0.21 with east on the left against r = -0.12 with
# east on the right, and both an along-track and a cross-track offset scan peak at zero. Reversing the
# row order collapses r to +0.03, which confirms row 0 is the earliest line. East on the left with the
# earliest line at the top is also what makes the viewer's 180-degree rotation produce a north-up,
# east-right picture, which is why that option exists.
EAST_ON_LEFT = True

# Rows per step of the coarse bracket search in pixel_of. Only a speed knob: the crossing is resolved
# row by row inside the bracket, so the answer does not depend on it (a test checks both code paths).
_STRIDE = 32


def scan_params(satellite: str | None) -> tuple[float, float]:
    """(scan angle, roll bias) in degrees for a satellite name, tolerant of spelling."""
    if not satellite:
        return DEFAULT_SCAN
    k = satellite.upper().replace("-", " ").replace("_", " ")
    k = " ".join(k.split())
    for name, v in SCAN.items():
        if " ".join(name.upper().replace("-", " ").split()) == k:
            return v
    return DEFAULT_SCAN


def _teme_to_ecef(r_teme, v_teme, jd):
    """TEME -> Earth-fixed, as orbit.Observer.look does, vectorised over time."""
    th = np.array([_gmst(j) for j in jd])
    c, s = np.cos(th), np.sin(th)
    r = np.empty_like(r_teme)
    r[:, 0] = c * r_teme[:, 0] + s * r_teme[:, 1]
    r[:, 1] = -s * r_teme[:, 0] + c * r_teme[:, 1]
    r[:, 2] = r_teme[:, 2]
    v = np.empty_like(v_teme)
    v[:, 0] = c * v_teme[:, 0] + s * v_teme[:, 1]
    v[:, 1] = -s * v_teme[:, 0] + c * v_teme[:, 1]
    v[:, 2] = v_teme[:, 2]
    v -= np.cross(np.array([0.0, 0.0, OMEGA_E]), r)        # rotating frame
    return r, v


class Track:
    """Satellite state and body frame for every row of an image."""

    def __init__(self, satrec, times_unix: np.ndarray, roll_deg: float = 0.0):
        t = np.asarray(times_unix, dtype=np.float64)
        jd = t / 86400.0 + 2440587.5
        jd_i = np.floor(jd - 0.5) + 0.5
        fr = jd - jd_i
        rs, vs = [], []
        for a, b in zip(jd_i, fr):
            e, r, v = satrec.sgp4(float(a), float(b))
            if e:
                r = v = (np.nan, np.nan, np.nan)
            rs.append(r)
            vs.append(v)
        self.t = t
        self.r, self.v = _teme_to_ecef(np.array(rs, float), np.array(vs, float), jd)
        rn = np.linalg.norm(self.r, axis=1, keepdims=True)
        self.nadir = -self.r / rn                                   # down
        vv = self.v - self.nadir * np.sum(self.v * self.nadir, axis=1, keepdims=True)
        self.along = vv / np.linalg.norm(vv, axis=1, keepdims=True)  # ground-track direction
        self.cross = np.cross(self.nadir, self.along)                # completes the right-handed frame
        if roll_deg:
            a = math.radians(roll_deg)
            n2 = self.nadir * math.cos(a) + self.cross * math.sin(a)
            self.cross = self.cross * math.cos(a) - self.nadir * math.sin(a)
            self.nadir = n2

    def __len__(self):
        return len(self.t)


def _ecef_to_geodetic(p: np.ndarray):
    """ECEF (km) -> (lat deg, lon deg), WGS84, by the usual closed-form iteration."""
    x, y, z = p[..., 0], p[..., 1], p[..., 2]
    lon = np.degrees(np.arctan2(y, x))
    e2 = WGS84_F * (2 - WGS84_F)
    r = np.hypot(x, y)
    lat = np.arctan2(z, r * (1 - e2))
    for _ in range(5):
        n = WGS84_A / np.sqrt(1 - e2 * np.sin(lat) ** 2)
        lat = np.arctan2(z + e2 * n * np.sin(lat), r)
    return np.degrees(lat), lon


def _geodetic_to_ecef(lat_deg, lon_deg, alt_km=0.0):
    lat, lon = np.radians(lat_deg), np.radians(lon_deg)
    e2 = WGS84_F * (2 - WGS84_F)
    n = WGS84_A / np.sqrt(1 - e2 * np.sin(lat) ** 2)
    return np.stack([(n + alt_km) * np.cos(lat) * np.cos(lon),
                     (n + alt_km) * np.cos(lat) * np.sin(lon),
                     (n * (1 - e2) + alt_km) * np.sin(lat)], axis=-1)


def scan_angles(width: int, scan_deg: float, east_left: bool = EAST_ON_LEFT) -> np.ndarray:
    """Scan angle (radians) of every column, nadir at the centre."""
    a = (np.arange(width, dtype=np.float64) + 0.5) / width - 0.5
    a = a * math.radians(scan_deg)
    return -a if east_left else a


def look_latlon(track: Track, angles: np.ndarray, rows: np.ndarray | None = None):
    """(lat, lon) of the ground point seen at each scan angle, for the given rows (default: all)."""
    idx = np.arange(len(track)) if rows is None else np.asarray(rows)
    r = track.r[idx][:, None, :]
    d = (np.cos(angles)[None, :, None] * track.nadir[idx][:, None, :]
         + np.sin(angles)[None, :, None] * track.cross[idx][:, None, :])
    k = WGS84_A / WGS84_B
    rp = r * np.array([1.0, 1.0, k])
    dp = d * np.array([1.0, 1.0, k])
    a = np.sum(dp * dp, axis=-1)
    b = 2.0 * np.sum(rp * dp, axis=-1)
    c = np.sum(rp * rp, axis=-1) - WGS84_A ** 2
    disc = b * b - 4 * a * c
    ok = disc >= 0
    s = np.where(ok, (-b - np.sqrt(np.maximum(disc, 0))) / (2 * a), np.nan)
    p = r + s[..., None] * d
    lat, lon = _ecef_to_geodetic(p)
    return np.where(ok, lat, np.nan), np.where(ok, lon, np.nan)


def pixel_of(track: Track, lat, lon, width: int, scan_deg: float, east_left: bool = EAST_ON_LEFT,
             chunk: int = 2000):
    """Pixel (row, col) of given latitudes / longitudes, NaN where the point is outside the swath.

    The row is where the target crosses the scan plane (along-track component zero); the column follows
    from the scan angle at that row. Targets are processed in chunks, so a whole coastline can be passed
    in one call without building an (N, rows) array for all of it at once."""
    lat = np.atleast_1d(np.asarray(lat, float))
    lon = np.atleast_1d(np.asarray(lon, float))
    if lat.size > chunk:
        rr, cc = [], []
        for i in range(0, lat.size, chunk):
            a, b = pixel_of(track, lat[i:i + chunk], lon[i:i + chunk], width, scan_deg, east_left, chunk)
            rr.append(a)
            cc.append(b)
        return np.concatenate(rr), np.concatenate(cc)
    p = _geodetic_to_ecef(lat, lon)
    n = len(track)
    base = np.sum(track.r * track.along, axis=1)

    def offsets(rows_idx):
        """Along-track offset of every target at the given rows, without materialising the differences."""
        return p @ track.along[rows_idx].T - base[rows_idx][None, :]

    # The offset decreases monotonically as the satellite flies past, so the crossing can be bracketed on
    # a coarse grid first and only then resolved row by row. Searching all rows for every vertex made a
    # whole-coastline overlay cost seconds; this is the same answer for a fraction of the work.
    if n > 4 * _STRIDE:
        coarse = np.arange(0, n, _STRIDE)
        if coarse[-1] != n - 1:
            coarse = np.append(coarse, n - 1)
        fc = offsets(coarse)
        sgn = fc > 0
        cr = sgn[:, :-1] != sgn[:, 1:]
        has = cr.any(axis=1)
        k = np.where(has, cr.argmax(axis=1), 0)
        lo = coarse[k]
        span = int(np.max(np.diff(coarse))) + 1
        fine = np.minimum(lo[:, None] + np.arange(span)[None, :], n - 1)
        f = np.sum((p[:, None, :] - track.r[fine]) * track.along[fine], axis=-1)
        sign = f > 0
        cross = sign[:, :-1] != sign[:, 1:]
        has &= cross.any(axis=1)
        first = np.where(has, cross.argmax(axis=1), 0)
        row0 = lo.astype(np.float64)
    else:
        f = offsets(np.arange(n))
        sign = f > 0
        cross = sign[:, :-1] != sign[:, 1:]
        has = cross.any(axis=1)
        first = np.where(has, cross.argmax(axis=1), 0)
        row0 = np.zeros(lat.size)
    f0 = np.take_along_axis(f, first[:, None], 1)[:, 0]
    f1 = np.take_along_axis(f, np.minimum(first + 1, f.shape[1] - 1)[:, None], 1)[:, 0]
    frac = np.where(np.abs(f1 - f0) > 1e-9, f0 / (f0 - f1), 0.0)
    row = np.where(has, row0 + first + np.clip(frac, 0.0, 1.0), np.nan)
    i = np.clip(np.nan_to_num(row).astype(int), 0, n - 1)
    rel_i = p - track.r[i]
    rel_i /= np.linalg.norm(rel_i, axis=1, keepdims=True)
    ang = np.arctan2(np.sum(rel_i * track.cross[i], axis=1), np.sum(rel_i * track.nadir[i], axis=1))
    if east_left:
        ang = -ang
    col = (ang / math.radians(scan_deg) + 0.5) * width - 0.5
    # the target must be on the near side of the Earth: a point on the far side has a direction from the
    # satellite that can still fall inside the scan cone, and would otherwise be drawn in the picture
    clat, slat = np.cos(np.radians(lat)), np.sin(np.radians(lat))
    up = np.stack([clat * np.cos(np.radians(lon)), clat * np.sin(np.radians(lon)), slat], axis=-1)
    visible = np.sum((track.r[i] - p) * up, axis=1) > 0
    # half a pixel of slack at each end, so the outermost column is not lost to rounding
    inside = has & visible & (col >= -0.5) & (col <= width - 0.5) & np.isfinite(row)
    return np.where(inside, row, np.nan), np.where(inside, np.clip(col, 0.0, width - 1.0), np.nan)

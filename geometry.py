"""Geometric correction of the MSU-MR scan (panoramic / "earth curvature" correction).

The MSU-MR mirror sweeps at a constant angular rate, so a transmitted line is uniform in **scan angle**:
near the edges of the 2800 km swath one pixel covers several kilometres of ground, and the picture is
foreshortened there (coastlines squeezed, the swath looking narrower than it is). The correction resamples
each line to a uniform **ground** spacing of 1 km per pixel, which widens a 1568-pixel line to 2800 pixels
and restores the proportions at the edges.

Geometry and parameters are those of SatDump's earth-curvature correction and of its
`resources/projections_settings/meteor_m2-*_msumr_lrpt.json` (swath 2800 km, 1 km/pixel, altitude 820 km,
Earth radius 6371 km), so the result is directly comparable with SatDump and with SmoothMeteor. One sub-pixel detail differs:
the normalised scan angle is mapped onto the last real sample (in_width - 1) rather than onto in_width, so
the edge of the swath lands on the edge pixel instead of overshooting it by 0.8 pixel and saturating.

For a satellite at radius r = R + h looking at a nadir angle theta, the Earth-central angle gamma from the
sub-satellite point obeys  tan(theta) = R.sin(gamma) / (r - R.cos(gamma)).  Output columns are uniform in
gamma (equal ground steps), input columns are uniform in theta (equal angular steps).

The decoded images are never modified: corrected copies are written beside them.
"""
from __future__ import annotations

import numpy as np

EARTH_RADIUS_KM = 6371.0
SWATH_KM = 2800.0          # MSU-MR swath
RESOL_KM = 1.0             # ground sampling of the corrected image
ALTITUDE_KM = 820.0        # Meteor-M orbit


def _nadir_angle(gamma: np.ndarray | float, r_orbit: float) -> np.ndarray | float:
    """Nadir (scan) angle seen from the satellite for an Earth-central angle gamma."""
    return -np.arctan(EARTH_RADIUS_KM * np.sin(gamma) / (np.cos(gamma) * EARTH_RADIUS_KM - r_orbit))


def source_columns(in_width: int, swath_km: float = SWATH_KM, resol_km: float = RESOL_KM,
                   altitude_km: float = ALTITUDE_KM) -> np.ndarray:
    """Fractional input column for each output column of the corrected image."""
    if in_width < 2:
        raise ValueError("in_width must be >= 2")
    r_orbit = EARTH_RADIUS_KM + altitude_km
    out_w = int(round(swath_km / resol_km))
    view = swath_km / EARTH_RADIUS_KM                      # Earth-central angle of the whole swath
    edge = _nadir_angle(view / 2.0, r_orbit)               # scan angle at the edge of the swath
    # Pixel centres on both sides: output column i covers the ground interval [i, i+1), so its centre is
    # at i + 0.5, and the input column it lands on is a centre too (x = in_width * n - 0.5). Mixing the
    # two conventions (edges mapped onto centres) made the grid asymmetric by a whole pixel, so a
    # corrected image was not the mirror of the mirrored one - which the overlay then had to disagree
    # with, since it maps columns before the viewer's flips. This mapping is symmetric to 1e-13.
    gamma = ((np.arange(out_w, dtype=np.float64) + 0.5) / out_w - 0.5) * view
    theta = _nadir_angle(gamma, r_orbit)
    return np.clip(in_width * ((theta / edge + 1.0) / 2.0) - 0.5, 0.0, in_width - 1.0)


def correct(img: np.ndarray, swath_km: float = SWATH_KM, resol_km: float = RESOL_KM,
            altitude_km: float = ALTITUDE_KM) -> np.ndarray:
    """Panoramic correction of a decoded image, grey (H, W) or RGB (H, W, 3), uint8.

    Returns a new image of width round(swath_km / resol_km); the input is left untouched. Columns are
    interpolated linearly; missing (black) pixels stay black instead of bleeding into their neighbours."""
    if img.ndim not in (2, 3) or img.shape[0] == 0 or img.shape[1] < 2:
        return img
    cols = source_columns(img.shape[1], swath_km, resol_km, altitude_km)
    i0 = np.floor(cols).astype(np.intp)
    i1 = np.minimum(i0 + 1, img.shape[1] - 1)
    frac = (cols - i0).astype(np.float32)
    if img.ndim == 3:
        frac = frac[:, None]
    a = img[:, i0].astype(np.float32)
    b = img[:, i1].astype(np.float32)
    out = a + (b - a) * frac
    # a missing pixel must not be averaged with a valid one: keep the valid side, stay black if both are
    out = np.where(a == 0, b, np.where(b == 0, a, out))
    return np.clip(out, 0, 255).astype(np.uint8)


def scale_factors(in_width: int = 1568, **kw) -> tuple[float, float]:
    """(width ratio, edge magnification): how much wider the corrected image is, and how much an input
    pixel at the edge of the swath is stretched compared with one at nadir. For documentation and tests."""
    cols = source_columns(in_width, **kw)
    d = np.diff(cols)
    # the outermost steps sit on the clip (the output grid reaches a hair past the last input sample),
    # so take the first step that is a real one
    k = int(np.flatnonzero(d > 0)[0]) + 1          # first step that is not clipped or part-clipped
    return len(cols) / in_width, float(d[len(d) // 2] / d[k])

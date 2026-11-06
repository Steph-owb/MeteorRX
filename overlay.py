"""Map overlays drawn on a decoded pass: coastlines and city labels, placed by real geolocation.

Every point is placed through `geo.pixel_of`, which inverts the instrument geometry from the TLE and the
MSU-MR line timestamps, so the overlay follows the real scan rather than a guessed affine fit. The same
geometry is what makes the overlay survive the panoramic correction: `pixel_of` always works on the
decoded 1568-pixel grid, and the column is mapped afterwards through whatever resampling was applied.

Bundled data, all redistributable:

- `data/coastline.bin.gz` : Natural Earth 1:10m coastline, public domain (naturalearthdata.com).
- `data/cities.tsv.gz`    : GeoNames cities of 50 000 inhabitants or more, CC BY 4.0 (geonames.org).
- `data/font.bin.gz`      : DejaVu Sans Bold baked to 1-bit glyphs, so labels need no font library.

Nothing here touches the decoded products: the caller passes a copy and gets a new RGB image back.
"""
from __future__ import annotations

import gzip
import os
import struct

import numpy as np

from . import geo, geometry

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

ATTRIBUTION = "Coastline: Natural Earth (public domain). Cities: GeoNames, CC BY 4.0."

# Overlay colours offered in the interface. The keys are stored in window.json and never translated; the
# names shown to the user come from i18n. Chosen to stay legible on a grey infrared scene, where pure red
# is the one that reads worst: cyan and yellow sit furthest from the greys of cloud, sea and land.
PALETTE: dict[str, tuple[int, int, int]] = {
    "cyan":    (0, 230, 255),
    "yellow":  (255, 225, 60),
    "green":   (80, 255, 110),
    "orange":  (255, 150, 40),
    "magenta": (255, 90, 220),
    "white":   (255, 255, 255),
    "black":   (0, 0, 0),
    "red":     (255, 70, 70),
}
DEFAULT_COAST = "cyan"
DEFAULT_CITY = "yellow"


def colour(key: str | None, fallback: str = DEFAULT_COAST) -> tuple[int, int, int]:
    """Palette colour for a stored key, tolerant of an unknown or missing one."""
    return PALETTE.get(key or "", PALETTE[fallback])


def _outline(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """Halo that keeps a label readable: dark behind a light colour, light behind a dark one."""
    luma = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    return (0, 0, 0) if luma > 90 else (255, 255, 255)

_coast: list[np.ndarray] | None = None
_cities: list[tuple[str, float, float, str, int]] | None = None
_font: "Font | None" = None


def load_coastline() -> list[np.ndarray]:
    """Coastline parts as (N, 2) float32 arrays of longitude / latitude. Cached."""
    global _coast
    if _coast is None:
        path = os.path.join(DATA_DIR, "coastline.bin.gz")
        with gzip.open(path, "rb") as f:
            raw = f.read()
        magic, ver, nparts = struct.unpack_from("<4sHI", raw, 0)
        if magic != b"MRXC" or ver != 1:
            raise ValueError(f"{path}: not a MeteorRX coastline file")
        out, pos = [], 10
        for _ in range(nparts):
            n = struct.unpack_from("<I", raw, pos)[0]
            pos += 4
            out.append(np.frombuffer(raw, "<f4", n * 2, pos).reshape(-1, 2))
            pos += n * 8
        _coast = out
    return _coast


def load_cities() -> list[tuple[str, float, float, str, int]]:
    """(name, lat, lon, country, population), largest first. Cached."""
    global _cities
    if _cities is None:
        out = []
        with gzip.open(os.path.join(DATA_DIR, "cities.tsv.gz"), "rt", encoding="utf-8") as f:
            for line in f:
                if line.startswith("#"):
                    continue
                p = line.rstrip("\n").split("\t")
                if len(p) >= 5:
                    out.append((p[0], float(p[1]), float(p[2]), p[3], int(p[4])))
        _cities = out
    return _cities


class Font:
    """The baked 1-bit label font."""

    def __init__(self, path: str):
        with gzip.open(path, "rb") as f:
            raw = f.read()
        magic, ver, self.height, self.ascent, n = struct.unpack_from("<4sHHHH", raw, 0)
        if magic != b"MRXF" or ver != 1:
            raise ValueError(f"{path}: not a MeteorRX font file")
        self.glyphs: dict[str, tuple[int, np.ndarray]] = {}
        pos = 12
        for _ in range(n):
            code, adv, w, nb = struct.unpack_from("<HBBH", raw, pos)
            pos += 6
            bits = np.unpackbits(np.frombuffer(raw, np.uint8, nb, pos))
            pos += nb
            self.glyphs[chr(code)] = (adv, bits[: self.height * w].reshape(self.height, w).astype(bool))

    def mask(self, text: str) -> np.ndarray:
        """Boolean (height, width) mask of a rendered string."""
        gl = [self.glyphs.get(c) or self.glyphs.get("?") for c in text]
        gl = [g for g in gl if g]
        if not gl:
            return np.zeros((self.height, 0), bool)
        total = sum(a for a, _ in gl) + 2
        m = np.zeros((self.height, total), bool)
        x = 0
        for adv, bm in gl:
            w = min(bm.shape[1], total - x)
            if w > 0:
                m[:, x:x + w] |= bm[:, :w]
            x += adv
        return m


def font() -> Font:
    global _font
    if _font is None:
        _font = Font(os.path.join(DATA_DIR, "font.bin.gz"))
    return _font


def track_for(satellite: str | None, tle, row_times) -> tuple[geo.Track, float] | None:
    """(track, scan angle) for a decoded pass, or None when it cannot be geolocated.

    Needs the two TLE lines the pass was received with and the per-row times from
    `lrpt.MsuMrImager.row_times`; without either there is nothing to place an overlay against."""
    if not tle or len(tle) < 2 or row_times is None or len(row_times) < 2:
        return None
    try:
        from sgp4.api import Satrec
        sat = Satrec.twoline2rv(tle[0], tle[1])
        scan_deg, roll_deg = geo.scan_params(satellite)
        track = geo.Track(sat, np.asarray(row_times, dtype=np.float64), roll_deg)
        if not np.isfinite(track.r).all():   # twoline2rv accepts rubbish; SGP4 only fails when propagated
            return None
        return track, scan_deg
    except Exception:  # noqa: BLE001  (a bad TLE must cost the overlay, not the picture)
        return None


def _as_rgb(img: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(img if img.ndim == 3 else np.dstack([img] * 3), dtype=np.uint8)


def _blit(rgb: np.ndarray, mask: np.ndarray, y: int, x: int, colour, outline=(0, 0, 0)) -> None:
    """Draw a glyph mask at (y, x), with a one-pixel dark outline so it reads over any background."""
    h, w = mask.shape
    H, W = rgb.shape[:2]
    y, x = int(y), int(x)
    y0, x0 = max(0, y - 1), max(0, x - 1)
    y1, x1 = min(H, y + h + 1), min(W, x + w + 1)
    if y1 <= y0 or x1 <= x0:
        return
    # A label beside a city near any edge hangs off the picture. Copy only the part of it that lands
    # inside: assuming it fits raised, and because the caller treats a failed overlay as "draw nothing",
    # one city at the top of the image took the whole overlay down on every refresh.
    i0, i1 = max(0, y0 - y), min(h, y1 - y)
    j0, j1 = max(0, x0 - x), min(w, x1 - x)
    if i1 <= i0 or j1 <= j0:
        return
    big = np.zeros((y1 - y0, x1 - x0), bool)
    big[(y + i0) - y0:(y + i1) - y0, (x + j0) - x0:(x + j1) - x0] = mask[i0:i1, j0:j1]
    halo = big.copy()
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            halo |= np.roll(np.roll(big, dy, 0), dx, 1)
    view = rgb[y0:y1, x0:x1]
    view[halo & ~big] = outline
    view[big] = colour


def _footprint(track: geo.Track, src_width: int, scan_deg: float, east_left: bool, margin: float = 2.0):
    """(lat_min, lat_max, lon_min, lon_max, wraps) covered by the pass, from its corners and edges.

    `wraps` is set when the swath crosses the 180th meridian or covers a pole, in which case longitude
    cannot be bounded and only the latitude test is applied."""
    n = len(track)
    rows = np.unique(np.clip(np.linspace(0, n - 1, min(n, 64)).astype(int), 0, n - 1))
    ang = geo.scan_angles(src_width, scan_deg, east_left)[[0, src_width // 2, src_width - 1]]
    lat, lon = geo.look_latlon(track, ang, rows)
    lat, lon = lat[np.isfinite(lat)], lon[np.isfinite(lon)]
    if lat.size == 0:
        return None
    la0, la1 = lat.min() - margin, lat.max() + margin
    wraps = la1 > 88.0 or la0 < -88.0 or (lon.max() - lon.min()) > 180.0
    return la0, la1, lon.min() - margin, lon.max() + margin, wraps


def _touches(box, pts: np.ndarray) -> bool:
    """Could this polyline reach the swath? Deliberately generous: it only has to reject the far side."""
    if box is None:
        return True
    la0, la1, lo0, lo1, wraps = box
    lat, lon = pts[:, 1], pts[:, 0]
    if lat.max() < la0 or lat.min() > la1:
        return False
    return True if wraps else not (lon.max() < lo0 or lon.min() > lo1)


def _column_map(src_width: int, out_width: int, corrected: bool):
    """Maps a decoded column onto the column of the image actually being drawn on."""
    if not corrected:
        if out_width == src_width:
            return lambda c: c
        k = (out_width - 1) / (src_width - 1)
        return lambda c: c * k
    cols = geometry.source_columns(src_width)
    idx = np.arange(len(cols), dtype=np.float64) * (out_width - 1) / (len(cols) - 1)
    return lambda c: np.interp(c, cols, idx)


def draw(img: np.ndarray, track: geo.Track, scan_deg: float, src_width: int, *,
         east_left: bool = geo.EAST_ON_LEFT,
         corrected: bool = False, rotated: bool = False, mirror: bool = False,
         coastlines: bool = True, cities: bool = False, min_pop: int = 200_000,
         max_cities: int = 60, label_gap: int = 90,
         coast_colour=DEFAULT_COAST, city_colour=DEFAULT_CITY) -> np.ndarray:
    """Return a new RGB copy of `img` with the requested overlays drawn on it.

    `src_width` is the width of the decoded image the geolocation refers to (1568 for MSU-MR); set
    `corrected` when `img` has been through `geometry.correct`, and `rotated` / `mirror` to match the
    flips the viewer applied, so the overlay lands on the same pixels the user is looking at."""
    rgb = _as_rgb(img)
    H, W = rgb.shape[:2]
    cmap = _column_map(src_width, W, corrected)
    # a palette key or a ready-made (r, g, b), so a caller can pass either
    coast_rgb = coast_colour if isinstance(coast_colour, tuple) else colour(coast_colour, DEFAULT_COAST)
    city_rgb = city_colour if isinstance(city_colour, tuple) else colour(city_colour, DEFAULT_CITY)
    # rows are never resampled - the correction and the enhancements only change the width - so an image
    # row is a track row. A composite truncated to its shortest channel simply ends early, and the points
    # past its last row fall outside and are dropped.

    def place(lat, lon):
        """Pixel (row, col) of each point, NaN-padded so the result lines up with the input: a vertex
        that fell outside the swath must break the line, never be joined to the next one that did not."""
        rr, c = geo.pixel_of(track, lat, lon, src_width, scan_deg, east_left)
        cc = np.where(np.isfinite(c), cmap(np.nan_to_num(c)), np.nan)
        # round to whole pixels BEFORE the flips, so that turning mirror on moves the overlay exactly
        # the way it moves the picture instead of shifting it by a pixel
        rr, cc = np.rint(rr), np.rint(cc)
        if rotated:
            rr, cc = (H - 1) - rr, (W - 1) - cc
        if mirror:
            cc = (W - 1) - cc
        return rr, cc

    # Ground footprint of the pass, with a margin for the swath. `pixel_of` costs O(points x rows), and
    # the world coastline is 400 000 vertices against a few thousand rows: without this the overlay takes
    # ten seconds and blocks the interface at every preview refresh. Almost all of the world is nowhere
    # near the pass, and a cheap box test removes it.
    box = _footprint(track, src_width, scan_deg, east_left)

    if coastlines:
        parts = [p for p in load_coastline() if len(p) >= 2 and _touches(box, p)]
        if parts:
            # One projection for every kept vertex at once: pixel_of costs O(points x rows) and its fixed
            # cost is the row dimension, so calling it per part made it ten times slower than it needs
            # to be. The parts are sliced back out afterwards, which keeps the line breaks exact.
            ends = np.cumsum([len(p) for p in parts])
            allpts = np.concatenate(parts)
            all_rr, all_cc = place(allpts[:, 1].astype(np.float64), allpts[:, 0].astype(np.float64))
        for i, part in enumerate(parts):
            lo = 0 if i == 0 else ends[i - 1]
            rr, cc = all_rr[lo:ends[i]], all_cc[lo:ends[i]]
            ok = np.isfinite(rr) & np.isfinite(cc)
            if not ok.any():
                continue
            ri, ci = np.nan_to_num(rr).astype(int), np.nan_to_num(cc).astype(int)
            # join consecutive vertices, but only where both ends were actually inside the swath
            joinable = np.flatnonzero(ok[:-1] & ok[1:])
            for a in joinable:
                dr, dc = ri[a + 1] - ri[a], ci[a + 1] - ci[a]
                n = int(max(abs(dr), abs(dc)))
                if n == 0:
                    ys, xs = ri[a:a + 1], ci[a:a + 1]
                else:
                    k = np.arange(n + 1)
                    ys = ri[a] + np.rint(dr * k / n).astype(int)
                    xs = ci[a] + np.rint(dc * k / n).astype(int)
                m = (ys >= 0) & (ys < H) & (xs >= 0) & (xs < W)
                rgb[ys[m], xs[m]] = coast_rgb
            lone = np.flatnonzero(ok & ~np.r_[False, ok[:-1]] & ~np.r_[ok[1:], False])
            if lone.size:
                ys, xs = ri[lone], ci[lone]
                m = (ys >= 0) & (ys < H) & (xs >= 0) & (xs < W)
                rgb[ys[m], xs[m]] = coast_rgb

    if cities:
        pick = [c for c in load_cities()
                if c[4] >= min_pop and _touches(box, np.array([[c[2], c[1]]]))]
        if pick:
            lat = np.array([c[1] for c in pick])
            lon = np.array([c[2] for c in pick])
            rr, cc = place(lat, lon)
            names = [c[0] for c in pick]
            pops = [c[4] for c in pick]
            sel = np.flatnonzero(np.isfinite(rr) & np.isfinite(cc))
            order = np.argsort([-pops[i] for i in sel])
            f = font()
            drawn: list[tuple[int, int]] = []
            for j in order:
                i = sel[j]
                y, x, name = int(rr[i]), int(cc[i]), names[i]
                if not (0 <= y < H and 0 <= x < W):
                    continue
                if any(abs(y - py) < label_gap and abs(x - px) < label_gap * 3 for py, px in drawn):
                    continue
                for dy in (-2, -1, 0, 1, 2):     # the marker dot
                    for dx in (-2, -1, 0, 1, 2):
                        if dy * dy + dx * dx <= 5 and 0 <= y + dy < H and 0 <= x + dx < W:
                            rgb[y + dy, x + dx] = city_rgb
                m = f.mask(name)
                lx = x + 6 if x + 6 + m.shape[1] < W else x - 6 - m.shape[1]
                _blit(rgb, m, y - f.ascent // 2, lx, city_rgb, _outline(city_rgb))
                drawn.append((y, x))
                if len(drawn) >= max_cities:
                    break
    return rgb

"""Optional image enhancements applied to the view (and to saved copies), never to the decoded originals.

- `colour()`   : colorimetric correction of an RGB composite. MSU-MR composites come out flat and greenish
                 because the three channels have different gains and a haze offset; each channel is stretched
                 on its own percentiles (which white-balances the scene), then saturation and gamma are
                 nudged. Missing (black) pixels stay black.
- `infrared()` : contrast-limited adaptive histogram equalisation (CLAHE) for an infrared channel, where the
                 useful range is a narrow band of cloud-top temperatures. Tiles are equalised separately with
                 a clip limit (so flat sea does not turn into amplified noise) and blended bilinearly, which
                 brings out cloud structure without the blotching of a global equalisation.

Missing-line interpolation is not here: it is `lrpt.fill_missing`, which knows the 8-line / 112-column
segment structure of the MSU-MR stream.
"""
from __future__ import annotations

import numpy as np


def _valid(img: np.ndarray) -> np.ndarray:
    """Mask of decoded pixels (black = missing)."""
    return img.max(axis=2) > 0 if img.ndim == 3 else img > 0


def colour(rgb: np.ndarray, lo_pct: float = 0.5, hi_pct: float = 99.5, balance: float = 0.35,
           saturation: float = 1.12, gamma: float = 1.0) -> np.ndarray:
    """Colorimetric enhancement of an RGB composite (H, W, 3) uint8. Returns a new image.

    The contrast stretch is computed over the three channels together, so the hue of the scene is kept;
    `balance` then blends in a per-channel stretch (0 = none, 1 = full grey-world white balance), which
    lifts the haze cast without turning vegetation acid-yellow. Missing pixels stay black."""
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        return rgb
    m = _valid(rgb)
    if m.sum() < 100:
        return rgb
    allv = rgb[m].astype(np.float32)
    glo, ghi = np.percentile(allv, [lo_pct, hi_pct])
    if ghi <= glo:
        ghi = glo + 1.0
    out = np.zeros(rgb.shape, np.float32)
    for c in range(3):
        x = rgb[:, :, c].astype(np.float32)
        v = rgb[:, :, c][m].astype(np.float32)
        lo, hi = np.percentile(v, [lo_pct, hi_pct])
        if hi <= lo:
            hi = lo + 1.0
        shared = (x - glo) * (255.0 / (ghi - glo))
        perchan = (x - lo) * (255.0 / (hi - lo))
        out[:, :, c] = shared + (perchan - shared) * balance
    out = np.clip(out, 0, 255)
    if saturation != 1.0:
        luma = out @ np.array([0.299, 0.587, 0.114], np.float32)
        out = np.clip(luma[:, :, None] + (out - luma[:, :, None]) * saturation, 0, 255)
    if gamma and gamma != 1.0:
        out = 255.0 * np.power(out / 255.0, gamma)
    out = np.clip(out, 0, 255).astype(np.uint8)
    out[~m] = 0
    return out


def _clahe_grey(img: np.ndarray, tiles: int = 8, clip: float = 3.0) -> np.ndarray:
    """Contrast-limited adaptive histogram equalisation on the valid pixels of a grey image."""
    h, w = img.shape
    ty, tx = max(1, min(tiles, h // 16)), max(1, min(tiles, w // 16))
    ys = np.linspace(0, h, ty + 1).astype(int)
    xs = np.linspace(0, w, tx + 1).astype(int)
    maps = np.zeros((ty, tx, 256), np.float32)
    ident = np.arange(256, dtype=np.float32)
    for i in range(ty):
        for j in range(tx):
            tile = img[ys[i]:ys[i + 1], xs[j]:xs[j + 1]]
            v = tile[tile > 0]
            if v.size < 64:
                maps[i, j] = ident
                continue
            hist = np.bincount(v, minlength=256).astype(np.float32)
            limit = max(1.0, clip * v.size / 256.0)          # contrast limit, excess spread evenly
            excess = np.maximum(hist - limit, 0).sum()
            hist = np.minimum(hist, limit) + excess / 256.0
            cdf = np.cumsum(hist)
            cdf = (cdf - cdf[0]) / max(cdf[-1] - cdf[0], 1e-6)
            maps[i, j] = np.clip(cdf * 255.0, 0, 255)
    # centres of the tiles, then bilinear blending of the four surrounding maps
    cy = (ys[:-1] + ys[1:] - 1) / 2.0
    cx = (xs[:-1] + xs[1:] - 1) / 2.0
    yy = np.arange(h, dtype=np.float32)
    xx = np.arange(w, dtype=np.float32)
    iy = np.clip(np.searchsorted(cy, yy) - 1, 0, max(ty - 2, 0))
    ix = np.clip(np.searchsorted(cx, xx) - 1, 0, max(tx - 2, 0))
    fy = np.zeros(h, np.float32) if ty == 1 else np.clip((yy - cy[iy]) / np.maximum(cy[iy + 1] - cy[iy], 1e-6), 0, 1)
    fx = np.zeros(w, np.float32) if tx == 1 else np.clip((xx - cx[ix]) / np.maximum(cx[ix + 1] - cx[ix], 1e-6), 0, 1)
    iy2 = iy if ty == 1 else iy + 1
    ix2 = ix if tx == 1 else ix + 1
    out = np.empty((h, w), np.float32)
    for row in range(h):
        g = img[row]
        a = maps[iy[row], ix][np.arange(w), g]
        b = maps[iy[row], ix2][np.arange(w), g]
        c = maps[iy2[row], ix][np.arange(w), g]
        d = maps[iy2[row], ix2][np.arange(w), g]
        top = a + (b - a) * fx
        bot = c + (d - c) * fx
        out[row] = top + (bot - top) * fy[row]
    return np.clip(out, 0, 255).astype(np.uint8)


def infrared(img: np.ndarray, tiles: int = 8, clip: float = 3.0) -> np.ndarray:
    """Infrared enhancement (CLAHE) of a grey channel (H, W) uint8. Returns a new image, black preserved."""
    if img.ndim != 2 or img.shape[0] < 16 or img.shape[1] < 16:
        return img
    m = img > 0
    if m.sum() < 100:
        return img
    out = _clahe_grey(img, tiles, clip)
    out[~m] = 0
    return out


def apply(img: np.ndarray, colour_on: bool = False, infrared_on: bool = False) -> np.ndarray:
    """Apply the selected enhancements to a grey or RGB image, in the order that makes sense."""
    if img.ndim == 3:
        return colour(img) if colour_on else img
    return infrared(img) if infrared_on else img

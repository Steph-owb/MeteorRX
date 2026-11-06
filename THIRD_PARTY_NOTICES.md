# Third-party notices

MeteorRX is licensed under the **GNU General Public License v3.0** (see `LICENSE`).

Copyright (C) 2026 Stephan, 9H2WB / F5OWB — Malta Amateur Radio League (MARL).

## Algorithms

The demodulation and decoding chain was re-implemented following the open-source projects below
(both GPL-3.0):

- **SatDump** — https://github.com/SatDump/SatDump (PSK demodulator, Viterbi, LRPT / MSU-MR decoding)
- **meteor_demod / meteor_decode** by dbdexter-dev — https://github.com/dbdexter-dev

## Test data

- `tests/data/meteor_m2_real_soft.npz`: soft symbols derived from the real Meteor-M LRPT recording
  `meteor_m2_72k.wav` of https://github.com/lar-tech/satellite-decoder (MIT licence).

## Map overlay data (bundled in `meteorrx/data/`)

| File | Content | Licence | Source |
|---|---|---|---|
| `coastline.bin.gz` | Natural Earth 1:10m coastline, converted to packed float32 polylines | Public domain (no restrictions) | https://www.naturalearthdata.com |
| `cities.tsv.gz` | GeoNames populated places of 50 000 inhabitants or more (name, latitude, longitude, country, population) | CC BY 4.0 | https://www.geonames.org — https://download.geonames.org/export/dump/ |
| `font.bin.gz` | DejaVu Sans Bold at 15 px, baked to 1-bit glyphs (Latin-1) for the city labels | DejaVu Fonts License (Bitstream Vera derivative, permissive) | https://dejavu-fonts.github.io |

The GeoNames attribution required by CC BY 4.0 is shown in the *Enhance…* panel whenever the overlays can
be used, and is also returned by `meteorrx.overlay.ATTRIBUTION`.

## Bundled in the Windows package

| Component | Licence | Source |
|---|---|---|
| `rtlsdr.dll` — RTL-SDR Blog driver library v1.3.6 (x64) | GPL-2.0-or-later | https://github.com/rtlsdrblog/rtl-sdr-blog (tag v1.3.6) |
| `zadig.exe` — Zadig 2.9 (libwdi v1.5.1), signed by Akeo Consulting | GPL-3.0 | https://github.com/pbatard/libwdi (tag v1.5.1) |
| Python 3.12 runtime | PSF License | https://www.python.org |
| NumPy | BSD-3-Clause | https://numpy.org |
| Numba, llvmlite | BSD-2-Clause | https://numba.pydata.org |
| PySide6 / Qt 6 | LGPL-3.0 | https://www.qt.io/qt-for-python |
| sgp4 | MIT | https://pypi.org/project/sgp4/ |
| certifi | MPL-2.0 | https://pypi.org/project/certifi/ |

The driver binaries are **not stored in this repository**: `tools/fetch_drivers.py` downloads them from the
official releases above and checks their SHA-256 at build time.

## Used from other installed programs (not distributed)

- SDRplay API 3.x (`sdrplay_api.dll`, proprietary, from sdrplay.com)
- `hackrf.dll` (GPL-2.0) and `LimeSuite.dll` (Apache-2.0) of an installed SDR program (SatDump, SDR++,
  PothosSDR)

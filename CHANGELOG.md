# Changelog

All notable changes to MeteorRX. Versions follow `MAJOR.MINOR.PATCH`.

## [1.7.3] — 2026-11-06

### Added
- **A floor on how often the TLE servers are contacted**, settable in `meteorrx.ini`. CelesTrak asks
  programs to fetch only what they need, and MeteorRX was not holding up its end: a single satellite
  missing from the cache marked the *whole* set as needing a refresh, and because a station that can
  never get that one keeps retrying — every 15 minutes, for as long as it is left running — each
  retry re-downloaded every satellite. A tester's log showed eleven downloads in seventeen hours,
  some of them duplicated in the same second. Now only the satellites actually missing or stale are
  fetched; `[tle] refresh_days` (default 0, "whatever `max_age_hours` says") sets a floor in days —
  2, 3 or 7 for a station left running or used occasionally; `[tle] retry_hours` (default 1) is the
  wait after a failed download, replacing the quarter-hourly knock. When it last asked is kept in
  `data\tle.txt.state`, so a restart does not reset the limit, and a read-only data folder costs the
  rate limiting rather than the elements. *TLE › Update now* still always downloads.
- **MSU-MR 124 false colour**, the combination SatDump produces under that name, requested by Tony
  IK1HGI. Red is channel 1 (visible), green channel 2 (near infrared), blue channel 4 (thermal
  infrared), raw and uninverted: sea dark, vegetation green, cloud tops yellow to white. The view
  selector is now built from `composites` in `meteorrx.ini` instead of being hard-coded, so any
  three-digit combination listed there appears in the drop-down. The shipped default is
  `composites = 221, 124, 123`; selecting a combination the pass does not carry falls back to a
  single channel and says so in the caption instead of showing a broken picture.
- **Shipped defaults are carried forward into an existing `meteorrx.ini`.** Without this the new
  combination would have reached nobody who already runs MeteorRX: `load` overlays the stored file
  on top of the defaults, so an older ini keeps its own `composites = 221, 123` for ever. A stored
  value that matches an old default verbatim was never a choice, so it is updated, the file on disk
  is kept in step and the log says what changed; a value the operator edited is never touched, and a
  read-only install still gets the new value in memory.

### Fixed
- **Recordings started by hand carried no satellite, so coastlines and city names were silently
  dropped from the picture and from the enhanced copy.** Reported by Tony IK1HGI, whose log showed
  the cause plainly: all three of his saved enhanced copies came from manual recordings, and a
  manual recording had no pass behind it and therefore no TLE. Without a TLE the picture cannot be
  geolocated, so the overlay was skipped — which defeats the purpose of recording by hand, since
  that is how an operator catches a pass the predictions missed. The engine now identifies the
  satellite itself: among the configured satellites with elements loaded it takes those actually
  above the horizon, preferring one whose downlink matches the frequency the receiver is tracking,
  and settles ties on elevation. Identification waits for the first decoded frame, because a frame
  proves a Meteor really is transmitting rather than merely being geometrically up; a satellite
  below the horizon is never a candidate; and when nothing qualifies, nothing is claimed and the
  search repeats, because operators routinely start a minute before the bird rises. Once found the
  satellite is held for that recording, so one picture is never projected two different ways.
  Checked across 48 hours at Malta, Geneva, Hamburg, Oslo and Tromsø: with the two Meteor satellites
  roughly 94° apart in right ascension, there is no moment when both are above the horizon, so the
  identification is not choosing between them. Operators who add their own satellites to the ini
  should know that the elevation test is what settles a tie.
- **Every pass folder now carries a `….tle`**, the orbital elements the pass was actually received
  against, written as a plain three-line TLE beside the `.cadu`. `_info.json` already held them, but
  that file is ours; this one is the universal format that SatDump, Gpredict and any tracking
  program read, a human can read it, and it survives someone copying away just the recording and it.
  Replaying a pass in a year therefore still places the coastlines where they really were, instead
  of against whatever the orbit has become by then. A recording that arrives without its elements
  can be rescued by dropping any matching `.tle` into the folder — the name need not match.
- **Replaying a recording no longer guesses.** The same identification would otherwise have run
  during a file replay and drawn the sky as it is *now* onto a recording made days earlier —
  possibly even attaching the other satellite. A replay instead reads the elements saved beside the
  recording: the `.tle` beside it first, then its `_info.json`. If they are missing or
  unusable, no overlay is drawn and the log says so once. The saved `_info.json` now also carries
  the satellite identified for a hand-started recording, and the direction of travel, so those
  recordings can be geolocated on replay like any other.
- **Hand-started recordings: the saved images disagreed with the screen.** The view and the enhanced
  copy were turned north-up using the identified satellite while the saved channel and composite
  files were not, because the rotation still came from the predicted pass. Both now use the same
  answer.
- **The map overlay could hold the interface for half of every pass.** Drawing coastlines on a full
  pass costs about a second, and the live preview offers a new picture every two, so with overlays
  switched on the Qt thread was busy most of the time — on the same machine that is demodulating the
  signal, and longer than the interval itself on a slower PC. Automatic redraws are now spaced in
  proportion to what the last one cost (measured, capped at 20 s), which takes a full pass from
  roughly 60 % of the thread to 25 % and adapts itself to the machine. Anything the operator does —
  a checkbox, the view selector, Save — still redraws at once. Present since 1.7.0.
- **The "cannot place overlays" warning repeated on every refresh of a live pass.** Its key included
  the image extent, which grows with every strip, so an un-geolocatable pass logged the warning
  roughly every two seconds for its whole duration instead of once.
- **`meteorrx.ini` files as operators really write them.** A byte-order mark (Notepad's "Save as
  UTF-8") or an accented character written as ANSI (cp1252 — this user base writes French and
  Italian place names) stopped the frozen program at start-up with nothing but a PyInstaller error
  box. Both are now read. The settings rewrite walks the file section by section instead of matching
  a key anywhere in it, so the same key name in two sections no longer leads to the wrong line being
  changed and the right one being rewritten at every start, and it handles the `key: value` form;
  it goes through a temporary file and a rename, so a crash mid-write cannot leave a truncated ini
  and silently reset the station's coordinates and callsign.
- **The caption under the picture claimed overlays that had not been drawn.** When a pass could not
  be placed, the label still read "coastlines, cities" over a bare image. That is what sent the
  above report towards the save code rather than towards the missing TLE. The caption now reads
  "overlays not placed (no TLE)" in that case, in all five languages.
- **The "cannot place coastlines" warning was logged once per run**, so every later pass failed in
  silence. It is now logged once per pass.
- **The Windows executable reported a version nobody had released.** `tools/version_info.txt` was
  maintained by hand and its binary tuple had drifted to 1.6.2 while its strings said 1.7.2, so
  Explorer's file properties disagreed with the application. The spec now generates the whole
  resource from `meteorrx.__version__` at build time, so it cannot drift again.
- **`orbit.Satellite.freqs_hz` held megahertz.** The name silently disabled a frequency comparison
  the first time one was written against it, and had already misled a test. Renamed `freqs_mhz`,
  with the unit pinned by a test. `ReceiverConfig.freqs_hz` is unchanged and is genuinely in hertz.

### Verified
- **Reviewed by a second model before release.** The MSU-MR 124 work and every fix above were put
  through an independent review, which ran the suite itself and probed the ini loader and the
  satellite identification with its own scripts. It found the replay guess, the repeated warning,
  the interface stall, the ini rewrite's blind spots, the screen-versus-disk orientation mismatch
  and a test of ours that depended on a package not in `requirements-dev.txt` — which would have
  failed CI and blocked the Windows build. All are fixed above.
- **The enhanced copy is exactly what the window was showing.** The report above was about content
  missing from a saved file, so the save path was checked rather than assumed: the real window is
  driven offscreen on a real pass, the real Save button pressed, and the PNG read back from disk and
  compared with the array on screen. It is identical pixel for pixel, overlay included. The save was
  never at fault; the overlay had never been drawn. Pinned by `tests/test_enhanced_save.py`.
- **No change made to the geolocation.** A gradient-response measurement over a ±24-pixel offset grid
  appeared to show a coastline offset, but its peak stood only 1.12–1.17× above the background and the
  along-track component disagreed in sign across three passes (+12, −19, +21 lines) while the
  cross-track one did not (+8, +9, +12 columns). That is the signature of a metric reading noise, not
  of a misregistration, so it was discarded. On the one coast that can be judged cleanly in the
  available recordings — Gibraltar, 23 % of the way to the swath edge, cloud-free, examined at 3×
  magnification against the same crop without the overlay — the drawn line sits on the land-sea
  boundary within one to two pixels, about two to four kilometres. The swath edge could not be
  assessed: in all four recordings on hand every coast beyond 60 % of the way out is under cloud.
  Accuracy near the edge therefore remains unmeasured rather than confirmed.

## [1.7.2] — 2026-11-05

### Fixed
- **Lost lines came out as bands of saturated blue or yellow in RGB composites.** A lost frame does not
  cost every channel the same pixels — the channels' segments are interleaved — so a gap often left one
  channel black while the others still carried data, and stacking them produced a false colour that reads
  as corruption rather than as absence. Measured on the testers' recordings: 1.2 % of a real night pass
  and 0.4 % of a daytime one, and still present after missing-line interpolation. A pixel missing from any
  contributing channel is now black.

### Verified
- **Meteor-M2-4's roll bias is now measured rather than inherited.** It had been copied from SatDump and
  never checked. On a real M2-4 pass (Baltic, 2026-11-04 12:31 UTC) the offset between Oland's bright
  near-infrared centroid and its projected outline was tracked across a roll sweep:

      roll   -1.40  -0.90  -0.40  +0.10  +0.60  +1.10   deg
      dcol    +4.6   +2.1   -0.5   -3.2   -5.3   -8.1   pixels across track

  The crossing is at about -0.45 deg against the -0.40 in use, so the inherited value is right to within
  a tenth of a degree — under 1.5 km at nadir. Along track the residual is 0.8 pixel. A second landmark
  pin in `tests/test_geo.py` now holds M2-4's geometry, as one already does for M2-3, and checks that a
  quarter-degree roll change trips it.
- The same pass confirms the line period independently: 153.9 ms per row, identical to the M2-3 pass of
  the day before, on a different satellite and a different day.

## [1.7.1] — 2026-11-04

### Fixed
- **A city label near an edge removed the whole map overlay.** `overlay._blit` assumed a label box fell
  entirely inside the picture; a city close to any edge made it raise, and because a failed overlay falls
  back to "draw nothing", one such city left the user with the option ticked and no coastlines and no
  cities at all. The label is now clipped to the picture. Found in a tester's log, which carried about
  fifty of these on a single pass; covered by a test that places labels off all four edges.
- **A newly armed session took over the pass on screen.** `Engine._note_pass` overwrote the remembered
  pass as soon as the next session armed, while the viewer still showed the pass just decoded. Two
  visible consequences, both gone: the overlay reported "no TLE or no line times for this pass" over a
  perfectly good image, and *Save* in the Enhancements panel fell back to a file dialog instead of
  writing beside the pass it came from.

## [1.7.0] — 2026-11-04

### Added
- **Map overlays on the decoded pass**, placed by real per-pixel geolocation rather than a fitted
  approximation: **Coastlines** (Natural Earth 1:10m) and **Major cities** (GeoNames, a red dot and the
  name, threshold `[recording] city_min_population`, default 200 000). New `meteorrx/geo.py` turns each
  MSU-MR line timestamp plus the TLE into the satellite's Earth-fixed state through SGP4, builds the
  nadir-down body frame, and intersects the scan ray with the WGS84 ellipsoid; the inverse (latitude and
  longitude to pixel) finds the row whose scan plane contains the point. `meteorrx/overlay.py` draws it,
  mapping columns through whatever resampling the view is showing.
- **Overlay colours** are chosen in the panel, separately for the coastlines and for the cities: eight
  colours with a swatch in the list, remembered between sessions. The default is cyan coastlines over
  yellow city labels, which read best on a grey infrared scene; each label gets a dark or light halo
  automatically, whichever keeps it legible against its own colour.
- **Resize to true proportions** in the *Enhance…* panel: the panoramic correction applied to the view, so
  the enhancements, the overlays and the correction can be combined and judged together before saving.
- **Save** in the *Enhance…* panel now writes into the pass folder as `<pass prefix>_enhanced.png`, then
  `..._enhanced_2.png` and so on, so each saved version carries the pass index and sorts with the session.
  It still falls back to a file dialog when the pass has no folder yet.
- `lrpt.MsuMrImager.row_times()`: UTC time of every image row, by least-squares over the line groups
  actually received, with the outlying stamps clipped so a corrupt one cannot tilt the fit.
- Bundled overlay data (~2.5 MB, no network needed): Natural Earth coastline (public domain), GeoNames
  cities (CC BY 4.0, attribution shown in the panel), and a DejaVu Sans label font baked to 1-bit glyphs
  so MeteorRX still ships no font or image library. See `THIRD_PARTY_NOTICES.md`.
- The six new options and the overlay messages are translated in all five interface languages.

### Changed
- The **preview now has the same orientation as the saved images**: a northbound pass arrives south-first,
  so it is turned through 180° in the view as well (`[recording] rotate_northbound`). Without this the
  picture — and any map drawn on it — was upside down relative to the files on disk.

### Verified
- Validated on a real Meteor-M2-3 pass (northern Italy, 2026-11-03 20:29 UTC): the Balearics, Corsica and
  Sardinia fall on their own infrared signatures at 1:1, including at the swath edge where the correction
  stretches the image 5.4x. `geo.Track` and the independent pass predictor in `meteorrx/orbit.py` agree on
  elevation to 0.02 degrees and range to under 1 km. The image handedness (east on the left, earliest line
  first) was settled by correlating a land mask against the high-passed thermal channel: r = -0.21 the
  right way against -0.12 the other, collapsing to +0.03 with the rows reversed.
- That whole-image correlation settles a sign and nothing finer. An error that changes sign across the
  swath averages away in a bulk statistic, so candidate changes to the body frame were judged by
  projecting the coastline onto the islands at full resolution instead. `tests/test_geo.py` now pins the
  validated geometry by landmark pixel and checks that each rejected alternative trips the pin.
- A point on the far side of the Earth can fall inside the scan cone by direction alone; it is now
  rejected explicitly (it used to be drawn as streaks across the picture), and a coastline is no longer
  joined across a vertex that fell outside the swath. Both are covered by tests.

### Reviewed
An independent review of the whole project found no defect in reception, decoding or saving: the DSP and
receiver modules are unchanged from 1.6.1, and `lrpt.get_channel` after its refactor produces
bit-identical images on a real pass. It raised several defects in the new map work, fixed above, and
proposed three changes to the geolocation body frame (an inertial velocity vector, a geodetic nadir, a
different roll bias). Each was measured against the real islands and each made the fit worse, so none was
adopted; the test suite now encodes that result rather than leaving it to memory.

## [1.6.2] — 2026-11-04

### Added
- **Panoramic ("earth curvature") correction**: a `..._corr.png` is written beside every saved image, with
  a uniform 1 km/pixel ground spacing (1568 -> 2800 px wide), so coastlines keep their shape out to the edge
  of the 2800 km swath instead of being squeezed. Same geometry and parameters as SatDump (and SmoothMeteor).
  `[recording] geometric_correction = yes`. The decoded originals and the `.cadu` are never modified.
  `MeteorRX-cli --correct <folder or .png>` applies it to images already on disk.
- **Image enhancements** in the view toolbar (*Enhance…*), applied to the display only, with a button to
  save an enhanced copy where you choose: colorimetric correction of the composites, infrared enhancement
  (contrast-limited adaptive histogram equalisation), and missing-line interpolation.
- **Mirror** toggle in the view toolbar, for passes that come out flipped. Display only; remembered between
  sessions, like the enhancement choices.
- **Housekeeping of `Documents\MeteorRX` at start-up**: `[recording] purge_empty = yes` removes folders a
  pass left completely empty; `[recording] purge_days = 0` (0 = never, 30 is a sensible value for an
  unattended station) deletes whole receptions older than that many days. A folder holding any data at all
  is never removed by `purge_empty`.

### Fixed
- **A false trigger could swallow the next real pass.** A recording started on something that never decodes
  (interference, a spur) kept running, and in a **manual** session neither the LOS nor the signal-loss stop
  applied, so it ran until the user noticed. Seen at a test station on 2026-11-04: a session triggered at
  08:12 ran for more than five hours, merging two later passes into one `.cadu` (a 46 688-frame VCDU counter
  gap) and one 8992-line image. Now a recording that has decoded nothing is abandoned after
  `[recording] false_trigger_s` (default 300 s, 0 = never), and a manual session stops on signal loss **once
  it has actually decoded a pass**, so each pass is saved on its own. A manual session that has decoded
  nothing still keeps listening.
- `python -m meteorrx.cli --live` / `--correct` / `--rtlprobe` raised `NameError`: the module entry point
  sat before those functions were defined. The frozen `MeteorRX-cli.exe` was not affected.

## [1.6.1] — 2026-11-03

### Fixed
- **No decoding of real satellite passes** (reported from several stations). The I/Q sense of the demodulated
  symbols depends on the receiver chain; MeteorRX only tried the textbook mapping, and rotations / skews cannot
  undo a mirror image, so on a mirrored chain real passes never locked while the bench test pattern decoded
  perfectly. Every orientation is now tried at 72k and 80k, as other LRPT decoders do. Verified on a real
  Meteor-M recording (105 s) through the full receiver: 0 frames with 1.6.0, 916 RS-valid frames and 3573 image
  packets with 1.6.1 (reference MATLAB decoder on the same file: 925 / 3523), clean image.
- Carrier AFC: plain-QPSK-shaped signals were rejected by the CW gate, leaving the Costas loop to pull in by
  itself (12 s on the real recording); now accepted, carriers and BPSK still rejected. Lock in 0.5 s.
- A 90° carrier slip is detected in 0.45 s (Viterbi BER > 0.118 for 4 windows) instead of 4 s.
- Acquisition CPU kept at the 1.6.0 level: all hypotheses for the first 2 s, then the two orientation groups
  alternate; the orientation that locked is tried first next time.

### Added
- Interface in **Italian, Spanish and German** besides French and English (`[gui] language = it | es | de`).
  All texts now live in one table per language (`meteorrx/i18n.py`), with English as fallback for missing
  keys, so a new language is a single entry.
- `tests/test_real_signal.py`: decodes 3 s of a real Meteor-M recording to imagery; AFC on plain QPSK and CW;
  mirrored 72k / 80k OQPSK signals must lock. `tests/test_i18n.py`: language tables complete and consistent.

### Not yet verified on real data
- Meteor-M2-3 / M2-4 OQPSK + NRZ-M path, 80k interleaved mode, Doppler tracking on a live pass (the real
  recording available is from the older QPSK satellite). A baseband recording of one M2-3 / M2-4 pass is the
  next step.

## [1.6.0] — 2026-11-02

### Fixed
- **RTL-SDR chopped reception**: the dongle was read synchronously between processing blocks, so its FIFO
  overflowed (short gaps every ~100 ms, chopped audio) and no complete 114 ms LRPT frame got through.
  Every hardware receiver now streams from its own thread into a 4-second buffer; the RTL-SDR uses
  `rtlsdr_read_async`. Bench: 258/258 frames, BER 0.000, no sample lost.
- Start-up sample loss while the DSP engine loads (warm-up before streaming, stale backlog discarded).

### Added
- **All SDRplay models** (RSP1, RSP1A, RSP1B, RSP2, RSPduo, RSPdx, RSPdx-R2) with per-model Bias-T, FM notch,
  antenna port and LNA range; structures checked against the SDRplay API 3.15 headers.
- **LimeSDR Mini 1.x / 2.0** through LimeSuite (the `LimeSuite.dll` 23.11 of SatDump 1.2). Bench: 250/250
  frames, SNR 50.7 dB, BER 0.000.
- **HackRF One** through libhackrf (`hackrf.dll` of SatDump / SDR++ / PothosSDR).
- **PlutoSDR** over its native network interface (IIOD protocol, no libiio needed).
- *SDR stream* health indicator (delivered sample rate, samples dropped) with warnings in the log.
- `MeteorRX-cli --live` receiver diagnostic (per-second stream rate, SNR, offset, BER, frames, optional IQ
  recording) and `--set section.key=value` overrides.
- User manual (`docs/MANUAL.md`).

### Changed
- Default `callsign` is now empty (set yours in `meteorrx.ini`).
- Console errors are reported as plain messages instead of Python tracebacks.

## [1.5.1]

### Added
- Carrier AFC (4th-power estimator with drift hypotheses) and RS-validated lock: implementation loss ≈ 0.4 dB
  (was ≈ 4 dB); decodes from Es/N0 ≈ 2 dB.
- Audio monitor (FM audio of the channel, muted at start-up).
- Sensitivity bench (`tools/bench_sensitivity.py`) and regression tests.

## Earlier versions (1.0 – 1.5.0)
- 1.0 / 1.1: first Windows releases.
- 1.3: no DLL of our own (DSP compiled on the PC by Numba), hash-pinned official drivers, PyInstaller
  bootloader rebuilt from source (fewer antivirus false positives).
- 1.4: receiver calibration, S-meter, preamplifier qualification.
- Receiver profiles per physical receiver, outputs never overwritten (sequence-numbered folders).

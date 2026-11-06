# MeteorRX 1.7.3 — Meteor-M LRPT receiver

This program is based on the request of my friend Joe 9H1VW and all the time spent decoding satellites...

A deliberately simple Windows application that does one thing: receive **Meteor-M N2-3 / N2-4 LRPT** imagery
with an **RTL-SDR**, an **SDRplay RSP** (any model), a **HackRF One**, a **LimeSDR Mini** (1.x / 2.0) or a
**PlutoSDR**, validate the signal with an **eye diagram**, and save the pictures.

Pass prediction (TLE), Doppler pre-correction and **signal-triggered recording** are automatic.
Self-contained: `MeteorRX.exe` + `meteorrx.ini` + a working RTL-SDR driver set.

**Download:** the Windows package `MeteorRX-x.y.z-win64.zip` is on the **Releases** page.
**User manual:** [docs/MANUAL.md](docs/MANUAL.md) — short, practical, English with a French summary.

*Written by Stephan **9H2WB / F5OWB** . Free software, GPL-3.0.*
* Many thanks for the long hours of testing to Anthony "Tony" Musumeci IK1HGI who has been a great partner on this project
* And a warm thank you to all the OMs who have been running after our successive versions — downloading, reinstalling and reporting back pass after pass. Your logs are what found most of what is fixed here. May this one stay put for a while.

*Version française plus bas.*

![MeteorRX](docs/screenshot.jpg)

---

## 1. Install and run

1. Unzip `MeteorRX-1.7.3-win64.zip` anywhere (Desktop, `C:\MeteorRX`, USB stick).
2. Run **`MeteorRX.exe`**. The first start compiles the DSP engine for this PC (20–60 s, a notice is shown);
   later starts take a second or two.
3. RTL-SDR on a new PC: if the receiver is not found, MeteorRX detects that Windows bound the wrong USB driver
   and offers to start **Zadig** (bundled) to install WinUSB — once per USB port. SDRplay: install the
   *SDRplay API 3.x* from sdrplay.com. HackRF, LimeSDR: see §3b. PlutoSDR: nothing to install (network, §3b).

`meteorrx.ini` (next to the exe) holds all settings; `MeteorRX-cli.exe --selftest` checks an installation.

| Folder content | Purpose |
|---|---|
| `MeteorRX.exe` | The application |
| `MeteorRX-cli.exe` | Console: decode recordings, `--selftest`, `--drivers`, `--live` (receiver diagnostic) |
| `meteorrx.ini` | Configuration (commented) |
| `drivers\rtlsdr\rtlsdr.dll` | RTL-SDR Blog v1.3.6 x64 driver library (V4/V4L/V3/generic) |
| `drivers\zadig\zadig.exe` | Zadig 2.9, WinUSB installer (signed by Akeo Consulting) |
| `data\` | TLE cache, `meteorrx.log` |
| `cache\` | Compiled DSP engine |
| `_internal\` | Runtime (Python, Qt, NumPy, Numba) |

## 2. Window layout

| Left panel (controls & validation) | Right panel (result) |
|---|---|
| Receiver: **profile** (● connected / ○ not connected), *Detect*, identity and calibration status; device, gains, Bias-T, *preamp in service* | Live image: RGB 221, RGB 124, RGB 123 or single channel |
| Satellite, LRPT mode (auto/72k/80k), Start / Stop, automatic scheduling | Zoom (mouse wheel), pan (drag), *Fit*, *Open .cadu…*, *Open folder* |
| LEDs: Signal · Viterbi · Frames · Image · Rec, **S-meter** (dBm / S-units, N0, C/N0), **audio monitor** (*Muted / Listen* button + volume: FM audio of the channel on the PC speakers, to check by ear; muted at start-up, no effect on decoding) | Below the image (always visible, height adjustable): **next passes** (local time, max elevation, direction, duration) and the **log** |
| **Eye diagram** (I branch, persistence, Q factor) and constellation side by side | |
| SNR, Viterbi BER, frames OK/bad, packets, mode · frequency, Doppler, loop offset, elevation, recording time | |
| 137 MHz spectrum with the 137.100 / 137.900 markers (Doppler-shifted) | |

The window opens **maximized** (`[gui] window = maximized`; `fullscreen`, or `last` = size and position of the
previous session). **F11** toggles full screen, **Esc** leaves it. Panel sizes are remembered (`data\window.json`).
On a small laptop screen (125–150 % display scaling) the left panel scrolls if needed; passes and log stay visible.

Menus: **TLE** (update, import a TLE file), **Measurements** (calibration / preamp, §6) and **Drivers** (RTL-SDR driver diagnostics, install the WinUSB
driver with Zadig, open the log file).

A healthy reception shows an open eye with two distinct levels at the centre, four tight constellation clusters,
Viterbi BER below ≈ 0.06, the *Frames* and *Image* LEDs green and the picture growing on the right.

## 3. RTL-SDR drivers

Two layers are involved, both handled by MeteorRX:

* **USB driver (kernel)** — must be WinUSB. Windows often binds its DVB-T driver (RTL2832UBDA) or nothing.
  MeteorRX reads the driver bound to every plugged RTL2832U (Windows SetupAPI) and, if it is wrong, offers to
  launch the bundled **Zadig** with the right defaults (WinUSB, all devices listed). In Zadig select the
  receiver (*Bulk-In, Interface (Interface 0)* or *Blog V4*) and click *Replace Driver*.
* **Driver library (`rtlsdr.dll`)** — MeteorRX first looks for an existing installation (`rtl_dll_dir` in the
  INI, SatDump, SDR++, SDR#, SDRangel, PATH) and uses it **only if it is suitable**:
  valid 64-bit DLL, exports every function MeteorRX calls, all its dependent DLLs present, and — when an
  **RTL-SDR Blog V4/V4L** is plugged in — a build containing the V4 support (older osmocom builds tune a V4
  wrongly). Otherwise the **bundled RTL-SDR Blog v1.3.6** library is used (libusb built in, backwards compatible
  with V3 and generic dongles). *Drivers › RTL-SDR driver diagnostics* (or `MeteorRX-cli --drivers`) shows every
  candidate with the reason it was accepted or rejected.

### Continuous streaming and stream health (v1.6.0)

Every hardware receiver streams from its own thread into a 4-second buffer: the USB / network flow never stops
while a block is processed. Up to v1.5.1 the RTL-SDR was read synchronously between two processing blocks, so
its FIFO overflowed during the processing: short gaps every ~100 ms (chopped audio, loops restarting), and since
an LRPT frame lasts 114 ms not a single complete frame got through, whatever the signal level. The RTL-SDR now
uses `rtlsdr_read_async` (15 queued USB transfers, as rtl_sdr, SDR# and SatDump do).

The delivered sample rate is measured permanently: *SDR stream* in the measurement panel (100 % = nothing lost,
⚠ below 95 % or when the PC drops samples), with a warning in the log. Console check of any receiver:

    MeteorRX-cli --live 60 [--device rtlsdr|sdrplay|hackrf|lime|pluto] [--mode 72k] [--record test.cs16]

prints every second the stream rate, samples dropped, SNR, carrier offset, BER and frames, and can keep the raw
IQ for analysis.

## 3b. Other receivers (v1.6.0)

| Receiver | What is needed | Settings (`[sdr]`) |
|---|---|---|
| **SDRplay** RSP1, RSP1A, RSP1B, RSP2, RSPduo, RSPdx, RSPdx-R2 | SDRplay API 3.x (service running, no other program using the RSP) | gain reduction 20–59 dB, LNA state (RSP1 0–3, RSP1A/1B/duo 0–9, RSP2 0–8, RSPdx 0–26 at 137 MHz), Bias-T, FM notch, `sdrplay_antenna` A/B (RSP2) or A/B/C (RSPdx). RSPduo: tuner 1 in single-tuner mode |
| **HackRF One** | WinUSB driver (Zadig) and `hackrf.dll` of an installed program: SatDump, SDR++ or PothosSDR (found automatically; `hackrf_dll_dir` to force a folder) | 2.4 MS/s, LNA 0–40 dB (8 dB steps), VGA 0–62 dB (2 dB steps), RF amp +14 dB, Bias-T |
| **LimeSDR Mini** 1.x / **2.0** | FTDI D3XX driver **1.3.0.10 x64 (WDF-based)** from ftdichip.com/drivers/d3xx-drivers (not the WinUSB-based 1.4.x: the LimeSuite library of SatDump only talks to the WDF driver) and `LimeSuite.dll` 23.11 of SatDump 1.2 (found automatically; `lime_dll_dir` to force a folder) | 2.4 MS/s, RX gain 0–73 dB, `lime_antenna` auto (LNAW below 2 GHz) / LNAW / LNAH / LNAL. Same call sequence as SatDump (no RX calibration, low-pass re-applied after the stream start: required by the Mini 2.0) |
| **PlutoSDR** (ADALM-Pluto) | Nothing: MeteorRX speaks the native IIOD network protocol of the Pluto (TCP 30431). Pluto on USB = network adapter 192.168.2.1 | `pluto_uri` (`ip:192.168.2.1` or `ip:<address>`), 2.16 MS/s, RX gain 0–71 dB or `auto` |

The PlutoSDR RX and TX share one sample clock: at 2.16 MS/s (default) MeteorRX can receive on the very Pluto that
transmits the MARL test pattern. Each receiver gets its own profile (serial number), settings and calibrations.

Bench results on the MARL LRPT test pattern (Pluto, 72k, −55/−60 dBm, `MeteorRX-cli --live 30`):

| Receiver | SNR | Frames | Viterbi BER | Stream |
|---|---|---|---|---|
| RTL-SDR (R820T, 1.2 MS/s) | 47 dB | 258 / 258 | 0.000 | 99.7 %, 0 lost |
| LimeSDR Mini 2.0 (2.4 MS/s) | 50.7 dB | 250 / 250 | 0.000 | 100 %, 0 lost |
| HackRF One (2.4 MS/s) | 7 dB | 0 | 0.025 | 99.7 %, 0 lost — the unit tested has a degraded signal (phase instability, periodic dips every 8192 samples); its capture fails offline through the exact path that decodes the RTL capture. Control, gains and stream are validated; decoding to be confirmed with another HackRF |

## 4. Configuration — `meteorrx.ini`

Next to `MeteorRX.exe` (if that folder is read-only, settings go to `%LOCALAPPDATA%\MeteorRX`).

| Section | Key | Default | Meaning |
|---|---|---|---|
| station | latitude / longitude / altitude_m | 35.88 / 14.45 / 50 | Station position (Malta) |
| station | min_elevation | 15 | Passes below this max elevation are ignored |
| sdr | device | rtlsdr | `rtlsdr`, `sdrplay`, `hackrf`, `lime`, `pluto` or `file` |
| sdr | center_mhz | 137.500 | One tuning covers **both** 137.100 and 137.900 |
| sdr | ppm | 0.0 | Reference error of the receiver (positive = reads high) |
| sdr | rtl_sample_rate / rtl_gain_db | 1 200 000 / 38.6 | RTL-SDR settings (`auto` gain allowed) |
| sdr | rtl_serial / sdrplay_serial | (profile) | Receiver to use, set by the active receiver profile |
| sdr | rtl_dll_dir | (auto) | Force the folder of the `rtlsdr.dll` to use |
| sdr | sdrplay_grdb / sdrplay_lna_state / sdrplay_fm_notch / sdrplay_antenna | 40 / 3 / yes / A | SDRplay gain, FM notch, antenna port |
| sdr | hackrf_lna_gain / hackrf_vga_gain / hackrf_amp / hackrf_dll_dir | 24 / 30 / no / (auto) | HackRF One |
| sdr | lime_gain_db / lime_antenna / lime_sample_rate / lime_dll_dir | 40 / auto / 2400000 / (auto) | LimeSDR Mini 1.x / 2.0 |
| sdr | pluto_uri / pluto_sample_rate / pluto_gain_db | ip:192.168.2.1 / 2160000 / 50 | PlutoSDR over the network |
| satellites | `NAME = NORAD ; MHz/MHz ; mode` | M2-3 57166, M2-4 59051 | Satellites, frequencies, 72k/80k/auto |
| tle | max_age_hours / refresh_days / retry_hours / urls / proxy | 12 / 0 / 1 / CelesTrak, SatNOGS / system | TLE refresh and how often the servers may be asked |
| decoder | trigger_snr_db | 3.0 | Recording starts above this SNR or on Viterbi lock |
| recording | output_dir | Documents\MeteorRX | One sub-folder per pass |
| recording | pre_aos_s / signal_loss_stop_s | 60 / 90 | Arm before AOS; stop after signal loss or at LOS |
| recording | save_cadu / save_baseband | yes / no | Keep frames (SatDump-compatible) / raw channel IQ |
| recording | geometric_correction | yes | Also write `..._corr.png`, panoramic correction, 1 km/pixel (§6b) |
| recording | purge_empty / purge_days | yes / 0 | Start-up housekeeping: drop folders left empty / delete receptions older than N days (0 = never) |
| recording | rotate_northbound / composites | yes / 221, 124, 123 | North-up; RGB products |
| gui | language / auto_start | fr / yes | `fr`, `en`, `it`, `es` or `de` (all texts in `meteorrx/i18n.py`: one table per language, English fills any missing key, so adding a language is one more entry); scheduler on at launch |
| gui | window | maximized | `maximized`, `fullscreen` or `last` |
| gui | audio_volume | 50 | Audio monitor volume (0–100); the monitor always starts muted |
| decoder | carrier_loop_bw / clock_loop_gain | (empty) | Advanced: Costas bandwidth / clock gain; empty = built-in tested values |

The receiver keys of `[sdr]` (device, gains, sample rates, ppm, Bias-T, tuning) are those of the **active
receiver profile** (§5) and are rewritten when another profile is selected.

## 5. Operation

* **Automatic** (default): before each pass above `min_elevation` the receiver is armed; recording starts when
  the signal appears (SNR threshold or decoder lock) and stops at LOS or after `signal_loss_stop_s` without
  signal; products are saved, then the next pass is awaited.
* **Manual**: choose a satellite (Doppler from its TLE) or *Manual*, then **Start / Stop**.
* **IQ file**: device *IQ file*, a WAV (SDR#, SatDump baseband) or raw `.cu8 .cs8 .cs16 .cf32` recording.
* **Console**: `MeteorRX-cli recording.wav [--rate 1000000] [--center 137.5] [--mode auto]` or
  `MeteorRX-cli pass.cadu` → PNG products next to the input.

**Outputs are never overwritten.** Every reception gets a **sequence number** (kept in `data\sequence.json`,
never reused, survives restarts) in front of its folder and file names, e.g.
`Documents\MeteorRX\0007_20261027_0912_METEOR-M2-4\0007_20261027_0912_METEOR-M2-4.cadu`, `…_ch1.png … ch6.png`,
`…_ch4_inv.png`, `…_rgb221.png`, `…_rgb124.png`, `…_rgb123.png`, `…_info.json` (contains `"session": 7`)
and `….tle`, the orbital elements the pass was received against, written as a plain three-line TLE. Restarting during the
same pass therefore creates `0008_…` next to `0007_…`. The number is allocated only when something is written
(no gap for sessions without signal). Files are opened in exclusive-create mode and flushed every 2 s (a crash
loses at most 2 s). Re-decoding a `.cadu` writes to `…_products`, then `…_products_2`, etc.; measurement
reports and the archived log (`meteorrx_<date>.log`) follow the same rule.

### Receiver profiles

Each physical receiver is identified by its **USB identity** (RTL-SDR: manufacturer, product, serial number,
tuner; SDRplay: model and serial number) and has its own profile in `data\receivers.json`: settings (gain,
sample rate, ppm, Bias-T, SDRplay gain reduction / LNA / AGC / notch), **calibrations** (one per gain setting),
the **calibration window values** (test frequency, cable loss, levels, preamp name) and its **preamplifier**.

* At start-up (and with *Detect*) MeteorRX lists the connected receivers. A new receiver gets a profile
  automatically (the very first one inherits the current settings and the calibrations made with v1.4). If the
  receiver of the active profile is not connected, a connected one with a profile is selected.
* Choosing a profile in the list restores all its settings; changing a gain updates that profile only.
  *⋯ › Rename / Delete* manage profiles (calibrations of a deleted profile stay archived).
* The status line under the list shows the serial number, connected or not, and whether the receiver is
  **calibrated for the current settings** (K, NF, date) or which gains are calibrated.
* If a pass starts with a different receiver than the active profile, reception still runs; the S-meter uses
  that receiver's own calibration if it has a profile, otherwise it shows dBFS.
* **Identical dongles**: many RTL-SDRs share the serial number `00000001` and then cannot be told apart. MeteorRX
  warns when two connected dongles have the same identity; give each a unique serial with
  `rtl_eeprom -s <serial>` (rtl-sdr tools), then replug.

## 6. Calibration, S-meter and preamplifier qualification

Menu **Measurements › Receiver calibration / preamplifier qualification** (stop any reception first; the
scheduler is paused while the window is open). Needs a signal generator in CW — e.g. Viavi 3920 — connected by
coax to the receiver input. The **current gain settings** are the ones calibrated (key shown in the window, e.g.
`rtlsdr|gain 38.6|1200000|137.500`); automatic gain must be off.

**Part 1 — receiver (generator straight into the receiver)**

1. Test frequency 137.900 MHz (away from the DC spike), enter the cable/attenuator loss.
2. Three points, default −70, −90, −110 dBm: set the level, RF ON, *Measure*. Each point gives the tone power
   (noise removed), the frequency error and the ADC peak. The highest point must not clip (a warning is shown);
   lower it if needed.
3. *Noise*: generator RF **OFF but still connected** (50 Ω at ≈ 290 K), *Measure*.
4. *Compute and save*: offset **K** (P[dBm] = P[dBFS] + K), slope (linearity, expected 1.000 ± 0.05), residuals
   (> 0.5 dB flagged: compression at the top, leakage/pick-up at the bottom), noise floor in dBm/Hz and the
   **receiver noise figure**, frequency error in ppm (*Apply the ppm correction to the .ini*).

**Part 2 — preamplifier (inserted between generator and receiver, powered / Bias-T on)**

1. −110 dBm, *Measure* → **real gain** of the preamp at a weak-signal level.
2. Optional strong level (−80 dBm) → gain again; a drop > 0.5 dB means compression.
3. *Noise* with RF OFF → system NF, then **preamp NF by Friis** (F_pre = F_sys − (F_rx − 1)/G).

After the preamp computation MeteorRX offers to **put the preamplifier in service** on this receiver: the S-meter
is then referred to the preamp input (antenna), i.e. the preamp gain is subtracted; the *Preamp in service* box in
the Receiver panel switches this on and off. All values typed in the window are remembered per receiver.

Results go to `data\calibration.json` (one calibration per receiver profile and settings key, preamp history) and to a text report
(*Save report…*). With a matching calibration the main window shows an **S-meter**: Meteor channel power in dBm
and S-units (IARU Region 1 scale above 30 MHz: S9 = −93 dBm, 6 dB per S-unit), noise density N0 in dBm/Hz and
C/N0; otherwise the level is shown in dBFS. Console equivalent: `MeteorRX-cli --measure 137.9 [seconds]`.

Accuracy: the processing itself is within ±0.1 dB (validated on synthetic signals); the result is limited by
the generator level accuracy at low level (see its specification), the cable loss value and leakage at
−110 dBm (use a good cable, preferably a fixed attenuator, keep the generator away from the dongle). The NF
method assumes a 290 K source; with an RTL-SDR (NF ≈ 6 dB) a preamp of ≥ 15 dB gain is measured to about ±0.3 dB.
The calibration is valid only for the settings it was made with (gain, sample rate, tuning).

## 6b. Image geometry, enhancements and map overlays

The MSU-MR mirror scans at a constant **angular** rate, so one transmitted pixel covers about 1 km at nadir
but 5.4 km at the edge of the 2800 km swath: the picture is foreshortened there. Beside every saved image
MeteorRX writes a **`..._corr.png`** resampled to a uniform 1 km/pixel on the ground (1568 → 2800 px wide),
using the same geometry and parameters as SatDump and SmoothMeteor (swath 2800 km, altitude 820 km, Earth
radius 6371 km, `meteorrx/geometry.py`). Switch it off with `[recording] geometric_correction = no`;
`MeteorRX-cli --correct <folder or .png>` applies it to images already on disk. The decoded originals and
the `.cadu` are never modified.

The **view toolbar** carries display-only controls; none of them changes a saved file, and all are
remembered between sessions. *Enhance…* opens a small panel you leave open while you try combinations:

| Control | What it does |
|---|---|
| *Resize to true proportions* | The panoramic correction above, applied to the view (1568 → 2800 px) |
| *Colour enhancement* | Contrast stretch computed over the three channels together (so the hue is kept) plus a partial grey-world white balance and a touch of saturation: lifts the haze cast off a composite |
| *Infrared enhancement* | Contrast-limited adaptive histogram equalisation (CLAHE, 8×8 tiles, clip 3) on an infrared channel: brings out cloud-top structure without amplifying noise over flat sea |
| *Interpolate missing lines* | Fills short gaps left by lost frames (on by default) |
| *Coastlines* | Natural Earth 1:10m coastline drawn where the geometry says it is |
| *Major cities* | A dot and the name of each city above `[recording] city_min_population` (default 200 000) |
| *Coastline colour* / *City colour* | Eight colours each (cyan, yellow, green, orange, magenta, white, black, red), chosen separately. Cyan coastlines over yellow cities is the default: on a grey infrared scene these sit furthest from the greys of cloud, sea and land, and white or red both get lost. Labels take a dark or light halo automatically, whichever keeps them readable |
| *Mirror* (toolbar) | Flips the displayed image left-right, for a pass that comes out the wrong way round |

**Save** at the bottom of the panel writes the picture exactly as displayed into the pass folder, as
`<pass prefix>_enhanced.png`, then `..._enhanced_2.png` and so on — so each saved version carries the pass
index and sits with the rest of the session. The decoded images and the `.cadu` are never touched.
"Exactly as displayed" is checked rather than assumed: a test drives the real window offscreen, presses
the real Save button and compares the PNG read back from disk with the array on screen, pixel for pixel.

If the caption under the picture reads **"overlays not placed (no TLE)"**, the pass could not be
geolocated and no coastline or city can be drawn — on screen or in the saved copy. That happens when no
satellite could be attached to the recording at all. It is not a fault in the save.

### Which combinations appear in the view selector

The drop-down is built from `[recording] composites` in `meteorrx.ini`, so adding a three-digit
combination there adds it to the list. The default is `221, 124, 123`:

| Combination | R, G, B | What it shows |
|---|---|---|
| `221` | ch2, ch2, ch1 | The everyday daytime view: near infrared in red and green, visible in blue |
| `124` | ch1, ch2, ch4 | **MSU-MR 124**, the combination SatDump produces under that name. Visible, near infrared and thermal infrared, raw and uninverted: sea dark, vegetation green, cloud tops yellow to white |
| `123` | ch1, ch2, ch3 | Daytime only, and only when channel 3 is transmitted |

Selecting a combination whose channels the pass does not carry — `123` on a night pass, where channel 3
is not transmitted — falls back to a single channel rather than showing a broken picture, and the caption
under the image says which channel you are actually looking at.

If you upgraded from a build before 1.7.3 and never edited `composites`, the line is brought forward to
include `124` the first time 1.7.3 starts, and the log says so. A value you edited yourself is never
touched: add `124` to it by hand if you want the false colour.

### How the overlays are placed

Nothing is fitted by hand. Each MSU-MR scan line carries its own timestamp, so `meteorrx/lrpt.py` gives a
UTC time for every image row (least-squares over the groups actually received, so a lost or corrupt stamp
cannot tilt it). From that time the TLE gives the satellite's Earth-fixed position and velocity through
SGP4; the look direction for a column is the scan angle (110.1° across the swath for M2-3 and M2-4, 110.6°
for M2, with the published roll bias) applied in a nadir-down body frame, and the pixel sees where that ray
meets the WGS84 ellipsoid (`meteorrx/geo.py`). Going the other way — latitude/longitude to pixel, which is
what places a coastline — uses the fact that the scan is perpendicular to the ground track: the row is the
one whose scan plane contains the point. The column is then mapped through whatever resampling the view is
showing, so the overlay stays put when you switch *Resize* on.

All of this needs a TLE, which normally comes from the predicted pass the recording was armed for. Two
cases have no predicted pass, and they are handled differently on purpose.

A recording **started by hand** is happening now, so the sky now is the evidence: the engine takes the
configured satellites that are actually above the horizon, prefers one whose downlink matches the
frequency the receiver is tracking, and settles any tie on elevation. It waits for the first decoded
frame first, because a frame proves a Meteor is really transmitting rather than merely being up. A
satellite below the horizon is never a candidate, so nothing is drawn on a guess; if nothing qualifies
the search simply repeats, since an operator usually starts a minute before the bird rises. The log says
which satellite was picked, and it is then held for the rest of that recording so one picture is never
projected two different ways. Across 48 hours at Malta, Geneva, Hamburg, Oslo and Tromsø there is no
moment when both Meteor satellites are above the horizon — they sit about 94° apart in right ascension —
so this is not a coin toss between them. If you add your own satellites to the ini, note that elevation
is what breaks a tie.

**Replaying a file** is the opposite case: the recording was made at some other time, so the sky now says
nothing about it. MeteorRX reads the elements saved beside the recording — the `….tle` file first, then
its `_info.json` — which is why both are written when the pass is received. If they are missing or
unusable, nothing is drawn and the log says so: an overlay built from today's orbit on last week's
recording would be confidently wrong, which is worse than none.

A recording that reaches you without its elements can still be rescued: drop any three-line `.tle` for
that satellite into the folder beside the `.cadu` and replay it. The name does not have to match.

**Checked against a real pass.** On a Meteor-M2-3 pass received in northern Italy on 2026-11-03 at
20:29 UTC, the Balearics, Corsica and Sardinia fall on their own infrared signatures at 1:1, including at
the edge of the swath where the correction stretches the image 5.4×. Islands are what settled it: a
coastline that lands on a small island in open sea leaves no room for interpretation.

Two weaker checks support it. `geo.Track` and the pass predictor in `meteorrx/orbit.py` agree on the
satellite's elevation to 0.02° and its range to under 1 km — they share the TEME-to-Earth-fixed rotation,
so this confirms the orbit and the body frame, not the rotation itself. And the image handedness was
decided by correlating "this pixel is land" against the high-passed thermal channel over the whole image
(at night land cools below the sea, so the right alignment is the most negative correlation): r = −0.21
one way against −0.12 the other, with reversing the row order collapsing it to +0.03.

That whole-image correlation is reported for what it is worth, which is less than it looks. It settles a
sign, because the wrong sign is grossly wrong. It cannot measure a small frame error: an error that
changes sign across the swath averages to nothing in a bulk correlation, and this scene's cloud dominates
the statistic. Candidate changes to the body frame were therefore judged by projecting the coastline onto
the islands at full resolution and looking, which separates them clearly where the correlation does not.
`tests/test_geo.py` pins the validated geometry by landmark pixel, and checks that each rejected
alternative moves those landmarks far enough to trip the pin.

**How close is it, in kilometres?** On the Strait of Gibraltar — cloud-free, 23 % of the way to the swath
edge, examined at 3× magnification beside the identical crop without the overlay — the drawn line sits on
the land-sea boundary within one to two pixels, about two to four kilometres. Near the **edge** of the
swath the question is open: in every recording available so far, each coast beyond 60 % of the way out
has been under cloud, so edge accuracy is unmeasured rather than confirmed. A gradient-response search
over a ±24-pixel offset grid was tried and discarded: its peak stood only 1.12–1.17× above the background
and its along-track component disagreed in sign across three passes, which is a metric reading noise
rather than a misregistration. Clear-sky recordings with a coast near the swath edge are welcome.

**Meteor-M2-4** was checked the same way on its own pass (Baltic, 2026-11-04 12:31 UTC). Its roll bias
had been copied from SatDump and never verified; tracking the offset between Oland's bright
near-infrared centroid and its projected outline across a roll sweep puts the true value at about
-0.45 degrees against the -0.40 in use — right to within a tenth of a degree, under 1.5 km at nadir, with
0.8 pixel of residual along track. A second landmark pin holds that result. A third pass, M2-3 again a day
later and geolocated with a two-day-old TLE, still placed Corsica and Sardinia on their own signatures,
so elements a couple of days old cost nothing visible at this scale.

Overlay data is bundled (about 2.5 MB) and needs no network: Natural Earth 1:10m coastline (public domain),
GeoNames cities of 50 000 inhabitants or more (CC BY 4.0 — the attribution is shown in the panel), and a
DejaVu Sans label font baked to 1-bit glyphs so no font or image library is needed.

## 7. How it works

The open-source chain behind SatDump was reduced to what Meteor LRPT needs and re-implemented compactly in
Python, the hot loops compiled on the user's PC by Numba (`meteorrx/dsp.py`):

| Stage | Implementation | Reference |
|---|---|---|
| Channelizer | NCO + decimating FIR, 137.1 and 137.9 at once, Doppler/ppm/AFC correction | — |
| **Carrier AFC** | 4th power of the channel (removes the QPSK modulation → line at 4 × offset), 8 averaged 16k FFTs along 13 drift hypotheses (±120 Hz/s), CW/spur rejection by the squared spectrum (OQPSK: lines at 2f ± Rs, none at 2f). Detects the carrier below Es/N0 = 1 dB and puts the NCO on it within a few Hz; while locked the NCO takes over the Costas frequency (Doppler / oscillator drift) | standard NDA estimator |
| OQPSK demodulator | Farrow resampler to 2 sps, AGC, RRC α = 0.5, narrow Costas (bw 0.0014), half-symbol Q alignment, Mueller & Müller timing (gain 0.002) | SatDump `psk_demod` |
| Ambiguity resolution / lock | 6 hypotheses (0/90° × I/Q skew −1/0/+1 symbol) decoded in parallel during acquisition; the first one delivering a **Reed-Solomon-valid CADU** wins (or a re-encode BER < 0.06 on strong signals). Unlock after 4 windows above BER 0.145 or 4 s without a good frame | SatDump `Viterbi1_2` |
| 80k mode | 0x27 markers every 80 bits tracked in absolute position; after a fade or phase slip the missing blocks are fed as erasures so the 36-branch de-interleaver (≈ 16 s span) stays aligned | meteor_decode / SatDump `deint` |
| FEC | Viterbi K = 7 r = ½ → NRZ-M → ASM 0x1ACFFC1D → derandomiser → RS(255,223) ×4 | libfec, SatDump |
| Data link / MSU-MR | VCDU (VCID 5) → CCSDS packets → APID 64–69 JPEG MCUs → 43-packet line loop | CCSDS 732.0, SatDump `msumr/lrpt` |
| Orbit | SGP4, TEME → ECEF → topocentric, pass search, range-rate Doppler | validated against Skyfield |

Default LRPT mode *auto* runs the 72k and 80k demodulators in parallel until one locks.

### Real satellite signal (v1.6.1)

The I/Q sense of the demodulated symbols depends on the receiver chain (SDR driver convention, spectrum
inversion in the front end), so the mapping of I and Q onto the two convolutional-code streams can be the
textbook one or its **mirror image**. MeteorRX up to 1.6.0 tried only the textbook mapping: carrier rotations
and I/Q skews cannot produce a mirror, so on a mirrored chain the bench test pattern decoded perfectly while
**real passes never locked**. On the only real recording available for verification (Meteor-M N2, QPSK 72k,
105 s, `meteor_m2_72k.wav` of github.com/lar-tech/satellite-decoder, MIT) the mirrored mapping was required:
0 frames with 1.6.0 in every rotation and skew; with 1.6.1, through the full production receiver (AFC, Costas,
hypotheses, relock, imager; only the QPSK / non-differential format of that older satellite patched in),
**916 Reed-Solomon-valid frames and 3573 image packets** against 925 / 3523 for the reference MATLAB decoder
of that project, lock 0.5 s after the start, one 90° carrier slip at 78 s re-locked in 0.1 s, clean image.
Every public LRPT decoder (SatDump, meteor_decode, that MATLAB reference) tries both mappings; MeteorRX now
does too.

Since 1.6.1 every orientation is tried (12 hypotheses at 72k: 0/90° × I/Q skew −1/0/+1 × mirrored or not;
180° is transparent through NRZ-M; the 80k marker search also covers the mirror). To keep the CPU load of
1.6.0, all 12 run during the first 2 s of an acquisition, then the two orientation groups alternate every
0.35 s; the orientation that locked is tried first at the next acquisition. Two further changes came out of
the review on that recording: the **carrier AFC** now accepts any (O)QPSK-shaped signal (it rejected plain
QPSK, leaving the narrow Costas loop to pull in 1.7 kHz by itself in 12 s) while still rejecting carriers
and BPSK, and a **90° slip is detected in 0.45 s** (Viterbi BER above 0.118 for 4 windows) instead of
waiting the 4 s no-frame timeout. `tests/test_real_signal.py` decodes 3 s of the real recording to imagery,
checks the AFC on plain QPSK and CW, and checks that mirrored M2-x OQPSK signals (72k and 80k) lock.

**Still unverified on real data:** the OQPSK half-symbol path with NRZ-M (Meteor-M2-3 / M2-4), the 80k
interleaved mode and Doppler tracking on a live pass — the older satellite's recording cannot exercise them.
One baseband recording of an M2-3 / M2-4 pass is the next step (`save_baseband = yes`).

## 8. Building the executable

* **`build_exe.bat`** — in the *source* package (`MeteorRX-x.y.z-src.zip`, at the root of the `MeteorRX` folder);
  the `-win64.zip` package is already the built program and contains no build script. Windows 10/11, Internet: finds or privately installs the official Python 3.12, installs the
  pinned packages, downloads the drivers from their official releases and **checks their SHA-256** (and Zadig's
  signature), builds with PyInstaller, runs the self-test **of the built exe**, and writes
  `dist\MeteorRX-1.7.3-win64.zip`. Set `METEORRX_SIGN_PFX` / `METEORRX_SIGN_PASSWORD` to sign the executables.
  A rebuild keeps `dist\MeteorRX\meteorrx.ini` and `dist\MeteorRX\data\` (calibrations, receiver profiles,
  session counter): they are set aside before PyInstaller and put back afterwards (`build_portable.bat` does the
  same). Received images are in `Documents\MeteorRX` and are never touched by a build.
* **PyInstaller bootloader** — compiled from source with the Microsoft C++ compiler (see §9). If *Build Tools for
  Visual Studio* are missing, the script **stops and asks**:
  `1` install them now (official Microsoft bootstrapper, Authenticode signature checked before it is run, only the
  *Desktop development with C++* workload, ≈ 2–3 GB download, administrator prompt; if Visual Studio is already
  present without C++, the workload is added to it; `winget` is used if the download fails),
  `2` continue with the stock bootloader (higher false-positive risk), `3` stop without installing anything.
  If the installation fails or the compile fails, it offers `2`/`3` again. Unattended: set
  `METEORRX_BUILDTOOLS=install` or `=stock` beforehand.
* **GitHub**: `.github/workflows/ci.yml` runs the test-suite on Windows and Linux for every push and pull request.
  Pushing a tag `vX.Y.Z` (equal to `__version__`) runs `.github/workflows/release.yml`: tests, MSVC bootloader,
  build, optional signing (secrets `SIGN_PFX_BASE64`, `SIGN_PFX_PASSWORD`) and a **GitHub Release** with the
  `-win64.zip` and `-src.zip` packages. Third-party driver binaries are not stored in the repository: they are
  downloaded and hash-checked by `tools/fetch_drivers.py`.
* Alternative without any executable of our own: `build_portable.bat` (official signed Python embeddable runtime,
  shortcut launcher) — useful if a particular PC's antivirus still objects to the exe.

## 9. Antivirus (Windows Defender)

Version 1.0/1.1 was deleted by Defender: unsigned PyInstaller exe with the stock bootloader plus two unsigned,
unknown DLLs. Since 1.3:

* no DLL of our own (DSP compiled on the PC by Numba); drivers are the **official, widely distributed**
  RTL-SDR Blog and Zadig binaries, hash-pinned;
* PyInstaller **bootloader rebuilt from source** (MinGW-w64 for the provided build, MSVC with Build Tools / CI),
  one-folder mode, no UPX, full version information and icon.

This removes the usual triggers but **an unsigned executable can never be guaranteed clean**: Windows SmartScreen
may still show *"Windows protected your PC"* for a newly downloaded exe (*More info › Run anyway*). If Defender
flags it, report it as a false positive at https://www.microsoft.com/en-us/wdsi/filesubmission (section
*Software developer*); the definitive fix is to sign the exe with a code-signing certificate (supported by
`build_exe.bat` and the CI).

## 10. TLE troubleshooting

TLEs come from `data\tle.txt`, refreshed when older than `max_age_hours`, trying **CelesTrak** then **SatNOGS**,
each first with Python's HTTPS stack and then with the **Windows network stack** (Schannel, system proxy and
certificates). The log (`data\meteorrx.log`, menu *Drivers › Open log file*) gives the exact cause per server and
stack. Without Internet the cache is used, and the download is retried no sooner than `retry_hours` later
(default 1 h); with no TLE at all use *TLE › Import TLE file…* (e.g.
`https://celestrak.org/NORAD/elements/gp.php?GROUP=weather&FORMAT=tle` saved from a browser).

**Be a good citizen of the TLE servers.** CelesTrak asks programs to fetch only what they need, and a station
left running for days can add up. MeteorRX downloads only the satellites that are actually missing or stale —
one satellite you can never get no longer drags the others into a refresh — and remembers across restarts when
it last asked, in `data\tle.txt.state`. Three settings control the rest:

| Setting | Default | What it does |
|---|---|---|
| `max_age_hours` | 12 | Elements older than this are refreshed: in practice once at start-up, then twice a day |
| `refresh_days` | 0 | A floor on how often the servers are contacted at all. `0` means "whatever `max_age_hours` says". Set `2`, `3` or `7` if you leave MeteorRX running for days or only work the odd pass. MeteorRX warns in the log once the elements pass 10 days, which is where pass times start to drift noticeably; a few days' age was checked here to leave the coastline on the same pixel |
| `retry_hours` | 1 | How long to wait before trying again after a failed download |

*TLE › Update now* always downloads, whatever these say.

## 11. Validation status

* 89 tests (`python -m pytest -q tests`): **decoding of a real Meteor-M recording to imagery**, AFC on plain QPSK / CW, language tables, mirrored-spectrum
  locking (72k / 80k), continuous RTL-SDR streaming against a dongle FIFO model, PlutoSDR
  IIOD client against a server following libiio, HackRF and LimeSuite (simulated libraries), SDRplay structure layout vs the API 3.15 headers, weak-signal decoding (72k/80k/auto, offset, drift) and AFC false alarms,
  audio monitor, RS, Viterbi, MSU-MR codec, orbit vs Skyfield, TLE failure modes,
  no-overwrite rules (sequence numbers, two receptions of the same pass, crash-left lock, INI upgrade), receiver
  profiles (identity, per-receiver settings and calibrations, v1.4 calibration adoption, selection by serial
  number, preamp in service, GUI profile switching, maximized / full-screen window),
  calibration accuracy (tone level, noise density, fit, NF, Friis, S-meter) and the calibration window driven
  end-to-end on a simulated receiver,
  driver selection policy (V4/V3, incomplete and 32-bit DLLs, USB driver status), 80k resynchronisation after a
  fade + 90° phase slip, and **end-to-end decoding of synthetic LRPT signals** (full transmitter model) with noise,
  ±4 kHz offsets, both frequencies, 1.2 and 2 MS/s.
* Run on Linux and on **Windows CPython 3.12 (under Wine)**; the frozen `MeteorRX.exe` / `MeteorRX-cli.exe` 1.6.x
  were built and run under Wine: self-test, bundled `rtlsdr.dll` loaded, GUI decoding the same IQ file twice into
  `0001_…` and `0002_…` (nothing overwritten), maximized window with passes and log visible.
* **Not yet verified**: a live pass received with 1.6.1 (the real-signal fix is validated on a real recording,
  offline), real USB hardware outside the bench (Zadig flow, SetupAPI driver detection), Defender's verdict
  on the rebuilt exe. For the first passes set `save_baseband = yes` so a recording can be replayed (MeteorRX or
  SatDump) if anything goes wrong.

## 12. Licences

Copyright (C) 2026 Stephan, 9H2WB / F5OWB — Malta Amateur Radio League (MARL).

MeteorRX is **GPL-3.0** (`LICENSE`; algorithms derived from SatDump and meteor_decode/meteor_demod, GPL-3.0).
Details in `THIRD_PARTY_NOTICES.md`. Bundled:
`rtlsdr.dll` RTL-SDR Blog v1.3.6 (GPL-2.0+), Zadig 2.9 (GPL-3.0) — sources linked in each `SOURCE.txt`; Python
(PSF), NumPy, Numba, llvmlite (BSD), PySide6/Qt (LGPL-3.0), sgp4 (MIT), certifi (MPL-2.0). The SDRplay API is
proprietary and not distributed.

---

# MeteorRX — version française (résumé)

*Par Stephan 9H2WB / F5OWB pour la Malta Amateur Radio League (MARL). Téléchargement : page **Releases** ;
mode d'emploi : [docs/MANUAL.md](docs/MANUAL.md) (anglais, résumé en français à la fin).*

*Ce programme est né d'une demande de mon ami Joe 9H1VW. Merci à Anthony « Tony » Musumeci IK1HGI pour ses
longues heures d'essais, et à tous les OM qui ont couru derrière nos versions successives — téléchargements,
réinstallations et comptes rendus, passage après passage. Ce sont vos logs qui ont trouvé l'essentiel de ce
qui est corrigé ici. Que celle-ci tienne un moment.*

**But** : recevoir simplement les images LRPT des Meteor-M N2-3 et N2-4 avec un RTL-SDR, un RSP SDRplay (tous
modèles), un HackRF One, un LimeSDR Mini (1.x / 2.0) ou un PlutoSDR, valider le
signal par un **diagramme de l'œil** et enregistrer les images ; rien d'autre. Paquet autonome :
`MeteorRX.exe` + `meteorrx.ini` + pilotes RTL-SDR fonctionnels.

**Installation** : dézipper `MeteorRX-1.7.3-win64.zip` n'importe où, lancer `MeteorRX.exe`. Premier démarrage :
compilation du moteur DSP (20 à 60 s). Si Windows a attribué un mauvais pilote USB au RTL-SDR, MeteorRX le
détecte et propose de lancer **Zadig** (fourni) pour installer WinUSB. SDRplay : installer SDRplay API 3.x.

**Récepteurs (v1.6.0)** : RTL-SDR ; SDRplay RSP1, RSP1A, RSP1B, RSP2, RSPduo, RSPdx, RSPdx-R2 (réglages propres à
chaque modèle : Bias-T, réjecteur FM, port d'antenne A/B/C) ; HackRF One (pilote WinUSB via Zadig + `hackrf.dll`
de SatDump, SDR++ ou PothosSDR, trouvée automatiquement) ; LimeSDR Mini 1.x / 2.0 (pilote FTDI D3XX **1.3.0.10 x64,
version WDF** — pas la 1.4 WinUSB — + `LimeSuite.dll` 23.11 de SatDump 1.2) ; PlutoSDR par son interface réseau native (protocole
IIOD, aucun pilote ni bibliothèque à installer, `pluto_uri = ip:192.168.2.1`). Le Pluto peut recevoir pendant
qu'il émet la mire MARL (même horloge d'échantillonnage, 2,16 MS/s).

**Aucun décodage sur les vrais satellites (corrigé en 1.6.1)** : le signal réel des Meteor-M arrive avec le
spectre inversé (I et Q permutés) par rapport à la convention théorique de la mire. Jusqu'à la 1.6.0 MeteorRX ne
testait que les rotations de phase, qui ne peuvent pas produire un spectre miroir : mire parfaite, passages réels
jamais verrouillés. Le sens I/Q dépend de la chaîne de réception (pilote SDR, inversion de spectre), d'où
l'essai des deux orientations dans tous les décodeurs LRPT publics. Vérifié sur un enregistrement réel
(Meteor-M N2, 105 s) : 0 trame en 1.6.0, 916 trames et 3573 paquets avec la 1.6.1 à travers le récepteur
complet (référence MATLAB du même enregistrement : 925 / 3523), verrouillage en 0,5 s, image nette. Reste à
éprouver sur un passage réel M2-3 / M2-4 (OQPSK + NRZ-M).

**Réception RTL-SDR hachée (corrigé en 1.6.0)** : jusqu'à la 1.5.1 le RTL-SDR était lu en mode synchrone entre
deux blocs de traitement ; sa FIFO débordait pendant le traitement → trous toutes les ~100 ms (son haché), et
comme une trame LRPT dure 114 ms, aucune trame complète ne passait, quel que soit le niveau. Désormais lecture
asynchrone continue (15 transferts USB en file, comme SatDump / SDR#) et **débit mesuré en permanence** (« Flux
SDR » : 100 % = aucune perte, ⚠ sinon, avec avertissement dans le journal). Diagnostic console :
`MeteorRX-cli --live 60 --device rtlsdr --mode 72k --record test.cs16`.

**Pilotes RTL-SDR** : MeteorRX cherche d'abord un `rtlsdr.dll` déjà installé (rtl_dll_dir, SatDump, SDR++, SDR#,
PATH) et ne l'utilise que s'il convient (DLL 64 bits valide, toutes les fonctions nécessaires, dépendances
présentes et, si un RTL-SDR Blog V4 est branché, support V4). Sinon il utilise la bibliothèque **RTL-SDR Blog
v1.3.6** fournie. Menu **Pilotes › Diagnostic** pour voir la décision.

**Écoute audio** : bouton *Muet / Écoute* et volume sous le S-mètre — démodulation FM du canal 137 MHz sur les
haut-parleurs du PC pour contrôler à l'oreille (souffle, signal LRPT, brouillage) ; muet au démarrage, sans effet
sur le décodage.

**Sensibilité (v1.6.0)** : la chaîne de décodage a été mesurée contre un décodeur idéal
(`tools/bench_sensitivity.py`). Les versions ≤ 1.5.0 perdaient ≈ 4 dB (boucle de Costas trop large + FLL qui
décrochaient à faible SNR, boucles « garées » sous 1 dB de SNR spectral, verrouillage décidé par un seuil de BER
trop bas) : il fallait Es/N0 ≈ 5,5 dB pour 90 % des trames. Désormais une **AFC par puissance 4** détecte la
porteuse sous Es/N0 = 1 dB et cale le NCO à quelques Hz près (y compris dérive Doppler ±120 Hz/s, rejet des
porteuses pures/raies parasites), la boucle de Costas est étroite, et le verrouillage est validé par une trame
Reed-Solomon correcte : 96 % des trames à Es/N0 = 2 dB (idéal 99 %), perte d'implémentation ≈ 0,4 dB. Au
générateur, Es/N0 = 2 dB correspond à environ **−117 dBm** (puissance totale du signal modulé) à l'entrée d'un
RTL-SDR de NF 6 dB, ≈ −122 dBm avec un préampli de NF 1 dB. Les clés
`pll_bw` / `fll_gain` des anciens .ini sont ignorées.

**Interface** : à gauche profil récepteur, commandes, voyants, S-mètre, diagramme de l'œil et constellation,
mesures, spectre ; à droite l'image en direct (RGB 221, RGB 124, RGB 123 ou canal, zoom à la molette) et, dessous, toujours
visibles, les **prochains passages** et le **journal**. La fenêtre s'ouvre **maximisée** (`[gui] window =
maximized | fullscreen | last`), **F11** = plein écran, **Échap** pour en sortir ; tailles des panneaux mémorisées.

**Fonctionnement automatique** : TLE (CelesTrak puis SatNOGS, pile réseau Python puis Windows) ; armement avant
l'AOS ; enregistrement déclenché par le signal ; arrêt au LOS ou après perte du signal.

**Aucun fichier n'est jamais écrasé** : chaque réception reçoit un **numéro de séquence** (conservé dans
`data\sequence.json`, jamais réutilisé, même après redémarrage) en tête du dossier et des fichiers, par ex.
`Documents\MeteorRX\0007_20261027_0912_METEOR-M2-4\0007_20261027_0912_METEOR-M2-4.cadu` (+ images, `_info.json`).
Un redémarrage pendant le même passage crée `0008_…` à côté de `0007_…`. Fichiers ouverts en création exclusive et
vidés sur disque toutes les 2 s. Le re-décodage d'un `.cadu` écrit dans `…_products`, puis `…_products_2`, etc.

**Profils récepteur** : chaque récepteur est reconnu par son identité USB (RTL-SDR : fabricant, produit, numéro de
série, tuner ; SDRplay : modèle et numéro de série) et possède son profil (`data\receivers.json`) : réglages (gain,
fréquence d'échantillonnage, ppm, Bias-T, réglages SDRplay), **étalonnages** (un par réglage de gain), **valeurs de
la fenêtre d'étalonnage** et **préampli**. Au démarrage (et bouton *Détecter*) les récepteurs branchés sont
identifiés ; un nouveau récepteur reçoit automatiquement un profil (le premier reprend les réglages et
étalonnages existants de la v1.4). Choisir un profil restaure tous ses réglages ; la ligne d'état indique s'il est
branché et **étalonné pour les réglages actuels**. Plusieurs dongles au numéro de série identique (souvent
`00000001`) ne peuvent pas être distingués : MeteorRX le signale ; leur donner un numéro unique avec
`rtl_eeprom -s <numéro>`.

**Étalonnage et S-mètre** : menu **Mesures › Étalonnage du récepteur / qualification préampli**. Générateur en
CW (Viavi 3920 par ex.) relié à l'entrée, gain fixe. Partie 1 : trois points (−70, −90, −110 dBm par défaut)
puis bruit générateur RF OFF mais branché → constante K (dBFS → dBm), linéarité (pente, résidus, compression),
facteur de bruit du récepteur, erreur de fréquence en ppm (applicable au .ini). Partie 2 : préampli inséré et
alimenté, −110 dBm → gain réel, −80 dBm → contrôle de compression, bruit → NF du préampli (Friis). Ensuite la
fenêtre principale affiche un S-mètre étalonné (dBm et unités S, échelle IARU R1 VHF S9 = −93 dBm), N0 et C/N0.
Étalonnage valable pour le récepteur et les réglages avec lesquels il a été fait ; rapport et historique dans
`data\`. Après la mesure du préampli, MeteorRX propose de le **mettre en service** sur ce récepteur : le S-mètre est
alors ramené à l'entrée du préampli (antenne) ; case *Préampli en service* pour l'activer ou non.

**Antivirus** : plus aucune DLL maison, pilotes officiels vérifiés par SHA-256, bootloader PyInstaller
recompilé depuis les sources, sans UPX, avec informations de version. Un exe non signé ne peut toutefois jamais
être garanti : SmartScreen peut afficher « Windows a protégé votre ordinateur » (*Informations
complémentaires › Exécuter quand même*). En cas de détection Defender : déclarer le faux positif à Microsoft ;
la solution définitive est la signature de code (prise en charge par `build_exe.bat` et la CI).

**Construire** : `build_exe.bat` (Python officiel installé si absent, pilotes vérifiés, autotest de l'exe
construit, zip ; une reconstruction conserve `meteorrx.ini` et `data\` — étalonnages, profils, compteur de
sessions), ou la CI GitHub fournie (bootloader compilé par MSVC, signature possible). Si les Build Tools de
Visual Studio manquent, le script s'arrête et propose : 1 = les installer (installeur officiel Microsoft, signature
vérifiée avant exécution, charge de travail C++ seule, ≈ 2–3 Go, accord administrateur ; si Visual Studio est déjà
installé sans C++, la charge C++ y est ajoutée), 2 = continuer avec le bootloader standard (risque antivirus plus
élevé), 3 = arrêter sans rien installer.

**Statut** : 89 tests validés sous Linux et sous Python Windows (Wine) — dont non-écrasement (deux réceptions du
même passage), profils récepteur, adoption des étalonnages v1.4, fenêtre maximisée ; exe 1.6.0 construit et testé
sous Wine (autotest, pilote chargé, deux décodages successifs du même fichier IQ → dossiers `0001_…` et `0002_…`). Non encore éprouvé : passage réel reçu en direct avec la 1.6.1 (correctif validé hors ligne sur un enregistrement
réel), matériel USB réel hors banc, verdict de Defender sur cet exe. Banc mire MARL (−55 dBm) : RTL-SDR 47 dB, LimeSDR Mini 2.0
50,7 dB, 0 erreur ; HackRF : exemplaire testé défectueux (signal dégradé), commande et flux validés. Pour les premiers passages : `save_baseband = yes`.

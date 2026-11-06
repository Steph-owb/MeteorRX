"""INI configuration (meteorrx.ini next to the executable)."""
from __future__ import annotations

import configparser
import os
import sys
from dataclasses import dataclass, field

DEFAULT_INI = r"""; =====================================================================
;  MeteorRX - Meteor-M LRPT receiver - configuration file
;  Fichier de configuration / configuration file (UTF-8)
;  Lines starting with ';' are comments.  Les lignes ';' sont des commentaires.
; =====================================================================

[station]
; Station position (decimal degrees, WGS84). Malta JM75FU by default.
latitude      = 35.8800
longitude     = 14.4500
altitude_m    = 50
; Only passes with a maximum elevation above this value are scheduled (deg)
min_elevation = 15
; Your callsign (shown in the window title)
callsign      =

[sdr]
; The receiver values below are those of the active receiver profile (data\receivers.json):
; each receiver (identified by its serial number) keeps its own settings and calibrations.
; device = rtlsdr | sdrplay | hackrf | lime | pluto | file
device          = rtlsdr
; Centre frequency (MHz). 137.500 covers both 137.100 and 137.900 at once.
center_mhz      = 137.500
; Frequency correction of the receiver reference (ppm, float)
ppm             = 0.0

; ---- RTL-SDR --------------------------------------------------------
rtl_index       = 0
; Serial number of the dongle to use (set by the receiver profile; empty = rtl_index)
rtl_serial      =
rtl_sample_rate = 1200000
; Tuner gain in dB, or "auto"
rtl_gain_db     = 38.6
rtl_bias_tee    = no
; Folder holding rtlsdr.dll + libusb-1.0.dll. Empty = automatic: MeteorRX\drivers, then the
; SatDump / SDR++ / SDR# installation folders.
rtl_dll_dir     =

; ---- SDRplay RSP, all models: RSP1, RSP1A, RSP1B, RSP2, RSPduo, RSPdx, RSPdx-R2 ----------------
; (SDRplay API 3.x must be installed)
; Leave empty to use the standard install path
sdrplay_api_dll   =
sdrplay_serial    =
sdrplay_sample_rate = 2000000
; IF gain reduction 20..59 dB; LNA state at 137 MHz: RSP1 0..3, RSP1A/1B/duo 0..9, RSP2 0..8, RSPdx 0..26
sdrplay_grdb      = 40
sdrplay_lna_state = 3
sdrplay_agc       = no
sdrplay_bias_tee  = no
; Broadcast FM notch (recommended near FM transmitters)
sdrplay_fm_notch  = yes
; Antenna port: RSP2 A/B, RSPdx A/B/C (other models: single port)
sdrplay_antenna   = A

; ---- HackRF One (uses hackrf.dll of SatDump / SDR++ / PothosSDR; WinUSB driver via Zadig) ----
hackrf_serial      =
; 2 MS/s minimum; 2.4 MS/s keeps both Meteor channels away from the DC spike
hackrf_sample_rate = 2400000
; LNA (IF) gain 0..40 dB in 8 dB steps, VGA (baseband) gain 0..62 dB in 2 dB steps, RF amplifier +14 dB
hackrf_lna_gain    = 24
hackrf_vga_gain    = 30
hackrf_amp         = no
hackrf_bias_tee    = no
; Folder of hackrf.dll; empty = automatic (SatDump, SDR++, PothosSDR installation folders)
hackrf_dll_dir     =

; ---- LimeSDR Mini 1.x / 2.0 (uses LimeSuite.dll of SatDump; FTDI D3XX driver) --------------
lime_serial        =
; 2.4 MS/s keeps both Meteor channels away from the DC residue
lime_sample_rate   = 2400000
; RX gain 0..73 dB
lime_gain_db       = 40
; RX path: auto (LNAW below 2 GHz) | LNAW | LNAH | LNAL
lime_antenna       = auto
; Folder of LimeSuite.dll; empty = automatic (SatDump installation folder first)
lime_dll_dir       =

; ---- PlutoSDR (ADALM-Pluto) over the network, no driver/library needed ----------------------
; ip:192.168.2.1 = Pluto on USB (USB network adapter); ip:<address> on Ethernet / Wi-Fi
pluto_uri          = ip:192.168.2.1
; >= 2083334 (AD9363 without FIR). 2160000 = same as the MARL test-pattern generator (shared RX/TX clock)
pluto_sample_rate  = 2160000
; RX gain 0..71 dB, or auto (AGC slow attack)
pluto_gain_db      = 50

; ---- File playback (testing / re-processing) ------------------------
; WAV IQ (SDR#, SatDump baseband .wav) or raw .cu8 / .cs8 / .cs16 / .cf32
file_path       =
; For raw files only (Hz); WAV files carry their own rate
file_sample_rate = 1000000
; Centre frequency of the recording (MHz), 0 = same as center_mhz
file_center_mhz = 0
; yes = paced in real time, no = as fast as the CPU allows
file_realtime   = no

[satellites]
; name = NORAD id ; frequencies MHz separated by "/" ; mode auto|72k|80k
METEOR-M2 3 = 57166 ; 137.900/137.100 ; auto
METEOR-M2 4 = 59051 ; 137.900/137.100 ; auto

[tle]
; TLEs are cached in data\tle.txt and refreshed when older than max_age_hours.
; Sources tried in order (CelesTrak, then SatNOGS). On failure the cache is kept.
; Offline: menu TLE > Import TLE file
; (e.g. https://celestrak.org/NORAD/elements/gp.php?GROUP=weather&FORMAT=tle saved as .txt).
max_age_hours = 12
; Shortest time between downloads, in days. 0 = whenever the cache is older than
; max_age_hours, which in practice means once at start-up and then twice a day.
; CelesTrak asks programs not to fetch more than they need, so if you leave MeteorRX
; running for days, or you only work the odd pass, set 2, 3 or 7 here. MeteorRX warns in
; the log once the elements pass 10 days old, which is where pass times start to drift.
; "TLE > Update now" always downloads, whatever this says.
refresh_days  = 0
; Shortest time before trying again after a failed download, in hours. Stops a station
; with no internet from knocking on the servers' door every quarter of an hour.
retry_hours   = 1
; Optional comma-separated list of URL templates replacing the defaults ({norad} = NORAD id)
urls          =
; Optional HTTP(S) proxy, e.g. http://192.168.1.1:3128 (the Windows proxy setting is used otherwise)
proxy         =

[decoder]
; Default mode when no satellite is selected (manual): auto | 72k | 80k
mode            = auto
; Recording starts when the spectrum SNR exceeds this value (dB), when the carrier is detected
; (4th-power AFC, works far below this SNR) or on decoder lock
trigger_snr_db  = 3.0
; Doppler correction from TLE during passes
doppler         = yes
; Advanced DSP - empty = built-in values (measured with tools\bench_sensitivity.py).
; The pll_bw / fll_gain keys of older versions are ignored.
carrier_loop_bw =
clock_loop_gain =
rrc_alpha       = 0.5

[recording]
; Output folder; {documents} = the user's Documents folder
output_dir          = {documents}\MeteorRX
; Arm the receiver this many seconds before AOS
pre_aos_s           = 60
; Stop when no decodable signal for this long (after it was present), or at LOS
signal_loss_stop_s  = 90
; Give up on a recording that decoded nothing this many seconds after the signal was detected (a spur or
; an interferer can trigger one). 0 = never give up. Keeps a false trigger from swallowing the next pass.
false_trigger_s     = 300
; Panoramic ("earth curvature") correction: writes a ..._corr.png beside each image, 1 km/pixel on the
; ground, 1568 -> 2800 px wide. The original images are kept untouched.
geometric_correction = yes
; Smallest city labelled by the "Major cities" overlay in the Enhancements window. Lower it for a denser
; map (25000 is about the smallest that stays readable), raise it for only the big ones.
city_min_population = 200000
; Housekeeping of Documents\MeteorRX at start-up.
; purge_empty: yes = remove folders a pass left completely empty, no = keep them
purge_empty         = yes
; purge_days: delete whole receptions older than this many days. 0 = never delete (default), 30 = a sensible
; value for an unattended station. This removes decoded images, so it only runs if you set it yourself.
purge_days          = 0
save_cadu           = yes
; Raw IQ of the channel (~300 kS/s, int16, ~1.2 MB/s). For debugging only.
save_baseband       = no
fill_missing        = yes
; Rotate images of northbound passes by 180 deg (north up)
rotate_northbound   = yes
; Composite outputs, and the views offered in the picture window. Each one is three channel numbers
; read as red, green, blue:
;   221 = natural colour (ch2, ch2, ch1)
;   124 = the MSU-MR false colour of SatDump (ch1, ch2, ch4): sea dark, land green, cloud tops yellow
;   123 = (ch1, ch2, ch3), only if channel 3 is being transmitted
composites          = 221, 124, 123
; Also save inverted versions of the IR channels (4, 5, 6)
ir_inverted         = yes

[gui]
; Interface language: fr | en | it | es | de  (table in meteorrx/i18n.py; add a language there)
language        = fr
; Start the automatic scheduler at launch
auto_start      = yes
; Window at launch: maximized | fullscreen | last (size/position of the previous session). F11 = full screen
window          = maximized
; Volume of the audio monitor (0-100); the monitor itself always starts muted
audio_volume    = 50
"""


def _exe_dir() -> str:
    """Installation root. Portable layout: <root>\\app\\meteorrx (sources) + <root>\\runtime (Python)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    parent = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    root = os.path.dirname(parent)
    if os.path.basename(parent).lower() == "app" and os.path.isdir(os.path.join(root, "runtime")):
        return root
    return parent


def _writable(d: str) -> bool:
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".meteorrx_write_test")
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        return True
    except OSError:
        return False


def app_dir() -> str:
    """Folder holding meteorrx.ini and data/: next to the exe (portable install), or
    %LOCALAPPDATA%\\MeteorRX when the program folder is read-only (e.g. Program Files)."""
    d = _exe_dir()
    if _writable(d):
        return d
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    alt = os.path.join(base, "MeteorRX")
    os.makedirs(alt, exist_ok=True)
    return alt


def documents_dir() -> str:
    home = os.path.expanduser("~")
    d = os.path.join(home, "Documents")
    return d if os.path.isdir(d) else home


@dataclass
class SatConf:
    name: str
    norad: int
    freqs_mhz: list
    mode: str


@dataclass
class Config:
    path: str
    raw: configparser.ConfigParser
    satellites: list = field(default_factory=list)
    _dirty: set = field(default_factory=set)
    migrated: list = field(default_factory=list)   # settings carried forward from an older ini

    # convenience accessors ---------------------------------------------------
    def get(self, sec, key, fallback=None):
        return self.raw.get(sec, key, fallback=fallback)

    def getf(self, sec, key, fallback=0.0) -> float:
        v = self.raw.get(sec, key, fallback=None)
        try:
            return float(v) if v not in (None, "") else fallback
        except ValueError:
            return fallback

    def geti(self, sec, key, fallback=0) -> int:
        return int(self.getf(sec, key, fallback))

    def getb(self, sec, key, fallback=False) -> bool:
        try:
            return self.raw.getboolean(sec, key, fallback=fallback)
        except ValueError:
            return fallback

    @property
    def output_dir(self) -> str:
        d = self.get("recording", "output_dir", "{documents}\\MeteorRX")
        d = d.replace("{documents}", documents_dir()).replace("\\", os.sep)
        return os.path.expandvars(d)

    @property
    def data_dir(self) -> str:
        return os.path.join(app_dir(), "data")

    def set(self, sec, key, value):
        if not self.raw.has_section(sec):
            self.raw.add_section(sec)
        self.raw.set(sec, key, str(value))
        self._dirty.add((sec, key))

    def save(self):
        """Rewrite only changed values, keeping comments: update lines in place.
        Keys set by the program that are not yet in the file (older INI) are appended to their section."""
        with open(self.path, "r", encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
        sec = None
        done = set()
        out = []
        for ln in lines:
            s = ln.strip()
            if s.startswith("[") and s.endswith("]"):
                sec = s[1:-1]
            elif sec and "=" in s and not s.startswith(";") and sec != "satellites":
                k = s.split("=", 1)[0].strip()
                if self.raw.has_option(sec, k):
                    ln = f"{ln.split('=', 1)[0]}= {self.raw.get(sec, k)}"
                    done.add((sec, k))
            out.append(ln)
        missing = sorted(k for k in self._dirty if k not in done and k[0] != "satellites")
        for sec, key in missing:
            line = f"{key:<15} = {self.raw.get(sec, key)}"
            idx = None
            cur = None
            for i, ln in enumerate(out):              # insert after the last line of the section
                st = ln.strip()
                if st.startswith("[") and st.endswith("]"):
                    cur = st[1:-1]
                elif cur == sec and st:
                    idx = i
                if cur == sec and idx is None:
                    idx = i
            if idx is None:
                out += ["", f"[{sec}]", line]
            else:
                out.insert(idx + 1, line)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        os.replace(tmp, self.path)


def _parse_sats(cp: configparser.ConfigParser) -> list[SatConf]:
    out = []
    if not cp.has_section("satellites"):
        return out
    for name, val in cp.items("satellites"):
        parts = [p.strip() for p in val.split(";")]
        try:
            norad = int(parts[0])
            freqs = [float(x) for x in parts[1].split("/")] if len(parts) > 1 and parts[1] else [137.9, 137.1]
            mode = parts[2].lower() if len(parts) > 2 and parts[2] else "auto"
            out.append(SatConf(name.upper(), norad, freqs, mode if mode in ("auto", "72k", "80k") else "auto"))
        except (ValueError, IndexError):
            continue
    return out


# Settings whose shipped default has changed. A stored value matching an old default verbatim was
# never a choice the operator made - it is just an ini written by an older build - so it is brought
# forward. Anything the operator actually edited is left exactly as they wrote it.
SUPERSEDED: dict[tuple[str, str], tuple[str, ...]] = {
    ("recording", "composites"): ("221, 123",),     # 1.7.3 added MSU-MR 124
}


def _carry_defaults_forward(cp, defaults, path, enc="utf-8-sig") -> list[str]:
    """Replace stored values that are only stale defaults, and say which were changed."""
    changed, values = [], {}
    for (sec, key), old in SUPERSEDED.items():
        if not cp.has_option(sec, key) or not defaults.has_option(sec, key):
            continue
        have = cp.get(sec, key).strip()
        new = defaults.get(sec, key).strip()
        if have != new and any(have == o.strip() for o in old):
            cp.set(sec, key, new)
            values[(sec, key)] = new            # only what actually changed is rewritten
            changed.append(f"[{sec}] {key}: {have} -> {new}")
    if values:
        _rewrite_in_place(path, values, enc)
    return changed


def _rewrite_in_place(path, values: dict, enc: str = "utf-8-sig") -> None:
    """Update the named values in the file, keeping every comment and every other line.

    Walks the sections the way Config.save does, rather than matching the key anywhere in the file:
    the same key name can legitimately appear in two sections. Written through a temporary file and
    renamed, so a crash mid-write cannot leave the operator with a truncated or empty ini - that
    would silently reset their station coordinates and callsign to the defaults on the next start.
    A read-only install simply keeps the new value in memory.
    """
    try:
        with open(path, "r", encoding=enc) as f:
            lines = f.read().splitlines()
        sec, out, seen = None, [], set()
        for ln in lines:
            s = ln.strip()
            if s.startswith("[") and s.endswith("]"):
                sec = s[1:-1]
            elif sec and not s.startswith(";") and ("=" in s or ":" in s):
                delim = min((s.index(d) for d in "=:" if d in s), default=-1)
                k = s[:delim].strip()
                if (sec, k) in values and (sec, k) not in seen:
                    seen.add((sec, k))          # only the first occurrence; configparser uses that one
                    ln = f"{ln[:ln.index(s[delim])]}{s[delim]} {values[(sec, k)]}"
            out.append(ln)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        os.replace(tmp, path)
    except (OSError, UnicodeError):
        pass                     # a read-only install keeps the new value in memory and runs on


def load(path: str | None = None) -> Config:
    path = path or os.path.join(app_dir(), "meteorrx.ini")
    if not os.path.isfile(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(DEFAULT_INI)
    cp = configparser.ConfigParser(inline_comment_prefixes=None, interpolation=None)
    cp.optionxform = str  # keep satellite names as written
    defaults = configparser.ConfigParser(interpolation=None)
    defaults.optionxform = str
    defaults.read_string(DEFAULT_INI)
    cp.read_string(DEFAULT_INI)
    if cp.has_section("satellites"):
        cp.remove_section("satellites")
        cp.add_section("satellites")
    # Operators edit this file in Notepad. "Save as UTF-8" there has historically added a byte-order
    # mark, and the ANSI default writes cp1252 - an accent in a callsign, a town or even a comment then
    # killed the frozen program at start-up with nothing but a PyInstaller error box. utf-8-sig eats
    # the mark, and cp1252 is the fallback for a file Notepad wrote as ANSI.
    enc = "utf-8-sig"
    for enc in ("utf-8-sig", "cp1252"):
        try:
            cp.read(path, encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    if not cp.items("satellites"):
        for k, v in defaults.items("satellites"):
            cp.set("satellites", k, v)
    migrated = _carry_defaults_forward(cp, defaults, path, enc)
    cfg = Config(path=path, raw=cp)
    cfg.satellites = _parse_sats(cp)
    cfg.migrated = migrated          # the engine logs these once at start-up
    return cfg

"""Station engine: pass scheduling, SDR session, signal trigger, recording and product saving.

Runs in its own thread; the GUI sends commands through a queue and reads snapshots."""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from . import audio, imaging, orbit, outputs, sdr
from .receiver import Receiver, ReceiverConfig, dsp_from_config, warm_up


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Snapshot:
    state: str = "idle"              # idle | waiting | armed | recording | saving | file
    message: str = ""
    source: str = ""
    sat: str = ""
    mode: str = ""
    pass_info: orbit.Pass | None = None
    passes: list = None
    elevation: float | None = None
    azimuth: float | None = None
    snr_db: float = -99.0
    freq_hz: float = 0.0
    doppler_hz: float = 0.0
    loop_hz: float = 0.0
    signal: bool = False
    vit_locked: bool = False
    vit_ber: float = 0.5
    frame_lock: bool = False
    frames_ok: int = 0
    frames_bad: int = 0
    packets: int = 0
    lrpt_mode: str = ""
    eye: tuple | None = None
    syms: np.ndarray | None = None
    spectrum: tuple | None = None
    preview: tuple | None = None
    preview_rev: int = 0
    recording: bool = False
    rec_seconds: float = 0.0
    out_folder: str = ""
    file_progress: float | None = None
    tle_info: str = ""
    level_dbfs: float = -999.0        # Meteor channel power (noise removed)
    n0_dbfs_hz: float = -999.0
    cal_k: float | None = None        # dBm = dBFS + cal_k when a calibration matches the settings
    stream_ratio: float | None = None # delivered / nominal sample rate of the SDR (1.00 = no loss)
    stream_dropped: int = 0           # samples dropped because the processing did not keep up


class Session:
    def __init__(self, engine: "Engine", sat: orbit.Satellite | None, pas: orbit.Pass | None, mode: str,
                 manual: bool):
        self.e = engine
        cfg = engine.cfg
        self.sat, self.pas, self.manual = sat, pas, manual
        self.source = sdr.open_source(cfg)
        freqs = [f * 1e6 for f in (sat.freqs_mhz if sat else [137.9, 137.1])]
        dsp = dsp_from_config(cfg)
        rc = ReceiverConfig(sample_rate=self.source.sample_rate, center_hz=self.source.center_hz, freqs_hz=freqs,
                            mode=mode, trigger_snr_db=cfg.getf("decoder", "trigger_snr_db", 3.0),
                            ppm=cfg.getf("sdr", "ppm", 0.0), dsp=dsp)
        self.rx = Receiver(rc, cadu_sink=self._cadu)
        mon = getattr(engine, "audio", None)
        self.rx.audio_sink = mon.push if mon is not None else None
        self.mode = mode
        self.start_utc = utcnow()
        self.recording = False
        self.rec_start: float | None = None
        self.last_signal = time.monotonic()
        self.cadu_f = None
        self.bb_f = None
        # Folder/prefix are allocated on the first write: 0007_20261027_0912_METEOR-M2-4 (persistent
        # sequence number, never reused, so a restart can never overwrite a previous reception).
        self._stamp = (pas.aos if pas else self.start_utc).strftime("%Y%m%d_%H%M")
        self._tag = (sat.name if sat else "METEOR").replace(" ", "-")
        self.folder: str | None = None
        self.prefix: str | None = None
        self.seq: int | None = None
        self._t_flush = 0.0
        self.health = None
        self._warned_rate = self._warned_drop = False
        self._last_dropped = 0
        self.save_cadu = cfg.getb("recording", "save_cadu", True)
        self.save_bb = cfg.getb("recording", "save_baseband", False)
        self.doppler = cfg.getb("decoder", "doppler", True) and sat is not None and sat.sat is not None \
            and not self.source.is_file
        self.preview_rev = 0
        self.cal_k = self._calibration(cfg)
        self._t_prev = 0.0
        if not self.source.is_file:
            warm_up(rc)                        # DSP ready before the SDR starts streaming
        self.source.start()

    def _check_stream(self):
        """Lost samples are fatal to LRPT (every gap restarts the loops): measure and report them."""
        try:
            h = self.source.health()
        except Exception:  # noqa: BLE001
            h = None
        self.health = h
        if not h:
            return
        r = h.get("ratio")
        if r is not None and r < 0.95 and not self._warned_rate:
            self._warned_rate = True
            self.e.log(f"WARNING: {self.source.name} delivers {h['rate'] / 1e6:.3f} MS/s instead of "
                       f"{h['nominal'] / 1e6:.3f} ({100 * (1 - r):.0f} % of the samples lost on USB/network): "
                       "use another USB port (no hub), close other SDR programs, or lower the sample rate")
        d = h.get("dropped", 0)
        if d > self._last_dropped:
            if not self._warned_drop:
                self.e.log(f"WARNING: the PC does not keep up, {d} samples dropped: use mode 72k instead of "
                           "auto, close other programs, or lower the sample rate")
                self._warned_drop = True
            self._last_dropped = d

    def _calibration(self, cfg) -> float | None:
        """S-meter constant of the active receiver profile (None = uncalibrated / other receiver)."""
        from . import calib, profiles
        if self.source.is_file:
            return None
        if getattr(self.source, "warning", ""):
            self.e.log("WARNING: " + self.source.warning)
        store = profiles.ProfileStore(cfg.data_dir)
        prof = store.active
        ident = getattr(self.source, "identity", None)
        if prof is not None and ident is not None and ident.key != prof["identity"]["key"]:
            other = store.find(ident)
            if other is None:
                self.e.log(f"Receiver {ident.label} has no profile: S-meter uncalibrated")
                return None
            prof = other                              # calibration of the receiver actually in use
        c = calib.CalStore(os.path.join(cfg.data_dir, "calibration.json")).get_for(cfg, prof["id"] if prof else None)
        if c is None or not c.ok:
            return None
        g = profiles.ProfileStore.preamp_gain(prof)
        if g:
            self.e.log(f"S-meter referred to the preamplifier input ({prof['preamp'].get('name', '')}, "
                       f"{g:+.1f} dB)")
        return c.k_db - g

    # -- sinks ----------------------------------------------------------------
    def _ensure_folder(self) -> str:
        if self.folder is None:
            self.folder, self.prefix, self.seq = outputs.new_session_dir(
                self.e.cfg.output_dir, self.e.cfg.data_dir, self._stamp, self._tag)
            self.e.log(f"Session #{self.seq:04d}: {self.folder}")
        return self.folder

    def _cadu(self, cadu: bytes):
        if not (self.save_cadu and self.recording):
            return
        if self.cadu_f is None:
            self._ensure_folder()
            self.cadu_f = outputs.open_new(os.path.join(self.folder, self.prefix + ".cadu"))
        self.cadu_f.write(cadu)

    def _bb(self, x: np.ndarray):
        if self.bb_f is None:
            self._ensure_folder()
            p = os.path.join(self.folder, f"{self.prefix}_baseband_{int(self.rx.chan_rate)}Hz.cs16")
            self.bb_f = outputs.open_new(p)
        v = np.clip(x.view(np.float32) * 8192.0, -32767, 32767).astype("<i2")
        self.bb_f.write(v.tobytes())

    # -- processing -----------------------------------------------------------
    def step(self) -> bool:
        """Process one block. Returns False when the session must end."""
        blk = self.source.read(int(self.source.sample_rate * 0.1))
        if blk is None:
            return False
        if len(blk) == 0:
            return True
        if self.doppler:
            rr = self.e.observer.look(self.sat, utcnow())[3]
            self.rx.set_range_rate(rr)
        self.rx.process(blk)
        now = time.monotonic()
        if now - self._t_flush > 2.0:          # data on disk even if the app is killed / PC crashes
            self._t_flush = now
            self._check_stream()
            for f in (self.cadu_f, self.bb_f):
                if f:
                    f.flush()
        locked = self.rx.locked_chain is not None
        if self.rx.signal_present or locked:
            self.last_signal = now
            if not self.recording:
                self.recording = True
                self.rec_start = now
                why = "carrier detected (AFC)" if self.rx.carrier_detected and self.rx.snr_db < \
                    self.rx.rc.trigger_snr_db else f"SNR {self.rx.snr_db:.1f} dB"
                self.e.log(f"Signal detected ({why}, offset {self.rx.total_offset_hz():+.0f} Hz) - recording started")
                if self.save_bb:
                    self.rx.baseband_sink = self._bb
        # end conditions
        if self.source.is_file:
            return True
        if self.pas and not self.manual and utcnow() > self.pas.los + timedelta(seconds=20):
            self.e.log("LOS reached")
            return False
        decoded = sum(st.frames_ok for st in self.rx.chain_stats())
        # A recording started on something that never decodes (interference, a spur, a neighbouring
        # emitter) is a false trigger: give up instead of filling the disk and, worse, swallowing the
        # next real pass into this session. Only in automatic mode: a manual recording is someone sitting
        # there on purpose - testing an antenna, chasing an interferer - and must not be cut short.
        false_s = self.e.cfg.getf("recording", "false_trigger_s", 300)
        if self.recording and not decoded and false_s > 0 and not self.manual and self.rec_start is not None \
                and now - self.rec_start > false_s:
            self.e.log(f"Nothing decoded {false_s:.0f} s after the signal was detected "
                       f"- false trigger, stopping")
            return False
        loss = self.e.cfg.getf("recording", "signal_loss_stop_s", 90)
        # In manual mode the session normally runs until the user stops it, but once a pass has actually
        # been decoded, losing the signal means that pass is over: save it rather than let the next one
        # be appended to the same image.
        if self.recording and now - self.last_signal > loss and (not self.manual or decoded):
            self.e.log(f"No signal for {loss:.0f} s - stopping")
            return False
        return True

    def finish(self) -> list[str]:
        try:
            self.source.stop()
        except Exception:
            pass
        for f in (self.cadu_f, self.bb_f):
            if f:
                f.close()
        files = []
        im = self.rx.imager
        if im.packets > 0:
            cfg = self.e.cfg
            comps = [c.strip() for c in (cfg.get("recording", "composites", "221") or "").split(",") if c.strip()]
            # A hand-started recording has no predicted pass, but the engine has since worked out which
            # satellite it is and which way it was going. Using that here keeps the saved products in
            # the same orientation as the picture on screen and as the enhanced copy.
            sat, north = self.sat, (self.pas.northbound if self.pas is not None else None)
            if sat is None:
                sat, north = getattr(self, "_manual_sat", (None, None))
            rotate = bool(cfg.getb("recording", "rotate_northbound", True) and north)
            self._ensure_folder()
            files = imaging.save_products(im, self.folder, self.prefix, comps,
                                          cfg.getb("recording", "fill_missing", True), rotate,
                                          cfg.getb("recording", "ir_inverted", True),
                                          cfg.getb("recording", "geometric_correction", True))
            st = [s.__dict__ for s in self.rx.chain_stats()]
            # the TLE is recorded with the pass: replaying the .cadu months later must geolocate with the
            # elements the pass was actually received against, not with whatever is current then
            # sat is the predicted satellite, or the one identified for a hand-started recording: the
            # elements have to be here, because this file is what lets a replay geolocate the pass
            info = {"session": self.seq, "satellite": sat.name if sat else None,
                    "tle": list(sat.tle) if sat is not None and sat.tle else None,
                    "identified": self.sat is None and sat is not None,
                    "northbound": north,          # also at top level: a manual session has no "pass"
                    "source": self.source.name,
                    "start_utc": self.start_utc.isoformat(), "end_utc": utcnow().isoformat(),
                    "pass": ({"aos": self.pas.aos.isoformat(), "los": self.pas.los.isoformat(),
                              "max_elevation": round(self.pas.max_el, 1), "northbound": self.pas.northbound}
                             if self.pas else None),
                    "frequency_hz": self.rx.active_freq, "chains": st,
                    "channels": [c + 1 for c in im.channels_present], "files": [os.path.basename(f) for f in files]}
            with outputs.open_new(os.path.join(self.folder, self.prefix + "_info.json"), "x") as f:
                json.dump(info, f, indent=2, default=str)
            # The same elements again, as a plain three-line TLE beside the .cadu. _info.json is ours;
            # this one is the universal format - SatDump, Gpredict and any tracking program read it,
            # a human can read it, and it survives someone copying away just the recording and this
            # file. Replaying the pass in a year then still places the coastlines where they were,
            # instead of against whatever the orbit has become by then.
            if sat is not None and sat.tle:
                try:
                    with outputs.open_new(os.path.join(self.folder, self.prefix + ".tle"), "x") as f:
                        f.write(f"{(sat.name or 'UNKNOWN')[:24]}\n{sat.tle[0]}\n{sat.tle[1]}\n")
                except OSError as e:
                    self.e.log(f"Could not write the pass TLE: {e}")
        return files


class Engine:
    def __init__(self, cfg, log_cb=None):
        self.cfg = cfg
        self._log_cb = log_cb
        if cfg.getb("recording", "purge_empty", True):       # passes that decoded nothing at all
            for d in outputs.clean_empty_sessions(cfg.output_dir):
                self.log(f"Removed empty session folder: {os.path.basename(d)}")
        _days = cfg.geti("recording", "purge_days", 0)           # 0 = keep everything (default)
        if _days > 0:
            for d in outputs.purge_old_sessions(cfg.output_dir, _days):
                self.log(f"Purged session older than {_days} days: {os.path.basename(d)}")
        self.audio = audio.AudioMonitor(log=self.log)
        self.q: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.snap = Snapshot(passes=[])
        self.sats = [orbit.Satellite(s.name, s.norad, s.freqs_mhz, s.mode) for s in cfg.satellites]
        self.observer = orbit.Observer(cfg.getf("station", "latitude", 35.88), cfg.getf("station", "longitude", 14.45),
                                       cfg.getf("station", "altitude_m", 50))
        self.passes: list[orbit.Pass] = []
        self.auto = cfg.getb("gui", "auto_start", True)
        self.session: Session | None = None
        self._quit = False
        self._t_passes = 0.0
        self._t_pub = 0.0
        self._t_tle = 0.0
        self._tle_ok = False
        self.usb_driver_alert = False   # read and cleared by the GUI
        self.last_imager = None
        self.last_pass = None           # what the GUI needs to geolocate the picture on screen
        self.thread = threading.Thread(target=self._run, name="MeteorRX-engine", daemon=True)

    # -- public (GUI thread) ----------------------------------------------------
    def start(self):
        for line in getattr(self.cfg, "migrated", ()):
            self.log(f"Settings carried forward from an older meteorrx.ini - {line}")
        self.thread.start()

    def command(self, name: str, **kw):
        self.q.put((name, kw))

    def snapshot(self) -> Snapshot:
        with self.lock:
            return self.snap

    def log(self, msg: str):
        line = f"{datetime.now().strftime('%H:%M:%S')}  {msg}"
        if self._log_cb:
            self._log_cb(line)
        self._log_file(line)

    def _log_file(self, line: str):
        """data\\meteorrx.log - kept for diagnosis (rotated at 1 MB)."""
        try:
            d = self.cfg.data_dir
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, "meteorrx.log")
            if os.path.isfile(p) and os.path.getsize(p) > 1_000_000:   # archived, never overwritten
                os.replace(p, outputs.unique_path(os.path.join(d, datetime.now().strftime("meteorrx_%Y%m%d_%H%M%S.log"))))
            with open(p, "a", encoding="utf-8") as f:
                f.write(datetime.now().strftime("%Y-%m-%d ") + line + "\n")
        except OSError:
            pass

    def quit(self):
        self._quit = True
        self.command("noop")
        self.thread.join(timeout=5)
        self.audio.close()

    # -- engine thread -----------------------------------------------------------
    def _run(self):
        self._update_tles()
        self._refresh_passes(force=True)
        while not self._quit:
            try:
                self._handle_commands()
                if self.session is not None:
                    ok = self.session.step()
                    now = time.monotonic()
                    if now - self._t_pub > 0.1 or not ok:
                        self._t_pub = now
                        self._publish()
                    if not ok:
                        self._end_session()
                else:
                    self._retry_tles()
                    self._refresh_passes()
                    self._maybe_arm()
                    self._publish()
                    time.sleep(0.25)
            except sdr.SdrError as e:
                self.log(f"SDR: {e}")
                self.usb_driver_alert = self.usb_driver_alert or e.usb_driver
                self._end_session(save=True)
            except Exception as e:  # noqa: BLE001
                self.log(f"Error: {e}")
                self.log(traceback.format_exc().splitlines()[-2].strip())
                self._end_session(save=True)
                time.sleep(1)
        if self.session:
            self._end_session()

    def _handle_commands(self):
        while True:
            try:
                name, kw = self.q.get_nowait()
            except queue.Empty:
                return
            if name == "start":
                if self.session:
                    self._end_session()
                sat = next((s for s in self.sats if s.name == kw.get("sat")), None)
                mode = kw.get("mode") or (sat.mode if sat else self.cfg.get("decoder", "mode", "auto"))
                pas = None
                if sat:
                    now = utcnow()
                    pas = next((p for p in self.passes if p.sat is sat and p.aos - timedelta(minutes=5) <= now <= p.los),
                               None)
                self._open_session(sat, pas, mode, manual=True)
            elif name == "stop":
                if self.session:
                    self.log("Stopped by user")
                    self._end_session()
            elif name == "auto":
                self.auto = bool(kw.get("enabled"))
                self.log("Automatic scheduling " + ("ON" if self.auto else "OFF"))
            elif name == "reload_tle":
                self._update_tles(force=True)
                self._refresh_passes(force=True)
            elif name == "import_tle":
                try:
                    if orbit.import_tle_file(kw["path"], self.sats, self._tle_cache, log=self.log):
                        self._tle_ok = all(x.sat is not None for x in self.sats)
                        self._refresh_passes(force=True)
                except OSError as e:
                    self.log(f"TLE import failed: {e}")
            elif name == "station":
                self.observer = orbit.Observer(self.cfg.getf("station", "latitude"),
                                               self.cfg.getf("station", "longitude"),
                                               self.cfg.getf("station", "altitude_m"))
                self._refresh_passes(force=True)

    @property
    def _tle_cache(self) -> str:
        return os.path.join(self.cfg.data_dir, "tle.txt")

    def _update_tles(self, force: bool = False):
        urls = [u.strip() for u in (self.cfg.get("tle", "urls", "") or "").split(",") if u.strip()] or None
        try:
            ok = orbit.update_tles(self.sats, self._tle_cache, self.cfg.getf("tle", "max_age_hours", 12),
                                   log=self.log, urls=urls, proxy=(self.cfg.get("tle", "proxy", "") or "").strip(),
                                   force=force,
                                   refresh_days=self.cfg.getf("tle", "refresh_days", 0.0),
                                   retry_h=self.cfg.getf("tle", "retry_hours", 1.0))
            ages = [orbit.tle_age_days(x.tle[0]) for x in self.sats if x.tle]
            self._tle_ok = ok and bool(ages) and max(ages) < self.cfg.getf("tle", "max_age_hours", 12) / 24 + 2
        except Exception as e:  # noqa: BLE001
            self.log(f"TLE update error: {e}")
            self._tle_ok = False
        self._t_tle = time.monotonic()

    def _retry_tles(self):
        """Retry a failed TLE update every 15 min while idle (not within 5 min of a pass)."""
        # update_tles enforces its own floor between downloads, but there is no point calling it
        # every quarter of an hour either: a station that is missing a satellite would otherwise
        # ask about it ninety-six times a day for as long as it is left running.
        if self._tle_ok or time.monotonic() - self._t_tle < max(900.0, 3600.0 * self.cfg.getf(
                "tle", "retry_hours", 1.0)):
            return
        if self.passes and (self.passes[0].aos - utcnow()).total_seconds() < 300:
            return
        self._update_tles()
        self._refresh_passes(force=True)

    def _refresh_passes(self, force: bool = False):
        if not force and time.monotonic() - self._t_passes < 600:
            return
        self._t_passes = time.monotonic()
        try:
            now = utcnow()
            ps = orbit.predict_passes(self.observer, self.sats, now - timedelta(minutes=20), 36,
                                      self.cfg.getf("station", "min_elevation", 15))
            self.passes = [p for p in ps if p.los > now]
        except Exception as e:  # noqa: BLE001
            self.log(f"Pass prediction error: {e}")

    def _maybe_arm(self):
        if not self.auto or not self.passes:
            return
        now = utcnow()
        pre = timedelta(seconds=self.cfg.getf("recording", "pre_aos_s", 60))
        p = self.passes[0]
        if p.aos - pre <= now < p.los - timedelta(seconds=60):
            self.log(f"Pass {p.sat.name}: AOS {p.aos.astimezone().strftime('%H:%M')} max {p.max_el:.0f} deg - armed")
            self._open_session(p.sat, p, p.sat.mode, manual=False)
            self.passes.pop(0)

    def _open_session(self, sat, pas, mode, manual: bool):
        try:
            self.session = Session(self, sat, pas, mode, manual)
            src = self.session.source
            extra = f", {src.gain_desc}" if getattr(src, "gain_desc", "") else ""
            extra += f", {src.mode} streaming" if getattr(src, "mode", "") else ""
            self.log(f"Receiver started: {src.name}, {src.sample_rate / 1e6:.3f} MS/s{extra}, mode {mode}")
            for n in getattr(src, "notes", []) or []:
                self.log("  " + n)
        except sdr.SdrError as e:
            self.log(f"SDR: {e}")
            self.usb_driver_alert = self.usb_driver_alert or e.usb_driver
            self.session = None

    def _end_session(self, save: bool = True):
        s = self.session
        self.session = None
        if s is None:
            return
        with self.lock:
            self.snap.state = "saving"
        try:
            files = s.finish() if save else []
            if files:
                self.log(f"Saved {len(files)} image(s) in {s.folder}")
            elif s.rx.imager.packets == 0:
                self.log("No imagery decoded")
        except Exception as e:  # noqa: BLE001
            self.log(f"Save error: {e}")
        with self.lock:
            self.snap.out_folder = s.folder or self.snap.out_folder
        self.last_imager = s.rx.imager
        self._note_pass(s)
        self._refresh_passes(force=True)

    def _note_pass(self, s):
        """Remember what the picture on screen is, so the GUI can place map overlays on it."""
        # A session that has just been armed has no folder and no imagery yet, while the viewer still
        # shows the pass before it. Clobbering this would hand the overlay an empty track and send an
        # enhanced copy to a file dialog instead of beside the pass it came from.
        if s.folder is None and self.last_pass is not None and s.rx.imager.packets == 0:
            return
        sat, north = s.sat, (s.pas.northbound if s.pas is not None else None)
        if sat is None:                      # nobody told us which satellite this is
            # Resolved once and kept: this runs on every tick, and a satellite that changed halfway
            # through would re-project the top and the bottom of one picture differently. Until one is
            # found the search repeats, because an operator often starts a minute before the bird rises.
            if getattr(s, "_manual_sat", None) is None:
                found = self._identify(s)
                # A live recording retries until the bird rises; a replay asks its own files once and
                # the answer cannot change, so a miss is remembered too and never logged twice.
                if found[0] is not None or s.source.is_file:
                    s._manual_sat = found
                sat, north = found
            else:
                sat, north = s._manual_sat
        self.last_pass = {
            "satellite": sat.name if sat else None,
            "tle": list(sat.tle) if sat is not None and sat.tle else None,
            "folder": s.folder, "prefix": s.prefix, "seq": s.seq,
            "rotated": bool(self.cfg.getb("recording", "rotate_northbound", True) and north),
        }

    def _identify(self, s):
        """Which satellite is this, when no predicted pass told us?

        Returns (satellite, northbound) or (None, None). Two quite different situations end up here.

        Replaying a file: the recording was made at some other time, so nothing about the sky now says
        anything about it. The elements that belong to it are the ones saved beside the recording when
        it was made, which is why they are written into `_info.json`. If they are not there, nothing is
        claimed - an overlay drawn from today's sky onto last week's recording would be confidently
        wrong, which is worse than no overlay at all.

        Recording live by hand: the sky now is exactly the evidence we have. Only satellites actually
        above the horizon are candidates and the tuned frequency is preferred, so the answer is either
        right or absent. Identification waits for the first decoded frame, because a frame proves a
        Meteor really is transmitting rather than merely being geometrically up.
        """
        if s.source.is_file:
            return self._sat_from_recording(s)
        if s.rx.imager.packets == 0:
            return None, None
        when = datetime.now(timezone.utc)
        # what the receiver is really listening to, which is not the centre of the tuned span
        tuned = getattr(s.rx, "active_freq", 0.0) or getattr(s.source, "center_hz", 0.0) or 0.0
        best = None
        for sat in self.sats:
            if sat.sat is None:
                continue
            try:
                el = self.observer.look(sat, when)[1]
                lat0 = self.observer.look(sat, when - timedelta(seconds=30))[4]
                lat1 = self.observer.look(sat, when)[4]
            except (ValueError, AttributeError, TypeError):
                continue
            if el <= 0.0:
                continue
            # 200 kHz is wider than any Doppler shift and far narrower than the 800 kHz between the
            # two LRPT downlinks, so this separates satellites that share neither frequency
            on_freq = any(abs(f * 1e6 - tuned) < 2e5 for f in (sat.freqs_mhz or [])) if tuned else False
            rank = (on_freq, el)
            if best is None or rank > best[0]:
                best = (rank, sat, lat1 > lat0)
        if best is None:
            return None, None
        (on_freq, el), sat, north = best
        self.log(f"Manual recording: {sat.name} is overhead at {el:.0f} deg"
                 f"{'' if on_freq else ' (frequency does not match its downlinks)'}"
                 " - using its elements to place the overlays")
        return sat, north

    def _sat_from_recording(self, s):
        """The elements saved beside a recording, when one of its own files is being replayed."""
        path = getattr(s.source, "path", None)
        if not path:
            return None, None
        folder = os.path.dirname(os.path.abspath(path))
        stem = os.path.basename(path)
        for suffix in (".cadu", ".wav", ".cs8", ".cs16", ".cu8", ".iq", ".raw"):
            if stem.lower().endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        try:
            here = sorted(os.listdir(folder))
        except OSError:
            return None, None
        # The .tle beside the recording first: it is the plain, universal form, so it is also the one
        # an operator can drop in by hand next to a .cadu that arrived without one.
        cands = [os.path.join(folder, stem + ".tle"),
                 *(os.path.join(folder, f) for f in here if f.lower().endswith(".tle")),
                 os.path.join(folder, stem + "_info.json"),
                 *(os.path.join(folder, f) for f in here if f.endswith("_info.json"))]
        seen = set()
        for cand in cands:
            if cand in seen or not os.path.isfile(cand):
                continue
            seen.add(cand)
            got = (self._read_tle_file(cand) if cand.lower().endswith(".tle")
                   else self._read_info_json(cand))
            if got is None:
                continue
            name, tle, north = got
            sat = orbit.Satellite(name or "recorded", 0, [])
            try:
                sat.load(*tle)
            except Exception:  # noqa: BLE001  (a corrupt stored TLE must not stop the replay)
                continue
            if north is None:          # a plain .tle carries no direction: take it from our own file
                for other in cands:
                    if other.endswith("_info.json") and os.path.isfile(other):
                        got2 = self._read_info_json(other)
                        if got2 and got2[2] is not None:
                            north = got2[2]
                            break
            self.log(f"Replay: using the elements recorded with {name or 'this pass'} "
                     f"({os.path.basename(cand)}) to place the overlays")
            return sat, bool(north)
        self.log("Replay: no elements recorded with this file - coastlines and cities cannot be placed. "
                 "Put the pass's .tle file beside it to place them.")
        return None, None

    @staticmethod
    def _read_tle_file(path):
        """Name + the two element lines, from a two- or three-line TLE. Returns None if it is not one."""
        try:
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                lines = [ln.rstrip() for ln in f if ln.strip()]
        except OSError:
            return None
        for i, ln in enumerate(lines):
            if ln.startswith("1 ") and i + 1 < len(lines) and lines[i + 1].startswith("2 "):
                name = lines[i - 1].strip() if i else ""
                return (name[2:].strip() if name.startswith("0 ") else name), (ln, lines[i + 1]), None
        return None

    @staticmethod
    def _read_info_json(path):
        try:
            with open(path, encoding="utf-8") as f:
                info = json.load(f)
        except (OSError, ValueError):
            return None
        tle = info.get("tle")
        if not tle or len(tle) != 2:
            return None
        north = info.get("northbound")
        if north is None:
            north = (info.get("pass") or {}).get("northbound")
        return info.get("satellite"), tuple(tle), north

    def _publish(self):
        s = self.session
        snap = Snapshot(passes=list(self.passes[:12]))
        snap.tle_info = ", ".join(
            f"{x.name}: {orbit.tle_age_days(x.tle[0]):.1f} d" if x.tle else f"{x.name}: -" for x in self.sats)
        with self.lock:
            old = self.snap
        snap.out_folder = old.out_folder
        snap.preview, snap.preview_rev = old.preview, old.preview_rev
        if s is None:
            snap.state = "waiting" if self.auto and self.passes else "idle"
        else:
            rx = s.rx
            snap.state = "file" if s.source.is_file else ("recording" if s.recording else "armed")
            snap.source = s.source.name
            snap.sat = s.sat.name if s.sat else "?"
            snap.mode = s.mode
            snap.pass_info = s.pas
            if s.sat and s.sat.sat is not None:
                try:
                    az, el, _, _, _ = self.observer.look(s.sat, utcnow())
                    snap.azimuth, snap.elevation = az, el
                except Exception:
                    pass
            snap.snr_db = rx.snr_db
            if rx.reports:
                rep = rx.reports[rx.active_idx]
                snap.level_dbfs, snap.n0_dbfs_hz = rep.band_dbfs, rep.n0_dbfs_hz
            snap.cal_k = s.cal_k
            h = s.health
            if h:
                snap.stream_ratio, snap.stream_dropped = h.get("ratio"), h.get("dropped", 0)
            snap.freq_hz = rx.active_freq
            snap.doppler_hz = rx.doppler_hz
            snap.loop_hz = rx.total_offset_hz()
            snap.signal = rx.signal_present
            stats = rx.chain_stats()
            lc = rx.locked_chain
            best = lc.stats if lc else min(stats, key=lambda x: x.vit_ber)
            snap.vit_locked = any(x.vit_locked for x in stats)
            snap.vit_ber = best.vit_ber
            snap.frame_lock = any(x.deframer_locked for x in stats)
            snap.frames_ok = sum(x.frames_ok for x in stats)
            snap.frames_bad = sum(x.frames_bad for x in stats)
            snap.packets = rx.imager.packets
            snap.lrpt_mode = lc.mode if lc else ("/".join(x.mode for x in stats))
            snap.eye = rx.eye_traces()
            snap.syms = rx.last_syms.copy()
            spec = rx.monitor.spectrum_db()
            if spec is not None:
                snap.spectrum = (rx.monitor.freqs + s.source.center_hz, spec,
                                 [r.freq_hz + rx.doppler_ratio * r.freq_hz for r in rx.reports])
            snap.recording = s.recording
            snap.rec_seconds = time.monotonic() - s.rec_start if s.rec_start else 0.0
            snap.out_folder = s.folder or snap.out_folder
            self.last_imager = rx.imager
            self._note_pass(s)
            if hasattr(s.source, "progress"):
                snap.file_progress = s.source.progress
            now = time.monotonic()
            if rx.imager.dirty and now - s._t_prev > 2.0:
                s._t_prev = now
                rx.imager.dirty = False
                pv = imaging.preview(rx.imager)
                if pv is not None:
                    snap.preview = pv
                    snap.preview_rev = old.preview_rev + 1
        with self.lock:
            self.snap = snap

"""Command-line decoding (MeteorRX-cli.exe): IQ recordings or .cadu files -> images."""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np

from . import __version__, config, imaging, lrpt, outputs, sdr
from .receiver import Receiver, ReceiverConfig, dsp_from_config, warm_up


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="MeteorRX-cli", description=f"MeteorRX {__version__} - offline LRPT decoding")
    ap.add_argument("input", help="IQ recording (.wav .cu8 .cs8 .cs16 .cf32) or .cadu file "
                                  "(or --selftest, --drivers, --measure MHz [s], --live s [--device D] "
                                  "[--record file.cs16])")
    ap.add_argument("--rate", type=float, default=None, help="sample rate (Hz) for raw IQ files")
    ap.add_argument("--center", type=float, default=None, help="centre frequency of the recording (MHz)")
    ap.add_argument("--freq", default="137.9,137.1", help="candidate downlink frequencies (MHz)")
    ap.add_argument("--mode", default="auto", choices=["auto", "72k", "80k"])
    ap.add_argument("--out", default=None, help="output folder (default: next to the input)")
    ap.add_argument("--northbound", action="store_true", help="rotate images 180 deg")
    ap.add_argument("--ini", default=None, help="configuration file")
    if argv is None:
        argv = sys.argv[1:]
    if "--selftest" in argv:
        from . import selftest
        return selftest.main()
    if "--measure" in argv:
        # MeteorRX-cli --measure 137.9 [seconds] : CW carrier level / frequency / noise with current settings
        from . import calib
        i = argv.index("--measure")
        f_mhz = float(argv[i + 1]) if len(argv) > i + 1 else 137.9
        secs = float(argv[i + 2]) if len(argv) > i + 2 else 2.0
        cfg = config.load()
        src = sdr.open_source(cfg)
        try:
            src.start()
            x = calib.capture(src, secs)
        finally:
            src.stop()
        m = calib.measure_tone(x, src.sample_rate, src.center_hz, f_mhz * 1e6)
        cal = calib.CalStore(os.path.join(cfg.data_dir, "calibration.json")).get(calib.settings_key(cfg))
        k = cal.k_db if cal is not None else None
        print(f"settings      : {calib.settings_key(cfg)}")
        print(f"tone          : {m.power_dbfs:.2f} dBFS" + (f" = {m.power_dbfs + k:.2f} dBm ({calib.s_units(m.power_dbfs + k)})"
                                                            if k is not None else "  (no calibration)"))
        print(f"frequency     : {m.freq_measured_hz / 1e6:.6f} MHz  error {m.freq_error_hz:+.0f} Hz ({m.ppm:+.2f} ppm)")
        print(f"noise density : {m.noise_dbfs_hz:.2f} dBFS/Hz" + (f" = {m.noise_dbfs_hz + k:.2f} dBm/Hz"
                                                                  if k is not None else ""))
        print(f"C/N0          : {m.cn0_dbhz:.1f} dBHz   ADC peak {m.peak_abs * 100:.0f}%")
        if m.warning:
            print("WARNING       : " + m.warning)
        return 0
    if "--correct" in argv:
        i = argv.index("--correct")
        return correct_images(argv[i + 1] if len(argv) > i + 1 else ".")
    if "--live" in argv:
        return live(argv)
    if "--rtlprobe" in argv:
        return rtl_probe()
    if "--drivers" in argv:
        from . import drivers
        rep = drivers.select_library((config.load().get("sdr", "rtl_dll_dir", "") or "").strip())
        print("\n".join(rep.lines))
        return 0 if rep.chosen else 1
    a = ap.parse_args(argv)

    cfg = config.load(a.ini)
    base = os.path.splitext(os.path.basename(a.input))[0]
    out = a.out or outputs.unique_path(os.path.join(os.path.dirname(os.path.abspath(a.input)), base + "_products"))
    comps = [c.strip() for c in (cfg.get("recording", "composites", "221") or "").split(",") if c.strip()]
    t0 = time.time()
    if a.input.lower().endswith(".cadu"):
        im = lrpt.MsuMrImager()
        n = lrpt.process_cadu_file(a.input, im)
        print(f"{n} CADUs, {im.packets} MSU-MR packets")
    else:
        center = (a.center if a.center is not None else cfg.getf("sdr", "center_mhz", 137.5)) * 1e6
        src = sdr.FileSource(a.input, a.rate or cfg.getf("sdr", "file_sample_rate", 1e6), center, realtime=False)
        rc = ReceiverConfig(sample_rate=src.sample_rate, center_hz=center,
                            freqs_hz=[float(f) * 1e6 for f in a.freq.split(",")], mode=a.mode,
                            trigger_snr_db=cfg.getf("decoder", "trigger_snr_db", 3.0),
                            dsp=dsp_from_config(cfg))
        os.makedirs(out, exist_ok=True)
        cadu_f = outputs.open_new(os.path.join(out, base + ".cadu"))
        rx = Receiver(rc, cadu_sink=cadu_f.write)
        for c in rx.chains:
            c.time_scale = 0.25
        last = 0.0
        while True:
            blk = src.read(int(src.sample_rate * 0.1))
            if blk is None:
                break
            rx.process(blk)
            if time.time() - last > 1.0:
                last = time.time()
                st = rx.chain_stats()
                ok = sum(s.frames_ok for s in st)
                ber = min(s.vit_ber for s in st)
                print(f"\r{src.progress * 100:5.1f}%  SNR {rx.snr_db:5.1f} dB  BER {ber:.3f}  frames {ok}  "
                      f"packets {rx.imager.packets}", end="", flush=True)
        cadu_f.close()
        print()
        im = rx.imager
        for s in rx.chain_stats():
            print(f"  {s.mode}: frames OK {s.frames_ok}, bad {s.frames_bad}")
    if im.packets == 0:
        print("No imagery decoded.")
        return 2
    files = imaging.save_products(im, out, base, comps, cfg.getb("recording", "fill_missing", True),
                                  a.northbound, cfg.getb("recording", "ir_inverted", True))
    print(f"Channels {[c + 1 for c in im.channels_present]} -> {len(files)} file(s) in {out} "
          f"({time.time() - t0:.1f} s)")
    return 0



def live(argv) -> int:
    """MeteorRX-cli --live 30 [--device rtlsdr|sdrplay|hackrf|lime|pluto] [--mode 72k] [--record x.cs16]

    Receives from the configured SDR for N seconds and prints, every second, the stream health (delivered /
    nominal sample rate, samples dropped), the carrier (SNR, AFC offset) and the decoder (BER, frames).
    --record keeps the raw wideband IQ (16-bit) for analysis."""
    def opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv and len(argv) > argv.index(name) + 1 else default
    secs = float(opt("--live", "30"))
    cfg = config.load(opt("--ini"))
    if opt("--device"):
        cfg.set("sdr", "device", opt("--device"))
    for i, a in enumerate(argv):                 # --set sdr.hackrf_lna_gain=32 (repeatable, not saved)
        if a == "--set" and i + 1 < len(argv) and "=" in argv[i + 1] and "." in argv[i + 1].split("=")[0]:
            k, v = argv[i + 1].split("=", 1)
            sec, key = k.split(".", 1)
            cfg.set(sec, key, v)
    mode = opt("--mode", cfg.get("decoder", "mode", "auto") or "auto")
    rec = opt("--record")
    try:
        src = sdr.open_source(cfg)
    except sdr.SdrError as e:
        print(f"ERROR: {e}")
        return 1
    print(f"{src.name}  {src.sample_rate / 1e6:.3f} MS/s  centre {src.center_hz / 1e6:.3f} MHz  mode {mode}"
          + (f"  {src.gain_desc}" if getattr(src, "gain_desc", "") else ""))
    for n in getattr(src, "notes", []) or []:
        print("  " + n)
    rc = ReceiverConfig(sample_rate=src.sample_rate, center_hz=src.center_hz, freqs_hz=[137.9e6, 137.1e6],
                        mode=mode, trigger_snr_db=cfg.getf("decoder", "trigger_snr_db", 3.0),
                        ppm=cfg.getf("sdr", "ppm", 0.0), dsp=dsp_from_config(cfg))
    rx = Receiver(rc)
    warm_up(rc)
    if rec:
        rec = outputs.unique_path(rec)
    f = open(rec, "xb") if rec else None
    t_end = time.monotonic() + secs
    last = time.monotonic()
    worst = 1.0
    try:
        src.start()
        if getattr(src, "mode", ""):
            print(f"  streaming: {src.mode}")
        while time.monotonic() < t_end:
            try:
                x = src.read(int(src.sample_rate * 0.1))
            except sdr.SdrError as e:
                print(f"ERROR: {e}")
                break
            if x is None:
                break
            if f is not None and len(x):
                f.write((np.clip(x.view(np.float32), -1, 1) * 32767).astype("<i2").tobytes())
            rx.process(x)
            if time.monotonic() - last >= 1.0:
                last = time.monotonic()
                h = src.health() or {}
                r = h.get("ratio")
                if r is not None:
                    worst = min(worst, r)
                st = max(rx.chain_stats(), key=lambda s: (s.frames_ok, -s.vit_ber))
                print(f"  stream {'  -  ' if r is None else f'{100 * r:5.1f}'}%  dropped {h.get('dropped', 0):7d}  "
                      f"SNR {rx.snr_db:5.1f} dB  offset {rx.total_offset_hz():+7.0f} Hz  "
                      f"carrier {'yes' if rx.carrier_detected else 'no '}  {st.mode} BER {st.vit_ber:.3f}  "
                      f"frames {st.frames_ok}/{st.frames_bad}  packets {st.packets}", flush=True)
    finally:
        src.stop()
        if f is not None:
            f.close()
    h = src.health() or {}
    ok = sum(s.frames_ok for s in rx.chain_stats())
    print(f"result: {ok} good frames, stream min {100 * worst:.1f} % of nominal, {h.get('dropped', 0)} samples dropped"
          + (f", IQ saved to {rec} (cs16, {src.sample_rate:.0f} S/s, centre {src.center_hz / 1e6:.3f} MHz)" if rec else ""))
    return 0 if ok else 2


def correct_images(path: str) -> int:
    """MeteorRX-cli --correct <folder or .png> : panoramic correction of already decoded images.

    Writes a `..._corr.png` beside each picture (1 km/pixel ground spacing, meteorrx/geometry.py) and never
    touches the original. Pictures already corrected, and those not 1568 pixels wide, are skipped."""
    import glob
    from . import geometry, imaging
    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, "**", "*.png"), recursive=True))
    else:
        files = [path]
    files = [f for f in files if not f.lower().endswith("_corr.png")]
    if not files:
        print(f"no image to correct in {path}")
        return 2
    done = 0
    for f in files:
        try:
            img = imaging.read_png(f)
        except (OSError, ValueError) as e:
            print(f"  {os.path.basename(f)}: unreadable ({e})")
            continue
        if img.shape[1] != lrpt.IMG_WIDTH:
            print(f"  {os.path.basename(f)}: {img.shape[1]} px wide, not an MSU-MR line - skipped")
            continue
        out = f[:-4] + "_corr.png"
        imaging.write_png(outputs.unique_path(out), geometry.correct(img))
        print(f"  {os.path.basename(f)}: {img.shape[1]} -> {geometry.source_columns(img.shape[1]).size} px"
              f" -> {os.path.basename(out)}")
        done += 1
    print(f"{done} image(s) corrected")
    return 0 if done else 2


def rtl_probe() -> int:
    """MeteorRX-cli --rtlprobe : RTL-SDR tuning / gain / transfer variants, ADC level and 137.9 MHz carrier."""
    import ctypes as C
    cfg = config.load()
    L, path, _ = sdr.load_rtlsdr((cfg.get("sdr", "rtl_dll_dir", "") or "").strip())
    sdr._rtl_prototypes(L)
    L.rtlsdr_open.argtypes = [C.POINTER(C.c_void_p), C.c_uint32]
    for fn, at in (("rtlsdr_set_center_freq", [C.c_void_p, C.c_uint32]), ("rtlsdr_set_sample_rate", [C.c_void_p, C.c_uint32]),
                   ("rtlsdr_set_tuner_gain_mode", [C.c_void_p, C.c_int]), ("rtlsdr_set_tuner_gain", [C.c_void_p, C.c_int]),
                   ("rtlsdr_set_agc_mode", [C.c_void_p, C.c_int]), ("rtlsdr_reset_buffer", [C.c_void_p]),
                   ("rtlsdr_close", [C.c_void_p]), ("rtlsdr_get_tuner_gain", [C.c_void_p]),
                   ("rtlsdr_get_center_freq", [C.c_void_p]), ("rtlsdr_get_sample_rate", [C.c_void_p]),
                   ("rtlsdr_get_tuner_gains", [C.c_void_p, C.POINTER(C.c_int)]),
                   ("rtlsdr_read_sync", [C.c_void_p, C.c_void_p, C.c_int, C.POINTER(C.c_int)])):
        getattr(L, fn).argtypes = at
    L.rtlsdr_get_center_freq.restype = C.c_uint32
    L.rtlsdr_get_sample_rate.restype = C.c_uint32
    print(f"library {path}, {L.rtlsdr_get_device_count()} device(s)")
    fs, fc = 1200000, int(cfg.getf("sdr", "center_mhz", 137.5) * 1e6)
    want = int(round(float(cfg.get("sdr", "rtl_gain_db", "38.6") or 38.6) * 10))

    def measure(dev, label):
        buf = (C.c_ubyte * (1 << 18))()
        got = C.c_int(0)
        L.rtlsdr_reset_buffer(dev)
        L.rtlsdr_read_sync(dev, buf, 1 << 18, C.byref(got))      # discard (settling)
        a = []
        for _ in range(5):
            L.rtlsdr_read_sync(dev, buf, 1 << 18, C.byref(got))
            a.append(np.frombuffer(bytes(buf)[:got.value], np.uint8).copy())
        raw = np.concatenate(a)
        x = (raw.astype(np.float32) - 127.4).view(np.complex64) / 128
        n = 8192
        seg = x[: len(x) // n * n].reshape(-1, n) * np.hanning(n)
        p = np.mean(np.abs(np.fft.fftshift(np.fft.fft(seg, axis=1), axes=1)) ** 2, 0)
        f = np.fft.fftshift(np.fft.fftfreq(n, 1 / fs)) + fc
        med = np.median(p)
        band = lambda f0: 10 * np.log10(np.mean(p[np.abs(f - f0) < 50e3]) / med)  # noqa: E731
        print(f"  {label:<38} ADC {raw.min():3d}..{raw.max():3d} rms {np.std(raw.astype(float)):5.2f} LSB  "
              f"137.1 {band(137.1e6):+5.1f} dB  137.9 {band(137.9e6):+5.1f} dB  gain {L.rtlsdr_get_tuner_gain(dev) / 10:.1f}"
              f"  f {L.rtlsdr_get_center_freq(dev)}  fs {L.rtlsdr_get_sample_rate(dev)}", flush=True)

    for order in ("rate-then-freq (v1.5)", "freq-rate-freq (v1.6)"):
        dev = C.c_void_p()
        r = L.rtlsdr_open(C.byref(dev), 0)
        if r != 0:
            print(f"open failed {r}")
            return 1
        if order.startswith("rate"):
            L.rtlsdr_set_sample_rate(dev, fs)
            L.rtlsdr_set_center_freq(dev, fc)
        else:
            L.rtlsdr_set_center_freq(dev, fc)
            L.rtlsdr_set_sample_rate(dev, fs)
            L.rtlsdr_set_center_freq(dev, fc)
        L.rtlsdr_set_agc_mode(dev, 0)
        L.rtlsdr_set_tuner_gain_mode(dev, 1)
        gains = (C.c_int * 64)()
        ng = L.rtlsdr_get_tuner_gains(dev, gains)
        g = min(list(gains)[:ng], key=lambda v: abs(v - want)) if ng > 0 else want
        L.rtlsdr_set_tuner_gain(dev, g)
        time.sleep(0.2)
        measure(dev, order + f", gain {g / 10:.1f}")
        for gg in (0, 197, 496):
            L.rtlsdr_set_tuner_gain(dev, min(list(gains)[:ng], key=lambda v: abs(v - gg)) if ng > 0 else gg)
            time.sleep(0.2)
            measure(dev, f"   same, gain {gg / 10:.1f}")
        L.rtlsdr_set_tuner_gain_mode(dev, 0)
        time.sleep(0.2)
        measure(dev, "   same, tuner AGC (auto)")
        L.rtlsdr_close(dev)
        time.sleep(0.5)
    print("streaming source (async) as used by the receiver:")
    src = sdr.RtlSdrSource(0, fs, fc, cfg.get("sdr", "rtl_gain_db", "38.6"), False,
                           (cfg.get("sdr", "rtl_dll_dir", "") or "").strip())
    src.start()
    x = np.concatenate([src.read(120000) for _ in range(12)])
    src.stop()
    raw = np.rint(x.view(np.float32) * 128 + 127.4)
    print(f"  async: {len(x)} samples, ADC {raw.min():.0f}..{raw.max():.0f} rms {np.std(raw):5.2f} LSB, mode {src.mode}")
    return 0


if __name__ == "__main__":   # must stay last: main() dispatches to functions defined below it
    sys.exit(main())

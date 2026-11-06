"""python -m meteorrx.selftest : checks the installation (DSP kernels, FEC, paths, drivers)."""
from __future__ import annotations

import os
import sys
import time

import numpy as np


def main() -> int:
    from . import __version__, config, core, dsp, lrpt
    ok = True
    print(f"MeteorRX {__version__} - Python {sys.version.split()[0]} - {sys.executable}")
    print(f"  install folder : {config.app_dir()}")
    print(f"  DSP cache      : {os.environ.get('NUMBA_CACHE_DIR')}")
    t = dsp.warmup()
    print(f"  DSP kernels    : ready in {t:.1f} s")
    # Reed-Solomon
    rng = np.random.default_rng(1)
    blk = np.zeros(1020, np.uint8)
    for k in range(4):
        d = rng.integers(0, 256, 223, dtype=np.uint8)
        blk[k:892:4] = d
        blk[892 + k::4] = core.rs_encode(d)
    ref = blk.copy()
    blk[rng.choice(1020, 40, replace=False)] ^= 0x5A
    good, errs = core.rs_decode_interleaved(blk, 4)
    rs_ok = good and (blk == ref).all()
    print(f"  Reed-Solomon   : {'OK' if rs_ok else 'FAILED'} ({sum(errs)} errors corrected)")
    # Viterbi
    bits = rng.integers(0, 2, 40000).astype(np.uint8)
    enc, _ = core.conv_encode(bits)
    soft = np.clip((2.0 * enc - 1) * 50 + rng.normal(0, 30, len(enc)), -127, 127).astype(np.int8)
    out = core.Viterbi().process(soft)
    v_ok = len(out) > 30000 and np.count_nonzero(out != bits[:len(out)]) == 0
    print(f"  Viterbi        : {'OK' if v_ok else 'FAILED'}")
    # MSU-MR codec
    img = (np.add.outer(np.arange(8) * 9, np.arange(112) * 2) % 256).astype(np.uint8)
    pix, n = lrpt.decode_segment_blocks(lrpt.encode_segment_blocks(img, 77), 77)
    j_ok = n == 14 and np.abs(pix.astype(int) - img).mean() < 4
    print(f"  MSU-MR decoder : {'OK' if j_ok else 'FAILED'}")
    # speed
    x = (rng.normal(size=600000) + 1j * rng.normal(size=600000)).astype(np.complex64)
    ch = core.Channelizer(1.2e6, 4, 95e3, 50e3)
    dm = core.Demodulator(300e3, 72e3)
    t0 = time.time()
    dm.process(ch.process(x))
    rt = 0.5 / max(time.time() - t0, 1e-6)
    print(f"  Demod speed    : {rt:.0f}x real time")
    ok = rs_ok and v_ok and j_ok and rt > 2
    # RTL-SDR drivers (informative: only needed with an RTL-SDR)
    from . import drivers
    rep = drivers.select_library((config.load().get("sdr", "rtl_dll_dir", "") or "").strip())
    print("  RTL-SDR drivers:")
    for line in rep.lines:
        print("     " + line)
    if rep.chosen is not None:
        try:
            lib = drivers.load_library(rep)
            n = lib.rtlsdr_get_device_count()
            print(f"     library loaded, {n} receiver(s) reachable")
        except OSError as e:
            print(f"     library load FAILED: {e}")
            ok = False
    else:
        print("     no usable rtlsdr library (bundled copy missing?)")
    print("SELFTEST " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

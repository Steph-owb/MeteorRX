"""Decoding sensitivity benchmark: fraction of CADUs recovered versus Es/N0.

    python tools/bench_sensitivity.py [--mode 72k] [--esn0 1,2,3,4,5,6] [--lines 40] [--ideal]

Es/N0 = energy per QPSK symbol / noise density (for LRPT, Eb/N0 of the information bits is ~0.6 dB higher
because of the RS overhead). --ideal also runs a genie decoder (perfect carrier and timing, soft symbols
straight into the Viterbi decoder): the difference is the implementation loss of the demodulator."""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from meteorrx import core, lrpt  # noqa: E402
from meteorrx.receiver import Receiver, ReceiverConfig  # noqa: E402
from tests import synth  # noqa: E402


def esn0_to_synth_snr(esn0_db: float) -> float:
    # synth: SNR measured in 1.5*Rs -> Es/N0 = SNR + 10log10(1.5)
    return esn0_db - 10 * np.log10(1.5)


def run_receiver(mode, esn0, lines, fs=1.2e6, offset=1500.0, seed=3, dsp=None, rx_mode=None, drift=0.0,
                 alpha=None):
    iq, _, ncadu = synth.make_iq(mode, lines=lines, snr_db=esn0_to_synth_snr(esn0), offset_hz=offset,
                                 fs=fs, seed=seed, **({"alpha": alpha} if alpha else {}))
    if drift:                                   # linear frequency drift (Doppler rate), Hz/s
        t = np.arange(len(iq)) / fs
        iq = (iq * np.exp(1j * np.pi * drift * t * t)).astype(np.complex64)
    rc = ReceiverConfig(sample_rate=fs, center_hz=137.5e6, freqs_hz=[137.9e6, 137.1e6], mode=rx_mode or mode,
                        dsp=dsp or {})
    rx = Receiver(rc)
    for c in rx.chains:
        c.time_scale = 0.05
    blk = int(fs * 0.1)
    for i in range(0, len(iq), blk):
        rx.process(iq[i:i + blk])
    ok = sum(s.frames_ok for s in rx.chain_stats())
    return ok, ncadu, rx


def run_ideal(esn0, lines, seed=3):
    """Genie decoder: BPSK-equivalent soft symbols with exact Es/N0, no synchronisation losses."""
    rng = np.random.default_rng(seed)
    pkts, _ = synth.build_packets(lines)
    cadus = synth.build_cadus(pkts)
    enc = synth.coded_bits(cadus)
    s = 2.0 * enc - 1.0
    # QPSK symbol = 2 coded bits, Es = 2 Ec ; per real dimension noise var = N0/2, Ec = 1
    ec_n0 = 10 ** (esn0 / 10) / 2.0
    y = s + rng.normal(0, np.sqrt(1 / (2 * ec_n0)), len(s))
    soft = np.clip(np.rint(y * 40), -127, 127).astype(np.int8)
    v = core.Viterbi()
    bits = v.process(soft)
    dec, _ = lrpt.nrzm_decode(bits, 0)
    fr = lrpt.Deframer().push(dec)
    ok = sum(1 for f in fr if lrpt.cadu_to_vcdu(f)[1])
    return ok, len(cadus)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="72k")
    ap.add_argument("--esn0", default="1,2,3,4,5,6,8")
    ap.add_argument("--lines", type=int, default=40)
    ap.add_argument("--ideal", action="store_true")
    ap.add_argument("--fs", type=float, default=1.2e6)
    ap.add_argument("--offset", type=float, default=1500.0, help="carrier offset, Hz")
    ap.add_argument("--drift", type=float, default=0.0, help="frequency drift, Hz/s")
    ap.add_argument("--rx-mode", default=None, help="receiver mode (auto/72k/80k), default = --mode")
    ap.add_argument("--alpha", type=float, default=None, help="transmitter RRC roll-off")
    ap.add_argument("--dsp", default="", help='JSON demodulator options, e.g. {"pll_bw": 0.003}')
    a = ap.parse_args()
    print(f"mode {a.mode} (rx {a.rx_mode or a.mode}), fs {a.fs / 1e6:.3f} MS/s, offset {a.offset:.0f} Hz, "
          f"drift {a.drift:.0f} Hz/s, {a.lines} image lines, dsp {a.dsp or 'default'}")
    for e in [float(x) for x in a.esn0.split(",")]:
        t0 = time.time()
        import json
        ok, n, rx = run_receiver(a.mode, e, a.lines, fs=a.fs, offset=a.offset, drift=a.drift, rx_mode=a.rx_mode,
                                 alpha=a.alpha, dsp=json.loads(a.dsp) if a.dsp else None)
        st = max(rx.chain_stats(), key=lambda s: s.frames_ok)
        line = f"Es/N0 {e:5.1f} dB : receiver {ok:4d}/{n} ({100 * ok / n:5.1f} %)  ber {st.vit_ber:.3f}"
        if a.ideal:
            oki, ni = run_ideal(e, a.lines)
            line += f"  | ideal {oki:4d}/{ni} ({100 * oki / ni:5.1f} %)"
        print(line + f"  [{time.time() - t0:.1f} s]", flush=True)


if __name__ == "__main__":
    main()

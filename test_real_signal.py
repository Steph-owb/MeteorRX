"""Real Meteor-M downlink conventions.

tests/data/meteor_m2_real_soft.npz: 3 s of soft symbols demodulated from a real Meteor-M N2 LRPT recording
(meteor_m2_72k.wav, 187.5 kS/s, from github.com/lar-tech/satellite-decoder, MIT licence), QPSK 72k.
It decodes ONLY with the I/Q swap (mirrored spectrum): MeteorRX <= 1.6.0 never tried it, so real passes
never locked although the bench test pattern (textbook orientation) decoded perfectly."""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from meteorrx import core, lrpt  # noqa: E402
from meteorrx.receiver import HYPOTHESES, PairFixer, Receiver, ReceiverConfig  # noqa: E402
from tests import synth  # noqa: E402

SOFT = os.path.join(os.path.dirname(__file__), "data", "meteor_m2_real_soft.npz")


def _decode(soft, rot, swap, shift=0):
    fx = PairFixer(rot, 0, shift, swap)
    bits = core.Viterbi().process(fx.push(soft))
    frames = lrpt.Deframer().push(bits)
    return [r for r in (lrpt.cadu_to_vcdu(f) for f in frames) if r[1]]


def test_real_meteor_signal_needs_iq_swap():
    soft = np.load(SOFT)["soft"]
    found = {}
    for swap in (0, 1):
        for rot in range(4):
            for shift in (0, 1):
                found[(swap, rot, shift)] = len(_decode(soft, rot, swap, shift))
    assert max(n for (sw, _, _), n in found.items() if sw == 0) == 0       # textbook orientation: nothing
    assert found[(1, 3, 0)] >= 24                                           # mirrored: ~26 RS-valid frames in 3 s


def test_real_meteor_frames_to_imagery():
    soft = np.load(SOFT)["soft"]
    imager, demux, pk = lrpt.MsuMrImager(), lrpt.Demuxer(), 0
    for vc, _, errs, _ in _decode(soft, 3, 1):
        v = lrpt.Vcdu(vc)
        assert v.vcid in (5, 63) or v.vcid < 64
        if v.vcid == 5:
            for p in demux.work(v):
                imager.work(p)
                pk += 1
    assert pk >= 80 and len(imager.channels_present) >= 3                  # real MSU-MR JPEG decoded
    ch = imager.get_channel(imager.channels_present[0])
    assert ch.shape[1] == lrpt.IMG_WIDTH and 20 < float(np.mean(ch[ch > 0])) < 250


def test_hypotheses_cover_mirrored_spectrum():
    assert {h[3] for h in HYPOTHESES} == {0, 1} and len(HYPOTHESES) == 12


@pytest.mark.parametrize("mode,lines", [("72k", 10), ("80k", 24)])
def test_mirrored_oqpsk_signal_decodes(mode, lines):
    """A conjugated (spectrum-mirrored) M2-x OQPSK signal must lock like the textbook one."""
    pytest.importorskip("scipy")
    fs = 1.2e6
    iq, _, n = synth.make_iq(mode, lines=lines, snr_db=8.0, offset_hz=1500.0, fs=fs, seed=5)
    iq = np.conj(iq).astype(np.complex64)
    rx = Receiver(ReceiverConfig(sample_rate=fs, center_hz=137.5e6, mode=mode))
    for c in rx.chains:
        c.time_scale = 0.05
    blk = int(fs * 0.1)
    for i in range(0, len(iq), blk):
        rx.process(iq[i:i + blk])
    ok = sum(s.frames_ok for s in rx.chain_stats())
    assert ok >= 0.85 * n, f"{ok}/{n} frames"


def test_afc_accepts_plain_qpsk_and_rejects_cw():
    """The AFC gate must let a plain-QPSK line through (no 2f lines at all) and still reject a carrier."""
    pytest.importorskip("scipy")
    from meteorrx.receiver import CarrierAfc
    rate = 300e3
    rng = np.random.default_rng(7)
    n = 300000
    t = np.arange(n) / rate
    # CW spur at +3 kHz on noise: rejected
    a = CarrierAfc(rate)
    for i in range(20):
        x = rng.normal(0, 1, n) + 1j * rng.normal(0, 1, n)
        ph = 2 * np.pi * 3000 * (t + i * n / rate)
        x = x + 3 * (np.cos(ph) + 1j * np.sin(ph))
        assert a.push(x.astype(np.complex64)) is None
    # plain QPSK (no OQPSK offset) at 72 ksym/s, +2345 Hz: accepted within a few Hz
    syms = (rng.integers(0, 2, 40000) * 2 - 1 + 1j * (rng.integers(0, 2, 40000) * 2 - 1)) / np.sqrt(2)
    from scipy.signal import resample_poly
    up = np.zeros(len(syms) * 25, np.complex128)
    up[::25] = syms
    from meteorrx.core import rrc_taps
    h = rrc_taps(25, 0.5, 251)
    y = np.convolve(up, h, "same")                      # 1.8 MS/s
    y = resample_poly(y, 1, 6)                           # 300 kS/s
    y = y * np.exp(2j * np.pi * 2345.0 * np.arange(len(y)) / rate)
    a = CarrierAfc(rate)
    ests = [e for e in (a.push(y[i:i + 30000].astype(np.complex64)) for i in range(0, len(y), 30000))
            if e is not None]
    assert ests and all(abs(e - 2345.0) < 5 for e in ests)

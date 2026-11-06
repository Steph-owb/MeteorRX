"""MeteorRX test suite: FEC primitives, MSU-MR codec, orbit maths and full end-to-end decoding
of synthetic Meteor-M LRPT signals (72k and 80k interleaved).   Run:  python -m pytest -q tests"""
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from meteorrx import core, lrpt, orbit  # noqa: E402
from meteorrx.receiver import Receiver, ReceiverConfig  # noqa: E402


def test_pn_sequence():
    assert lrpt.PN[:8].tobytes().hex() == "ff480ec09a0d70bc"


def test_reed_solomon_interleaved():
    rng = np.random.default_rng(1)
    blk = np.zeros(1020, np.uint8)
    for k in range(4):
        d = rng.integers(0, 256, 223, dtype=np.uint8)
        blk[k:892:4] = d
        blk[892 + k::4] = core.rs_encode(d)
    ref = blk.copy()
    bad = blk.copy()
    idx = np.concatenate([k + 4 * rng.choice(255, 15, replace=False) for k in range(4)])  # 15 per codeword
    bad[idx] ^= rng.integers(1, 256, 60, dtype=np.uint8)
    ok, errs = core.rs_decode_interleaved(bad, 4)
    assert ok and sum(errs) == 60 and (bad == ref).all()
    bad = blk.copy()
    bad[:4 * 17] ^= 0x55  # 17 errors per codeword: beyond capability
    ok, _ = core.rs_decode_interleaved(bad, 4)
    assert not ok


def test_viterbi_soft():
    rng = np.random.default_rng(2)
    bits = rng.integers(0, 2, 60000).astype(np.uint8)
    enc, _ = core.conv_encode(bits)
    soft = np.clip(((2.0 * enc - 1) + rng.normal(0, 0.6, len(enc))) * 50, -127, 127).astype(np.int8)
    v = core.Viterbi()
    out = v.process(soft)
    assert len(out) > 50000
    assert np.count_nonzero(out != bits[:len(out)]) < 10
    assert 0.0 <= v.take_ber() < 0.1


def test_msumr_codec_roundtrip():
    rng = np.random.default_rng(3)
    img = (np.add.outer(np.arange(8) * 9, np.arange(112) * 2) % 256).astype(np.int32)
    img = np.clip(img + rng.integers(-6, 6, img.shape), 0, 255).astype(np.uint8)
    for q in (40, 77, 95):
        data = lrpt.encode_segment_blocks(img, q)
        pix, n = lrpt.decode_segment_blocks(data, q)
        assert n == 14
        assert np.abs(pix.astype(int) - img).mean() < 6


def test_orbit_against_reference():
    """ISS 2019-12-09 pass over Malta; reference values computed with Skyfield."""
    l1 = "1 25544U 98067A   19343.69339541  .00001764  00000-0  38792-4 0  9991"
    l2 = "2 25544  51.6439 211.2001 0007417  17.6667  85.6398 15.50103472202482"
    s = orbit.Satellite("ISS", 25544, [437.8])
    s.load(l1, l2)
    obs = orbit.Observer(35.90, 14.45, 50)
    ps = orbit.predict_passes(obs, [s], datetime(2019, 12, 9, 18, 0, tzinfo=timezone.utc), 6, 5)
    p = ps[0]
    assert abs((p.aos - datetime(2019, 12, 9, 18, 15, 12, tzinfo=timezone.utc)).total_seconds()) < 3
    assert abs(p.max_el - 26.21) < 0.1
    az, el, _, rr, _ = obs.look(s, p.tca)
    assert abs(az - 229.09) < 0.1 and abs(rr - (-0.0283)) < 0.002


def _decode(mode, rx_mode, snr, offset, freq=137.9e6, fs=1.2e6, lines=16):
    pytest.importorskip("scipy")
    from tests.synth import make_iq
    iq, scenes, ncadu = make_iq(mode, lines=lines, snr_db=snr, offset_hz=offset, freq=freq, fs=fs)
    rx = Receiver(ReceiverConfig(sample_rate=fs, center_hz=137.5e6, freqs_hz=[137.9e6, 137.1e6], mode=rx_mode))
    blk = int(fs * 0.1)
    for i in range(0, len(iq), blk):
        rx.process(iq[i:i + blk])
    return rx, scenes, ncadu


@pytest.mark.parametrize("snr,offset,freq", [(10.0, 1200.0, 137.9e6), (6.0, -2500.0, 137.1e6)])
def test_end_to_end_72k(snr, offset, freq):
    rx, scenes, ncadu = _decode("72k", "auto", snr, offset, freq)
    ok = sum(s.frames_ok for s in rx.chain_stats())
    # allow ~1.5 s of acquisition at the start of the burst (weak signal, large offset)
    assert ok >= ncadu - 13, f"only {ok}/{ncadu} CADUs"
    assert rx.active_freq == freq
    for ch in range(3):
        img = rx.imager.get_channel(ch)
        ref = scenes[64 + ch]
        assert img.shape[1] == lrpt.IMG_WIDTH and img.shape[0] >= ref.shape[0] - 16
        # find vertical alignment (first lines may be lost during acquisition) and compare
        # decoded pixels only (missing segments are black and counted separately)
        best, cover = 1e9, 0.0
        for d in range(0, 33, 8):
            n = min(img.shape[0], ref.shape[0] - d)
            a, r = img[:n].astype(int), ref[d:d + n].astype(int)
            m = a > 0
            if m.mean() > 0.5:
                e = np.abs(a - r)[m].mean()
                if e < best:
                    best, cover = e, m.mean()
        assert cover > 0.8
        assert best < 3.0


def test_end_to_end_80k_interleaved():
    rx, scenes, ncadu = _decode("80k", "auto", 9.0, 800.0)
    st = {s.mode: s for s in rx.chain_stats()}
    assert st["80k"].frames_ok >= ncadu - 6
    img = rx.imager.get_channel(0)
    assert np.abs(img[:scenes[64].shape[0]].astype(int) - scenes[64][:img.shape[0]].astype(int)).mean() < 3.0


def test_80k_resync_keeps_interleaver_alignment():
    """Fade + 90 deg carrier slip in the middle of an 80k stream: the de-interleaver must resynchronise
    on the marker grid and keep its branches aligned (erasures), so decoding resumes immediately."""
    from tests.synth import build_cadus, build_packets, coded_bits, interleave_80k
    from meteorrx.receiver import Deinterleaver80k, rotate_soft
    rng = np.random.default_rng(11)
    pkts, _ = build_packets(30)
    cadus = build_cadus(pkts)
    tx = interleave_80k(coded_bits(cadus), rng)
    soft = np.where(tx > 0, 60, -60).astype(np.int8)
    soft = np.clip(soft + rng.normal(0, 20, len(soft)), -127, 127).astype(np.int8)
    cut = (len(soft) // 2) & ~1
    soft[cut:cut + 4000] = rng.integers(-127, 127, 4000).astype(np.int8)   # fade (noise only)
    soft[cut + 4000:] = rotate_soft(soft[cut + 4000:], 1)                  # 90 deg slip afterwards
    di = Deinterleaver80k()
    vit = core.Viterbi()
    dfr = lrpt.Deframer()
    last = 0
    ok = 0
    for i in range(0, len(soft), 50000):
        out = di.push(soft[i:i + 50000])
        bits = vit.process(out[: (len(out) // 2) * 2]) if len(out) else np.zeros(0, np.uint8)
        dec, last = lrpt.nrzm_decode(bits, last)
        for f in dfr.push(dec):
            ok += lrpt.cadu_to_vcdu(f)[1]
    assert di.sync_count == 2 and di.erased_blocks > 0
    assert ok >= len(cadus) - 4, f"{ok}/{len(cadus)} CADUs after resync"

"""Weak-signal performance (regression guard for v1.5.1): decoding near the theoretical threshold,
carrier acquisition by the 4th-power AFC, false-alarm immunity. Es/N0 = energy per QPSK symbol / N0;
an ideal decoder (perfect synchronisation) recovers ~90 % of the frames at 1.5 dB and all at 2 dB."""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from meteorrx import config  # noqa: E402
from meteorrx.receiver import CarrierAfc, Receiver, ReceiverConfig, dsp_from_config  # noqa: E402
from tests import synth  # noqa: E402


def decode(mode, esn0, offset=1500.0, drift=0.0, fs=1.2e6, lines=16, seed=3, rx_mode=None):
    pytest.importorskip("scipy")       # synthetic transmitter (resample_poly)
    iq, _, n = synth.make_iq(mode, lines=lines, snr_db=esn0 - 10 * np.log10(1.5), offset_hz=offset, fs=fs,
                             seed=seed)
    if drift:
        t = np.arange(len(iq)) / fs
        ph = np.pi * drift * t * t
        iq = (iq * (np.cos(ph) + 1j * np.sin(ph))).astype(np.complex64)
    rx = Receiver(ReceiverConfig(sample_rate=fs, center_hz=137.5e6, mode=rx_mode or mode))
    for c in rx.chains:
        c.time_scale = 0.05
    blk = int(fs * 0.1)
    for i in range(0, len(iq), blk):
        rx.process(iq[i:i + blk])
    return sum(s.frames_ok for s in rx.chain_stats()), n, rx


@pytest.mark.parametrize("esn0,offset,drift", [(2.5, 1500.0, 0.0), (2.5, 9000.0, 0.0), (3.0, -4000.0, 60.0)])
def test_72k_weak_signal(esn0, offset, drift):
    ok, n, rx = decode("72k", esn0, offset, drift)
    assert ok >= 0.9 * n, f"{ok}/{n} frames at Es/N0 {esn0} dB"
    if not drift:
        assert abs(rx.afc_hz - offset) < 50     # the AFC NCO sits on the carrier (end-of-signal wander incl.)


def test_80k_weak_signal():
    ok, n, _ = decode("80k", 3.0, lines=24)
    assert ok >= 0.9 * n, f"{ok}/{n}"


def test_auto_mode_weak_signal():
    ok, n, _ = decode("72k", 3.0, rx_mode="auto")
    assert ok >= 0.9 * n


def test_afc_rejects_noise_and_carriers_and_finds_lrpt():
    rate = 300e3
    rng = np.random.default_rng(1)
    a = CarrierAfc(rate)
    t = np.arange(100000) / rate
    for i in range(150):                       # 50 s of noise, then 20 s with a strong unmodulated spur
        x = rng.normal(0, 1, 100000) + 1j * rng.normal(0, 1, 100000)
        if i >= 90:
            ph = 2 * np.pi * 3000 * (t + i * 100000 / rate)
            x = x + 3 * (np.cos(ph) + 1j * np.sin(ph))
        assert a.push(x.astype(np.complex64)) is None
    pytest.importorskip("scipy")
    from meteorrx import core
    iq, _, _ = synth.make_iq("72k", lines=6, snr_db=1.5 - 1.76, offset_hz=2345.0, fs=1.2e6, seed=2)
    ch = core.Channelizer(1.2e6, 4, 95e3, 50e3)
    ch.set_offset(400e3)
    a = CarrierAfc(300e3)
    y = ch.process(iq)
    ests = [e for e in (a.push(y[i:i + 30000]) for i in range(0, len(y), 30000)) if e is not None]
    assert ests and all(abs(e - 2345.0) < 3 for e in ests)


def test_legacy_loop_settings_are_ignored(tmp_path):
    ini = tmp_path / "m.ini"
    ini.write_text(config.DEFAULT_INI.replace("carrier_loop_bw =", "pll_bw = 0.005\nfll_gain = 0.00002\n"
                                              "carrier_loop_bw ="))
    cfg = config.load(str(ini))
    assert dsp_from_config(cfg) == {"rrc_alpha": 0.5}
    cfg.set("decoder", "carrier_loop_bw", "0.001")
    assert dsp_from_config(cfg)["pll_bw"] == 0.001

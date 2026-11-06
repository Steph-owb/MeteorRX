"""Audio monitor: FM discriminator output, muting, buffering, failure handling (no sound device)."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from meteorrx import audio  # noqa: E402


class FakePlayer:
    def __init__(self):
        self.chunks = []

    def write(self, pcm):
        self.chunks.append(pcm)

    def close(self):
        pass


def test_fm_discriminator_recovers_a_tone():
    fs = 300e3
    t = np.arange(int(fs)) / fs
    dev = 5000.0
    ph = 2 * np.pi * dev / 1000.0 * np.sin(2 * np.pi * 1000.0 * t)       # 1 kHz FM tone
    x = np.cos(ph) + 1j * np.sin(ph)   # (no complex exp: missing in Wine's ucrtbase)
    fm = audio.FmDiscriminator(fs)
    out = np.concatenate([fm.process(x[i:i + 30000].astype(np.complex64), 0.5) for i in range(0, len(x), 30000)])
    assert abs(len(out) - audio.AUDIO_RATE) <= 2                   # rate conversion exact across blocks
    sp = np.abs(np.fft.rfft(out[4800:4800 + 38400].astype(float)))
    f = np.fft.rfftfreq(38400, 1 / audio.AUDIO_RATE)
    assert abs(f[np.argmax(sp)] - 1000.0) < 5


def test_monitor_muted_by_default_and_plays_when_enabled():
    m = audio.AudioMonitor()
    p = FakePlayer()
    m.player_factory = lambda: p
    x = (np.random.default_rng(1).normal(size=30000) + 1j).astype(np.complex64)
    m.push(x, 300e3)
    assert p.chunks == []
    m.set(True, 0.3)
    m.push(x, 300e3)
    assert len(p.chunks) == 1 and p.chunks[0].dtype == np.int16 and len(p.chunks[0]) > 4000
    m.set(False)
    m.push(x, 300e3)
    assert len(p.chunks) == 1


def test_no_sound_device_disables_the_monitor():
    logs = []
    m = audio.AudioMonitor(log=logs.append)

    def fail():
        raise OSError("waveOutOpen failed (2): no sound output device?")
    m.player_factory = fail
    m.set(True)
    m.push(np.ones(3000, np.complex64), 300e3)
    assert not m.enabled and "waveOutOpen" in m.error and logs


def test_waveout_under_windows():
    if os.name != "nt":
        return
    try:
        w = audio.WaveOut()
    except OSError:
        return                                   # machine without a sound device (CI, Wine)
    w.write((np.sin(np.arange(4800) / 10) * 1000).astype(np.int16))
    w.close()

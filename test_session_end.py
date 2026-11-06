"""Session end conditions — reproduces what a tester's log showed on 2026-11-04:

a manual session triggered at 08:12 by a carrier that never decoded ran for more than five hours and
swallowed two later real passes into one .cadu (a 46688-frame VCDU counter gap) and one 8992-line image.
Neither the LOS nor the signal-loss stop applied, because both were skipped in manual mode."""
import os
import sys
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from meteorrx import engine  # noqa: E402


class FakeChain:
    def __init__(self, frames_ok=0):
        self.frames_ok = frames_ok


def _session(manual, frames_ok, recording=True, rec_age=0.0, quiet=0.0, false_trigger_s=300, pas=None):
    """A Session with only what step()'s end conditions touch."""
    s = object.__new__(engine.Session)
    now = time.monotonic()
    cfg = types.SimpleNamespace(getf=lambda sec, k, d: {"false_trigger_s": false_trigger_s,
                                                        "signal_loss_stop_s": 90}.get(k, d))
    logged = []
    s.e = types.SimpleNamespace(cfg=cfg, log=logged.append)
    s.manual, s.pas, s.recording = manual, pas, recording
    s.rec_start = now - rec_age
    s.last_signal = now - quiet
    s.source = types.SimpleNamespace(is_file=False)
    s.rx = types.SimpleNamespace(chain_stats=lambda: [FakeChain(frames_ok), FakeChain(0)])
    return s, logged, now




def test_false_trigger_stops_a_session_that_decodes_nothing():
    # the real case: carrier detected, nothing ever decoded, still running 6 minutes later
    s, logged, now = _session(manual=True, frames_ok=0, rec_age=400)
    assert _end_conditions(s, now) is False
    assert any("false trigger" in m for m in logged)
    # still inside the grace period: keep going (a weak pass can take a while to lock)
    s, logged, now = _session(manual=True, frames_ok=0, rec_age=100)
    assert _end_conditions(s, now) is True
    # disabled by configuration
    s, logged, now = _session(manual=True, frames_ok=0, rec_age=4000, false_trigger_s=0)
    assert _end_conditions(s, now) is True


def test_manual_session_stops_once_a_pass_has_been_decoded_and_the_signal_is_gone():
    # this is what kept two passes in one session: manual + decoded + long silence used to keep running
    s, logged, now = _session(manual=True, frames_ok=5351, quiet=120, rec_age=800)
    assert _end_conditions(s, now) is False
    assert any("No signal" in m for m in logged)
    # a manual session that has decoded nothing keeps listening (within the false-trigger grace period)
    s, logged, now = _session(manual=True, frames_ok=0, quiet=120, rec_age=100)
    assert _end_conditions(s, now) is True
    # automatic sessions are unchanged
    s, logged, now = _session(manual=False, frames_ok=10, quiet=120, rec_age=800)
    assert _end_conditions(s, now) is False


def test_a_good_pass_is_not_cut_short():
    """Signal present and frames coming in: the session must keep running."""
    s, logged, now = _session(manual=True, frames_ok=5351, quiet=1.0, rec_age=700)
    assert _end_conditions(s, now) is True
    assert logged == []


def _end_conditions(s, now):
    """The end-condition block of Session.step(), evaluated directly."""
    if s.source.is_file:
        return True
    decoded = sum(st.frames_ok for st in s.rx.chain_stats())
    false_s = s.e.cfg.getf("recording", "false_trigger_s", 300)
    if s.recording and not decoded and false_s > 0 and s.rec_start is not None \
            and now - s.rec_start > false_s:
        s.e.log(f"Nothing decoded {false_s:.0f} s after the signal was detected - false trigger, stopping")
        return False
    loss = s.e.cfg.getf("recording", "signal_loss_stop_s", 90)
    if s.recording and now - s.last_signal > loss and (not s.manual or decoded):
        s.e.log(f"No signal for {loss:.0f} s - stopping")
        return False
    return True


def test_the_helper_matches_the_real_code():
    """Guard: the conditions above must be the ones in engine.Session.step()."""
    import inspect
    src = inspect.getsource(engine.Session.step)
    for needle in ('false_trigger_s', 'not decoded and false_s > 0',
                   '(not self.manual or decoded)', 'signal_loss_stop_s'):
        assert needle in src, needle

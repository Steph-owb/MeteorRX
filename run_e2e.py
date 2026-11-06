import sys, os, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests.synth import make_iq
from meteorrx.receiver import Receiver, ReceiverConfig

def run(mode="72k", rx_mode=None, lines=24, snr=12.0, offset=1200.0, freq=137.9e6, fs=1.2e6, verbose=True):
    t0=time.time()
    iq, scenes, ncadu = make_iq(mode, lines=lines, snr_db=snr, offset_hz=offset, freq=freq, fs=fs)
    tg=time.time()-t0
    rc = ReceiverConfig(sample_rate=fs, center_hz=137.5e6, freqs_hz=[137.9e6,137.1e6], mode=rx_mode or mode)
    rx = Receiver(rc)
    blk = int(fs*0.1)
    t0=time.time()
    for i in range(0, len(iq), blk):
        rx.process(iq[i:i+blk])
    tp=time.time()-t0
    st = rx.chain_stats()
    res = {}
    for ch in range(3):
        img = rx.imager.get_channel(ch)
        ref = scenes[64+ch]
        if img.shape[0] == 0:
            res[ch] = None; continue
        # align: compare overlapping lines
        h=min(img.shape[0], ref.shape[0])
        diff = np.abs(img[:h].astype(int)-ref[:h].astype(int))
        res[ch] = (img.shape, float(diff.mean()), float((img[:h]==0).all(axis=1).mean()))
    if verbose:
        print(f"mode={mode} rx={rx_mode or mode} snr={snr} off={offset} gen {tg:.1f}s proc {tp:.1f}s for {len(iq)/fs:.1f}s signal")
        for s in st: print("  ", s)
        print("   cadus sent", ncadu, "active freq", rx.active_freq, "snr", round(rx.snr_db,1))
        print("   images", res)
    return rx, st, res

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv)>1 else "72k"
    rxm = sys.argv[2] if len(sys.argv)>2 else None
    snr = float(sys.argv[3]) if len(sys.argv)>3 else 12.0
    run(mode, rxm, snr=snr)

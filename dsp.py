"""DSP and FEC kernels (Numba): channelizer, OQPSK demodulator, Viterbi K=7 r=1/2, RS(255,223).

Compiled on the user's machine from this source at first start (then cached), so MeteorRX ships
no native binary of its own. Algorithms follow SatDump and meteor_demod/meteor_decode (GPL-3)."""
from __future__ import annotations

import os

import numpy as np


def _set_cache_dir():
    if os.environ.get("NUMBA_CACHE_DIR"):
        return
    from .config import app_dir
    for d in (os.path.join(app_dir(), "cache"),
              os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache"), "MeteorRX", "cache")):
        d = os.path.abspath(d)
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".w")
            with open(probe, "w") as f:
                f.write("x")
            os.remove(probe)
            os.environ["NUMBA_CACHE_DIR"] = d
            return
        except OSError:
            continue


_set_cache_dir()

from numba import njit  # noqa: E402

JIT = dict(cache=True, nogil=True, fastmath=False)
TWO_PI = 2.0 * np.pi


@njit(inline="always")
def _clip(x, lo, hi):
    return lo if x < lo else (hi if x > hi else x)


@njit(inline="always")
def _cubic(y0, y1, y2, y3, m):
    """4-point Lagrange through t=-1,0,1,2 evaluated at t=m."""
    c0 = -m * (m - 1.0) * (m - 2.0) / 6.0
    c1 = (m + 1.0) * (m - 1.0) * (m - 2.0) / 2.0
    c2 = -(m + 1.0) * m * (m - 2.0) / 2.0
    c3 = (m + 1.0) * m * (m - 1.0) / 6.0
    return c0 * y0 + c1 * y1 + c2 * y2 + c3 * y3


# ============================================================================
# Channelizer: NCO + decimating FIR
#   st = [phase, inc, hpos, phase_cnt]
# ============================================================================
@njit(**JIT)
def chan_process(x, taps, hist, st, decim, out):
    n = x.shape[0]
    N = taps.shape[0]
    ph = st[0]
    inc = st[1]
    hpos = int(st[2])
    cnt = int(st[3])
    rot = np.cos(ph) + 1j * np.sin(ph)
    drot = np.cos(inc) + 1j * np.sin(inc)
    nout = 0
    for i in range(n):
        y = x[i] * rot
        rot = rot * drot
        if (i & 1023) == 1023:
            rot = rot / abs(rot)
        hist[hpos] = y
        hist[hpos + N] = y
        hpos += 1
        if hpos == N:
            hpos = 0
        cnt += 1
        if cnt >= decim:
            cnt = 0
            acc = 0j
            for k in range(N):
                acc += hist[hpos + k] * taps[k]
            out[nout] = acc
            nout += 1
    st[0] = (ph + inc * n) % TWO_PI
    st[2] = hpos
    st[3] = cnt
    return nout


# ============================================================================
# Demodulator: Farrow -> AGC -> FLL -> RRC -> Costas(4) -> OQPSK align -> M&M
# ============================================================================
RS_STEP, RS_MU, RB_FILL, AGC_G, AGC_R, RPOS, C_PH, C_FR, C_AL, C_BE, C_MAXF, C_POST, FLL_PH, FLL_FR, FLL_G, \
    FLL_LR, FLL_LI, OQ, LAST_IM, MM_OM, MM_OMID, MM_OLIM, MM_MU, MM_GMU, MM_GOM, P0R, P0I, P1R, P1I, P2R, P2I, \
    C0R, C0I, C1R, C1I, C2R, C2I, MLEN, MM_ABS, SAMP_CNT, SYM_MAG = range(41)
DEMOD_STATE_LEN = 41


def demod_state(in_rate, sym_rate, pll_bw, clock_gain_mu, clock_gain_omega, omega_rel_limit, oqpsk,
                fll_gain, costas_post):
    st = np.zeros(DEMOD_STATE_LEN, np.float64)
    st[RS_STEP] = in_rate / (2.0 * sym_rate)
    st[AGC_G] = 1.0
    st[AGC_R] = 1e-3
    damp = 0.70710678
    den = 1.0 + 2.0 * damp * pll_bw + pll_bw * pll_bw
    st[C_AL] = 4.0 * damp * pll_bw / den
    st[C_BE] = 4.0 * pll_bw * pll_bw / den
    st[C_MAXF] = 1.0
    st[C_POST] = 1.0 if costas_post else 0.0
    st[FLL_G] = fll_gain
    st[OQ] = 1.0 if oqpsk else 0.0
    st[MM_OM] = st[MM_OMID] = 2.0
    st[MM_OLIM] = omega_rel_limit * 2.0
    st[MM_MU] = 0.5
    st[MM_GMU] = clock_gain_mu
    st[MM_GOM] = clock_gain_omega
    st[SYM_MAG] = 1.0
    return st


@njit(**JIT)
def demod_process(x, st, rb, rrc, rh, mb, soft, syms, wave, strobes):
    """Returns (n_symbols, n_wave)."""
    N = rrc.shape[0]
    mcap = mb.shape[0]
    wave_cap = wave.shape[0]
    nwave = 0
    wave_base = st[SAMP_CNT]
    mlen = int(st[MLEN])
    rpos = int(st[RPOS])
    for i in range(x.shape[0]):
        rb[0] = rb[1]
        rb[1] = rb[2]
        rb[2] = rb[3]
        rb[3] = x[i]
        if st[RB_FILL] < 4:
            st[RB_FILL] += 1
            continue
        while st[RS_MU] < 1.0:
            s = _cubic(rb[0], rb[1], rb[2], rb[3], st[RS_MU])
            # ---- AGC
            y = s * st[AGC_G]
            mag = abs(y)
            st[AGC_G] += st[AGC_R] * (1.0 - mag) * st[AGC_G]
            st[AGC_G] = _clip(st[AGC_G], 1e-6, 1e9)
            # ---- FLL (delay discriminator)
            if st[FLL_G] > 0.0:
                u = y * (np.cos(-st[FLL_PH]) + 1j * np.sin(-st[FLL_PH]))
                e = u.imag * st[FLL_LR] - u.real * st[FLL_LI]
                st[FLL_LR] = u.real
                st[FLL_LI] = u.imag
                st[FLL_FR] = _clip(st[FLL_FR] + st[FLL_G] * _clip(e, -1.0, 1.0), -0.6, 0.6)
                st[FLL_PH] += st[FLL_FR]
                if st[FLL_PH] > np.pi:
                    st[FLL_PH] -= TWO_PI
                if st[FLL_PH] < -np.pi:
                    st[FLL_PH] += TWO_PI
                y = u
            # ---- RRC
            rh[rpos] = y
            rh[rpos + N] = y
            rpos += 1
            if rpos == N:
                rpos = 0
            f = 0j
            for k in range(N):
                f += rh[rpos + k] * rrc[k]
            # ---- Costas
            t = f * (np.cos(-st[C_PH]) + 1j * np.sin(-st[C_PH]))
            outr = t.real
            outi = t.imag
            if st[OQ] > 0.0:
                outi = st[LAST_IM]
                st[LAST_IM] = t.imag
            if st[C_POST] > 0.0:
                er = outr
                ei = outi
            else:
                er = t.real
                ei = t.imag
            err = (1.0 if er > 0.0 else -1.0) * ei - (1.0 if ei > 0.0 else -1.0) * er
            err = _clip(err, -1.0, 1.0)
            st[C_FR] = _clip(st[C_FR] + st[C_BE] * err, -st[C_MAXF], st[C_MAXF])
            st[C_PH] += st[C_FR] + st[C_AL] * err
            while st[C_PH] > TWO_PI:
                st[C_PH] -= TWO_PI
            while st[C_PH] < -TWO_PI:
                st[C_PH] += TWO_PI
            if mlen < mcap:
                mb[mlen] = outr + 1j * outi
                mlen += 1
            if nwave < wave_cap:
                wave[nwave] = outr + 1j * outi
                nwave += 1
            st[SAMP_CNT] += 1.0
            st[RS_MU] += st[RS_STEP]
        st[RS_MU] -= 1.0
    st[RPOS] = rpos

    # ---- Mueller & Muller clock recovery
    nsym = 0
    cap = strobes.shape[0]
    pos = 0
    while pos + 3 < mlen and nsym < cap:
        m = st[MM_MU]
        s = _cubic(mb[pos], mb[pos + 1], mb[pos + 2], mb[pos + 3], m)
        sr = s.real
        si = s.imag
        strobes[nsym] = st[MM_ABS] + pos + 1 + m - wave_base
        st[P2R] = st[P1R]
        st[P2I] = st[P1I]
        st[P1R] = st[P0R]
        st[P1I] = st[P0I]
        st[P0R] = sr
        st[P0I] = si
        st[C2R] = st[C1R]
        st[C2I] = st[C1I]
        st[C1R] = st[C0R]
        st[C1I] = st[C0I]
        st[C0R] = 1.0 if sr > 0.0 else -1.0
        st[C0I] = 1.0 if si > 0.0 else -1.0
        xr_ = (st[C0R] - st[C2R]) * st[P1R] + (st[C0I] - st[C2I]) * st[P1I]
        yr_ = (st[P0R] - st[P2R]) * st[C1R] + (st[P0I] - st[P2I]) * st[C1I]
        mmv = _clip(yr_ - xr_, -1.0, 1.0)
        st[MM_OM] = _clip(st[MM_OM] + st[MM_GOM] * mmv, st[MM_OMID] - st[MM_OLIM], st[MM_OMID] + st[MM_OLIM])
        st[MM_MU] += st[MM_OM] + st[MM_GMU] * mmv
        adv = int(np.floor(st[MM_MU]))
        st[MM_MU] -= adv
        pos += adv
        a = 0.5 * (abs(sr) + abs(si))
        st[SYM_MAG] += 0.001 * (a - st[SYM_MAG])
        g = 64.0 / (st[SYM_MAG] if st[SYM_MAG] > 1e-6 else 1e-6)
        soft[2 * nsym] = np.int8(np.rint(_clip(sr * g, -127.0, 127.0)))
        soft[2 * nsym + 1] = np.int8(np.rint(_clip(si * g, -127.0, 127.0)))
        syms[nsym] = (sr + 1j * si) * (g / 64.0)
        nsym += 1
    if pos > 0:
        if pos > mlen:
            pos = mlen
        for k in range(mlen - pos):
            mb[k] = mb[k + pos]
        mlen -= pos
        st[MM_ABS] += pos
    st[MLEN] = mlen
    return nsym, nwave


# ============================================================================
# Convolutional code K=7 r=1/2, CCSDS polynomials 171/133 octal (0x4F / 0x6D, newest bit = LSB)
# ============================================================================
POLYA, POLYB = 0x4F, 0x6D
VIT_TB, VIT_BLOCK = 160, 2048
VIT_CAP = VIT_TB + VIT_BLOCK + 8


@njit(inline="always")
def _parity(x):
    x ^= x >> 4
    x ^= x >> 2
    x ^= x >> 1
    return x & 1


@njit(**JIT)
def conv_encode(state, bits, out):
    for i in range(bits.shape[0]):
        state = ((state << 1) | (bits[i] & 1)) & 0x7F
        out[2 * i] = _parity(state & POLYA)
        out[2 * i + 1] = _parity(state & POLYB)
    return state & 0x3F


def vit_tables():
    out_sym = np.zeros((64, 2), np.int64)
    for s in range(64):
        out_sym[s, 0] = bin(s & POLYA).count("1") & 1
        out_sym[s, 1] = bin(s & POLYB).count("1") & 1
    masks = np.array([np.uint64(1) << np.uint64(s) for s in range(64)], dtype=np.uint64)
    return out_sym, masks


_VIT_OUT, _VIT_MASK = vit_tables()


@njit(**JIT)
def _vit_traceback(metric, dec, hard, vs, nout, out, out_off):
    """vs = [nsteps, enc_state, ber_err, ber_tot]."""
    nsteps = vs[0]
    s = 0
    best = metric[0]
    for i in range(1, 64):
        if metric[i] > best:
            best = metric[i]
            s = i
    for i in range(nsteps - 1, nout - 1, -1):
        dbit = (dec[i] >> np.uint64(s)) & np.uint64(1)
        s = (s >> 1) | (int(dbit) << 5)
    for i in range(nout - 1, -1, -1):
        dbit = (dec[i] >> np.uint64(s)) & np.uint64(1)
        out[out_off + i] = s & 1
        s = (s >> 1) | (int(dbit) << 5)
    enc = vs[1]
    for i in range(nout):
        enc = ((enc << 1) | out[out_off + i]) & 0x7F
        e0 = _parity(enc & POLYA)
        e1 = _parity(enc & POLYB)
        vs[2] += (1 if e0 != (hard[i] >> 1) else 0) + (1 if e1 != (hard[i] & 1) else 0)
        vs[3] += 2
    vs[1] = enc & 0x3F
    rem = nsteps - nout
    for i in range(rem):
        dec[i] = dec[i + nout]
        hard[i] = hard[i + nout]
    vs[0] = rem
    return nout


@njit(**JIT)
def vit_process(soft, metric, dec, hard, vs, out_sym, masks, out):
    npairs = soft.shape[0] // 2
    nm = np.empty(64, np.int64)
    nout = 0
    for i in range(npairs):
        a = np.int64(soft[2 * i])
        b = np.int64(soft[2 * i + 1])
        d = np.uint64(0)
        mx = -(1 << 62)
        for s in range(64):
            p0 = s >> 1
            p1 = p0 | 32
            bm0 = (a if out_sym[s, 0] else -a) + (b if out_sym[s, 1] else -b)
            m0 = metric[p0] + bm0
            m1 = metric[p1] - bm0
            if m1 > m0:
                nm[s] = m1
                d |= masks[s]
            else:
                nm[s] = m0
            if nm[s] > mx:
                mx = nm[s]
        for s in range(64):
            metric[s] = nm[s] - mx
        k = vs[0]
        dec[k] = d
        hard[k] = (2 if a > 0 else 0) | (1 if b > 0 else 0)
        vs[0] = k + 1
        if vs[0] >= VIT_TB + VIT_BLOCK:
            nout += _vit_traceback(metric, dec, hard, vs, VIT_BLOCK, out, nout)
    return nout


# ============================================================================
# Reed-Solomon (255,223) CCSDS conventional basis: poly 0x187, fcr 112, prim 11
# ============================================================================
NN, NROOTS, FCR, PRIM, IPRIM, A0 = 255, 32, 112, 11, 116, 255


def rs_tables():
    alpha_to = np.zeros(256, np.int64)
    index_of = np.zeros(256, np.int64)
    index_of[0] = A0
    sr = 1
    for i in range(NN):
        index_of[sr] = i
        alpha_to[i] = sr
        sr <<= 1
        if sr & 256:
            sr ^= 0x187
        sr &= 255
    gen = np.zeros(NROOTS + 1, np.int64)
    gen[0] = 1
    root = FCR * PRIM
    for i in range(NROOTS):
        gen[i + 1] = 1
        for j in range(i, 0, -1):
            if gen[j] != 0:
                gen[j] = gen[j - 1] ^ alpha_to[(index_of[gen[j]] + root) % NN]
            else:
                gen[j] = gen[j - 1]
        gen[0] = alpha_to[(index_of[gen[0]] + root) % NN]
        root += PRIM
    gen = np.array([index_of[g] for g in gen], np.int64)
    return alpha_to, index_of, gen


ALPHA_TO, INDEX_OF, GENPOLY = rs_tables()


@njit(inline="always")
def _modnn(x):
    while x >= NN:
        x -= NN
        x = (x >> 8) + (x & NN)
    return x


@njit(**JIT)
def rs_encode(data, parity, alpha_to, index_of, gen):
    for j in range(NROOTS):
        parity[j] = 0
    for i in range(NN - NROOTS):
        fb = index_of[data[i] ^ parity[0]]
        if fb != A0:
            for j in range(1, NROOTS):
                parity[j] ^= alpha_to[_modnn(fb + gen[NROOTS - j])]
        for j in range(NROOTS - 1):
            parity[j] = parity[j + 1]
        parity[NROOTS - 1] = alpha_to[_modnn(fb + gen[0])] if fb != A0 else 0


@njit(**JIT)
def rs_decode(data, alpha_to, index_of):
    """Decode a 255-byte codeword in place; returns corrections or -1."""
    s = np.empty(NROOTS, np.int64)
    for i in range(NROOTS):
        s[i] = data[0]
    for j in range(1, NN):
        for i in range(NROOTS):
            if s[i] == 0:
                s[i] = data[j]
            else:
                s[i] = data[j] ^ alpha_to[_modnn(index_of[s[i]] + (FCR + i) * PRIM)]
    syn = 0
    for i in range(NROOTS):
        syn |= s[i]
        s[i] = index_of[s[i]]
    if syn == 0:
        return 0
    lam = np.zeros(NROOTS + 1, np.int64)
    lam[0] = 1
    b = np.empty(NROOTS + 1, np.int64)
    t = np.empty(NROOTS + 1, np.int64)
    for i in range(NROOTS + 1):
        b[i] = index_of[lam[i]]
    r = 0
    el = 0
    while True:
        r += 1
        if r > NROOTS:
            break
        dr = 0
        for i in range(r):
            if lam[i] != 0 and s[r - i - 1] != A0:
                dr ^= alpha_to[_modnn(index_of[lam[i]] + s[r - i - 1])]
        dr = index_of[dr]
        if dr == A0:
            for i in range(NROOTS, 0, -1):
                b[i] = b[i - 1]
            b[0] = A0
        else:
            t[0] = lam[0]
            for i in range(NROOTS):
                if b[i] != A0:
                    t[i + 1] = lam[i + 1] ^ alpha_to[_modnn(dr + b[i])]
                else:
                    t[i + 1] = lam[i + 1]
            if 2 * el <= r - 1:
                el = r - el
                for i in range(NROOTS + 1):
                    b[i] = A0 if lam[i] == 0 else _modnn(index_of[lam[i]] - dr + NN)
            else:
                for i in range(NROOTS, 0, -1):
                    b[i] = b[i - 1]
                b[0] = A0
            for i in range(NROOTS + 1):
                lam[i] = t[i]
    deg_lambda = 0
    for i in range(NROOTS + 1):
        lam[i] = index_of[lam[i]]
        if lam[i] != A0:
            deg_lambda = i
    reg = np.empty(NROOTS + 1, np.int64)
    for i in range(1, NROOTS + 1):
        reg[i] = lam[i]
    root = np.empty(NROOTS, np.int64)
    loc = np.empty(NROOTS, np.int64)
    count = 0
    k = IPRIM - 1
    for i in range(1, NN + 1):
        q = 1
        for j in range(deg_lambda, 0, -1):
            if reg[j] != A0:
                reg[j] = _modnn(reg[j] + j)
                q ^= alpha_to[reg[j]]
        if q == 0:
            root[count] = i
            loc[count] = k
            count += 1
            if count == deg_lambda:
                break
        k = _modnn(k + IPRIM)
    if deg_lambda != count:
        return -1
    deg_omega = deg_lambda - 1
    omega = np.empty(NROOTS + 1, np.int64)
    for i in range(deg_omega + 1):
        tmp = 0
        for j in range(i, -1, -1):
            if s[i - j] != A0 and lam[j] != A0:
                tmp ^= alpha_to[_modnn(s[i - j] + lam[j])]
        omega[i] = index_of[tmp]
    for j in range(count - 1, -1, -1):
        num1 = 0
        for i in range(deg_omega, -1, -1):
            if omega[i] != A0:
                num1 ^= alpha_to[_modnn(omega[i] + i * root[j])]
        num2 = alpha_to[_modnn(root[j] * (FCR - 1) + NN)]
        den = 0
        i = (deg_lambda if deg_lambda < NROOTS - 1 else NROOTS - 1) & ~1
        while i >= 0:
            if lam[i + 1] != A0:
                den ^= alpha_to[_modnn(lam[i + 1] + i * root[j])]
            i -= 2
        if den == 0:
            return -1
        if num1 != 0 and loc[j] < NN:
            data[loc[j]] ^= alpha_to[_modnn(index_of[num1] + index_of[num2] + NN - index_of[den])]
    return count


@njit(**JIT)
def rs_decode_interleaved(block, depth, errs, alpha_to, index_of):
    cw = np.empty(NN, np.uint8)
    ok = True
    for k in range(depth):
        for i in range(NN):
            cw[i] = block[i * depth + k]
        errs[k] = rs_decode(cw, alpha_to, index_of)
        if errs[k] < 0:
            ok = False
        else:
            for i in range(NN):
                block[i * depth + k] = cw[i]
    return ok


def warmup():
    """Compile (or load from cache) every kernel once; returns elapsed seconds."""
    import time
    t0 = time.time()
    st = np.array([0.0, -0.1, 0.0, 0.0])
    chan_process(np.zeros(64, np.complex64), np.ones(8), np.zeros(16, np.complex128), st, 4,
                 np.zeros(32, np.complex128))
    ds = demod_state(300e3, 72e3, 0.005, 8.7e-3, 2e-5, 0.005, True, 2e-5, True)
    demod_process(np.zeros(256, np.complex64), ds, np.zeros(4, np.complex128), np.ones(31), np.zeros(62, np.complex128),
                  np.zeros(4096, np.complex128), np.zeros(512, np.int8), np.zeros(256, np.complex64),
                  np.zeros(1024, np.complex64), np.zeros(256))
    out = np.zeros(256, np.uint8)
    conv_encode(0, np.zeros(128, np.uint8), out)
    vit_process(np.zeros(256, np.int8), np.zeros(64, np.int64), np.zeros(VIT_CAP, np.uint64),
                np.zeros(VIT_CAP, np.uint8), np.zeros(4, np.int64), _VIT_OUT, _VIT_MASK, np.zeros(4096, np.uint8))
    blk = np.zeros(1020, np.uint8)
    rs_decode_interleaved(blk, 4, np.zeros(4, np.int64), ALPHA_TO, INDEX_OF)
    rs_encode(np.zeros(223, np.uint8), np.zeros(32, np.uint8), ALPHA_TO, INDEX_OF, GENPOLY)
    return time.time() - t0

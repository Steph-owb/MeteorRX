"""Meteor-M LRPT protocol stack: decoded bits -> CADU -> VCDU -> CCSDS packets -> MSU-MR imagery.

References: SatDump (meteor_support, ccsds_aos demuxer), meteor_decode (dbdexter-dev).
"""
from __future__ import annotations

import numpy as np

from . import core

ASM = 0x1ACFFC1D
CADU_BITS = 8192
CADU_BYTES = 1024
VCDU_DATA = 882
IMG_WIDTH = 1568
MSUMR_APIDS = {64: 0, 65: 1, 66: 2, 67: 3, 68: 4, 69: 5}
SEG_CNT = 20000


# ---------------------------------------------------------------------------
# CCSDS pseudo-random sequence (x^8+x^7+x^5+x^3+1, seed 0xFF)
# ---------------------------------------------------------------------------
def _pn_sequence(n: int) -> np.ndarray:
    out = np.zeros(n, dtype=np.uint8)
    sr = 0xFF
    for i in range(n):
        byte = 0
        for _ in range(8):
            bit = sr & 1  # output = x^0 tap
            byte = (byte << 1) | bit
            fb = (sr ^ (sr >> 3) ^ (sr >> 5) ^ (sr >> 7)) & 1
            sr = (sr >> 1) | (fb << 7)
        out[i] = byte
    return out


PN = _pn_sequence(1020)
assert PN[:4].tolist() == [0xFF, 0x48, 0x0E, 0xC0], "CCSDS PN generator mismatch"

_ASM_BITS = np.unpackbits(np.frombuffer(ASM.to_bytes(4, "big"), dtype=np.uint8)).astype(np.int8)
_ASM_PM = (2 * _ASM_BITS.astype(np.int16) - 1)


def nrzm_decode(bits: np.ndarray, last: int) -> tuple[np.ndarray, int]:
    """NRZ-M differential decoding: out[n] = in[n] xor in[n-1]."""
    if len(bits) == 0:
        return bits, last
    prev = np.empty_like(bits)
    prev[0] = last
    prev[1:] = bits[:-1]
    return (bits ^ prev).astype(np.uint8), int(bits[-1])


class Deframer:
    """Finds the 0x1ACFFC1D attached sync marker in a bit stream and cuts 1024-byte CADUs."""

    def __init__(self, search_tol: int = 2, lock_tol: int = 8, max_miss: int = 4):
        self.buf = np.zeros(0, dtype=np.uint8)
        self.locked = False
        self.miss = 0
        self.search_tol = search_tol
        self.lock_tol = lock_tol
        self.max_miss = max_miss

    @staticmethod
    def _errors_at(buf: np.ndarray, pos: int) -> int:
        return int(np.count_nonzero(buf[pos:pos + 32] != _ASM_BITS))

    def push(self, bits: np.ndarray) -> list[bytes]:
        self.buf = np.concatenate([self.buf, bits.astype(np.uint8)])
        frames = []
        while True:
            if not self.locked:
                if len(self.buf) < 32:
                    break
                pm = 2 * self.buf.astype(np.int16) - 1
                corr = np.correlate(pm, _ASM_PM, mode="valid")  # 32 - 2*errors
                hits = np.nonzero(corr >= 32 - 2 * self.search_tol)[0]
                if len(hits) == 0:
                    self.buf = self.buf[-31:]
                    break
                self.buf = self.buf[hits[0]:]
                self.locked = True
                self.miss = 0
            if len(self.buf) < CADU_BITS + 32:
                break
            frame_bits = self.buf[:CADU_BITS]
            nxt = self._errors_at(self.buf, CADU_BITS)
            if self._errors_at(self.buf, 0) <= self.lock_tol:
                frames.append(np.packbits(frame_bits).tobytes())
            if nxt <= self.lock_tol:
                self.miss = 0
                self.buf = self.buf[CADU_BITS:]
            else:
                self.miss += 1
                if self.miss >= self.max_miss:
                    self.locked = False
                    self.buf = self.buf[1:]
                else:
                    self.buf = self.buf[CADU_BITS:]
        return frames


def cadu_to_vcdu(cadu: bytes):
    """Derandomise + RS(255,223)x4.

    Returns (vcdu bytes (892), ok, errs, corrected CADU (ASM + 1020 bytes, SatDump .cadu layout))."""
    blk = np.frombuffer(cadu, dtype=np.uint8)[4:].copy()
    blk ^= PN
    ok, errs = core.rs_decode_interleaved(blk, 4)
    return blk[:892].tobytes(), ok, errs, ASM.to_bytes(4, "big") + blk.tobytes()


def process_cadu_file(path: str, imager: "MsuMrImager", progress=None) -> int:
    """Re-process a SatDump-compatible .cadu file (1024-byte frames, derandomised, RS-corrected)."""
    demux = Demuxer()
    n = 0
    size = max(1, __import__("os").path.getsize(path))
    with open(path, "rb") as f:
        while True:
            fr = f.read(CADU_BYTES)
            if len(fr) < CADU_BYTES:
                break
            v = Vcdu(fr[4:896])
            n += 1
            if v.vcid == 5:
                for pkt in demux.work(v):
                    imager.work(pkt)
            if progress and n % 200 == 0:
                progress(f.tell() / size)
    return n


class Vcdu:
    __slots__ = ("version", "scid", "vcid", "counter", "fhp", "data")

    def __init__(self, b: bytes):
        self.version = b[0] >> 6
        self.scid = ((b[0] & 0x3F) << 2) | (b[1] >> 6)
        self.vcid = b[1] & 0x3F
        self.counter = (b[2] << 16) | (b[3] << 8) | b[4]
        self.fhp = ((b[8] & 0x07) << 8) | b[9]
        self.data = b[10:10 + VCDU_DATA]


class Packet:
    __slots__ = ("apid", "seq_flag", "seq", "length", "payload")

    def __init__(self, raw: bytes):
        self.apid = ((raw[0] & 0x07) << 8) | raw[1]
        self.seq_flag = raw[2] >> 6
        self.seq = ((raw[2] & 0x3F) << 8) | raw[3]
        self.length = ((raw[4] << 8) | raw[5]) + 1
        self.payload = raw[6:]


class Demuxer:
    """CCSDS AOS M_PDU packet extraction (virtual channel continuity checked)."""

    def __init__(self):
        self.cur: bytearray | None = None
        self.last_counter: int | None = None

    def _drain(self, final: bool = False) -> list[Packet]:
        out = []
        while self.cur is not None and len(self.cur) >= 6:
            total = 6 + ((self.cur[4] << 8) | self.cur[5]) + 1
            if len(self.cur) >= total:
                out.append(Packet(bytes(self.cur[:total])))
                del self.cur[:total]
            else:
                break
        if final:
            self.cur = None
        return out

    def work(self, v: Vcdu) -> list[Packet]:
        out: list[Packet] = []
        if self.last_counter is not None and v.counter != ((self.last_counter + 1) & 0xFFFFFF):
            self.cur = None  # lost frame(s): resync on next header pointer
        self.last_counter = v.counter
        if v.fhp == 0x7FF:
            if self.cur is not None:
                self.cur += v.data
                out += self._drain()
            return out
        if v.fhp >= VCDU_DATA:
            self.cur = None
            return out
        if self.cur is not None:
            self.cur += v.data[:v.fhp]
            out += self._drain(final=True)
        self.cur = bytearray(v.data[v.fhp:])
        out += self._drain()
        return out


# ---------------------------------------------------------------------------
# MSU-MR JPEG-like MCU decoding
# ---------------------------------------------------------------------------
QTABLE = np.array([
    16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99], dtype=np.float64)

ZIGZAG = np.array([
    0, 1, 5, 6, 14, 15, 27, 28, 2, 4, 7, 13, 16, 26, 29, 42,
    3, 8, 12, 17, 25, 30, 41, 43, 9, 11, 18, 24, 31, 40, 44, 53,
    10, 19, 23, 32, 39, 45, 52, 54, 20, 22, 33, 38, 46, 51, 55, 60,
    21, 34, 37, 47, 50, 56, 59, 61, 35, 36, 48, 49, 57, 58, 62, 63], dtype=np.int64)

_DC_CODES = {}  # (len, code) -> category
for _cat, (_len, _code) in enumerate([(2, 0b00), (3, 0b010), (3, 0b011), (3, 0b100), (3, 0b101), (3, 0b110),
                                      (4, 0b1110), (5, 0b11110), (6, 0b111110), (7, 0b1111110),
                                      (8, 0b11111110), (9, 0b111111110)]):
    _DC_CODES[(_len, _code)] = _cat

_AC_BITS = [0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 125]  # count per length 1..16
_AC_VALS = [
    0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06, 0x13, 0x51, 0x61, 0x07,
    0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xa1, 0x08, 0x23, 0x42, 0xb1, 0xc1, 0x15, 0x52, 0xd1, 0xf0,
    0x24, 0x33, 0x62, 0x72, 0x82, 0x09, 0x0a, 0x16, 0x17, 0x18, 0x19, 0x1a, 0x25, 0x26, 0x27, 0x28,
    0x29, 0x2a, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3a, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49,
    0x4a, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59, 0x5a, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69,
    0x6a, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78, 0x79, 0x7a, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
    0x8a, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9a, 0xa2, 0xa3, 0xa4, 0xa5, 0xa6, 0xa7,
    0xa8, 0xa9, 0xaa, 0xb2, 0xb3, 0xb4, 0xb5, 0xb6, 0xb7, 0xb8, 0xb9, 0xba, 0xc2, 0xc3, 0xc4, 0xc5,
    0xc6, 0xc7, 0xc8, 0xc9, 0xca, 0xd2, 0xd3, 0xd4, 0xd5, 0xd6, 0xd7, 0xd8, 0xd9, 0xda, 0xe1, 0xe2,
    0xe3, 0xe4, 0xe5, 0xe6, 0xe7, 0xe8, 0xe9, 0xea, 0xf1, 0xf2, 0xf3, 0xf4, 0xf5, 0xf6, 0xf7, 0xf8,
    0xf9, 0xfa]
assert len(_AC_VALS) == 162 and sum(_AC_BITS) == 162


def _canonical(bits_per_len, vals):
    table = {}
    enc = {}
    code = 0
    k = 0
    for length in range(1, 17):
        for _ in range(bits_per_len[length - 1]):
            table[(length, code)] = vals[k]
            enc[vals[k]] = (length, code)
            code += 1
            k += 1
        code <<= 1
    return table, enc


AC_DECODE, AC_ENCODE = _canonical(_AC_BITS, _AC_VALS)
DC_ENCODE = {v: k for k, v in _DC_CODES.items()}

# IDCT basis
_C = np.array([np.sqrt(0.5)] + [1.0] * 7)
_T = np.array([[_C[u] / 2 * np.cos((2 * x + 1) * u * np.pi / 16) for x in range(8)] for u in range(8)])


def quant_table(qf: int) -> np.ndarray:
    q = float(qf)
    q = 5000.0 / q if 20 <= q < 50 else 200.0 - 2.0 * q
    t = np.floor(q / 100.0 * QTABLE + 0.5)
    t[t < 1] = 1
    return t


def _extend(v: int, cat: int) -> int:
    return v if (v >> (cat - 1)) & 1 else v - (1 << cat) + 1


class _BitReader:
    __slots__ = ("val", "nbits", "pos")

    def __init__(self, data: bytes):
        self.val = int.from_bytes(data, "big")
        self.nbits = len(data) * 8
        self.pos = 0

    def peek(self, n: int) -> int:
        end = self.pos + n
        if end > self.nbits:
            if self.pos >= self.nbits:
                raise EOFError
            rem = self.nbits - self.pos  # pad with zeros past the end
            return (self.val & ((1 << rem) - 1)) << (n - rem)
        return (self.val >> (self.nbits - end)) & ((1 << n) - 1)

    def read(self, n: int) -> int:
        if n == 0:
            return 0
        if self.pos + n > self.nbits:
            raise EOFError
        v = self.peek(n)
        self.pos += n
        return v


def decode_segment_blocks(data: bytes, qf: int):
    """Decode up to 14 MCUs. Returns (pixels uint8 8x112, n_valid_mcus)."""
    br = _BitReader(data)
    q = quant_table(qf)
    out = np.zeros((8, 112), dtype=np.uint8)
    last_dc = 0
    n_ok = 0
    try:
        for m in range(14):
            # DC
            cat = None
            for length in range(2, 10):
                c = br.peek(length)
                cat = _DC_CODES.get((length, c))
                if cat is not None:
                    br.pos += length
                    break
            if cat is None:
                break
            diff = _extend(br.read(cat), cat) if cat else 0
            last_dc += diff
            zz = np.zeros(64, dtype=np.float64)
            zz[0] = last_dc
            k = 1
            while k < 64:
                sym = None
                for length in range(2, 17):
                    c = br.peek(length)
                    sym = AC_DECODE.get((length, c))
                    if sym is not None:
                        br.pos += length
                        break
                if sym is None:
                    raise ValueError("bad AC code")
                if sym == 0x00:
                    break
                run, size = sym >> 4, sym & 0x0F
                k += run
                if k > 63:
                    raise ValueError("AC overrun")
                zz[k] = _extend(br.read(size), size) if size else 0
                k += 1
            nat = zz[ZIGZAG] * q
            blk = _T.T @ nat.reshape(8, 8) @ _T
            out[:, m * 8:(m + 1) * 8] = np.clip(np.rint(blk + 128.0), 0, 255).astype(np.uint8)
            n_ok += 1
    except (EOFError, ValueError):
        pass
    return out, n_ok


def encode_segment_blocks(pixels: np.ndarray, qf: int) -> bytes:
    """Inverse of decode_segment_blocks (used by the test harness only)."""
    q = quant_table(qf)
    bits: list[str] = []
    last_dc = 0
    inv = np.argsort(ZIGZAG)  # zigzag index -> natural position
    for m in range(14):
        blk = pixels[:, m * 8:(m + 1) * 8].astype(np.float64) - 128.0
        coef = _T @ blk @ _T.T
        nat = np.rint(coef.reshape(64) / q).astype(np.int64)
        zz = nat[inv]
        dc = int(zz[0])
        diff = dc - last_dc
        last_dc = dc
        cat = 0 if diff == 0 else int(abs(diff)).bit_length()
        ln, code = DC_ENCODE[cat]
        bits.append(format(code, f"0{ln}b"))
        if cat:
            v = diff if diff > 0 else diff + (1 << cat) - 1
            bits.append(format(v, f"0{cat}b"))
        run = 0
        last_nz = max([i for i in range(1, 64) if zz[i] != 0], default=0)
        for i in range(1, last_nz + 1):
            a = int(zz[i])
            if a == 0:
                run += 1
                continue
            while run > 15:
                ln, code = AC_ENCODE[0xF0]
                bits.append(format(code, f"0{ln}b"))
                run -= 16
            size = abs(a).bit_length()
            ln, code = AC_ENCODE[(run << 4) | size]
            bits.append(format(code, f"0{ln}b"))
            v = a if a > 0 else a + (1 << size) - 1
            bits.append(format(v, f"0{size}b"))
            run = 0
        if last_nz < 63:
            ln, code = AC_ENCODE[0x00]
            bits.append(format(code, f"0{ln}b"))
    s = "".join(bits)
    s += "1" * ((-len(s)) % 8)
    return int(s, 2).to_bytes(len(s) // 8, "big") if s else b""


# ---------------------------------------------------------------------------
# MSU-MR image assembly (alignment logic ported from SatDump lrpt_msumr_reader)
# ---------------------------------------------------------------------------
class MsuMrImager:
    NONE = 4294967295

    def __init__(self):
        self.segments: list[dict[int, tuple[np.ndarray, bool]]] = [dict() for _ in range(6)]
        self.first = [self.NONE] * 6
        self.last = [0] * 6
        self.offset = [self.NONE] * 6
        self.rollover = [0] * 6
        self.last_seq = [0] * 6
        self.timestamps: list[float] = []
        # line group (8 image rows) -> MSU-MR UTC time, PER CHANNEL. It has to be per channel: the
        # segment ids of the channels below the lowest-offset one are shifted by 14 relative to the
        # others (that is exactly what _extent corrects for), so one shared table would hand those
        # channels a time one line period early - eight rows, about 8 km on the ground.
        self.line_time: list[dict[int, float]] = [dict() for _ in range(6)]
        self.packets = 0
        self.dirty = False

    @property
    def channels_present(self) -> list[int]:
        return [i for i in range(6) if self.segments[i]]

    def work(self, pkt: Packet):
        ch = MSUMR_APIDS.get(pkt.apid)
        if ch is None:
            return
        p = pkt.payload
        if len(p) <= 14:
            return
        mcun, qt, dcac, qfm, qf = p[8], p[9], p[10], (p[11] << 8) | p[12], p[13]
        if qt != 0 or dcac != 0 or qfm != 0xFFF0 or qf == 0 or mcun % 14 or mcun > 182:
            return
        pix, nok = decode_segment_blocks(p[14:], qf)
        if nok == 0:
            return
        partial = nok < 14
        seq = pkt.seq
        mcu_count = mcun // 14
        if self.last_seq[ch] > seq and self.last_seq[ch] > 13926 and seq < 2458:
            self.rollover[ch] += 16384
        if self.offset[ch] == self.NONE:
            mcu_seq = seq + (16384 if mcu_count > seq else 0) - mcu_count
            self.offset[ch] = (mcu_seq + self.rollover[ch]) % 43
        sid = ((seq + self.rollover[ch] - self.offset[ch]) // 43) * 14 + mcu_count
        nf = min(self.first[ch], sid)
        nl = max(self.last[ch], sid)
        if nl - nf > SEG_CNT:
            return
        self.first[ch], self.last[ch], self.last_seq[ch] = nf, nl, seq
        self.segments[ch][sid] = (pix, partial)
        self.packets += 1
        self.dirty = True
        day = (p[0] << 8) | p[1]
        ms = (p[2] << 24) | (p[3] << 16) | (p[4] << 8) | p[5]
        ts = (11322 + day) * 86400.0 + ms / 1000.0      # MSU-MR clock, UNIX seconds UTC
        self.timestamps.append(ts)
        self.line_time[ch].setdefault(sid // 14, ts)

    def _extent(self, channel: int) -> tuple[int, int]:
        """(first segment id, number of line groups) of the assembled image for one channel."""
        NONE = self.NONE
        fl = NONE; fb = NONE; fa = NONE
        ll = 0; lb = 0; la = 0
        low_off = 6; low_tx = 6
        for i in range(6):
            if low_tx == 6:
                low_tx = i
            if self.offset[i] < (43 if low_off == 6 else self.offset[low_off]):
                low_off = i
        for i in range(6):
            if self.offset[i] == NONE:
                continue
            fl = min(fl, self.first[i]); ll = max(ll, self.last[i])
            if i < low_off:
                fb = min(fb, self.first[i]); lb = max(lb, self.last[i])
            else:
                fa = min(fa, self.first[i]); la = max(la, self.last[i])
        if low_tx != low_off:
            first_dir = (fb - fb % 14) >= (fa - fa % 14)
            last_dir = (lb - lb % 14) < (la - la % 14)
            if channel < low_off:
                if first_dir:
                    fl -= 14
                if last_dir:
                    ll -= 14
            else:
                if not first_dir:
                    fl += 14
                if not last_dir:
                    ll += 14
        ll += 14
        if self.first[channel] == NONE:
            fl = 0
        if self.last[channel] == 0:
            ll = 0
        fl -= fl % 14
        ll -= ll % 14
        return fl, max(0, (ll - fl) // 14)

    def get_channel(self, channel: int) -> np.ndarray:
        fl, nlines = self._extent(channel)
        img = np.zeros((nlines * 8, IMG_WIDTH), dtype=np.uint8)
        segs = self.segments[channel]
        for li in range(nlines):
            base = fl + li * 14
            for j in range(14):
                s = segs.get(base + j)
                if s is not None:
                    img[li * 8:(li + 1) * 8, j * 112:(j + 1) * 112] = s[0]
        return img

    def row_times(self, channel: int) -> np.ndarray:
        """UTC time (UNIX seconds) of every row of get_channel(channel).

        The MSU-MR stamps each scan line, so time is linear in the line group; a least-squares fit over the
        groups actually received (clipped to the robust central band, so a corrupt stamp cannot tilt it)
        gives a time for every row, including rows whose own packets were lost."""
        fl, nlines = self._extent(channel)
        if nlines <= 0:
            return np.zeros(0)
        stamps = self.line_time[channel]
        g = np.array(sorted(stamps), dtype=np.float64)
        if g.size < 2:
            t0 = self.first_timestamp() or 0.0
            return np.full(nlines * 8, t0)
        t = np.array([stamps[int(k)] for k in g], dtype=np.float64)
        a, b = np.polyfit(g, t, 1)
        res = t - (a * g + b)
        keep = np.abs(res - np.median(res)) < 5.0 * (np.median(np.abs(res - np.median(res))) + 1e-3)
        if keep.sum() >= 2:
            a, b = np.polyfit(g[keep], t[keep], 1)
        rows = np.repeat(np.arange(nlines, dtype=np.float64), 8) + (fl / 14.0)
        rows += np.tile(np.arange(8) / 8.0, nlines)          # within a group, 8 scan lines
        return a * rows + b

    def first_timestamp(self) -> float | None:
        return float(np.median(self.timestamps[:50])) if self.timestamps else None


def fill_missing(img: np.ndarray, max_lines: int = 50) -> np.ndarray:
    """Vertical interpolation over black (missing) 8-line strips, per column block."""
    out = img.copy()
    h = img.shape[0]
    if h < 16:
        return out
    blocks = img.reshape(h // 8, 8, 14, 112)
    missing = (blocks.max(axis=(1, 3)) == 0)  # [line, segment]
    for seg in range(14):
        col = missing[:, seg]
        i = 0
        nl = len(col)
        while i < nl:
            if col[i]:
                j = i
                while j < nl and col[j]:
                    j += 1
                if i > 0 and j < nl and (j - i) * 8 <= max_lines:
                    top = out[i * 8 - 1, seg * 112:(seg + 1) * 112].astype(np.float32)
                    bot = out[j * 8, seg * 112:(seg + 1) * 112].astype(np.float32)
                    n = (j - i) * 8
                    for k in range(n):
                        w = (k + 1) / (n + 1)
                        out[i * 8 + k, seg * 112:(seg + 1) * 112] = np.rint(top * (1 - w) + bot * w).astype(np.uint8)
                i = j
            else:
                i += 1
    return out

"""TLE retrieval (CelesTrak), SGP4 propagation, pass prediction and Doppler for Meteor-M."""
from __future__ import annotations

import json
import math
import os
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
from sgp4.api import Satrec, jday

C_LIGHT = 299792458.0
WGS84_A = 6378.137
WGS84_F = 1 / 298.257223563
OMEGA_E = 7.2921150e-5  # rad/s

# TLE sources tried in order; {norad} is replaced by the catalogue number.
DEFAULT_TLE_URLS = [
    "https://celestrak.org/NORAD/elements/gp.php?CATNR={norad}&FORMAT=TLE",
    "https://db.satnogs.org/api/tle/?norad_cat_id={norad}&format=json",
]


@dataclass
class Satellite:
    name: str
    norad: int
    freqs_mhz: list          # MHz, not Hz: the receiver scales them when it tunes
    mode: str = "auto"
    tle: tuple | None = None   # (line1, line2)
    sat: Satrec | None = None

    def load(self, l1: str, l2: str):
        self.tle = (l1, l2)
        sat = Satrec.twoline2rv(l1, l2)
        # Guard against a broken C extension (seen under Wine): fall back to the pure-Python SGP4
        if not (sat.no_kozai > 0) or sat.sgp4(sat.jdsatepoch, sat.jdsatepochF)[0] != 0:
            from sgp4.model import Satrec as PySatrec
            sat = PySatrec.twoline2rv(l1, l2)
        self.sat = sat

    @property
    def tle_epoch(self) -> datetime | None:
        if self.sat is None:
            return None
        jd = self.sat.jdsatepoch + self.sat.jdsatepochF
        return datetime(2000, 1, 1, 12, tzinfo=timezone.utc) + timedelta(days=jd - 2451545.0)


@dataclass
class Pass:
    sat: Satellite
    aos: datetime
    los: datetime
    tca: datetime
    max_el: float
    northbound: bool
    aos_az: float = 0.0
    los_az: float = 0.0

    @property
    def duration_s(self) -> float:
        return (self.los - self.aos).total_seconds()


# ----------------------------------------------------------------------------
# TLE management
# ----------------------------------------------------------------------------
class TleError(Exception):
    def __init__(self, msg: str, offline: bool = False):
        super().__init__(msg)
        self.offline = offline   # network unreachable: no point trying other URLs/satellites


def _ssl_context() -> ssl.SSLContext:
    """System trust store (Windows certificate store) + Mozilla bundle from certifi.

    A frozen Python on Windows only sees root certificates already present in the local
    store; Windows installs some roots lazily, so a rarely used PC may lack the one the
    TLE server needs. Adding the certifi bundle removes that dependency."""
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except Exception:  # noqa: BLE001
        pass
    return ctx


def describe_error(e: BaseException) -> tuple[str, bool]:
    """Human readable reason and whether it means 'no network'."""
    if isinstance(e, urllib.error.HTTPError):
        hint = " (server refused: rate limit or blocked address)" if e.code in (403, 429) else ""
        return f"HTTP {e.code} {e.reason}{hint}", False
    r = e.reason if isinstance(e, urllib.error.URLError) else e
    if isinstance(r, ssl.SSLCertVerificationError):
        return f"SSL certificate not trusted: {getattr(r, 'verify_message', None) or r}", False
    if isinstance(r, ssl.SSLError):
        return f"SSL error: {r}", False
    if isinstance(r, socket.gaierror):
        return "DNS lookup failed - no Internet connection?", True
    if isinstance(r, (TimeoutError, socket.timeout)):
        return "timeout - no Internet connection or firewall?", True
    if "Tunnel connection failed" in str(r):
        return f"proxy refused the connection ({r})", True
    if isinstance(r, ConnectionRefusedError):
        return "connection refused (proxy/firewall?)", True
    if isinstance(r, OSError):
        return f"network error: {r.strerror or r}", True
    if "Tunnel connection failed" in str(r):
        return f"proxy refused the connection ({r})", True
    return str(r), False


def tle_checksum_ok(line: str) -> bool:
    if len(line) < 69:
        return False
    s = sum(int(c) if c.isdigit() else (1 if c == "-" else 0) for c in line[:68])
    return line[68].isdigit() and s % 10 == int(line[68])


def valid_tle(l1: str, l2: str, norad: int | None = None) -> bool:
    l1, l2 = l1.rstrip(), l2.rstrip()
    if not (l1.startswith("1 ") and l2.startswith("2 ") and tle_checksum_ok(l1) and tle_checksum_ok(l2)):
        return False
    try:
        n1, n2 = int(l1[2:7]), int(l2[2:7])
        Satrec.twoline2rv(l1, l2)
    except Exception:  # noqa: BLE001
        return False
    return n1 == n2 and (norad is None or n1 == norad)


def parse_tle_text(txt: str) -> dict[int, tuple[str, str]]:
    """Extract every valid TLE from 2-line or 3-line text."""
    lines = [x.rstrip() for x in txt.replace("\r", "").splitlines() if x.strip()]
    out = {}
    for i in range(len(lines) - 1):
        if lines[i].startswith("1 ") and lines[i + 1].startswith("2 ") and valid_tle(lines[i], lines[i + 1]):
            out[int(lines[i][2:7])] = (lines[i], lines[i + 1])
    return out


def _http_get(url: str, timeout: float, proxy: str = "") -> str:
    handlers = [urllib.request.HTTPSHandler(context=_ssl_context())]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": "MeteorRX/1.1 (+amateur weather station)",
                                               "Accept": "text/plain, application/json"})
    with opener.open(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _qt_get(url: str, timeout: float) -> str:
    """HTTP GET through QtNetwork: on Windows this is Schannel (the TLS stack and certificate
    store used by Edge), with the system proxy configuration. Independent of Python's ssl."""
    from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer, QUrl
    from PySide6.QtNetwork import (QNetworkAccessManager, QNetworkProxyFactory, QNetworkReply, QNetworkRequest,
                                   QSslSocket)
    if QCoreApplication.instance() is None:
        raise OSError("Qt network not available (no application)")
    if os.name == "nt" and "schannel" in QSslSocket.availableBackends():
        QSslSocket.setActiveBackend("schannel")
    QNetworkProxyFactory.setUseSystemConfiguration(True)
    nam = QNetworkAccessManager()
    req = QNetworkRequest(QUrl(url))
    req.setHeader(QNetworkRequest.UserAgentHeader, "MeteorRX/1.2 (+amateur weather station)")
    req.setTransferTimeout(int(timeout * 1000))
    reply = nam.get(req)
    loop = QEventLoop()
    reply.finished.connect(loop.quit)
    QTimer.singleShot(int(timeout * 1000) + 500, loop.quit)
    loop.exec()
    try:
        if not reply.isFinished():
            reply.abort()
            raise TimeoutError("timeout")
        if reply.error() != QNetworkReply.NetworkError.NoError:
            code = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
            raise OSError(f"HTTP {code}: {reply.errorString()}" if code else reply.errorString())
        return bytes(reply.readAll().data()).decode("utf-8", "replace")
    finally:
        reply.deleteLater()
        nam.deleteLater()


def _parse_answer(body: str, norad: int) -> tuple[str, str]:
    b = body.lstrip()
    if b.startswith("[") or b.startswith("{"):
        try:
            data = json.loads(b)
        except ValueError as e:
            raise TleError("invalid JSON answer") from e
        for item in (data if isinstance(data, list) else [data]):
            l1, l2 = item.get("tle1", ""), item.get("tle2", "")
            if valid_tle(l1, l2, norad):
                return l1.rstrip(), l2.rstrip()
        raise TleError("no TLE for this satellite in the answer")
    found = parse_tle_text(body)
    if norad in found:
        return found[norad]
    snippet = b[:60].replace("\n", " ")
    raise TleError(f"no valid TLE in the answer ({snippet!r})")


USE_QT_NETWORK = True


def fetch_tle(norad: int, url: str, timeout: float = 8.0, proxy: str = "") -> tuple[str, str, str]:
    """Download one TLE (plain TLE text or SatNOGS JSON). Tries Python's HTTPS stack, then the
    Windows/Qt one. Returns (line1, line2, stack used)."""
    u = url.format(norad=norad)
    try:
        return (*_parse_answer(_http_get(u, timeout, proxy), norad), "python")
    except TleError:
        raise
    except Exception as e:  # noqa: BLE001
        msg1, off1 = describe_error(e)
    if not USE_QT_NETWORK:
        raise TleError(msg1, off1)
    try:
        return (*_parse_answer(_qt_get(u, timeout), norad), "system")
    except TleError:
        raise
    except Exception as e:  # noqa: BLE001
        raise TleError(f"{msg1}; system stack: {e}", off1) from e


def read_tle_file(path: str) -> dict[int, tuple[str, str]]:
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return parse_tle_text(f.read())


def write_tle_cache(path: str, sats: list[Satellite], cache: dict[int, tuple[str, str]]):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="ascii") as f:
        for s in sats:
            if s.norad in cache:
                f.write(f"{s.name}\n{cache[s.norad][0]}\n{cache[s.norad][1]}\n")
    os.replace(tmp, path)


def tle_age_days(l1: str) -> float:
    yy, doy = int(l1[18:20]), float(l1[20:32])
    year = 2000 + yy if yy < 57 else 1900 + yy
    epoch = datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=doy - 1)
    return (datetime.now(timezone.utc) - epoch).total_seconds() / 86400


def _attempt_state(cache_path: str) -> tuple[str, dict]:
    """When the servers were last contacted, and when that last worked. Survives a restart."""
    path = cache_path + ".state"
    try:
        with open(path, encoding="utf-8") as f:
            return path, json.load(f)
    except (OSError, ValueError):
        return path, {}


def _note_attempt(path: str, ok: bool) -> None:
    now = time.time()
    _, st = _attempt_state(path[: -len(".state")])
    st["last_attempt"] = now
    if ok:
        st["last_ok"] = now
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except OSError:
        pass            # a read-only data folder only costs us the rate limiting, not the TLEs


def update_tles(sats: list[Satellite], cache_path: str, max_age_h: float, log=print,
                urls: list[str] | None = None, proxy: str = "", force: bool = False,
                refresh_days: float = 0.0, retry_h: float = 1.0) -> bool:
    """Load cached TLEs and refresh them when older than max_age_h.

    Each satellite is tried on every source in turn; a network-level failure (no DNS,
    timeout) stops the attempt immediately. The cache is kept on failure.

    Only the satellites that actually need it are downloaded. This matters: one satellite
    missing from the cache used to mark the whole set as needing a refresh, and because a
    station that never gets that one keeps retrying, every retry re-downloaded all of them.
    CelesTrak asks programs not to do that. `refresh_days` sets a floor on how often the
    servers are contacted at all, and `retry_h` keeps a station with no internet from
    knocking every quarter of an hour. An explicit "Update now" (force) ignores both.

    Returns True when every satellite has a usable TLE."""
    urls = urls or DEFAULT_TLE_URLS
    cache = read_tle_file(cache_path)
    age_h = (time.time() - os.path.getmtime(cache_path)) / 3600 if os.path.isfile(cache_path) else 1e9
    missing = [s for s in sats if s.norad not in cache]
    stale = age_h > max_age_h
    need = force or stale or bool(missing)
    state_path, state = _attempt_state(cache_path)
    if need and not force:
        now = time.time()
        waited_ok = (now - state.get("last_ok", 0.0)) / 3600.0
        waited_try = (now - state.get("last_attempt", 0.0)) / 3600.0
        if refresh_days > 0 and waited_ok < refresh_days * 24.0:
            need = False
            if missing:         # silence when everything is simply still fresh enough
                log(f"TLE: next download in {refresh_days * 24.0 - waited_ok:.0f} h "
                    f"([tle] refresh_days = {refresh_days:g})")
        elif waited_try < retry_h and state.get("last_attempt", 0.0) > state.get("last_ok", 0.0):
            need = False        # the last try failed; do not knock again yet
    if need:
        # only what is actually out of date: a stale cache ages every satellite at once,
        # otherwise just the ones we have nothing for
        targets = list(sats) if (force or stale) else missing
        fresh = {}
        dead = {}   # host -> reason: unreachable hosts are not retried for the other satellites
        for s in targets:
            errors = []
            for url in urls:
                host = urllib.parse.urlsplit(url).hostname or url
                if host in dead:
                    errors.append(f"{host}: {dead[host]}")
                    continue
                try:
                    l1, l2, via = fetch_tle(s.norad, url, proxy=proxy)
                    fresh[s.norad] = (l1, l2)
                    log(f"TLE {s.name}: downloaded from {host}" + (" (Windows network stack)" if via == "system" else ""))
                    break
                except TleError as e:
                    errors.append(f"{host}: {e}")
                    if e.offline:
                        dead[host] = str(e)
            else:
                log(f"TLE {s.name}: download failed - " + " | ".join(errors))
        _note_attempt(state_path, ok=len(fresh) == len(targets))
        if fresh:
            cache.update(fresh)
            try:
                write_tle_cache(cache_path, sats, cache)
            except OSError as e:
                log(f"TLE cache not written ({e})")
    ok = True
    for s in sats:
        if s.norad in cache:
            s.load(*cache[s.norad])
            age = tle_age_days(cache[s.norad][0])
            if age > 10:
                log(f"TLE {s.name}: {age:.0f} days old - pass times may be off; update or import TLEs")
        else:
            ok = False
            log(f"TLE {s.name}: none available - use 'TLE > Import TLE file' (e.g. celestrak weather.txt)")
    return ok


def import_tle_file(path: str, sats: list[Satellite], cache_path: str, log=print) -> int:
    """Merge the configured satellites found in a user TLE file into the cache."""
    found = read_tle_file(path)
    cache = read_tle_file(cache_path)
    n = 0
    for s in sats:
        if s.norad in found:
            cache[s.norad] = found[s.norad]
            s.load(*found[s.norad])
            n += 1
            log(f"TLE {s.name}: imported ({tle_age_days(found[s.norad][0]):.1f} days old)")
    if n:
        write_tle_cache(cache_path, sats, cache)
    else:
        log(f"No TLE for {', '.join(str(s.norad) for s in sats)} in {os.path.basename(path)}")
    return n


# ----------------------------------------------------------------------------
# Geometry
# ----------------------------------------------------------------------------
def _gmst(jd_ut1: float) -> float:
    t = (jd_ut1 - 2451545.0) / 36525.0
    g = (67310.54841 + (876600.0 * 3600 + 8640184.812866) * t + 0.093104 * t * t - 6.2e-6 * t ** 3)
    return math.radians((g % 86400.0) / 240.0)


def _observer_ecef(lat_deg: float, lon_deg: float, alt_m: float) -> np.ndarray:
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    e2 = WGS84_F * (2 - WGS84_F)
    n = WGS84_A / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    h = alt_m / 1000.0
    return np.array([(n + h) * math.cos(lat) * math.cos(lon), (n + h) * math.cos(lat) * math.sin(lon),
                     (n * (1 - e2) + h) * math.sin(lat)])


class Observer:
    def __init__(self, lat: float, lon: float, alt_m: float):
        self.lat, self.lon, self.alt = lat, lon, alt_m
        self.ecef = _observer_ecef(lat, lon, alt_m)
        la, lo = math.radians(lat), math.radians(lon)
        # ECEF -> ENU rotation
        self.R = np.array([[-math.sin(lo), math.cos(lo), 0],
                           [-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo), math.cos(la)],
                           [math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)]])

    def look(self, sat: Satellite, when: datetime):
        """Returns (az_deg, el_deg, range_km, range_rate_km_s, sat_lat_deg)."""
        jd, fr = jday(when.year, when.month, when.day, when.hour, when.minute,
                      when.second + when.microsecond * 1e-6)
        err, r, v = sat.sat.sgp4(jd, fr)
        if err:
            raise ValueError(f"SGP4 error {err}")
        th = _gmst(jd + fr)
        c, s = math.cos(th), math.sin(th)
        rot = np.array([[c, s, 0], [-s, c, 0], [0, 0, 1]])
        r_e = rot @ np.array(r)
        v_e = rot @ np.array(v) - np.cross([0, 0, OMEGA_E], r_e)
        d = r_e - self.ecef
        rng = float(np.linalg.norm(d))
        enu = self.R @ d
        az = (math.degrees(math.atan2(enu[0], enu[1])) + 360) % 360
        el = math.degrees(math.asin(enu[2] / rng))
        rr = float(np.dot(d, v_e) / rng)
        lat = math.degrees(math.atan2(r_e[2], math.hypot(r_e[0], r_e[1])))
        return az, el, rng, rr, lat

    def doppler(self, sat: Satellite, when: datetime, freq_hz: float) -> float:
        return -self.look(sat, when)[3] * 1000.0 / C_LIGHT * freq_hz


def predict_passes(obs: Observer, sats: list[Satellite], start: datetime, hours: float,
                   min_el: float) -> list[Pass]:
    out: list[Pass] = []
    step = 30.0
    for s in sats:
        if s.sat is None:
            continue
        t = start
        end = start + timedelta(hours=hours)
        prev_el = obs.look(s, t)[1]
        while t < end:
            t2 = t + timedelta(seconds=step)
            el2 = obs.look(s, t2)[1]
            if prev_el < 0 <= el2:
                aos = _bisect(obs, s, t, t2, rising=True)
                # walk to LOS
                tt = t2
                while obs.look(s, tt)[1] >= 0 and tt < end + timedelta(hours=1):
                    tt += timedelta(seconds=step)
                los = _bisect(obs, s, tt - timedelta(seconds=step), tt, rising=False)
                tca, mel = _max_el(obs, s, aos, los)
                if mel >= min_el:
                    lat_a = obs.look(s, aos)[4]
                    lat_l = obs.look(s, los)[4]
                    out.append(Pass(s, aos, los, tca, mel, lat_l > lat_a,
                                    obs.look(s, aos)[0], obs.look(s, los)[0]))
                t = los
                prev_el = obs.look(s, t)[1]
                continue
            t, prev_el = t2, el2
    out.sort(key=lambda p: p.aos)
    return out


def _bisect(obs, s, a, b, rising: bool):
    for _ in range(20):
        m = a + (b - a) / 2
        el = obs.look(s, m)[1]
        if (el >= 0) == rising:
            b = m
        else:
            a = m
    return a + (b - a) / 2


def _max_el(obs, s, aos, los):
    n = 60
    best = (aos, -90.0)
    for i in range(n + 1):
        t = aos + (los - aos) * (i / n)
        el = obs.look(s, t)[1]
        if el > best[1]:
            best = (t, el)
    return best

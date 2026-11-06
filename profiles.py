"""Receiver profiles: each physical receiver (identified by its USB strings / serial number) keeps its own
settings, calibrations, calibration-dialog parameters and preamplifier. Stored in data\\receivers.json.

The [sdr] section of meteorrx.ini always mirrors the settings of the active profile, so the rest of the
program (and the command line) keeps reading the INI as before.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass

# [sdr] keys that belong to a receiver (not to the station)
PROFILE_KEYS = ("device", "rtl_gain_db", "rtl_bias_tee", "rtl_sample_rate", "ppm", "center_mhz",
                "sdrplay_grdb", "sdrplay_lna_state", "sdrplay_agc", "sdrplay_bias_tee", "sdrplay_fm_notch",
                "sdrplay_sample_rate", "sdrplay_antenna", "hackrf_sample_rate", "hackrf_lna_gain", "hackrf_vga_gain",
                "hackrf_amp", "hackrf_bias_tee", "lime_sample_rate", "lime_gain_db", "lime_antenna",
                "pluto_uri", "pluto_sample_rate", "pluto_gain_db")

SDRPLAY_HW = {1: "RSP1", 2: "RSP2", 3: "RSPduo", 4: "RSPdx", 6: "RSP1B", 7: "RSPdx-R2", 255: "RSP1A"}
GENERIC_RTL_SERIALS = {"", "00000001", "0", "00000000"}


@dataclass
class DeviceId:
    kind: str                    # rtlsdr | sdrplay | hackrf | lime | pluto
    manufacturer: str = ""
    product: str = ""
    serial: str = ""
    tuner: str = ""
    index: int = -1              # enumeration index at detection time (not part of the identity)

    @property
    def key(self) -> str:
        if self.kind in ("sdrplay", "hackrf", "lime", "pluto"):
            return f"{self.kind}|{self.product}|{self.serial}"
        return f"rtlsdr|{self.manufacturer}|{self.product}|{self.serial}"

    @property
    def label(self) -> str:
        if self.kind == "sdrplay":
            return f"SDRplay {self.product} SN {self.serial}"
        if self.kind in ("hackrf", "lime", "pluto"):
            return f"{self.product} SN {self.serial[-8:]}"
        name = " ".join(x for x in (self.manufacturer, self.product) if x) or "RTL-SDR"
        return f"{name} SN {self.serial or '?'}" + (f" ({self.tuner})" if self.tuner else "")

    @property
    def generic_serial(self) -> bool:
        return self.kind == "rtlsdr" and self.serial.strip() in GENERIC_RTL_SERIALS


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


class ProfileStore:
    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "receivers.json")
        self.data = {"version": 1, "active": None, "profiles": {}}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d.get("profiles"), dict):
                self.data.update(d)
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------------ access
    @property
    def profiles(self) -> list[dict]:
        return sorted(self.data["profiles"].values(), key=lambda p: p.get("created", ""))

    def get(self, pid: str | None) -> dict | None:
        return self.data["profiles"].get(pid) if pid else None

    @property
    def active_id(self) -> str | None:
        a = self.data.get("active")
        return a if a in self.data["profiles"] else None

    @property
    def active(self) -> dict | None:
        return self.get(self.active_id)

    def find(self, dev: DeviceId) -> dict | None:
        return next((p for p in self.profiles if p["identity"]["key"] == dev.key), None)

    # ------------------------------------------------------------------ changes
    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def create(self, dev: DeviceId, cfg, name: str | None = None) -> dict:
        n = 1
        while f"rx{n}" in self.data["profiles"]:
            n += 1
        pid = f"rx{n}"
        settings = {k: cfg.get("sdr", k, "") for k in PROFILE_KEYS if cfg.get("sdr", k, None) is not None}
        settings["device"] = dev.kind
        ident = asdict(dev)
        ident.pop("index", None)
        ident["key"] = dev.key
        p = {"id": pid, "name": name or dev.label, "identity": ident, "settings": settings,
             "cal_params": {}, "preamp": None, "created": _now(), "last_seen": _now()}
        self.data["profiles"][pid] = p
        if self.active_id is None:
            self.data["active"] = pid
        self.save()
        return p

    def set_active(self, pid: str):
        if pid in self.data["profiles"]:
            self.data["active"] = pid
            self.save()

    def seen(self, pid: str, dev: DeviceId | None = None):
        p = self.get(pid)
        if p:
            p["last_seen"] = _now()
            if dev is not None and dev.tuner:
                p["identity"]["tuner"] = dev.tuner
            self.save()

    def update_settings(self, pid: str | None, cfg):
        p = self.get(pid)
        if not p or (cfg.get("sdr", "device", "") or "").lower() != p["identity"]["kind"]:
            return                     # screen shows another device type: not this receiver's settings
        p["settings"].update({k: cfg.get("sdr", k, "") for k in PROFILE_KEYS if cfg.get("sdr", k, None) is not None})
        self.save()

    def set_cal_params(self, pid: str | None, params: dict):
        p = self.get(pid)
        if p:
            p["cal_params"] = params
            self.save()

    def set_preamp(self, pid: str | None, preamp: dict | None):
        p = self.get(pid)
        if p:
            p["preamp"] = preamp
            self.save()

    def set_preamp_in_service(self, pid: str | None, on: bool):
        p = self.get(pid)
        if p and p.get("preamp"):
            p["preamp"]["in_service"] = bool(on)
            self.save()

    def rename(self, pid: str, name: str):
        p = self.get(pid)
        if p and name.strip():
            p["name"] = name.strip()
            self.save()

    def delete(self, pid: str):
        self.data["profiles"].pop(pid, None)
        if self.data.get("active") == pid:
            ps = self.profiles
            self.data["active"] = ps[0]["id"] if ps else None
        self.save()

    # ------------------------------------------------------------------ INI mirror
    @staticmethod
    def apply(p: dict, cfg):
        """Copy the profile settings into [sdr] and select the physical device by serial number."""
        for k, v in p["settings"].items():
            if k in PROFILE_KEYS:
                cfg.set("sdr", k, v)
        ident = p["identity"]
        kind = ident["kind"]
        if kind == "sdrplay":
            cfg.set("sdr", "sdrplay_serial", ident.get("serial", ""))
        elif kind == "hackrf":
            cfg.set("sdr", "hackrf_serial", ident.get("serial", ""))
        elif kind == "lime":
            cfg.set("sdr", "lime_serial", ident.get("serial", ""))
        elif kind == "rtlsdr":
            cfg.set("sdr", "rtl_serial", ident.get("serial", ""))

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def preamp_gain(p: dict | None) -> float:
        """Gain of the preamplifier in service (S-meter referred to the preamp input), else 0."""
        pa = (p or {}).get("preamp") or {}
        try:
            return float(pa.get("gain_db") or 0.0) if pa.get("in_service") else 0.0
        except (TypeError, ValueError):
            return 0.0


def reconcile(store: ProfileStore, found: list[DeviceId], cfg) -> tuple[dict | None, list[str], list[DeviceId]]:
    """Match the connected receivers with the stored profiles.

    Unknown receivers get a new profile (the very first one inherits the current INI settings, and with them
    the calibrations made before profiles existed). The active profile is kept when its receiver is connected,
    otherwise the first connected receiver that has a profile becomes active.
    Returns (active profile, messages, new devices)."""
    msgs, new = [], []
    keys = [d.key for d in found]
    for k in sorted(set(keys)):
        if keys.count(k) > 1:
            msgs.append(f"WARNING: {keys.count(k)} receivers have the same identity ({k}). They cannot be told "
                        "apart: give each a unique serial number with  rtl_eeprom -s <serial>  (then replug).")
    for d in found:
        p = store.find(d)
        if p is None:
            first = not store.profiles
            p = store.create(d, cfg)
            new.append(d)
            msgs.append(f"New receiver: {d.label} -> profile '{p['name']}'"
                        + (" (current settings and calibrations kept)" if first else ""))
            if d.generic_serial:
                msgs.append("  note: generic serial number; if you use several dongles, set a unique one with "
                            "rtl_eeprom -s <serial>")
        store.seen(p["id"], d)
    act = store.active
    connected = {d.key for d in found}
    if act is None or (found and act["identity"]["key"] not in connected):
        cur_kind = (cfg.get("sdr", "device", "rtlsdr") or "rtlsdr").lower()
        cands = [store.find(d) for d in found]
        cands = [p for p in cands if p] or ([act] if act else [])
        cands.sort(key=lambda p: p["identity"]["kind"] != cur_kind)
        if cands:
            if act is not None and act["id"] != cands[0]["id"]:
                msgs.append(f"Receiver of profile '{act['name']}' not connected: switching to '{cands[0]['name']}'")
            store.set_active(cands[0]["id"])
            act = cands[0]
    if act is not None and act["identity"]["key"] not in connected:
        msgs.append(f"Receiver of profile '{act['name']}' is not connected")
    return act, msgs, new

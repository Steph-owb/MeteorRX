"""Language tables: every language complete and consistent with English (keys, format fields, device list)."""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from meteorrx import i18n  # noqa: E402

FIELDS = re.compile(r"\{(\w+)")


def test_all_languages_complete_and_consistent():
    en = i18n.LANGUAGES["en"]
    assert set(i18n.names()) >= {"en", "fr", "it", "es", "de"}
    for code, lang in i18n.LANGUAGES.items():
        assert lang["name"]
        for sec in ("gui", "calib"):
            missing = set(en[sec]) - set(lang[sec])
            extra = set(lang[sec]) - set(en[sec])
            assert not missing and not extra, f"{code}/{sec}: missing {missing}, extra {extra}"
            for k, v in lang[sec].items():
                ref = en[sec][k]
                if isinstance(ref, list):
                    assert isinstance(v, list) and len(v) == len(ref), f"{code}/{sec}/{k}"
                else:
                    assert isinstance(v, str) and v.strip(), f"{code}/{sec}/{k} empty"
                    assert set(FIELDS.findall(v)) == set(FIELDS.findall(ref)), f"{code}/{sec}/{k} format fields"
        if code != "en":                                 # a real translation, not a copy of English
            strs = [(k, v) for sec in ("gui", "calib") for k, v in lang[sec].items() if isinstance(v, str)]
            same = [k for k, v in strs if v == en["gui" if k in en["gui"] else "calib"][k]]
            assert len(same) <= 0.2 * len(strs), f"{code}: too many strings identical to English: {same}"


def test_lookup_fallback_and_normalisation():
    assert i18n.normalize("FR-fr") == "fr" and i18n.normalize("de_DE") == "de" and i18n.normalize("xx") == "en"
    assert i18n.normalize(None) == "en"
    t = i18n.texts("it", "gui")
    assert t["start"] == "Avvia" and len(t["devices"]) == 6
    # a partial language falls back to English for missing keys
    i18n.LANGUAGES["zz"] = {"name": "Test", "gui": {"start": "Go"}, "calib": {}}
    try:
        t = i18n.texts("zz", "gui")
        assert t["start"] == "Go" and t["stop"] == "Stop" and i18n.texts("zz", "calib")["close"] == "Close"
    finally:
        del i18n.LANGUAGES["zz"]


def test_device_list_matches_gui_keys():
    from meteorrx import gui
    for code in i18n.LANGUAGES:
        assert len(i18n.texts(code, "gui")["devices"]) == len(gui.DEV_KEYS)

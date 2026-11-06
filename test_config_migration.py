"""An operator who already has a meteorrx.ini must still receive new shipped defaults.

Adding MSU-MR 124 to the default `composites` would otherwise have reached nobody: `load` overlays the
stored file on top of the defaults, so an ini written by an older build keeps its own `221, 123` and the
new combination never appears in the view selector. A stored value that matches an old default verbatim
was never a choice, so it is carried forward; anything the operator actually edited is left alone.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from meteorrx import config  # noqa: E402


def write_ini(tmp_path, composites):
    """A full default ini with the composites line replaced, or removed when composites is None."""
    lines = config.DEFAULT_INI.split("\n")
    if composites is None:
        lines = [l for l in lines if not l.startswith("composites")]
    else:
        lines = [f"composites          = {composites}" if l.startswith("composites") else l
                 for l in lines]
    p = tmp_path / "meteorrx.ini"
    p.write_text("\n".join(lines), encoding="utf-8")
    return str(p)


def test_an_old_shipped_default_is_carried_forward(tmp_path):
    cfg = config.load(write_ini(tmp_path, "221, 123"))
    assert cfg.get("recording", "composites") == "221, 124, 123"
    assert cfg.migrated and "composites" in cfg.migrated[0]


def test_a_value_the_operator_edited_is_left_alone(tmp_path):
    """The whole point of the ini is that it is the operator's. Never overwrite a real choice."""
    cfg = config.load(write_ini(tmp_path, "125, 321"))
    assert cfg.get("recording", "composites") == "125, 321"
    assert cfg.migrated == []


def test_a_missing_key_takes_the_default_without_claiming_a_migration(tmp_path):
    cfg = config.load(write_ini(tmp_path, None))
    assert cfg.get("recording", "composites") == "221, 124, 123"
    assert cfg.migrated == []


def test_the_file_on_disk_is_brought_in_step(tmp_path):
    """Reading meteorrx.ini must still explain what the application is doing."""
    path = write_ini(tmp_path, "221, 123")
    config.load(path)
    stored = [l for l in open(path, encoding="utf-8") if l.startswith("composites")]
    assert stored and "221, 124, 123" in stored[0]


def test_migrating_twice_changes_nothing_the_second_time(tmp_path):
    path = write_ini(tmp_path, "221, 123")
    assert config.load(path).migrated
    assert config.load(path).migrated == []


def test_a_read_only_install_still_gets_the_new_value(tmp_path):
    """A USB stick or a Program Files install must not fail to start over this."""
    path = write_ini(tmp_path, "221, 123")
    os.chmod(path, 0o444)
    try:
        cfg = config.load(path)
        assert cfg.get("recording", "composites") == "221, 124, 123"
    finally:
        os.chmod(path, 0o644)


def test_the_shipped_default_lists_the_false_colour(tmp_path):
    """If the default ever drops 124, the migration table above would silently rewrite users back."""
    cfg = config.load(write_ini(tmp_path, None))
    specs = [s.strip() for s in cfg.get("recording", "composites").split(",")]
    assert "124" in specs
    for sup in config.SUPERSEDED[("recording", "composites")]:
        assert sup.strip() != cfg.get("recording", "composites").strip()


# --- files as operators really write them ------------------------------------------------------

def variant(tmp_path, name, transform, encoding="utf-8"):
    p = tmp_path / name
    p.write_text(transform(config.DEFAULT_INI), encoding=encoding)
    return str(p)


OLD = "composites          = 221, 123"
NEW = "composites          = 221, 124, 123"


def test_a_byte_order_mark_does_not_stop_the_program(tmp_path):
    """Notepad's "Save as UTF-8" has historically written a BOM, which configparser rejects."""
    p = variant(tmp_path, "bom.ini", lambda t: t.replace(NEW, OLD), encoding="utf-8-sig")
    cfg = config.load(p)
    assert cfg.get("recording", "composites") == "221, 124, 123"
    assert config.load(p).migrated == [], "the rewrite must stick, or it migrates at every start"


def test_an_ansi_accent_does_not_stop_the_program(tmp_path):
    """Notepad's ANSI default is cp1252, and this user base writes French and Italian place names."""
    p = variant(tmp_path, "ansi.ini",
                lambda t: t.replace("; Composite outputs", "; Réglages à Genève, été").replace(NEW, OLD),
                encoding="cp1252")
    cfg = config.load(p)
    assert cfg.get("recording", "composites") == "221, 124, 123"
    assert config.load(p).migrated == []


def test_only_the_right_section_is_rewritten(tmp_path):
    """The same key name may legitimately appear in two sections; a file-wide match hit the wrong one."""
    p = variant(tmp_path, "two.ini",
                lambda t: t.replace(NEW, OLD).replace("[gui]", "[gui]\ncomposites          = 999, 888"))
    config.load(p)
    lines = [l.strip() for l in open(p, encoding="utf-8-sig") if l.strip().startswith("composites")]
    assert lines == ["composites          = 221, 124, 123", "composites          = 999, 888"]


def test_a_colon_delimiter_is_rewritten_too(tmp_path):
    """configparser accepts "key: value"; a rewrite that misses it migrates again at every start."""
    p = variant(tmp_path, "colon.ini", lambda t: t.replace(NEW, "composites          : 221, 123"))
    assert config.load(p).migrated
    assert config.load(p).migrated == []


def test_the_rewrite_is_atomic(tmp_path):
    """A crash mid-write must never leave a truncated ini: the operator would silently lose their
    station coordinates and callsign to the defaults at the next start."""
    import os
    p = variant(tmp_path, "atomic.ini", lambda t: t.replace(NEW, OLD))
    before = open(p, encoding="utf-8").read()
    real_replace, seen = os.replace, []

    def boom(src, dst):
        seen.append((src, dst))
        raise OSError("simulated crash at the rename")

    os.replace = boom
    try:
        config.load(p)
    finally:
        os.replace = real_replace
    assert seen, "the rewrite must go through a temporary file and a rename"
    assert open(p, encoding="utf-8").read() == before, "the original must survive untouched"

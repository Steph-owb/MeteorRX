"""Keep dist\\MeteorRX\\data (calibrations, receiver profiles, session counter, log) across a rebuild:
PyInstaller --noconfirm deletes the whole dist\\MeteorRX folder.

    python tools/keep_data.py save      (before PyInstaller)
    python tools/keep_data.py restore   (after PyInstaller)

Files are moved, never copied over (same volume, nothing can be half-written); a newer file of the same name
replaces the older one only in the _keep folder."""
import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA = os.path.join(SRC, "dist", "MeteorRX", "data")
KEEP = os.path.join(SRC, "_keep", "data")


def _move_tree(src: str, dst: str) -> int:
    n = 0
    for root, dirs, files in os.walk(src, topdown=False):
        rel = os.path.relpath(root, src)
        out = os.path.normpath(os.path.join(dst, rel))
        os.makedirs(out, exist_ok=True)
        for f in files:
            os.replace(os.path.join(root, f), os.path.join(out, f))
            n += 1
        try:
            os.rmdir(root)
        except OSError:
            pass
    return n


def main(argv) -> int:
    what = argv[1] if len(argv) > 1 else ""
    if what == "save":
        if os.path.isdir(DATA):
            n = _move_tree(DATA, KEEP)
            if n:
                print(f"      data\\ set aside ({n} file(s): calibrations, receiver profiles, session numbers)")
        return 0
    if what == "restore":
        if os.path.isdir(KEEP):
            n = _move_tree(KEEP, DATA)
            try:
                os.rmdir(os.path.dirname(KEEP))
            except OSError:
                pass
            if n:
                print(f"      data\\ restored ({n} file(s))")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

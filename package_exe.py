"""Finish dist/MeteorRX after PyInstaller: drivers, configuration, docs, self-test, distribution zip.

    python tools/package_exe.py            (run from the project folder, after PyInstaller)"""
import os
import shutil
import subprocess
import sys
import zipfile

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(SRC, "dist", "MeteorRX")


def main():
    if not os.path.isfile(os.path.join(OUT, "MeteorRX.exe")):
        sys.exit("dist/MeteorRX/MeteorRX.exe not found - run PyInstaller first")
    drv = os.path.join(OUT, "drivers")
    if os.path.isdir(drv):
        shutil.rmtree(drv)
    shutil.copytree(os.path.join(SRC, "drivers"), drv)
    for need in (os.path.join(drv, "rtlsdr", "rtlsdr.dll"), os.path.join(drv, "zadig", "zadig.exe")):
        if not os.path.isfile(need):
            sys.exit(f"missing {need} - run tools/fetch_drivers.py")
    for f in ("README.md", "LICENSE"):
        shutil.copy2(os.path.join(SRC, f), OUT)
    if not os.path.isfile(os.path.join(OUT, "meteorrx.ini")):
        shutil.copy2(os.path.join(SRC, "meteorrx.ini"), OUT)
    for d in ("data", "cache"):
        os.makedirs(os.path.join(OUT, d), exist_ok=True)
    # self-test of the frozen program (also compiles the DSP kernels into cache\)
    cli = os.path.join(OUT, "MeteorRX-cli.exe")
    cmd = [cli, "--selftest"] if os.name == "nt" else ["wine", cli, "--selftest"]
    r = subprocess.run(cmd, cwd=OUT)
    if r.returncode:
        sys.exit("self-test of the frozen program FAILED")
    sys.path.insert(0, SRC)
    from meteorrx import __version__
    zpath = os.path.join(SRC, "dist", f"MeteorRX-{__version__}-win64.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for root, dirs, files in os.walk(OUT):
            rel = os.path.relpath(root, OUT).split(os.sep)[0]
            if rel in ("cache", "data"):
                dirs[:] = []
                z.write(root, os.path.relpath(root, os.path.dirname(OUT)))
                continue
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                p = os.path.join(root, f)
                z.write(p, os.path.relpath(p, os.path.dirname(OUT)))
    print(f"Distribution zip: {zpath} ({os.path.getsize(zpath) / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()

"""Assemble the portable MeteorRX folder around an official Python embeddable runtime.

Called by build_portable.bat with the embedded interpreter:
    runtime\\python.exe tools\\make_portable.py <out_dir>
Copies the application sources, writes the launchers, removes pip-generated .exe stubs,
pre-compiles the DSP kernels, runs the self-test and writes the distribution zip."""
import os
import shutil
import subprocess
import sys
import zipfile

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.abspath(sys.argv[1])
RT = os.path.join(OUT, "runtime")
APP = os.path.join(OUT, "app")

LAUNCH_GUI = '@echo off\r\nstart "" "%~dp0runtime\\pythonw.exe" -m meteorrx\r\n'
LAUNCH_CLI = '@echo off\r\n"%~dp0runtime\\python.exe" -m meteorrx.cli %*\r\n'
SELFTEST = '@echo off\r\n"%~dp0runtime\\python.exe" -m meteorrx.selftest\r\npause\r\n'
SHORTCUTS = r'''@echo off
rem Creates MeteorRX shortcuts (this folder, Desktop, Start menu) pointing to this installation.
rem Run it again after moving the MeteorRX folder.
set "R=%~dp0"
powershell -NoProfile -Command "$w=New-Object -ComObject WScript.Shell; $r=$env:R; foreach($d in @($r,[Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('Programs'))){ $l=$w.CreateShortcut((Join-Path $d 'MeteorRX.lnk')); $l.TargetPath=(Join-Path $r 'runtime\pythonw.exe'); $l.Arguments='-m meteorrx'; $l.WorkingDirectory=$r; $l.IconLocation=(Join-Path $r 'app\assets\meteorrx.ico'); $l.Description='MeteorRX - Meteor-M LRPT receiver'; $l.Save() }"
if errorlevel 1 (echo Shortcut creation failed.) else (echo Shortcuts created: MeteorRX folder, Desktop, Start menu.)
'''


def write(path, text):
    with open(path, "w", newline="", encoding="ascii") as f:
        f.write(text.replace("\r\n", "\n").replace("\n", "\r\n"))


def main():
    ign = shutil.ignore_patterns("__pycache__", "*.pyc")
    for d in ("meteorrx", "assets"):
        dst = os.path.join(APP, d)
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        shutil.copytree(os.path.join(SRC, d), dst, ignore=ign)
    for f in ("README.md", "LICENSE"):
        shutil.copy2(os.path.join(SRC, f), OUT)
    if not os.path.isfile(os.path.join(OUT, "meteorrx.ini")):
        shutil.copy2(os.path.join(SRC, "meteorrx.ini"), OUT)
    drv = os.path.join(OUT, "drivers")
    if os.path.isdir(drv):
        shutil.rmtree(drv)
    shutil.copytree(os.path.join(SRC, "drivers"), drv)
    for d in ("data", "cache"):
        os.makedirs(os.path.join(OUT, d), exist_ok=True)
    write(os.path.join(OUT, "MeteorRX.cmd"), LAUNCH_GUI)
    write(os.path.join(OUT, "MeteorRX-cli.cmd"), LAUNCH_CLI)
    write(os.path.join(OUT, "Self-test.cmd"), SELFTEST)
    write(os.path.join(OUT, "Create shortcuts.cmd"), SHORTCUTS)
    # pip writes small launcher .exe files (pip.exe, pyside6-*.exe...): not needed, removed so that the
    # package contains no unsigned executable of unknown origin.
    shutil.rmtree(os.path.join(RT, "Scripts"), ignore_errors=True)
    for root, _, files in os.walk(RT):
        for f in files:
            if f.lower().endswith(".exe") and os.path.dirname(os.path.join(root, f)) != RT:
                os.remove(os.path.join(root, f))
    # self-test (also compiles the DSP kernels into cache\)
    env = dict(os.environ, PYTHONPATH=APP)
    r = subprocess.run([sys.executable, "-m", "meteorrx.selftest"], env=env, cwd=OUT)
    if r.returncode:
        sys.exit("self-test failed")
    # distribution zip (without per-machine cache/data)
    sys.path.insert(0, APP)
    from meteorrx import __version__
    zpath = os.path.join(os.path.dirname(OUT), f"MeteorRX-{__version__}-win64.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        base = os.path.dirname(OUT)
        for root, dirs, files in os.walk(OUT):
            rel = os.path.relpath(root, OUT).split(os.sep)[0]
            if rel in ("cache", "data"):
                continue
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                if f.endswith(".lnk"):
                    continue
                p = os.path.join(root, f)
                z.write(p, os.path.relpath(p, base))
    print(f"Distribution zip: {zpath} ({os.path.getsize(zpath) / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()

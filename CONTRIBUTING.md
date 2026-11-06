# Contributing

Reports from real stations are the most valuable contribution.

## Reporting a problem

Open an issue with:

1. MeteorRX version and receiver (model, driver), Windows version.
2. The output of the receiver test, run in the MeteorRX folder during a pass or with a test signal:

       MeteorRX-cli --selftest
       MeteorRX-cli --live 30 --device rtlsdr      (or sdrplay, hackrf, lime, pluto)

3. `data\meteorrx.log`, and if possible a short IQ recording (`--record test.cs16`, or
   `save_baseband = yes` in `meteorrx.ini`).

## Development

    python -m pip install -r requirements-dev.txt
    python tools/fetch_drivers.py drivers        (Windows package only)
    python -m pytest -q tests

Run from source: `python MeteorRX_gui.py` / `python MeteorRX_cli.py`. Build the Windows package:
`build_exe.bat`. Please keep the test-suite green and add a test for any decoding or receiver change.

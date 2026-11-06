Drivers bundled with MeteorRX
=============================
rtlsdr\rtlsdr.dll   RTL-SDR Blog driver library v1.3.6 (x64, official release, libusb built in,
                    supports RTL-SDR Blog V4/V4L, V3 and generic RTL2832U dongles). GPL-2.0+.
zadig\zadig.exe     Zadig 2.9 - installs the WinUSB USB driver (digitally signed by Akeo Consulting). GPL-3.0.
                    Started from MeteorRX: menu Drivers > Install the WinUSB USB driver.

Both are downloaded at build time from their official GitHub releases and verified by SHA-256
(tools\fetch_drivers.py). See the SOURCE.txt file in each folder.

At start-up MeteorRX first looks for an rtlsdr.dll already installed (rtl_dll_dir in meteorrx.ini,
SatDump, SDR++, SDR#, PATH...) and uses it if it is a valid 64-bit build exporting every function
needed and - when an RTL-SDR Blog V4 is connected - one with V4 support. Otherwise the bundled
copy is used. Menu Drivers > RTL-SDR driver diagnostics shows the decision.

"""Download the third-party drivers bundled with MeteorRX and verify them (pinned SHA-256).

  drivers/rtlsdr/rtlsdr.dll  official RTL-SDR Blog Windows release v1.3.6, x64 (libusb linked statically,
                             RTL-SDR Blog V4/V4L support, backwards compatible with V3 and generic dongles)
  drivers/zadig/zadig.exe    Zadig 2.9 (WinUSB installer, digitally signed by Akeo Consulting)

Usage:  python tools/fetch_drivers.py [drivers_dir]
Files already present with the right hash are not downloaded again (offline rebuilds)."""
import hashlib
import io
import os
import sys
import urllib.request
import zipfile

RTL_URL = "https://github.com/rtlsdrblog/rtl-sdr-blog/releases/download/v1.3.6/Release.zip"
RTL_ZIP_SHA256 = "a331691535fc99cfe21bc83b825c7b03f9f71022f93bc3f85254eb1d49fa77ab"
RTL_DLL_SHA256 = "9805b8c6195d12c43e670ea99462a62814e5953dfa8b3f113e3c9c702b0e5b54"
ZADIG_URL = "https://github.com/pbatard/libwdi/releases/download/v1.5.1/zadig-2.9.exe"
ZADIG_SHA256 = "4ecaa95df3da3621486a043aef8b3050b8bafe7c901402871e816229ef82039b"

RTL_NOTICE = """rtlsdr.dll - RTL-SDR Blog driver library, release v1.3.6 (x64)
Source : https://github.com/rtlsdrblog/rtl-sdr-blog  (tag v1.3.6)
Binary : {url}
SHA-256: {sha}
Licence: GNU GPL v2 or later. The complete corresponding source code is available at the URL above.
"""
ZADIG_NOTICE = """zadig.exe - Zadig 2.9 (libwdi v1.5.1), USB driver installer by Pete Batard / Akeo Consulting
Source : https://github.com/pbatard/libwdi  (tag v1.5.1)
Binary : {url}
SHA-256: {sha}
Licence: GNU GPL v3. Digitally signed by Akeo Consulting.
"""


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "MeteorRX-build"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def ensure(path: str, want: str, fetch) -> None:
    if os.path.isfile(path):
        with open(path, "rb") as f:
            if sha256(f.read()) == want:
                print(f"  ok (cached)  {path}")
                return
    data = fetch()
    if sha256(data) != want:
        sys.exit(f"SHA-256 mismatch for {path}: got {sha256(data)}, expected {want}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    print(f"  downloaded   {path}")


def main():
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..",
                                                                             "drivers"))

    def rtl():
        z = get(RTL_URL)
        if sha256(z) != RTL_ZIP_SHA256:
            sys.exit(f"SHA-256 mismatch for {RTL_URL}")
        return zipfile.ZipFile(io.BytesIO(z)).read("x64/rtlsdr.dll")

    ensure(os.path.join(root, "rtlsdr", "rtlsdr.dll"), RTL_DLL_SHA256, rtl)
    ensure(os.path.join(root, "zadig", "zadig.exe"), ZADIG_SHA256, lambda: get(ZADIG_URL))
    with open(os.path.join(root, "rtlsdr", "SOURCE.txt"), "w", encoding="ascii") as f:
        f.write(RTL_NOTICE.format(url=RTL_URL, sha=RTL_DLL_SHA256))
    with open(os.path.join(root, "zadig", "SOURCE.txt"), "w", encoding="ascii") as f:
        f.write(ZADIG_NOTICE.format(url=ZADIG_URL, sha=ZADIG_SHA256))
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from meteorrx.drivers import ZADIG_INI, inspect_library
    with open(os.path.join(root, "zadig", "zadig.ini"), "w", encoding="ascii") as f:
        f.write(ZADIG_INI)
    c = inspect_library(os.path.join(root, "rtlsdr", "rtlsdr.dll"), "bundled")
    if not (c.usable and c.v4):
        sys.exit(f"bundled rtlsdr.dll failed inspection: {c.reason}")
    print(f"  rtlsdr.dll   {c.reason}")


if __name__ == "__main__":
    main()

"""Entry point of MeteorRX-cli.exe (console: decoding, --selftest, --drivers)."""
import sys

from meteorrx.cli import main

if __name__ == "__main__":
    sys.exit(main())

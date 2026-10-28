"""PyInstaller entry point for the Windows EXE."""

import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from blenderbridge.app import main

    sys.exit(main())

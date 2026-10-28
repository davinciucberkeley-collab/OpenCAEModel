"""PyInstaller entry point for OpenCAE Studio."""
import multiprocessing
import sys

from opencae.app.main import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())

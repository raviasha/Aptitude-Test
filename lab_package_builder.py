"""Frozen GUI entry point; lab details are runtime inputs, never private keys."""
import multiprocessing
import sys
from pathlib import Path


def main(argv=None):
    from ksat.lab_builder.gui import run_gui
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return run_gui(root / "lab-payload")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())

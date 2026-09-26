#!/usr/bin/env python3
"""Entry point: pull the latest LLVM and build it with the latest winlibs MinGW-w64 GCC.

Works unchanged on Windows (where winlibs GCC actually runs), Linux and macOS.
Run ``python build.py --help`` for the full option list, or ``python build.py
doctor`` to check the environment first.

Typical use on a Windows machine / CI runner::

    python build.py all --scope core

Typical use to validate the wiring on a non-Windows box (uses the host
compiler instead of winlibs, which is Windows-only)::

    python3 build.py all --native --scope minimal --build-targets llvm-tblgen
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lmb.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

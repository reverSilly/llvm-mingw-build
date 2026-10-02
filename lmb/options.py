"""Discover which CMake options a given LLVM source tree actually declares.

Why this exists
---------------
LLVM's option names drift between releases.  Building "the latest" against a
hard-coded flag list therefore rots:

* ``LLVM_ENABLE_TERMINFO``    - gone in LLVM 23
* ``CLANG_ENABLE_ARCMT``      - gone in LLVM 23 (ARCMigrate removed)
* ``FLANG_BUILD_NEW_DRIVER``  - gone in LLVM 23
* ``LLDB_ENABLE_*``           - LLVM 23 declares these through a new
                                ``add_optional_dependency()`` macro rather than
                                ``option()``

Passing an unknown ``-D`` is only a warning today, but it silently does
nothing, which is worse than failing: you think you disabled libedit and you
did not.  So before configuring we scan the source tree for the option
declarations of the projects we are about to build and drop any generated flag
that is not declared there.

User-supplied ``--cmake-arg`` values are **never** filtered - if you pass a
flag explicitly you know why.
"""

from __future__ import annotations

import os
import re
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

# option(NAME ...), set(NAME ...), add_optional_dependency(NAME ...),
# plus a few project-specific helper macros seen over the years.
_DECL_RE = re.compile(
    r"^\s*(?:option|set|add_optional_dependency|llvm_optional_dependency|"
    r"clang_option|lldb_option|add_llvm_option)\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)

# Names that are CMake builtins or come from our own toolchain file, so they are
# never in the LLVM corpus and must always be kept.
_ALWAYS_KEEP_PREFIXES = ("CMAKE_",)
_ALWAYS_KEEP = {
    "WINLIBS_ROOT",
    "MINGW_TRIPLE",
    "MINGW_SUBDIR",
    "LLVM_LIT_ARGS",
    "PYTHON_EXECUTABLE",
    "Python3_EXECUTABLE",
}

_SCAN_FILENAMES = ("CMakeLists.txt",)
_SCAN_SUFFIXES = (".cmake",)

_cache: Dict[str, Set[str]] = {}


def _candidate_files(src_dir: str, projects: Sequence[str], runtimes: Sequence[str]) -> List[str]:
    """The CMake files that can declare options for the components we build."""
    files: List[str] = []

    def add_file(path: str) -> None:
        if os.path.isfile(path):
            files.append(path)

    def add_dir(root: str, max_files: int = 400) -> None:
        if not os.path.isdir(root):
            return
        count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            # Skip trees that cannot declare build options but hold thousands of files.
            dirnames[:] = [d for d in dirnames if d not in ("test", "tests", "unittests", "docs", "bindings", ".git")]
            for name in filenames:
                if name in _SCAN_FILENAMES or name.endswith(_SCAN_SUFFIXES):
                    files.append(os.path.join(dirpath, name))
                    count += 1
                    if count >= max_files:
                        return

    # Monorepo-wide helpers (cmake/Modules/LLVMVersion.cmake, CMakePolicy.cmake ...)
    add_dir(os.path.join(src_dir, "cmake"))

    # llvm itself: top-level CMakeLists plus llvm/cmake/**
    add_file(os.path.join(src_dir, "llvm", "CMakeLists.txt"))
    add_dir(os.path.join(src_dir, "llvm", "cmake"))
    add_file(os.path.join(src_dir, "llvm", "runtimes", "CMakeLists.txt"))

    for proj in projects:
        pdir = os.path.join(src_dir, proj)
        add_file(os.path.join(pdir, "CMakeLists.txt"))
        add_dir(os.path.join(pdir, "cmake"))

    for rt in runtimes:
        # runtimes live either at the monorepo root or under llvm/runtimes
        for base in (src_dir, os.path.join(src_dir, "llvm", "runtimes")):
            rdir = os.path.join(base, rt)
            add_file(os.path.join(rdir, "CMakeLists.txt"))
            add_dir(os.path.join(rdir, "cmake"))

    # de-duplicate, keep order
    seen: Set[str] = set()
    out = []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def declared_options(src_dir: str, projects: Iterable[str] = (), runtimes: Iterable[str] = ()) -> Set[str]:
    """Every CMake option name declared by the relevant part of the tree."""
    key = f"{os.path.abspath(src_dir)}|{';'.join(sorted(projects))}|{';'.join(sorted(runtimes))}"
    if key in _cache:
        return _cache[key]

    found: Set[str] = set()
    for path in _candidate_files(src_dir, list(projects), list(runtimes)):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        found.update(_DECL_RE.findall(text))

    _cache[key] = found
    return found


def filter_args(
    args: Sequence[str],
    declared: Optional[Set[str]],
    dropped_out: Optional[List[str]] = None,
) -> List[str]:
    """Remove ``-DNAME=...`` entries whose NAME is not declared by the tree.

    Returns the kept arguments; appends human readable notes to *dropped_out*.
    """
    if not declared:
        return list(args)

    kept: List[str] = []
    for arg in args:
        if not arg.startswith("-D") or "=" not in arg:
            kept.append(arg)
            continue
        name = arg[2:].split("=", 1)[0]
        if name.startswith(_ALWAYS_KEEP_PREFIXES) or name in _ALWAYS_KEEP or name in declared:
            kept.append(arg)
        else:
            if dropped_out is not None:
                dropped_out.append(name)
    return kept


def report(dropped: Sequence[str]) -> None:
    if not dropped:
        return
    print("[options] dropped flags not declared by this LLVM tree (version drift):")
    for name in dropped:
        print(f"[options]   -D{name}")
    print("[options]   (pass them via --cmake-arg if you really need them)")

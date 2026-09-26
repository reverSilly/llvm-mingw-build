"""lmb - build LLVM/Clang for Windows with the winlibs MinGW-w64 GCC toolchain.

This package is the engine behind ``build.py``.  It is intentionally
dependency-free (standard library only) so it runs unchanged on a Windows
runner, a Linux container and macOS.

Sub-modules
-----------
``config``      build configuration, scope presets, CMake flag generation
``net``         HTTP/JSON helpers, resumable downloads, checksum verification
``toolchain``   winlibs discovery / download / extraction / compiler lookup
``llvm``        LLVM release discovery and source acquisition (3 fallbacks)
``builder``     CMake configure + Ninja/Make build + install orchestration
``packaging``   archive creation (zip/7z) with sha256/sha512 sidecar files
``smoketest``   compile-and-run sanity checks against the produced toolchain
``cli``         argument parsing and command dispatch
"""

__version__ = "1.0.0"

__all__ = ["__version__"]

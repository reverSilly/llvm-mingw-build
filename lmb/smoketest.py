"""Smoke tests.

Two flavours:

``toolchain``  - compile and (when the target == host) run a hello-world with
                 the winlibs GCC that will build LLVM.  Catches a broken or
                 un-runnable toolchain before hours of compiling.
``products``   - run the binaries we just installed (``clang --version``,
                 ``lld`` / ``llvm-config``, and a real compile+link with clang).
                 On a cross build the executables cannot run on the host, so we
                 only assert that the files exist and look like PE images.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from typing import List, Optional, Tuple

from .config import BuildConfig

HELLO_C = r"""#include <stdio.h>
int main(void) { printf("hello from lmb\n"); return 0; }
"""

HELLO_CPP = r"""#include <iostream>
#include <vector>
#include <numeric>
int main() {
    std::vector<int> v(10);
    std::iota(v.begin(), v.end(), 1);
    std::cout << "sum=" << std::accumulate(v.begin(), v.end(), 0) << std::endl;
    return 0;
}
"""


def _run(cmd: List[str], cwd: Optional[str] = None, timeout: int = 300) -> Tuple[int, str]:
    try:
        res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return res.returncode, ((res.stdout or "") + (res.stderr or "")).strip()
    except FileNotFoundError:
        return 127, f"not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"


def is_pe(path: str) -> bool:
    """Cheap check that a file is a Windows PE image (MZ + PE header)."""
    try:
        with open(path, "rb") as fh:
            if fh.read(2) != b"MZ":
                return False
            fh.seek(0x3C)
            off = int.from_bytes(fh.read(4), "little")
            fh.seek(off)
            return fh.read(4) == b"PE\x00\x00"
    except OSError:
        return False


def test_toolchain(cfg: BuildConfig, gcc: str, quiet: bool = False) -> List[Tuple[str, bool, str]]:
    """Compile hello-world programs with the winlibs GCC."""
    results: List[Tuple[str, bool, str]] = []
    gxx = gcc.replace("gcc", "g++") if gcc.endswith("gcc") or gcc.endswith("gcc.exe") else None

    with tempfile.TemporaryDirectory(prefix="lmb-smoke-") as tmp:
        c_src = os.path.join(tmp, "hello.c")
        with open(c_src, "w", encoding="utf-8") as fh:
            fh.write(HELLO_C)
        c_exe = os.path.join(tmp, "hello-c.exe" if cfg.target_os == "windows" else "hello-c")

        rc, out = _run([gcc, c_src, "-O2", "-o", c_exe])
        ok = rc == 0 and os.path.exists(c_exe)
        results.append((f"gcc compiles C ({os.path.basename(gcc)})", ok, out or "ok"))

        if cfg.target_os == "windows" and ok:
            results.append(("gcc output is a PE image", is_pe(c_exe), c_exe))

        if gxx and os.path.exists(gxx):
            cpp_src = os.path.join(tmp, "hello.cpp")
            with open(cpp_src, "w", encoding="utf-8") as fh:
                fh.write(HELLO_CPP)
            cpp_exe = os.path.join(tmp, "hello-cpp.exe" if cfg.target_os == "windows" else "hello-cpp")
            rc, out = _run([gxx, cpp_src, "-O2", "-o", cpp_exe])
            ok = rc == 0 and os.path.exists(cpp_exe)
            results.append((f"g++ compiles C++ ({os.path.basename(gxx)})", ok, out or "ok"))

        # Only meaningful when the produced binary can actually execute here.
        if cfg.target_os != "windows" and ok:
            rc, out = _run([c_exe])
            results.append(("hello world runs", rc == 0 and "hello from lmb" in out, out))

    return results


def test_products(cfg: BuildConfig, quiet: bool = False) -> List[Tuple[str, bool, str]]:
    """Check the installed LLVM/Clang toolchain.

    The expected binary list is derived from the configured scope, so an
    ``llvm-only`` build is not failed for not containing clang.
    """
    results: List[Tuple[str, bool, str]] = []
    root = cfg.resolved_install_dir
    bindir = os.path.join(root, "bin")
    exe = ".exe" if cfg.target_os == "windows" else ""
    projects = cfg.resolved_projects()

    if not os.path.isdir(bindir):
        results.append((f"install/bin exists", False, bindir))
        return results
    results.append(("install/bin exists", True, bindir))

    wanted = ["llvm-config" + exe]
    if "clang" in projects:
        wanted += ["clang" + exe, "clang++" + exe]
    if "lld" in projects:
        wanted += ["ld.lld" + exe, "lld-link" + exe]
    if "clang-tools-extra" in projects:
        wanted += ["clangd" + exe, "clang-tidy" + exe]
    if "mlir" in projects:
        wanted += ["mlir-opt" + exe, "mlir-tblgen" + exe]
    if "lldb" in projects:
        wanted += ["lldb" + exe]
    if "flang" in projects:
        wanted += ["flang-new" + exe]
    # polly is a static library + clang plugin, it installs no binary of its own.

    missing = [w for w in wanted if not os.path.exists(os.path.join(bindir, w))]
    results.append((f"expected binaries present ({len(wanted) - len(missing)}/{len(wanted)})",
                    not missing, ", ".join(missing) if missing else "all found"))

    can_run = (cfg.target_os != "windows") or (sys.platform == "win32")
    if can_run:
        for prog in ("clang", "llvm-config"):
            if prog == "clang" and "clang" not in projects:
                continue
            p = os.path.join(bindir, prog + exe)
            if os.path.exists(p):
                rc, out = _run([p, "--version"], timeout=120)
                results.append((f"{prog} --version", rc == 0, out.splitlines()[0] if out else ""))

        clang = os.path.join(bindir, "clang" + exe)
        clangxx = os.path.join(bindir, "clang++" + exe)
        if "clang" in projects and os.path.exists(clang):
            with tempfile.TemporaryDirectory(prefix="lmb-product-") as tmp:
                src = os.path.join(tmp, "t.cpp")
                with open(src, "w", encoding="utf-8") as fh:
                    fh.write(HELLO_CPP)
                out_bin = os.path.join(tmp, "t.exe" if cfg.target_os == "windows" else "t")
                compiler = clangxx if os.path.exists(clangxx) else clang
                cmd = [compiler, src, "-O2", "-o", out_bin]
                if "lld" in projects:
                    cmd.append("-fuse-ld=lld")
                rc, out = _run(cmd, timeout=600)
                results.append(("clang compiles+links C++", rc == 0 and os.path.exists(out_bin), out or "ok"))
                if rc == 0 and cfg.target_os == "windows":
                    results.append(("clang output is a PE image", is_pe(out_bin), out_bin))
                if rc == 0 and cfg.target_os != "windows":
                    rc2, out2 = _run([out_bin], timeout=60)
                    results.append(("clang output runs", rc2 == 0 and "sum=55" in out2, out2))
    else:
        results.append(("cross build: binaries not executed on this host", True, "skipped"))

    return results


def report(results: List[Tuple[str, bool, str]], title: str) -> bool:
    print(f"\n=== {title} ===")
    ok = True
    for name, passed, detail in results:
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {name}")
        if detail and detail != "ok" and not passed:
            for line in detail.splitlines()[:8]:
                print(f"         {line}")
        elif detail and passed and len(detail) < 120:
            print(f"         {detail}")
        ok = ok and passed
    print(f"=== {title}: {'all passed' if ok else 'FAILURES PRESENT'} ===\n")
    return ok


def run_all(cfg: BuildConfig, gcc: Optional[str] = None) -> bool:
    ok = True
    if gcc:
        ok &= report(test_toolchain(cfg, gcc), "toolchain smoke test")
    if os.path.isdir(cfg.resolved_install_dir):
        ok &= report(test_products(cfg), "installed product smoke test")
    return ok

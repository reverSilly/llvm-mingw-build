"""CMake / Ninja orchestration.

Responsibilities:
  * locate cmake + ninja (pip wheels, system packages, or a pinned download),
  * decide which compiler drivers to use (winlibs, a system MinGW cross
    toolchain, or the host compiler for validation builds),
  * configure, build and install, streaming output and keeping a log,
  * remember the exact configuration so later commands reuse it.
"""

from __future__ import annotations

import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple

from .config import BuildConfig

CONFIG_STAMP = "lmb-config.json"


# --------------------------------------------------------------------------- #
# tool discovery
# --------------------------------------------------------------------------- #


def find_cmake(cfg: BuildConfig) -> str:
    """cmake from PATH, or from ``~/.local/bin`` where pip installs it."""
    for cand in ("cmake", "cmake3"):
        p = shutil.which(cand)
        if p:
            return p
    extra = [
        os.path.expanduser("~/.local/bin/cmake"),
        r"C:\Python312\Scripts\cmake.exe",
        "/usr/local/bin/cmake",
    ]
    for p in extra:
        if os.path.isfile(p):
            return p
    raise RuntimeError(
        "cmake not found. Install it with `pip install cmake` (>= 3.20 required)."
    )


def find_ninja(cfg: BuildConfig) -> Optional[str]:
    if cfg.generator.lower() not in ("ninja", "ninja multi-config"):
        return None
    p = shutil.which("ninja") or shutil.which("ninja-build")
    if p:
        return p
    local = os.path.expanduser("~/.local/bin/ninja")
    if os.path.isfile(local):
        return local
    raise RuntimeError("ninja not found. Install it with `pip install ninja`, or pass --generator Make.")


def cmake_version(cmake: str) -> Tuple[int, ...]:
    out = subprocess.run([cmake, "--version"], capture_output=True, text=True)
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", out.stdout or "")
    if not m:
        return (0,)
    return tuple(int(g) for g in m.groups() if g is not None)


def find_system_mingw(cfg: BuildConfig) -> Optional[str]:
    """A distro-provided cross compiler, e.g. ``x86_64-w64-mingw32-gcc``."""
    name = f"{cfg.arch}-w64-mingw32-gcc" + (".exe" if sys.platform == "win32" else "")
    return shutil.which(name) or shutil.which(f"{cfg.arch}-w64-mingw32-gcc")


def resolve_compilers(cfg: BuildConfig) -> Dict[str, str]:
    """Decide which compiler drivers CMake should use.

    Priority: explicit --cc/--cxx > winlibs toolchain > system MinGW cross
    > host compiler (``--native``).
    """
    if cfg.cc and cfg.cxx:
        out = {"cc": cfg.cc, "cxx": cfg.cxx}
        if cfg.rc:
            out["rc"] = cfg.rc
        if cfg.fortran:
            out["fortran"] = cfg.fortran
        out["_source"] = "explicit"
        return out

    tc = cfg.resolved_toolchain_dir
    if os.path.isdir(tc):
        try:
            from .toolchain import compilers as tc_compilers

            found = tc_compilers(tc, cfg)
            found["_source"] = f"winlibs ({tc})"
            return found
        except RuntimeError as exc:
            if not cfg.native and cfg.target_os == "windows":
                # A toolchain directory exists but is unusable - say so loudly
                # rather than silently falling through to the host compiler.
                raise RuntimeError(f"toolchain at {tc} is unusable: {exc}") from exc

    if cfg.target_os == "windows" and not cfg.native:
        sysmingw = find_system_mingw(cfg)
        if sysmingw:
            base = sysmingw[: -len("-gcc")]
            out = {"cc": sysmingw, "cxx": base + "-g++", "_source": f"system mingw ({sysmingw})"}
            for prog, key in (("windres", "rc"), ("gfortran", "fortran")):
                p = shutil.which(f"{cfg.arch}-w64-mingw32-{prog}")
                if p:
                    out[key] = p
            return out
        raise RuntimeError(
            "no MinGW toolchain found. Fetch one with `build.py fetch-toolchain`, "
            "install a system cross compiler, or pass --native to build for the host."
        )

    # Native host build.
    cc = cfg.cc or shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    cxx = cfg.cxx or shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
    if sys.platform == "win32" and not cc:
        cc, cxx = "cl", "cl"
    if not cc or not cxx:
        raise RuntimeError("no host C/C++ compiler found on PATH")
    return {"cc": cc, "cxx": cxx, "_source": f"host ({cc})"}


def build_environment(cfg: BuildConfig, compilers: Dict[str, str]) -> Dict[str, str]:
    """Environment for configure/build: PATH must contain the toolchain bin."""
    env = dict(os.environ)
    if cfg.native:
        return env

    tc = cfg.resolved_toolchain_dir
    if os.path.isdir(tc):
        from .toolchain import bin_path

        extra = bin_path(tc, cfg)
        if extra:
            env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    return env


# --------------------------------------------------------------------------- #
# running commands
# --------------------------------------------------------------------------- #


class Runner:
    """Run commands with live output, logging and dry-run support."""

    def __init__(self, cfg: BuildConfig, log_path: Optional[str] = None):
        self.cfg = cfg
        self.log_path = log_path
        self._log = None
        if log_path:
            os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
            self._log = open(log_path, "a", encoding="utf-8")

    def log(self, text: str) -> None:
        if self._log:
            self._log.write(text + "\n")
            self._log.flush()

    def run(self, cmd: Sequence[str], cwd: Optional[str] = None,
            env: Optional[Dict[str, str]] = None, echo: bool = True) -> int:
        printable = " ".join(shlex.quote(str(c)) if not _simple(str(c)) else str(c) for c in cmd)
        if echo:
            print(f"\n$ {printable}")
            if cwd:
                print(f"  (in {cwd})")
        self.log(f"\n$ {printable}\n  cwd={cwd or os.getcwd()}")

        if self.cfg.dry_run:
            return 0

        start = time.time()
        proc = subprocess.Popen(
            [str(c) for c in cmd],
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            errors="replace",
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            self.log(line.rstrip("\n"))
        rc = proc.wait()
        elapsed = time.time() - start
        summary = f"  -> exit {rc} in {elapsed:.1f}s"
        print(summary)
        self.log(summary)
        if rc != 0 and not self.cfg.keep_going:
            raise SystemExit(f"command failed with exit code {rc}: {printable}")
        return rc

    def close(self) -> None:
        if self._log:
            self._log.close()
            self._log = None


def _simple(s: str) -> bool:
    return all(c.isalnum() or c in "-_./:=+,@" for c in s) and s != ""


# --------------------------------------------------------------------------- #
# stages
# --------------------------------------------------------------------------- #


def configure(cfg: BuildConfig, compilers: Dict[str, str], runner: Runner,
              toolchain_file: Optional[str] = None, force: bool = False) -> str:
    """Run ``cmake`` configure; returns the build directory."""
    cmake = find_cmake(cfg)
    ver = cmake_version(cmake)
    build_dir = cfg.resolved_build_dir
    src = cfg.resolved_source_dir
    os.makedirs(build_dir, exist_ok=True)

    stamp = os.path.join(build_dir, CONFIG_STAMP)
    if os.path.exists(os.path.join(build_dir, "CMakeCache.txt")) and os.path.exists(stamp) and not force:
        print(f"[build] reusing configured tree at {build_dir} (pass --reconfigure to redo)")
        return build_dir

    if not os.path.isfile(os.path.join(src, "llvm", "CMakeLists.txt")):
        raise RuntimeError(f"LLVM sources not found at {src} (run fetch-llvm first)")

    # Fail fast with a useful message instead of a cryptic CMake error deep in
    # flang's configure step.
    if "flang" in cfg.resolved_projects():
        fc = compilers.get("fortran") or cfg.fortran or shutil.which("gfortran")
        if not fc:
            raise RuntimeError(
                "flang was requested but no Fortran compiler is available. "
                "winlibs ships gfortran.exe; on other hosts install gfortran, "
                "or drop flang with --projects 'clang;clang-tools-extra;lld;mlir'."
            )
        compilers.setdefault("fortran", fc)

    # Learn which options this exact LLVM tree declares, so generated flags
    # cannot silently rot as LLVM renames them release over release.
    from .options import declared_options, report as report_dropped

    declared = declared_options(src, cfg.resolved_projects(), cfg.resolved_runtimes())
    dropped: List[str] = []
    print(f"[build] {len(declared)} CMake options discovered in {os.path.basename(src)}")

    args = [cmake] + cfg.cmake_configure_args(
        source_dir=src, build_dir=build_dir, toolchain_file=toolchain_file,
        cmake_version=ver, declared=declared, dropped_out=dropped,
    )
    args += [f"-DCMAKE_C_COMPILER={compilers['cc']}", f"-DCMAKE_CXX_COMPILER={compilers['cxx']}"]
    if compilers.get("rc") and cfg.target_os == "windows":
        args.append(f"-DCMAKE_RC_COMPILER={compilers['rc']}")
    if compilers.get("fortran") and "flang" in cfg.resolved_projects():
        args.append(f"-DCMAKE_Fortran_COMPILER={compilers['fortran']}")
    if find_ninja(cfg) and cfg.generator.lower().startswith("ninja"):
        args.insert(1, f"-DCMAKE_MAKE_PROGRAM={find_ninja(cfg)}")

    env = build_environment(cfg, compilers)
    report_dropped(dropped)
    runner.run(args, env=env)

    cfg.save(stamp)
    return build_dir


def build(cfg: BuildConfig, runner: Runner, targets: Optional[Sequence[str]] = None) -> None:
    cmake = find_cmake(cfg)
    build_dir = cfg.resolved_build_dir
    if not os.path.isdir(build_dir):
        raise RuntimeError(f"build directory {build_dir} does not exist (run configure first)")

    args = [cmake, "--build", build_dir, "--parallel", str(cfg.jobs)]
    wanted = list(targets or cfg.build_targets)
    if wanted:
        args += ["--target", *wanted]
    if cfg.keep_going:
        args += ["--", "-k", "0"] if _is_ninja_build(cfg) else []
    runner.run(args, env=dict(os.environ))


def _is_ninja_build(cfg: BuildConfig) -> bool:
    return cfg.generator.lower().startswith("ninja")


def install(cfg: BuildConfig, runner: Runner, component: Optional[str] = None) -> str:
    cmake = find_cmake(cfg)
    build_dir = cfg.resolved_build_dir
    args = [cmake, "--install", build_dir, "--prefix", cfg.resolved_install_dir]
    if component:
        args += ["--component", component]
    runner.run(args, env=dict(os.environ))
    return cfg.resolved_install_dir


def available_build_targets(build_dir: str) -> List[str]:
    """Targets ninja knows about (best effort, used by ``list-targets``)."""
    ninja = shutil.which("ninja")
    if not ninja:
        return []
    try:
        out = subprocess.run([ninja, "-C", build_dir, "-t", "targets"],
                             capture_output=True, text=True, timeout=120)
    except Exception:
        return []
    names = []
    for line in out.stdout.splitlines():
        name = line.split(":", 1)[0].strip()
        if name and not name.endswith((".o", ".obj", ".a", ".lib", ".d", ".cxx", ".c")):
            names.append(name)
    return sorted(set(names))


def host_summary() -> str:
    lines = [
        f"host            : {platform.system()} {platform.release()} ({platform.machine()})",
        f"python          : {sys.version.split()[0]}",
        f"cpus            : {os.cpu_count()}",
    ]
    try:
        import resource  # POSIX only

        mem = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")  # type: ignore[attr-defined]
        lines.append(f"memory          : {mem / (1024 ** 3):.1f} GiB")
    except Exception:
        pass
    return "\n".join(lines)

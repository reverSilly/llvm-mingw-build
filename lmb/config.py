"""Build configuration and scope presets.

A *scope* is a named bundle of ``LLVM_ENABLE_PROJECTS`` /
``LLVM_ENABLE_RUNTIMES`` / ``LLVM_TARGETS_TO_BUILD`` values.  The presets are
ordered from "the whole monorepo" down to "the smallest thing that still
proves the toolchain works", and every one of them can be overridden
piecemeal on the command line.

Notes on MinGW-w64 feasibility (learned the hard way, kept here so the
defaults stay honest):

* ``bolt``    - BOLT is effectively Linux/x86-64 only; it does not build for
                Windows targets.  Excluded from every Windows preset.
* ``lldb``    - needs libedit/SWIG/Python and is fragile with GCC/MinGW.
                Only part of the ``everything`` scope, and disabled features
                are passed explicitly.
* ``flang``   - requires a Fortran compiler (winlibs ships gfortran) plus MLIR.
                Only part of the ``everything`` scope.
* ``polly``   - builds, but is rarely wanted in a distributed toolchain.
                Only part of the ``everything`` scope.
* runtimes    - ``compiler-rt`` and ``libc`` are built with the just-built
                clang in a second stage; with a GCC-hosted build they are
                frequently broken, so they are opt-in via ``everything``.
"""

from __future__ import annotations

import dataclasses
import json
import os
import platform
import sys
from typing import Dict, List, Optional, Sequence

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #

WINLIBS_REPO = "brechtsanders/winlibs_mingw"
LLVM_REPO = "llvm/llvm-project"

# Everything that can appear in LLVM_ENABLE_PROJECTS, in build order.
ALL_PROJECTS = (
    "bolt",
    "clang",
    "clang-tools-extra",
    "flang",
    "lld",
    "lldb",
    "mlir",
    "polly",
)

# Everything that can appear in LLVM_ENABLE_RUNTIMES.
ALL_RUNTIMES = (
    "compiler-rt",
    "libc",
    "libcxx",
    "libcxxabi",
    "libunwind",
    "openmp",
)

# Targets that actually matter for a distributed Windows toolchain.
COMMON_TARGETS = (
    "X86",
    "ARM",
    "AArch64",
    "RISCV",
    "WebAssembly",
    "NVPTX",
    "AMDGPU",
)

SCOPES: Dict[str, Dict[str, Sequence[str]]] = {
    # The whole monorepo, minus what cannot build for Windows at all.
    "everything": {
        "projects": ("clang", "clang-tools-extra", "lld", "lldb", "mlir", "flang", "polly"),
        "runtimes": ("libcxx", "libcxxabi", "libunwind", "openmp", "compiler-rt"),
        "targets": ("all",),
    },
    # A complete, shippable toolchain.  This is the default.
    "core": {
        "projects": ("clang", "clang-tools-extra", "lld", "mlir"),
        "runtimes": ("libcxx", "libcxxabi", "libunwind"),
        "targets": ("all",),
    },
    # clang / clang-tools-extra / lld only, no C++ runtime.
    "toolchain": {
        "projects": ("clang", "clang-tools-extra", "lld"),
        "runtimes": (),
        "targets": ("all",),
    },
    # Fastest possible meaningful build (smoke tests, CI wiring checks).
    "minimal": {
        "projects": ("clang",),
        "runtimes": (),
        "targets": ("X86",),
    },
    # LLVM libraries and tools, no clang.  Handy for a first cross-compile.
    "llvm-only": {
        "projects": (),
        "runtimes": (),
        "targets": ("X86",),
    },
}

BUILD_TYPES = ("Release", "RelWithDebInfo", "MinSizeRel", "Debug")


def host_is_windows() -> bool:
    """True when we are running on Windows (including MSYS/Cygwin shells)."""
    return platform.system() == "Windows" or sys.platform in ("win32", "cygwin", "msys")


def default_jobs() -> int:
    """Sane default compile parallelism (leaves a core for the OS/linker)."""
    try:
        cpus = len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
    except AttributeError:
        cpus = os.cpu_count() or 2
    return max(1, cpus)


# --------------------------------------------------------------------------- #
# configuration object
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class BuildConfig:
    """Everything needed to reproduce one build."""

    # ---- what to build -------------------------------------------------- #
    scope: str = "core"
    projects: Optional[List[str]] = None
    runtimes: Optional[List[str]] = None
    targets: Optional[List[str]] = None
    build_type: str = "Release"
    assertions: bool = False
    tests: bool = False
    benchmarks: bool = False
    examples: bool = False
    docs: bool = False
    shared_libs: bool = False
    toolchain_only_install: bool = True
    static_libgcc: bool = True

    # ---- which LLVM ----------------------------------------------------- #
    llvm_ref: Optional[str] = None          # None => latest stable release
    llvm_repo: str = LLVM_REPO

    # ---- which winlibs toolchain ---------------------------------------- #
    winlibs_tag: Optional[str] = None       # None => latest release
    winlibs_repo: str = WINLIBS_REPO
    arch: str = "x86_64"                    # x86_64 | i686
    runtime: str = "ucrt"                   # ucrt | msvcrt
    threads_model: str = "posix"            # posix | win32
    exception_model: Optional[str] = None   # None => seh (x86_64) / dwarf (i686)
    toolchain_dir: Optional[str] = None     # pre-extracted toolchain (skips dl)

    # ---- where ---------------------------------------------------------- #
    prefix: str = "work"
    source_dir: Optional[str] = None        # existing llvm-project checkout
    build_dir: Optional[str] = None
    install_dir: Optional[str] = None
    dist_dir: Optional[str] = None

    # ---- how ------------------------------------------------------------ #
    generator: str = "Ninja"
    jobs: int = dataclasses.field(default_factory=default_jobs)
    link_jobs: int = 1
    native: bool = False                    # use the host compiler, not MinGW
    cc: Optional[str] = None
    cxx: Optional[str] = None
    rc: Optional[str] = None
    fortran: Optional[str] = None
    extra_cmake_args: List[str] = dataclasses.field(default_factory=list)
    build_targets: List[str] = dataclasses.field(default_factory=list)

    # ---- packaging ------------------------------------------------------ #
    archive_formats: List[str] = dataclasses.field(default_factory=lambda: ["zip"])
    package_name: Optional[str] = None

    # ---- behaviour ------------------------------------------------------ #
    dry_run: bool = False
    verbose: bool = False
    offline: bool = False                   # never hit the network
    skip_download: bool = False
    keep_going: bool = False

    # ------------------------------------------------------------------ #
    # derived helpers
    # ------------------------------------------------------------------ #
    def __post_init__(self) -> None:
        if self.scope not in SCOPES and (self.projects is None or self.runtimes is None):
            raise ValueError(
                f"unknown scope {self.scope!r}; choose one of {', '.join(SCOPES)} "
                "or pass explicit --projects/--runtimes"
            )
        if self.build_type not in BUILD_TYPES:
            raise ValueError(f"build_type must be one of {', '.join(BUILD_TYPES)}")
        if self.exception_model is None:
            self.exception_model = "seh" if self.arch == "x86_64" else "dwarf"
        self.prefix = os.path.abspath(self.prefix)
        self.jobs = max(1, int(self.jobs))
        self.link_jobs = max(1, int(self.link_jobs))

    # --- resolved component lists ------------------------------------- #
    def resolved_projects(self) -> List[str]:
        items = self.projects if self.projects is not None else list(SCOPES[self.scope]["projects"])
        out = []
        for p in items:
            if p == "bolt" and self.target_os == "windows":
                # BOLT has no Windows support; skip it instead of failing the build.
                continue
            out.append(p)
        return out

    def resolved_runtimes(self) -> List[str]:
        return list(self.runtimes if self.runtimes is not None else SCOPES[self.scope]["runtimes"])

    def resolved_targets(self) -> List[str]:
        items = list(self.targets if self.targets is not None else SCOPES[self.scope]["targets"])
        return items

    @property
    def target_os(self) -> str:
        """OS of the binaries we are producing."""
        if self.native:
            return {"Windows": "windows", "Darwin": "darwin", "Linux": "linux"}.get(
                platform.system(), platform.system().lower()
            )
        return "windows"

    @property
    def target_triple(self) -> str:
        if self.native:
            return ""  # let CMake/LLVM detect it
        vendor = "pc" if self.runtime == "msvcrt" else "win7"
        # GCC's own triple for a UCRT runtime is x86_64-w64-mingw32; keep that,
        # LLVM understands it and it matches the winlibs layout.
        return f"{self.arch}-w64-mingw32" if vendor else f"{self.arch}-w64-mingw32"

    # --- directories --------------------------------------------------- #
    @property
    def download_dir(self) -> str:
        return os.path.join(self.prefix, "downloads")

    @property
    def resolved_source_dir(self) -> str:
        return self.source_dir or os.path.join(self.prefix, "src", "llvm-project")

    @property
    def resolved_build_dir(self) -> str:
        return self.build_dir or os.path.join(self.prefix, "build")

    @property
    def resolved_install_dir(self) -> str:
        return self.install_dir or os.path.join(self.prefix, "install")

    @property
    def resolved_dist_dir(self) -> str:
        return self.dist_dir or os.path.join(self.prefix, "dist")

    @property
    def resolved_toolchain_dir(self) -> str:
        return self.toolchain_dir or os.path.join(self.prefix, "toolchain")

    # --- serialisation -------------------------------------------------- #
    def to_dict(self) -> Dict[str, object]:
        d = dataclasses.asdict(self)
        d["_resolved"] = {
            "projects": self.resolved_projects(),
            "runtimes": self.resolved_runtimes(),
            "targets": self.resolved_targets(),
            "target_os": self.target_os,
            "target_triple": self.target_triple,
            "source_dir": self.resolved_source_dir,
            "build_dir": self.resolved_build_dir,
            "install_dir": self.resolved_install_dir,
            "dist_dir": self.resolved_dist_dir,
            "toolchain_dir": self.resolved_toolchain_dir,
        }
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True, default=str)

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.to_json() + "\n")

    @classmethod
    def load(cls, path: str) -> "BuildConfig":
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data.pop("_resolved", None)
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    # ------------------------------------------------------------------ #
    # CMake arguments
    # ------------------------------------------------------------------ #
    def cmake_configure_args(
        self,
        source_dir: Optional[str] = None,
        build_dir: Optional[str] = None,
        toolchain_file: Optional[str] = None,
        cmake_version: Optional[tuple] = None,
        declared: Optional[set] = None,
        dropped_out: Optional[List[str]] = None,
    ) -> List[str]:
        """Full ``cmake`` command line (excluding the ``cmake`` binary).

        *declared* is the set of CMake option names the source tree actually
        declares (see :mod:`lmb.options`); when given, generated flags that are
        not in it are dropped and their names appended to *dropped_out*.
        """
        src = source_dir or self.resolved_source_dir
        bld = build_dir or self.resolved_build_dir
        llvm_dir = os.path.join(src, "llvm")

        args: List[str] = ["-S", src, "-B", bld, "-G", self.generator]
        # CMake >= 4 dropped compatibility with cmake_minimum_required(<3.5),
        # which a few vendored LLVM sub-projects still use.
        if cmake_version and cmake_version[0] >= 4:
            args += ["-DCMAKE_POLICY_VERSION_MINIMUM=3.5"]

        args += [
            f"-DCMAKE_BUILD_TYPE={self.build_type}",
            f"-DCMAKE_INSTALL_PREFIX={self.resolved_install_dir}",
            f"-DLLVM_ENABLE_PROJECTS={';'.join(self.resolved_projects())}",
            f"-DLLVM_ENABLE_RUNTIMES={';'.join(self.resolved_runtimes())}",
            f"-DLLVM_TARGETS_TO_BUILD={';'.join(self.resolved_targets())}",
            f"-DLLVM_ENABLE_ASSERTIONS={'ON' if self.assertions else 'OFF'}",
            f"-DLLVM_INCLUDE_TESTS={'ON' if self.tests else 'OFF'}",
            f"-DLLVM_INCLUDE_BENCHMARKS={'ON' if self.benchmarks else 'OFF'}",
            f"-DLLVM_INCLUDE_EXAMPLES={'ON' if self.examples else 'OFF'}",
            f"-DLLVM_INCLUDE_DOCS={'ON' if self.docs else 'OFF'}",
            f"-DLLVM_PARALLEL_LINK_JOBS={self.link_jobs}",
            f"-DLLVM_INSTALL_TOOLCHAIN_ONLY={'ON' if self.toolchain_only_install else 'OFF'}",
            "-DLLVM_ENABLE_RTTI=ON",
            "-DLLVM_ENABLE_EH=ON",
            "-DLLVM_ENABLE_LIBXML2=OFF",
            "-DLLVM_ENABLE_ZLIB=ON",
            "-DLLVM_ENABLE_ZSTD=OFF",
            "-DLLVM_INCLUDE_UTILS=ON",
            "-DCLANG_ENABLE_STATIC_ANALYZER=ON",
            "-DCLANG_DEFAULT_LINKER=lld" if "lld" in self.resolved_projects() else "-DCLANG_DEFAULT_LINKER=",
        ]

        if self.shared_libs:
            args += ["-DLLVM_BUILD_LLVM_DYLIB=ON", "-DLLVM_LINK_LLVM_DYLIB=ON"]

        # lldb/flang need these turned down when building with MinGW GCC.
        projects = self.resolved_projects()
        if "lldb" in projects:
            args += [
                "-DLLDB_ENABLE_PYTHON=OFF",
                "-DLLDB_ENABLE_LIBEDIT=OFF",
                "-DLLDB_ENABLE_CURSES=OFF",
                "-DLLDB_ENABLE_LZMA=OFF",
                "-DLLDB_INCLUDE_TESTS=OFF",
            ]
        if "flang" in projects:
            args += ["-DFLANG_INCLUDE_TESTS=OFF"]
        if "mlir" in projects:
            args += ["-DMLIR_INCLUDE_TESTS=OFF", "-DMLIR_INCLUDE_INTEGRATION_TESTS=OFF"]
        if "clang" in projects:
            args += ["-DCLANG_INCLUDE_TESTS=OFF"]

        # Host/target triple: only meaningful for a real MinGW cross/native build.
        if not self.native and self.target_triple:
            args += [
                f"-DLLVM_HOST_TRIPLE={self.target_triple}",
                f"-DLLVM_DEFAULT_TARGET_TRIPLE={self.target_triple}",
            ]

        # Windows binaries should not depend on libgcc/libstdc++ DLLs.
        if self.target_os == "windows" and self.static_libgcc:
            link_flags = "-static-libgcc -static-libstdc++"
            args += [
                f"-DCMAKE_EXE_LINKER_FLAGS={link_flags}",
                f"-DCMAKE_SHARED_LINKER_FLAGS={link_flags}",
            ]

        if toolchain_file:
            args.append(f"-DCMAKE_TOOLCHAIN_FILE={toolchain_file}")

        # Drop generated flags this LLVM tree does not declare (option names move
        # between releases); user-supplied --cmake-arg values are always kept.
        if declared is not None:
            from .options import filter_args

            args = filter_args(args, declared, dropped_out=dropped_out)

        args.extend(self.extra_cmake_args)
        args.append(os.path.abspath(llvm_dir))
        return args

    def describe(self) -> str:
        """One-paragraph human readable summary."""
        lines = [
            f"scope           : {self.scope}",
            f"projects        : {';'.join(self.resolved_projects()) or '(none)'}",
            f"runtimes        : {';'.join(self.resolved_runtimes()) or '(none)'}",
            f"targets         : {';'.join(self.resolved_targets())}",
            f"build type      : {self.build_type} (assertions={'on' if self.assertions else 'off'})",
            f"target platform : {self.target_os} / {self.target_triple or 'host'}",
            f"compiler        : {'host (native)' if self.native else 'winlibs MinGW-w64 GCC'}",
            f"parallelism     : {self.jobs} compile / {self.link_jobs} link",
            f"source          : {self.resolved_source_dir}",
            f"build           : {self.resolved_build_dir}",
            f"install         : {self.resolved_install_dir}",
        ]
        return "\n".join(lines)

"""Command line interface for the LLVM + winlibs build driver."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from typing import List, Optional

from . import builder, llvm, net, packaging, smoketest, toolchain
from .config import BUILD_TYPES, SCOPES, BuildConfig, host_is_windows

COMMANDS = (
    "doctor",
    "info",
    "fetch-toolchain",
    "fetch-llvm",
    "configure",
    "build",
    "install",
    "package",
    "smoke-test",
    "all",
)


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #


def _split(value: Optional[str]) -> Optional[List[str]]:
    if value is None:
        return None
    value = value.strip()
    if not value or value.lower() in ("none", "off", "-"):
        return []
    parts = [p.strip() for p in value.replace(",", ";").split(";") if p.strip()]
    return parts


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="build.py",
        description="Pull the latest LLVM and build it with the latest winlibs MinGW-w64 GCC.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python build.py info                     # what would we build?\n"
            "  python build.py all                      # fetch + build + install + package\n"
            "  python build.py all --scope everything   # the whole monorepo\n"
            "  python build.py all --native --scope minimal --build-targets llvm-tblgen\n"
            "                                           # quick local validation build\n"
        ),
    )
    ap.add_argument("command", nargs="?", default="all", choices=COMMANDS,
                    help="stage to run (default: all)")

    g = ap.add_argument_group("what to build")
    g.add_argument("--scope", default="core", choices=sorted(SCOPES),
                   help="preset bundle of projects/runtimes/targets (default: core)")
    g.add_argument("--projects", default=None, help="LLVM_ENABLE_PROJECTS override, e.g. 'clang;lld'")
    g.add_argument("--runtimes", default=None, help="LLVM_ENABLE_RUNTIMES override, e.g. 'libcxx;libunwind'")
    g.add_argument("--targets", default=None, help="LLVM_TARGETS_TO_BUILD override, e.g. 'X86;ARM' or 'all'")
    g.add_argument("--build-type", default="Release", choices=BUILD_TYPES)
    g.add_argument("--assertions", action="store_true", help="LLVM_ENABLE_ASSERTIONS=ON")
    g.add_argument("--tests", action="store_true", help="build the test suites")
    g.add_argument("--benchmarks", action="store_true")
    g.add_argument("--examples", action="store_true")
    g.add_argument("--docs", action="store_true")
    g.add_argument("--shared-libs", action="store_true",
                   help="build+link a single libLLVM shared library (smaller, faster to link)")
    g.add_argument("--full-install", action="store_true",
                   help="install everything instead of LLVM_INSTALL_TOOLCHAIN_ONLY=ON")

    g = ap.add_argument_group("which LLVM")
    g.add_argument("--llvm-ref", default=None,
                   help="release tag/version (23.1.2) or branch (main); default: latest stable")
    g.add_argument("--llvm-prerelease", action="store_true", help="allow release candidates")
    g.add_argument("--source-dir", default=None, help="use an existing llvm-project checkout")

    g = ap.add_argument_group("which winlibs toolchain")
    g.add_argument("--winlibs-tag", default=None, help="pin a winlibs release tag; default: latest")
    g.add_argument("--arch", default="x86_64", choices=["x86_64", "i686", "aarch64"])
    g.add_argument("--runtime", default="ucrt", choices=["ucrt", "msvcrt"])
    g.add_argument("--threads-model", default="posix", choices=["posix", "win32"])
    g.add_argument("--exception-model", default=None, choices=[None, "seh", "sjlj", "dwarf"],
                   help="default: seh (x86_64) / dwarf (i686)")
    g.add_argument("--toolchain-dir", default=None, help="use an already extracted toolchain")
    g.add_argument("--native", action="store_true",
                   help="build for the host with the host compiler (validation only)")
    g.add_argument("--cc", default=None)
    g.add_argument("--cxx", default=None)

    g = ap.add_argument_group("where")
    g.add_argument("--prefix", default="work", help="root for downloads/src/build/install/dist (default: work)")
    g.add_argument("--build-dir", default=None)
    g.add_argument("--install-dir", default=None)
    g.add_argument("--dist-dir", default=None)

    g = ap.add_argument_group("how")
    g.add_argument("--generator", default="Ninja", help="CMake generator (default: Ninja)")
    g.add_argument("--jobs", "-j", type=int, default=None, help="compile parallelism (default: nproc)")
    g.add_argument("--link-jobs", type=int, default=1, help="parallel link jobs (default: 1, avoids OOM)")
    g.add_argument("--build-targets", default=None,
                   help="only build these ninja targets, e.g. 'llvm-tblgen;FileCheck' (default: everything)")
    g.add_argument("--cmake-arg", action="append", default=[], metavar="KEY=VALUE",
                   help="extra raw CMake flag, repeatable")
    g.add_argument("--toolchain-file", default=None, help="explicit CMake toolchain file")
    g.add_argument("--reconfigure", action="store_true", help="force a fresh cmake configure")
    g.add_argument("--keep-going", action="store_true", help="do not abort on the first failing target")

    g = ap.add_argument_group("packaging")
    g.add_argument("--archive", default="zip", help="zip, 7z, or 'zip,7z' (default: zip)")
    g.add_argument("--package-name", default=None, help="override the archive base name")

    g = ap.add_argument_group("behaviour")
    g.add_argument("--dry-run", action="store_true", help="print commands without running them")
    g.add_argument("--skip-download", action="store_true", help="use cached downloads only")
    g.add_argument("--offline", action="store_true", help="never touch the network")
    g.add_argument("--quiet", action="store_true")
    g.add_argument("--verbose", "-v", action="store_true")
    g.add_argument("--json", action="store_true", help="machine readable output for info/doctor")
    return ap


def config_from_args(args: argparse.Namespace) -> BuildConfig:
    cfg = BuildConfig(
        scope=args.scope,
        projects=_split(args.projects),
        runtimes=_split(args.runtimes),
        targets=_split(args.targets),
        build_type=args.build_type,
        assertions=args.assertions,
        tests=args.tests,
        benchmarks=args.benchmarks,
        examples=args.examples,
        docs=args.docs,
        shared_libs=args.shared_libs,
        toolchain_only_install=not args.full_install,
        llvm_ref=args.llvm_ref,
        source_dir=args.source_dir,
        winlibs_tag=args.winlibs_tag,
        arch=args.arch,
        runtime=args.runtime,
        threads_model=args.threads_model,
        exception_model=args.exception_model,
        toolchain_dir=args.toolchain_dir,
        prefix=args.prefix,
        build_dir=args.build_dir,
        install_dir=args.install_dir,
        dist_dir=args.dist_dir,
        generator=args.generator,
        jobs=args.jobs if args.jobs else BuildConfig().jobs,
        link_jobs=args.link_jobs,
        native=args.native,
        cc=args.cc,
        cxx=args.cxx,
        extra_cmake_args=list(args.cmake_arg),
        build_targets=_split(args.build_targets) or [],
        archive_formats=[f.strip() for f in args.archive.replace(";", ",").split(",") if f.strip()],
        package_name=args.package_name,
        dry_run=args.dry_run,
        verbose=args.verbose,
        offline=args.offline,
        skip_download=args.skip_download,
        keep_going=args.keep_going,
    )
    return cfg


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def cmd_info(cfg: BuildConfig, args: argparse.Namespace) -> int:
    data = {"config": cfg.to_dict()}
    if not cfg.offline:
        try:
            lv = (llvm.resolve_ref(cfg, cfg.llvm_ref) if cfg.llvm_ref
                  else llvm.resolve_latest(cfg, include_prerelease=args.llvm_prerelease))
            data["llvm"] = {
                "tag": lv.tag, "version": lv.version, "asset": lv.asset,
                "asset_size": lv.asset_size, "published_at": lv.published_at,
            }
        except Exception as exc:  # noqa: BLE001
            data["llvm"] = {"error": str(exc)}
        try:
            wl = (toolchain.resolve_tag(cfg, cfg.winlibs_tag) if cfg.winlibs_tag
                  else toolchain.resolve_latest(cfg))
            data["winlibs"] = {
                "tag": wl.tag, "gcc": wl.gcc_version, "mingw": wl.mingw_version,
                "runtime": wl.runtime, "threads": wl.threads, "release": wl.release,
                "archive": toolchain.select_archive(cfg, wl),
                "published_at": wl.published_at,
            }
        except Exception as exc:  # noqa: BLE001
            data["winlibs"] = {"error": str(exc)}

    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0

    print("=== resolved build ===")
    print(cfg.describe())
    lv = data.get("llvm", {})
    wl = data.get("winlibs", {})
    print()
    if "error" in lv:
        print(f"LLVM            : ERROR {lv['error']}")
    else:
        print(f"LLVM            : {lv.get('tag')} (version {lv.get('version')})")
        if lv.get("asset_size"):
            print(f"                  asset {lv.get('asset')} = {net.human_size(lv['asset_size'])}")
    if "error" in wl:
        print(f"winlibs         : ERROR {wl['error']}")
    else:
        print(f"winlibs         : GCC {wl.get('gcc')} + MinGW-w64 {wl.get('mingw')} "
              f"{str(wl.get('runtime')).upper()} r{wl.get('release')} ({wl.get('threads')} threads)")
        print(f"                  archive {wl.get('archive')}")
    return 0


def cmd_doctor(cfg: BuildConfig, args: argparse.Namespace) -> int:
    """Environment health check: tools, resources, network reachability."""
    import platform as _p

    checks: List[dict] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    add("python >= 3.8", sys.version_info >= (3, 8), sys.version.split()[0])

    cmake = shutil.which("cmake")
    if cmake:
        ver = builder.cmake_version(cmake)
        add("cmake >= 3.20", ver >= (3, 20), f"{'.'.join(map(str, ver))} ({cmake})")
    else:
        add("cmake installed", False, "pip install cmake")

    if cfg.generator.lower().startswith("ninja"):
        ninja = shutil.which("ninja")
        add("ninja installed", bool(ninja), ninja or "pip install ninja")

    add("git installed", bool(shutil.which("git")), shutil.which("git") or "")

    cpus = os.cpu_count() or 1
    add("cpus >= 4 recommended", cpus >= 4, f"{cpus} cpu(s)")

    try:
        mem_gib = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 ** 3)  # type: ignore[attr-defined]
        add("memory >= 8 GiB recommended", mem_gib >= 8, f"{mem_gib:.1f} GiB")
    except (ValueError, OSError, AttributeError):
        mem_gib = 0.0
        add("memory", True, "unknown on this platform")

    free = net.disk_free(cfg.prefix) / (1024 ** 3)
    add("free disk >= 40 GiB for a full build", free >= 40, f"{free:.1f} GiB free at {cfg.prefix}")

    if not cfg.offline:
        hosts = {
            "api.github.com": "https://api.github.com/repos/llvm/llvm-project",
            "codeload.github.com": "https://codeload.github.com/",
            "release-assets.githubusercontent.com":
                "https://github.com/llvm/llvm-project/releases/download/llvmorg-23.1.2/llvm-project-23.1.2.src.tar.xz",
        }
        for name, url in hosts.items():
            ok, why = net.reachable(url, timeout=20)
            add(f"network: {name}", ok, why)

    if cfg.native:
        cc = cfg.cc or shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
        add("host compiler", bool(cc), cc or "none found")
    else:
        tc = cfg.resolved_toolchain_dir
        gcc = toolchain.locate_gcc(tc, cfg) if os.path.isdir(tc) else None
        add("winlibs toolchain extracted", bool(gcc), gcc or f"nothing at {tc}")
        if gcc and not host_is_windows():
            add("winlibs runs on this host", False,
                f"{_p.system()} host cannot execute Windows PE binaries; run on Windows or pass --native")

    if args.json:
        print(json.dumps({"checks": checks, "all_ok": all(c["ok"] for c in checks)}, indent=2))
    else:
        print("=== environment ===")
        print(builder.host_summary())
        print(f"free disk       : {free:.1f} GiB at {cfg.prefix}")
        print()
        for c in checks:
            print(f"  [{' ok ' if c['ok'] else 'WARN'}] {c['name']}" + (f" - {c['detail']}" if c['detail'] else ""))
    return 0


def cmd_fetch_toolchain(cfg: BuildConfig, args: argparse.Namespace) -> int:
    root = toolchain.fetch(cfg, quiet=args.quiet)
    gcc = toolchain.locate_gcc(root, cfg)
    if gcc and host_is_windows():
        print(f"[toolchain] {toolchain.version_string(gcc)}")
    return 0


def cmd_fetch_llvm(cfg: BuildConfig, args: argparse.Namespace) -> int:
    ver = (llvm.resolve_ref(cfg, cfg.llvm_ref) if cfg.llvm_ref
           else llvm.resolve_latest(cfg, include_prerelease=args.llvm_prerelease))
    llvm.fetch(cfg, ver, quiet=args.quiet)
    return 0


def _runner(cfg: BuildConfig) -> builder.Runner:
    log = os.path.join(cfg.resolved_build_dir, "build.log")
    return builder.Runner(cfg, log_path=None if cfg.dry_run else log)


def cmd_configure(cfg: BuildConfig, args: argparse.Namespace) -> int:
    compilers = builder.resolve_compilers(cfg)
    print(f"[build] compiler source: {compilers.get('_source')}")
    tf = args.toolchain_file
    if tf is None and cfg.target_os == "windows" and not host_is_windows() and not cfg.native:
        # Cross-compiling from Linux/macOS: use the bundled toolchain file if a
        # cross toolchain was found.
        bundled = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "cmake", "toolchain-mingw-winlibs.cmake")
        if os.path.isfile(bundled) and "system mingw" in str(compilers.get("_source", "")):
            tf = bundled
    runner = _runner(cfg)
    try:
        builder.configure(cfg, compilers, runner, toolchain_file=tf, force=args.reconfigure)
    finally:
        runner.close()
    return 0


def cmd_build(cfg: BuildConfig, args: argparse.Namespace) -> int:
    runner = _runner(cfg)
    try:
        builder.build(cfg, runner)
    finally:
        runner.close()
    return 0


def cmd_install(cfg: BuildConfig, args: argparse.Namespace) -> int:
    runner = _runner(cfg)
    try:
        builder.install(cfg, runner)
    finally:
        runner.close()
    return 0


def cmd_package(cfg: BuildConfig, args: argparse.Namespace) -> int:
    llvm_version = llvm.source_version(cfg.resolved_source_dir) or (cfg.llvm_ref or "unknown")
    gcc_version = mingw_version = None
    wl_stamp = os.path.join(cfg.resolved_toolchain_dir, ".winlibs-release")
    if os.path.isfile(wl_stamp):
        with open(wl_stamp, encoding="utf-8") as fh:
            tag = fh.read().strip().split(":", 1)[0]
        import re

        m = re.match(r"^([\d.]+)(?:posix|win32)-([\d.]+)-", tag)
        if m:
            gcc_version, mingw_version = m.group(1), m.group(2)
    files = packaging.package_install(cfg, llvm_version, gcc_version, mingw_version, quiet=args.quiet)
    print("\n=== package ===")
    for f in files:
        print(f"  {f}  ({net.human_size(os.path.getsize(f))})")
    return 0


def cmd_smoke_test(cfg: BuildConfig, args: argparse.Namespace) -> int:
    gcc = None
    tc = cfg.resolved_toolchain_dir
    if os.path.isdir(tc):
        gcc = toolchain.locate_gcc(tc, cfg)
    ok = smoketest.run_all(cfg, gcc)
    return 0 if ok else 1


def cmd_all(cfg: BuildConfig, args: argparse.Namespace) -> int:
    t0 = time.time()
    stages: List[tuple] = []

    if not cfg.native and cfg.target_os == "windows":
        if host_is_windows():
            stages.append(("fetch-toolchain", cmd_fetch_toolchain))
        else:
            print("[all] host is not Windows: winlibs GCC cannot execute here.")
            print("[all] expecting a toolchain at", cfg.resolved_toolchain_dir)
            if os.path.isdir(cfg.resolved_toolchain_dir):
                stages.append(("fetch-toolchain", cmd_fetch_toolchain))
            else:
                print("[all] skipping toolchain fetch stage; configure will fail unless "
                      "--toolchain-dir/--native/--cc is given", file=sys.stderr)
    stages.append(("fetch-llvm", cmd_fetch_llvm))
    stages.append(("configure", cmd_configure))
    stages.append(("build", cmd_build))
    if cfg.build_targets:
        # A partial build cannot be installed: `cmake --install` would ask ninja
        # to build every missing dependency first (i.e. the whole project).
        print("[all] --build-targets given: skipping install/package/smoke-test "
              "(a partial build tree cannot be installed)")
    else:
        stages.append(("install", cmd_install))
        stages.append(("package", cmd_package))
        stages.append(("smoke-test", cmd_smoke_test))

    failed = []
    for name, fn in stages:
        print(f"\n########## stage: {name} ##########")
        st = time.time()
        try:
            rc = fn(cfg, args)
        except (SystemExit, RuntimeError, net.NetworkError) as exc:
            rc = 1
            print(f"[{name}] FAILED: {exc}", file=sys.stderr)
        took = time.time() - st
        print(f"[{name}] finished rc={rc} in {took:.1f}s")
        if rc != 0:
            failed.append(name)
            if not cfg.keep_going:
                break

    total = time.time() - t0
    print(f"\n=== all stages done in {total / 60:.1f} min; failures: {', '.join(failed) or 'none'} ===")
    return 1 if failed else 0


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = config_from_args(args)

    if args.verbose:
        print(json.dumps(cfg.to_dict(), indent=2, default=str))

    dispatch = {
        "doctor": cmd_doctor,
        "info": cmd_info,
        "fetch-toolchain": cmd_fetch_toolchain,
        "fetch-llvm": cmd_fetch_llvm,
        "configure": cmd_configure,
        "build": cmd_build,
        "install": cmd_install,
        "package": cmd_package,
        "smoke-test": cmd_smoke_test,
        "all": cmd_all,
    }
    fn = dispatch[args.command]
    try:
        return fn(cfg, args)
    except net.NetworkError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

"""winlibs MinGW-w64 GCC toolchain discovery, download and extraction.

winlibs (https://winlibs.com, releases mirrored on GitHub as
``brechtsanders/winlibs_mingw``) publishes **Windows-native** toolchains only:
the archives contain PE executables under ``mingw64/bin`` (x86_64) or
``mingw32/bin`` (i686).  There are no Linux-hosted cross builds, so on a
non-Windows host this module can still *resolve and download* a toolchain, but
running it needs Windows (or Wine, which is out of scope here).

Archive naming, e.g.::

    winlibs-x86_64-posix-seh-gcc-16.2.0-mingw-w64ucrt-14.0.0-r1.zip
    |      |      |     |      |        |              |     |
    prefix arch  threads exc.  gcc ver  mingw-w64 rt   rt ver rel

Release tags mirror the same fields: ``16.2.0posix-14.0.0-ucrt-r1``.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass, field
from typing import List, Optional

from . import net
from .config import BuildConfig

TAG_RE = re.compile(
    r"^(?P<gcc>\d+\.\d+\.\d+)(?P<threads>posix|win32)-"
    r"(?P<mingw>\d+\.\d+\.\d+)-(?P<runtime>ucrt|msvcrt)-r(?P<release>\d+)$"
)

ASSET_RE = re.compile(
    r"^winlibs-(?P<arch>i686|x86_64|aarch64)-(?P<threads>posix|win32)-"
    r"(?P<exc>seh|sjlj|dwarf)-gcc-(?P<gcc>\d+\.\d+\.\d+)-mingw-w64"
    r"(?P<runtime>ucrt|msvcrt)-(?P<mingw>\d+\.\d+\.\d+)-r(?P<release>\d+)\.(?P<ext>zip|7z)$"
)


@dataclass
class WinlibsRelease:
    """One resolved winlibs release."""

    tag: str
    gcc_version: str
    mingw_version: str
    runtime: str
    threads: str
    release: int
    assets: List[dict] = field(default_factory=list)
    published_at: str = ""

    @classmethod
    def from_api(cls, rel: dict) -> Optional["WinlibsRelease"]:
        m = TAG_RE.match(rel.get("tag_name", ""))
        if not m:
            return None
        return cls(
            tag=rel["tag_name"],
            gcc_version=m.group("gcc"),
            mingw_version=m.group("mingw"),
            runtime=m.group("runtime"),
            threads=m.group("threads"),
            release=int(m.group("release")),
            assets=rel.get("assets", []),
            published_at=rel.get("published_at", ""),
        )

    def asset_name(self, arch: str, threads: str, exc: str, ext: str = "zip") -> Optional[str]:
        want = f"winlibs-{arch}-{threads}-{exc}-gcc-{self.gcc_version}-mingw-w64{self.runtime}-{self.mingw_version}-r{self.release}.{ext}"
        names = [a.get("name", "") for a in self.assets]
        return want if want in names else None

    def asset_url(self, name: str) -> Optional[str]:
        for a in self.assets:
            if a.get("name") == name:
                return a.get("browser_download_url")
        return None

    def asset_size(self, name: str) -> Optional[int]:
        for a in self.assets:
            if a.get("name") == name:
                return a.get("size")
        return None

    def describe(self) -> str:
        return (
            f"GCC {self.gcc_version} ({self.threads} threads) + MinGW-w64 "
            f"{self.mingw_version} {self.runtime.upper()} (release {self.release})"
            f" [{self.tag}]"
        )


def resolve_latest(cfg: BuildConfig) -> WinlibsRelease:
    """Newest winlibs release matching the configured threads/runtime model.

    Falls back to the newest release of any flavour if the requested
    combination does not exist (e.g. ``win32`` threads stopped being built).
    """
    rels = net.releases(cfg.winlibs_repo, limit=30, include_prerelease=False)
    parsed = [r for r in (WinlibsRelease.from_api(x) for x in rels) if r]
    if not parsed:
        raise net.NetworkError(f"could not parse any winlibs release tags from {cfg.winlibs_repo}")

    for match_threads, match_runtime in ((True, True), (True, False), (False, False)):
        for r in parsed:
            if match_threads and r.threads != cfg.threads_model:
                continue
            if match_runtime and r.runtime != cfg.runtime:
                continue
            if cfg.arch == "x86_64" and r.asset_name(cfg.arch, r.threads, cfg.exception_model or "seh", "zip"):
                return r
            if cfg.arch != "x86_64" and r.asset_name(cfg.arch, r.threads, "dwarf" if cfg.arch == "i686" else cfg.exception_model or "seh", "zip"):
                return r
    raise net.NetworkError(
        f"no winlibs release found for arch={cfg.arch} threads={cfg.threads_model} runtime={cfg.runtime}"
    )


def resolve_tag(cfg: BuildConfig, tag: str) -> WinlibsRelease:
    """Resolve an explicit release tag."""
    rels = net.releases(cfg.winlibs_repo, limit=100, include_prerelease=True)
    for rel in rels:
        if rel.get("tag_name") == tag:
            parsed = WinlibsRelease.from_api(rel)
            if parsed:
                return parsed
            raise net.NetworkError(f"release tag {tag} has an unrecognised format")
    raise net.NetworkError(f"winlibs release tag {tag!r} not found in the last 100 releases")


def exception_model_for(cfg: BuildConfig) -> str:
    """winlibs uses seh for x86_64 and dwarf for i686."""
    if cfg.exception_model:
        return cfg.exception_model
    return "seh" if cfg.arch == "x86_64" else "dwarf"


def sub_prefix(cfg: BuildConfig) -> str:
    """Directory inside the archive holding the toolchain (``mingw64``/``mingw32``)."""
    return "mingw64" if cfg.arch == "x86_64" else ("mingw32" if cfg.arch == "i686" else "mingw64")


def select_archive(cfg: BuildConfig, rel: WinlibsRelease) -> str:
    """Pick the archive file name, preferring .zip (extractable everywhere)."""
    exc = exception_model_for(cfg)
    for ext in ("zip", "7z"):
        name = rel.asset_name(cfg.arch, cfg.threads_model, exc, ext)
        if name:
            return name
    raise net.NetworkError(
        f"release {rel.tag} has no archive for arch={cfg.arch} threads={cfg.threads_model} "
        f"exception={exc}; available: {', '.join(a.get('name','') for a in rel.assets)}"
    )


def fetch(cfg: BuildConfig, quiet: bool = False) -> str:
    """Download, verify and extract the winlibs toolchain.

    Returns the toolchain root (the directory that contains ``mingw64/bin``).
    """
    dest_root = cfg.resolved_toolchain_dir
    stamp = os.path.join(dest_root, ".winlibs-release")

    rel = resolve_tag(cfg, cfg.winlibs_tag) if cfg.winlibs_tag else resolve_latest(cfg)
    name = select_archive(cfg, rel)

    if os.path.exists(stamp):
        with open(stamp, encoding="utf-8") as fh:
            recorded = fh.read().strip()
        if recorded == f"{rel.tag}:{name}" and locate_gcc(dest_root, cfg):
            if not quiet:
                print(f"[toolchain] already present: {rel.describe()}")
            return dest_root

    if not quiet:
        size = rel.asset_size(name)
        print(f"[toolchain] {rel.describe()}")
        print(f"[toolchain] archive: {name}" + (f" ({net.human_size(size)})" if size else ""))

    os.makedirs(cfg.download_dir, exist_ok=True)
    archive_path = os.path.join(cfg.download_dir, name)

    if cfg.skip_download and os.path.exists(archive_path):
        if not quiet:
            print(f"[toolchain] using pre-downloaded archive (skip-download)")
    else:
        sha = _fetch_checksum(cfg, rel, name, quiet=quiet)
        urls = [u for u in (rel.asset_url(name),) if u]
        urls.append(net.release_asset_url(cfg.winlibs_repo, rel.tag, name))
        try:
            net.download(
                urls,
                archive_path,
                sha256=sha,
                quiet=quiet,
                progress=None if quiet else _progress_printer(name),
            )
        except net.NetworkError as exc:
            raise net.NetworkError(
                f"cannot download the winlibs toolchain.\n{exc}\n\n"
                "winlibs archives are only served from GitHub release assets, "
                "winlibs.com and SourceForge. If those hosts are blocked, download\n"
                f"  {name}\nmanually and either place it in\n  {cfg.download_dir}\n"
                "and re-run with --skip-download, or point --toolchain-dir at an "
                "already-extracted copy."
            ) from exc

    if not quiet:
        print(f"[toolchain] extracting to {dest_root}")
    extract_archive(archive_path, dest_root, cfg)

    os.makedirs(dest_root, exist_ok=True)
    with open(stamp, "w", encoding="utf-8") as fh:
        fh.write(f"{rel.tag}:{name}\n")

    gcc = locate_gcc(dest_root, cfg)
    if not gcc:
        raise RuntimeError(f"extracted toolchain but could not find gcc under {dest_root}")
    if not quiet:
        print(f"[toolchain] gcc: {gcc}")
    return dest_root


def _fetch_checksum(cfg: BuildConfig, rel: WinlibsRelease, name: str, quiet: bool = False) -> Optional[str]:
    """Download the ``.sha256`` sidecar so the archive can be verified."""
    sha_name = name + ".sha256"
    url = rel.asset_url(sha_name) or net.release_asset_url(cfg.winlibs_repo, rel.tag, sha_name)
    path = os.path.join(cfg.download_dir, sha_name)
    try:
        net.download([url], path, quiet=True)
        digest = net.parse_sha256_file(path)
        if digest and not quiet:
            print(f"[toolchain] expected sha256: {digest}")
        return digest
    except net.NetworkError as exc:
        if not quiet:
            print(f"[toolchain] WARNING: could not fetch {sha_name} ({exc}); skipping verification",
                  file=sys.stderr)
        return None


def _progress_printer(label: str):
    state = {"last": -1}

    def cb(done: int, total: Optional[int]) -> None:
        if total:
            pct = int(done * 100 / total)
            bucket = pct // 5
            if bucket != state["last"]:
                state["last"] = bucket
                sys.stdout.write(f"\r[toolchain] {label}: {pct:3d}% ({net.human_size(done)})   ")
                sys.stdout.flush()
                if pct >= 100:
                    sys.stdout.write("\n")
        else:
            sys.stdout.write(f"\r[toolchain] {label}: {net.human_size(done)}   ")
            sys.stdout.flush()

    return cb


def extract_archive(archive_path: str, dest_root: str, cfg: Optional[BuildConfig] = None) -> None:
    """Extract a winlibs ``.zip`` (or ``.7z`` when a 7z tool is available).

    The archive contains a single top-level directory (``mingw64`` /
    ``mingw32``); we strip it so ``dest_root`` itself is the toolchain root.
    """
    if os.path.exists(dest_root):
        shutil.rmtree(dest_root, ignore_errors=True)
    os.makedirs(dest_root, exist_ok=True)

    if archive_path.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            members = zf.namelist()
            tops = {m.split("/")[0] for m in members if "/" in m or m}
            strip = len(tops) == 1 and all(m.startswith(next(iter(tops)) + "/") for m in members if m)
            prefix = next(iter(tops)) + "/" if strip else ""
            for member in members:
                if not member or member.endswith("/"):
                    continue
                rel = member[len(prefix):] if prefix and member.startswith(prefix) else member
                if not rel:
                    continue
                target = os.path.join(dest_root, *rel.split("/"))
                # Guard against path traversal in hostile archives.
                if not os.path.abspath(target).startswith(os.path.abspath(dest_root) + os.sep):
                    raise RuntimeError(f"unsafe path in archive: {member}")
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
        return

    if archive_path.endswith(".7z"):
        for tool in ("7z", "7za", "7zr"):
            exe = shutil.which(tool)
            if exe:
                import subprocess

                subprocess.run(
                    [exe, "x", "-y", f"-o{dest_root}", archive_path],
                    check=True,
                    stdout=subprocess.DEVNULL,
                )
                _flatten_single_top_dir(dest_root)
                return
        raise RuntimeError(
            "extracting a .7z winlibs archive needs 7z/7za/7zr on PATH; "
            "re-run with --archive-ext zip (the default) instead"
        )

    raise RuntimeError(f"unsupported archive type: {archive_path}")


def _flatten_single_top_dir(root: str) -> None:
    entries = [os.path.join(root, e) for e in os.listdir(root)]
    if len(entries) == 1 and os.path.isdir(entries[0]):
        inner = entries[0]
        tmp = root + ".tmp-flatten"
        shutil.move(inner, tmp)
        os.rmdir(root)
        shutil.move(tmp, root)


# --------------------------------------------------------------------------- #
# compiler lookup
# --------------------------------------------------------------------------- #


def _bindirs(toolchain_root: str) -> List[str]:
    """Candidate ``bin`` directories, best first."""
    out = []
    for sub in ("mingw64", "mingw32", "clang64"):
        p = os.path.join(toolchain_root, sub, "bin")
        if os.path.isdir(p):
            out.append(p)
    # A cross-toolchain layout: <root>/bin/x86_64-w64-mingw32-gcc
    if os.path.isdir(os.path.join(toolchain_root, "bin")):
        out.append(os.path.join(toolchain_root, "bin"))
    return out


def _name_variants(name: str, arch: str) -> List[str]:
    """Candidate file names for a tool, in preference order.

    ``.exe`` is always tried - even from a Linux/macOS host - because a winlibs
    tree holds Windows binaries and CI frequently inspects one from a
    non-Windows machine (verifying a download, listing versions, packaging).
    """
    prefixed = f"{arch}-w64-mingw32-{name}"
    variants = []
    for base in (name, prefixed):
        for cand in (base + ".exe", base):
            if cand not in variants:
                variants.append(cand)
    return variants


def find_program(toolchain_root: str, name: str, cfg: Optional[BuildConfig] = None) -> Optional[str]:
    """Locate *name* (``gcc``, ``g++``, ``windres``, ``ar``, ...) in the toolchain."""
    arch = (getattr(cfg, "arch", None) if cfg else None) or "x86_64"
    for bindir in _bindirs(toolchain_root):
        for variant in _name_variants(name, arch):
            cand = os.path.join(bindir, variant)
            if os.path.isfile(cand):
                return os.path.abspath(cand)
    return None


def locate_gcc(toolchain_root: str, cfg: Optional[BuildConfig] = None) -> Optional[str]:
    return find_program(toolchain_root, "gcc", cfg)


def compilers(toolchain_root: str, cfg: BuildConfig) -> dict:
    """Resolve C/C++/RC/Fortran/assembler drivers out of the toolchain tree."""
    want = {
        "cc": "gcc",
        "cxx": "g++",
        "rc": "windres",
        "fortran": "gfortran",
        "ar": "gcc-ar",
        "ranlib": "gcc-ranlib",
        "dlltool": "dlltool",
        "strip": "strip",
        "as": "as",
    }
    found = {}
    missing = []
    for key, prog in want.items():
        p = find_program(toolchain_root, prog, cfg)
        if p:
            found[key] = p
        elif key in ("cc", "cxx"):
            missing.append(prog)
    if missing:
        raise RuntimeError(
            f"toolchain at {toolchain_root} is missing required programs: {', '.join(missing)}"
        )
    return found


def bin_path(toolchain_root: str, cfg: BuildConfig) -> List[str]:
    """Directories that must be prepended to PATH for gcc to find as/ld/etc."""
    paths = _bindirs(toolchain_root)
    sub = sub_prefix(cfg)
    extra = os.path.join(toolchain_root, sub, "lib")
    if os.path.isdir(extra):
        paths.append(extra)
    return paths


def version_string(gcc_path: str, timeout: int = 60) -> str:
    """``gcc --version`` first line, or a clear error if it cannot be executed."""
    import subprocess

    try:
        out = subprocess.run([gcc_path, "--version"], capture_output=True, text=True, timeout=timeout)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except OSError as exc:
        return f"<cannot execute {gcc_path}: {exc}>"
    except subprocess.TimeoutExpired:
        return f"<timeout executing {gcc_path}>"
    except IndexError:
        return "<no output>"

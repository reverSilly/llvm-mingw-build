"""Packaging: build a winlibs-style archive of the installed toolchain.

Produces, for example::

    llvm-23.1.2-gcc-16.2.0-mingw-w64ucrt-14.0.0-x86_64-posix-seh-r1.zip
    llvm-23.1.2-gcc-16.2.0-mingw-w64ucrt-14.0.0-x86_64-posix-seh-r1.zip.sha256
    llvm-23.1.2-gcc-16.2.0-mingw-w64ucrt-14.0.0-x86_64-posix-seh-r1.zip.sha512

``zip`` is done with the standard library (always available); ``7z`` is used
when a 7z/7za/7zr binary exists, which is the format winlibs itself ships.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import zipfile
from typing import List, Optional

from . import net
from .config import BuildConfig


def package_name(cfg: BuildConfig, llvm_version: str, gcc_version: Optional[str],
                 mingw_version: Optional[str], ext: str) -> str:
    if cfg.package_name:
        base = cfg.package_name
        return base if base.endswith(f".{ext}") else f"{base}.{ext}"
    parts = [f"llvm-{llvm_version}"]
    if gcc_version:
        parts.append(f"gcc-{gcc_version}")
    if mingw_version:
        parts.append(f"mingw-w64{cfg.runtime}-{mingw_version}")
    parts.append(cfg.arch)
    parts.append(cfg.threads_model)
    parts.append(cfg.exception_model or ("seh" if cfg.arch == "x86_64" else "dwarf"))
    parts.append(cfg.build_type.lower())
    if cfg.native:
        parts.append("host")
    return "-".join(parts) + f".{ext}"


def make_zip(src_dir: str, dest: str, arc_prefix: str = "", quiet: bool = False) -> str:
    """Zip *src_dir* into *dest*, optionally nesting everything under *arc_prefix*."""
    files: List[str] = []
    for root, _dirs, names in os.walk(src_dir):
        for name in names:
            files.append(os.path.join(root, name))
    files.sort()
    if not quiet:
        print(f"[package] zipping {len(files)} files -> {dest}")
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
    start = time.time()
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for i, path in enumerate(files, 1):
            rel = os.path.relpath(path, src_dir).replace(os.sep, "/")
            zf.write(path, os.path.join(arc_prefix, rel) if arc_prefix else rel)
            if not quiet and i % 500 == 0:
                sys.stdout.write(f"\r[package] {i}/{len(files)} files   ")
                sys.stdout.flush()
    if not quiet:
        sys.stdout.write("\n")
        print(f"[package] {net.human_size(os.path.getsize(dest))} in {time.time() - start:.1f}s")
    return dest


def make_7z(src_dir: str, dest: str, arc_prefix: str = "", quiet: bool = False) -> Optional[str]:
    tool = None
    for cand in ("7z", "7za", "7zr"):
        tool = shutil.which(cand)
        if tool:
            break
    if not tool:
        if not quiet:
            print("[package] 7z not available (install p7zip); skipping .7z output", file=sys.stderr)
        return None
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
    cmd = [tool, "a", "-t7z", "-mx=7", "-mmt=on", dest]
    cmd.append(os.path.join(src_dir, "*"))
    if not quiet:
        print(f"[package] 7z -> {dest}")
    res = subprocess.run(cmd, cwd=src_dir, capture_output=quiet, text=True)
    if res.returncode != 0:
        if quiet:
            print((res.stderr or "").strip(), file=sys.stderr)
        return None
    return dest


def write_checksums(path: str, quiet: bool = False) -> List[str]:
    """Write ``<path>.sha256`` and ``<path>.sha512`` in sha256sum format."""
    base = os.path.basename(path)
    out = []
    for algo, fn in (("sha256", net.file_sha256), ("sha512", net.file_sha512)):
        digest = fn(path)
        sidecar = f"{path}.{algo}"
        with open(sidecar, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(f"{digest}  {base}\n")
        out.append(sidecar)
        if not quiet:
            print(f"[package] {algo}: {digest}")
    return out


def verify_checksum(path: str, sidecar: str) -> bool:
    expected = net.parse_sha256_file(sidecar)
    if not expected:
        return False
    return net.file_sha256(path) == expected


def package_install(cfg: BuildConfig, llvm_version: str, gcc_version: Optional[str] = None,
                    mingw_version: Optional[str] = None, quiet: bool = False) -> List[str]:
    """Archive the install tree into ``cfg.resolved_dist_dir``; returns file paths."""
    install_dir = cfg.resolved_install_dir
    if not os.path.isdir(install_dir) or not os.listdir(install_dir):
        raise RuntimeError(f"nothing to package: {install_dir} is missing or empty")

    dist = cfg.resolved_dist_dir
    os.makedirs(dist, exist_ok=True)
    produced: List[str] = []

    for fmt in cfg.archive_formats:
        fmt = fmt.lower()
        if fmt == "zip":
            name = package_name(cfg, llvm_version, gcc_version, mingw_version, "zip")
            produced.append(make_zip(install_dir, os.path.join(dist, name), quiet=quiet))
        elif fmt == "7z":
            name = package_name(cfg, llvm_version, gcc_version, mingw_version, "7z")
            res = make_7z(install_dir, os.path.join(dist, name), quiet=quiet)
            if res:
                produced.append(res)
        else:
            raise ValueError(f"unknown archive format {fmt!r} (use zip or 7z)")

    for path in list(produced):
        produced.extend(write_checksums(path, quiet=quiet))
    return produced

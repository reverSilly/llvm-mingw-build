"""LLVM release discovery and source acquisition.

Three independent ways to get the sources, tried in order:

1. the official ``llvm-project-<ver>.src.tar.xz`` release asset,
2. the auto-generated codeload tarball of the release tag,
3. a shallow ``git clone`` of the tag (or of ``main`` for bleeding edge).

(1) is the smallest download but lives on the release asset CDN, which is
blocked in some CI sandboxes; (2) is a plain github.com-adjacent host and is
what usually saves the day; (3) always works when git is available.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from typing import List, Optional, Tuple

from . import net
from .config import BuildConfig

TAG_RE = re.compile(r"^llvmorg-(?P<version>\d+\.\d+\.\d+)$")


@dataclass
class LlvmVersion:
    tag: str
    version: str
    prerelease: bool = False
    published_at: str = ""
    asset: Optional[str] = None
    asset_url: Optional[str] = None
    asset_size: Optional[int] = None

    @property
    def is_branch(self) -> bool:
        return self.tag in ("main", "master") or not TAG_RE.match(self.tag)

    def describe(self) -> str:
        if self.is_branch:
            return f"branch {self.tag} (rolling)"
        return f"LLVM {self.version} [{self.tag}]"


def resolve_latest(cfg: BuildConfig, include_prerelease: bool = False) -> LlvmVersion:
    """Newest stable LLVM release."""
    rel = net.latest_release(cfg.llvm_repo, include_prerelease=include_prerelease)
    tag = rel.get("tag_name", "")
    m = TAG_RE.match(tag)
    version = m.group("version") if m else tag.replace("llvmorg-", "")
    asset_name = f"llvm-project-{version}.src.tar.xz"
    url = net.asset_url(rel, asset_name)
    size = None
    for a in rel.get("assets", []):
        if a.get("name") == asset_name:
            size = a.get("size")
            url = url or a.get("browser_download_url")
    return LlvmVersion(
        tag=tag,
        version=version,
        prerelease=bool(rel.get("prerelease")),
        published_at=rel.get("published_at", ""),
        asset=asset_name,
        asset_url=url,
        asset_size=size,
    )


def resolve_ref(cfg: BuildConfig, ref: str) -> LlvmVersion:
    """Turn a user-supplied ``--llvm-ref`` into an :class:`LlvmVersion`.

    Accepts ``23.1.2``, ``llvmorg-23.1.2``, ``main``, ``release/23.x`` or any
    other git ref.
    """
    if ref in ("main", "master") or "/" in ref:
        return LlvmVersion(tag=ref, version=ref)
    tag = ref if ref.startswith("llvmorg-") else f"llvmorg-{ref}"
    m = TAG_RE.match(tag)
    version = m.group("version") if m else ref
    return LlvmVersion(
        tag=tag,
        version=version,
        asset=f"llvm-project-{version}.src.tar.xz" if m else None,
        asset_url=(net.release_asset_url(cfg.llvm_repo, tag, f"llvm-project-{version}.src.tar.xz") if m else None),
    )


def source_version(src_dir: str) -> Optional[str]:
    """Read the LLVM version out of an existing checkout.

    The version moved around over the releases:
      * LLVM <= 20: ``set(LLVM_VERSION_MAJOR 19)`` in ``llvm/CMakeLists.txt``
      * LLVM >= 21: ``cmake/Modules/LLVMVersion.cmake`` (guarded by
        ``if(NOT DEFINED ...)``), included from ``llvm/CMakeLists.txt``
    Both forms are handled, newest location first.
    """
    candidates = [
        os.path.join(src_dir, "cmake", "Modules", "LLVMVersion.cmake"),
        os.path.join(src_dir, "llvm", "CMakeLists.txt"),
    ]
    pattern = re.compile(r"set\(\s*LLVM_VERSION_(MAJOR|MINOR|PATCH)\s+(\d+)\s*\)")
    for path in candidates:
        if not os.path.isfile(path):
            continue
        found: dict = {}
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = pattern.search(line)
                if m:
                    found[m.group(1)] = m.group(2)
                if len(found) == 3:
                    break
        if len(found) == 3:
            return f"{found['MAJOR']}.{found['MINOR']}.{found['PATCH']}"
    return None


def source_is_valid(src_dir: str) -> bool:
    """Enough of the monorepo present to configure from ``llvm/``?"""
    required = [
        os.path.join(src_dir, "llvm", "CMakeLists.txt"),
        os.path.join(src_dir, "llvm", "cmake", "modules"),
    ]
    return all(os.path.exists(p) for p in required)


# --------------------------------------------------------------------------- #
# acquisition
# --------------------------------------------------------------------------- #


def fetch(cfg: BuildConfig, ver: Optional[LlvmVersion] = None, quiet: bool = False) -> str:
    """Make sure the LLVM sources are on disk; return the source root."""
    src = cfg.resolved_source_dir
    if source_is_valid(src):
        existing = source_version(src)
        if not quiet:
            print(f"[llvm] reusing existing sources at {src}" + (f" (version {existing})" if existing else ""))
        return src

    ver = ver or resolve_latest(cfg)
    if not quiet:
        print(f"[llvm] fetching {ver.describe()}")

    os.makedirs(os.path.dirname(src) or ".", exist_ok=True)

    if cfg.skip_download:
        raise RuntimeError(
            f"--skip-download was given but no usable LLVM sources exist at {src}"
        )

    errors: List[str] = []
    for method in ("release-asset", "codeload", "git"):
        try:
            if method == "release-asset" and not ver.is_branch and ver.asset:
                _fetch_tarball(cfg, ver, src, quiet)
            elif method == "codeload" and not ver.is_branch:
                _fetch_codeload(cfg, ver, src, quiet)
            elif method == "git":
                _fetch_git(cfg, ver, src, quiet)
            else:
                continue
            if source_is_valid(src):
                if not quiet:
                    got = source_version(src)
                    print(f"[llvm] sources ready at {src}" + (f" (version {got})" if got else ""))
                return src
            errors.append(f"{method}: extracted but the tree looks incomplete")
            shutil.rmtree(src, ignore_errors=True)
        except (net.NetworkError, RuntimeError, subprocess.CalledProcessError, tarfile.TarError) as exc:
            msg = f"{method}: {exc}"
            errors.append(msg)
            if not quiet:
                print(f"[llvm] {msg}", file=sys.stderr)
            shutil.rmtree(src, ignore_errors=True)

    raise RuntimeError("could not obtain LLVM sources; tried:\n  " + "\n  ".join(errors))


def _fetch_tarball(cfg: BuildConfig, ver: LlvmVersion, src: str, quiet: bool) -> None:
    """Method 1: official release asset (``.src.tar.xz``)."""
    if not ver.asset:
        raise RuntimeError("no release asset known for this ref")
    urls = [u for u in (ver.asset_url,) if u]
    urls.append(net.release_asset_url(cfg.llvm_repo, ver.tag, ver.asset))
    path = os.path.join(cfg.download_dir, ver.asset)
    os.makedirs(cfg.download_dir, exist_ok=True)
    if not quiet:
        print(f"[llvm] trying release asset {ver.asset}"
              + (f" ({net.human_size(ver.asset_size)})" if ver.asset_size else ""))
    net.download(urls, path, quiet=quiet, progress=None if quiet else _printer(ver.asset))
    _extract_tar(path, src, quiet)


def _fetch_codeload(cfg: BuildConfig, ver: LlvmVersion, src: str, quiet: bool) -> None:
    """Method 2: codeload.github.com auto-generated tarball of the tag."""
    url = net.codeload_tarball(cfg.llvm_repo, ver.tag)
    name = f"llvm-project-{ver.tag}.tar.gz"
    path = os.path.join(cfg.download_dir, name)
    os.makedirs(cfg.download_dir, exist_ok=True)
    if not quiet:
        print(f"[llvm] trying codeload tarball {url}")
    net.download([url], path, quiet=quiet, progress=None if quiet else _printer(name))
    _extract_tar(path, src, quiet)


def _fetch_git(cfg: BuildConfig, ver: LlvmVersion, src: str, quiet: bool) -> None:
    """Method 3: shallow git clone (works for tags and branches)."""
    git = shutil.which("git")
    if not git:
        raise RuntimeError("git not found on PATH")
    repo_url = f"https://github.com/{cfg.llvm_repo}.git"
    cmd = [git, "clone", "--depth", "1", "--single-branch", "--branch", ver.tag, repo_url, src]
    if not quiet:
        print(f"[llvm] trying git: {' '.join(cmd)}")
    res = subprocess.run(cmd, capture_output=not quiet, text=True)
    if res.returncode != 0:
        detail = (res.stderr or "").strip().splitlines()[-1:] or ["unknown git error"]
        raise RuntimeError(detail[0])


def _extract_tar(archive: str, dest: str, quiet: bool) -> None:
    """Extract a .tar.xz/.tar.gz into *dest*, flattening the single top dir."""
    if os.path.exists(dest):
        shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)

    mode = "r:xz" if archive.endswith(".xz") else ("r:gz" if archive.endswith(".gz") else "r:*")
    tmp = dest + ".extracting"
    shutil.rmtree(tmp, ignore_errors=True)
    if not quiet:
        print(f"[llvm] extracting {os.path.basename(archive)}")
    with tarfile.open(archive, mode) as tf:
        # Reject absolute/parent-traversing members before touching the disk.
        for m in tf.getmembers():
            if m.name.startswith("/") or ".." in m.name.split("/"):
                raise RuntimeError(f"unsafe path in tarball: {m.name}")
        tf.extractall(tmp, filter="data" if sys.version_info >= (3, 12) else None)

    entries = [os.path.join(tmp, e) for e in os.listdir(tmp)]
    if len(entries) == 1 and os.path.isdir(entries[0]):
        shutil.move(entries[0], dest)
        os.rmdir(tmp)
    else:
        shutil.move(tmp, dest)


def _printer(label: str):
    state = {"last": -1}

    def cb(done: int, total: Optional[int]) -> None:
        if total:
            pct = int(done * 100 / total)
            bucket = pct // 5
            if bucket != state["last"]:
                state["last"] = bucket
                sys.stdout.write(f"\r[llvm] {label}: {pct:3d}% ({net.human_size(done)})   ")
                sys.stdout.flush()
                if pct >= 100:
                    sys.stdout.write("\n")
        else:
            sys.stdout.write(f"\r[llvm] {label}: {net.human_size(done)}   ")
            sys.stdout.flush()

    return cb


def git_head(src_dir: str) -> Optional[str]:
    """Commit id of a git checkout, or None (never walks up into a parent repo)."""
    if not os.path.exists(os.path.join(src_dir, ".git")):
        return None
    try:
        out = subprocess.run(
            ["git", "-C", src_dir, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30
        )
        return out.stdout.strip() or None
    except Exception:
        return None

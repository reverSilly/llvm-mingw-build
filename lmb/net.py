"""Network helpers: GitHub API access, resilient downloads, checksums.

Standard library only.  Everything funnels through :func:`open_url` so that
proxies, tokens, retries and timeouts are handled in exactly one place.

Downloads try a list of candidate URLs in order, which is what makes the LLVM
fetch work in locked-down networks: the release asset CDN is frequently
unreachable from CI sandboxes while ``codeload.github.com`` (the git tarball
service) is not.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

USER_AGENT = "llvm-mingw-build/1.0 (+https://github.com/reverSilly/llvm-mingw-build)"
API = "https://api.github.com"

DEFAULT_TIMEOUT = 60
DEFAULT_RETRIES = 4
CHUNK = 1024 * 256


class NetworkError(RuntimeError):
    """Raised when every candidate URL failed."""

    def __init__(self, message: str, attempts: Optional[List[Tuple[str, str]]] = None):
        super().__init__(message)
        self.attempts = attempts or []


def github_token() -> Optional[str]:
    """Token from the environment, if any (raises the API rate limit 60->5000/h)."""
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        tok = os.environ.get(var)
        if tok:
            return tok.strip()
    return None


def open_url(
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    headers: Optional[dict] = None,
    token: Optional[str] = None,
) -> urllib.request.addinfourl:
    """Open *url* with a browser-ish UA and (optionally) a GitHub token."""
    hdrs = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    if token is None:
        token = github_token()
    if token and "github.com" in url:
        hdrs.setdefault("Authorization", f"Bearer {token}")
    req = urllib.request.Request(url, headers=hdrs)
    return urllib.request.urlopen(req, timeout=timeout)


def get_json(url: str, timeout: int = DEFAULT_TIMEOUT, retries: int = 3) -> object:
    """GET a URL and decode it as JSON, retrying on transient failures."""
    last: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            with open_url(url, timeout=timeout, headers={"Accept": "application/vnd.github+json"}) as fh:
                return json.loads(fh.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, TimeoutError, ValueError) as exc:
            last = exc
            # 403/429 == rate limited: pointless to hammer.
            code = getattr(exc, "code", None)
            if code in (403, 429):
                reset = _rate_limit_reset(exc)
                raise NetworkError(
                    f"GitHub API rate limited ({code}) for {url}"
                    + (f"; resets {reset}" if reset else "")
                    + ". Set GITHUB_TOKEN to raise the limit."
                ) from exc
            if attempt < retries:
                time.sleep(min(2 ** attempt, 10))
    raise NetworkError(f"failed to fetch JSON from {url}: {last}")


def _rate_limit_reset(exc: Exception) -> str:
    try:
        raw = exc.read().decode("utf-8", "replace")  # type: ignore[attr-defined]
        data = json.loads(raw)
        msg = data.get("message", "")
        return msg.split("exceeds")[0].strip() if "exceeds" in msg else ""
    except Exception:
        return ""


def releases(repo: str, limit: int = 30, include_prerelease: bool = False) -> List[dict]:
    """Return up to *limit* releases of *repo*, newest first."""
    url = f"{API}/repos/{repo}/releases?per_page={min(limit, 100)}"
    data = get_json(url)
    if not isinstance(data, list):
        raise NetworkError(f"unexpected response for {url}: {type(data).__name__}")
    if not include_prerelease:
        data = [r for r in data if not r.get("prerelease")]
    return data


def latest_release(repo: str, include_prerelease: bool = False) -> dict:
    rels = releases(repo, limit=10, include_prerelease=include_prerelease)
    if not rels:
        raise NetworkError(f"no releases found for {repo}")
    return rels[0]


def asset_url(release: dict, pattern: str) -> Optional[str]:
    """First asset of *release* whose name contains *pattern*."""
    for asset in release.get("assets", []):
        if pattern in asset.get("name", ""):
            return asset.get("browser_download_url")
    return None


def asset_names(release: dict) -> List[str]:
    return [a.get("name", "") for a in release.get("assets", [])]


# --------------------------------------------------------------------------- #
# checksums
# --------------------------------------------------------------------------- #


def file_sha256(path: str, chunk: int = CHUNK) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def file_sha512(path: str, chunk: int = CHUNK) -> str:
    h = hashlib.sha512()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def parse_sha256_file(path: str) -> Optional[str]:
    """Parse a ``sha256sum``-style file (``<hex>  <name>`` or ``<hex> *<name>``)."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            return line.split()[0].lower()
    return None


# --------------------------------------------------------------------------- #
# download
# --------------------------------------------------------------------------- #


def download(
    urls: Sequence[str],
    dest: str,
    sha256: Optional[str] = None,
    timeout: int = 600,
    retries: int = DEFAULT_RETRIES,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
    quiet: bool = False,
) -> str:
    """Download *urls* (tried in order, duplicates collapsed) into *dest*.

    The file is written to a temporary name first and moved into place only
    after a successful checksum, so an interrupted download never looks valid.
    Already-complete files are reused.

    A URL that fails twice in a row with the *same* reason is abandoned early
    (that is the signature of a blocked/unreachable host, not a transient
    blip), so the fallback mirrors get a chance quickly.
    """
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)

    if os.path.exists(dest) and sha256:
        actual = file_sha256(dest)
        if actual == sha256.lower():
            if not quiet:
                print(f"[net] cached, checksum ok: {dest}")
            return dest
        if not quiet:
            print(f"[net] cached file has wrong checksum ({actual[:12]}...), re-downloading")
        os.remove(dest)
    elif os.path.exists(dest) and not sha256:
        if not quiet:
            print(f"[net] cached (unverified): {dest}")
        return dest

    # Collapse duplicates while preserving order.
    seen: set = set()
    unique_urls = [u for u in urls if u and not (u in seen or seen.add(u))]

    attempts: List[Tuple[str, str]] = []
    fd, tmp = tempfile.mkstemp(prefix=".dl-", dir=os.path.dirname(os.path.abspath(dest)))
    os.close(fd)
    try:
        for url in unique_urls:
            previous_reason: Optional[str] = None
            for attempt in range(1, retries + 1):
                reason: Optional[str] = None
                try:
                    _download_one(url, tmp, timeout, progress, quiet)
                    if sha256:
                        actual = file_sha256(tmp)
                        if actual != sha256.lower():
                            raise NetworkError(
                                f"sha256 mismatch for {os.path.basename(dest)}: "
                                f"expected {sha256.lower()[:16]}..., got {actual[:16]}..."
                            )
                    shutil.move(tmp, dest)
                    return dest
                except NetworkError as exc:
                    # A checksum mismatch is not transient: try the next mirror.
                    reason = str(exc)
                    attempts.append((url, reason))
                    if not quiet:
                        print(f"[net] {reason}", file=sys.stderr)
                    break
                except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout,
                        TimeoutError, ConnectionError, OSError) as exc:
                    reason = _reason(exc)
                    attempts.append((url, reason))
                    if not quiet:
                        print(f"[net] attempt {attempt}/{retries} failed for {url}: {reason}",
                              file=sys.stderr)
                if reason is not None and reason == previous_reason and attempt >= 2:
                    # Same hard failure twice: stop hammering this mirror.
                    if not quiet:
                        print(f"[net] giving up on {url} (repeated: {reason})", file=sys.stderr)
                    break
                previous_reason = reason
                if attempt < retries:
                    time.sleep(min(2 ** attempt, 15))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    # One line per (url, reason) pair, with a repeat count.
    collapsed: List[str] = []
    for url, reason in attempts:
        line = f"{url} -> {reason}"
        if collapsed and collapsed[-1].split(" (x")[0] == line:
            count = 2 if "(x" not in collapsed[-1] else int(collapsed[-1].rsplit("(x", 1)[1].rstrip(")")) + 1
            collapsed[-1] = f"{line} (x{count})"
        else:
            collapsed.append(line)

    raise NetworkError(
        f"could not download {os.path.basename(dest)}; tried:\n  " + "\n  ".join(collapsed),
        attempts,
    )


def _download_one(
    url: str,
    tmp: str,
    timeout: int,
    progress: Optional[Callable[[int, Optional[int]], None]],
    quiet: bool,
) -> None:
    with open_url(url, timeout=timeout) as resp, open(tmp, "wb") as out:
        total = resp.headers.get("Content-Length")
        total_i = int(total) if total and total.isdigit() else None
        done = 0
        last_report = 0.0
        while True:
            block = resp.read(CHUNK)
            if not block:
                break
            out.write(block)
            done += len(block)
            if progress:
                now = time.time()
                if now - last_report > 0.5 or (total_i and done >= total_i):
                    progress(done, total_i)
                    last_report = now
        if total_i is not None and done != total_i:
            raise ConnectionError(f"truncated download: got {done} of {total_i} bytes")
    if not quiet:
        mb = done / (1024 * 1024)
        print(f"[net] downloaded {url.split('/')[-1]} ({mb:.1f} MiB)")


def _reason(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if code:
        return f"HTTP {code}"
    if isinstance(exc, socket.timeout) or isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, urllib.error.URLError):
        inner = exc.reason
        return str(inner)
    return f"{type(exc).__name__}: {exc}"


def reachable(url: str, timeout: int = 15) -> Tuple[bool, str]:
    """Cheap HEAD/GET probe used to pick mirrors before committing to a download."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, str(resp.status)
    except Exception as exc:  # noqa: BLE001 - probing must never raise
        return False, _reason(exc)


def codeload_tarball(repo: str, ref: str) -> str:
    """URL of the auto-generated source tarball for *ref* (works when the
    release asset CDN is blocked)."""
    return f"https://codeload.github.com/{repo}/tar.gz/refs/tags/{ref}"


def release_asset_url(repo: str, tag: str, filename: str) -> str:
    """Conventional release asset download URL (redirects to the asset CDN)."""
    return f"https://github.com/{repo}/releases/download/{tag}/{filename}"


def candidate_urls(repo: str, tag: str, filename: str, codeload_ref: Optional[str] = None) -> List[str]:
    """Ordered download candidates: release asset first, codeload fallback."""
    urls = [release_asset_url(repo, tag, filename)]
    if codeload_ref:
        urls.append(codeload_tarball(repo, codeload_ref))
    return urls


def human_size(num: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(num) < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TiB"


def iter_dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def disk_free(path: str) -> int:
    st = shutil.disk_usage(path if os.path.exists(path) else os.path.dirname(path) or ".")
    return st.free

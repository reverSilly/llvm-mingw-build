# llvm-mingw-build

Prebuilt **LLVM/Clang binaries for Windows, compiled with the winlibs MinGW-w64 GCC
toolchain** — and the automation that produces them.

The driver always pulls *the latest of everything* by default: it asks the GitHub
API which LLVM release and which winlibs release are newest, verifies checksums,
configures the whole monorepo with CMake, builds it with Ninja, installs it, smoke
tests it and packages it into a winlibs-style archive with `.sha256`/`.sha512`
sidecars.

```
LLVM 23.1.2   (released 2026-09-22)   <- resolved live, not pinned
winlibs       GCC 16.2.0 (POSIX/SEH) + MinGW-w64 14.0.0 UCRT, release 1
```

---

## Why Windows / why winlibs

winlibs publishes **Windows-native toolchains only**. Across all 245 releases of
[`brechtsanders/winlibs_mingw`](https://github.com/brechtsanders/winlibs_mingw)
there is not a single Linux-hosted or macOS-hosted cross compiler — the archives
contain PE executables under `mingw64/bin`. So "build LLVM with winlibs GCC"
means *run the build on Windows*. That is exactly what the bundled GitHub Actions
workflow does.

On Linux/macOS the same scripts still work end to end for **validation** using the
host compiler (`--native`), which is how the CMake wiring is tested without a
Windows machine.

---

## Quick start

### Windows (produces real Windows binaries)

```powershell
# 1. prerequisites: Python 3.8+, git
python -m pip install --upgrade cmake ninja

# 2. see what would be built (latest LLVM + latest winlibs)
.\build.ps1 info

# 3. check the machine (cpu/ram/disk/network + toolchain presence)
.\build.ps1 doctor

# 4. the whole thing: fetch -> configure -> build -> install -> package -> smoke test
.\build.ps1 all --scope core --jobs 16

# result: work\dist\llvm-23.1.2-gcc-16.2.0-mingw-w64ucrt-14.0.0-x86_64-posix-seh-release.zip
#         + .sha256 + .sha512
```

### Linux / macOS (validation with the host compiler)

```bash
make info                                    # resolve latest versions
make doctor                                  # environment + network health
make validate                                # configure the full core scope and
                                             # build a few small tools (~5 min)
make test                                    # 42 unit tests
```

### GitHub Actions (the real full build)

`.github/workflows/build-llvm-winlibs.yml` runs on a Windows runner: it resolves
the newest LLVM + winlibs, downloads and verifies them, builds, smoke tests,
packages and uploads the archive (optionally publishing a GitHub release).

Trigger it from the repo's **Actions** tab → *build-llvm-winlibs* → **Run
workflow**, or:

```bash
gh workflow run build-llvm-winlibs.yml \
  -f scope=core -f targets=all -f build_type=Release -f publish_release=true
```

> `workflow_dispatch` only sees workflows that exist on the **default branch**, so
> merge this branch to `main` first. Pushing a `v*` tag triggers it too.

---

## What "the whole project" means

`--scope` selects a bundle of `LLVM_ENABLE_PROJECTS` / `LLVM_ENABLE_RUNTIMES` /
`LLVM_TARGETS_TO_BUILD`. Anything can be overridden individually.

| scope | projects | runtimes | targets | notes |
|---|---|---|---|---|
| `everything` | clang, clang-tools-extra, lld, lldb, mlir, flang, polly | libcxx, libcxxabi, libunwind, openmp, compiler-rt | all | the full monorepo; needs a big runner |
| `core` *(default)* | clang, clang-tools-extra, lld, mlir | libcxx, libcxxabi, libunwind | all | a complete shippable toolchain |
| `toolchain` | clang, clang-tools-extra, lld | – | all | no C++ runtime |
| `minimal` | clang | – | X86 | fastest meaningful build |
| `llvm-only` | – | – | X86 | LLVM libraries and tools |

Deliberate exclusions, with reasons:

* **`bolt`** — BOLT is Linux/x86-64 only; it cannot be built for a Windows target
  and is filtered out automatically on Windows.
* **`lldb`/`flang`/`polly`** — only in `everything`. lldb needs libedit/SWIG/Python
  (disabled explicitly: `LLDB_ENABLE_PYTHON=OFF`, `LLDB_ENABLE_LIBEDIT=OFF`,
  `LLDB_ENABLE_CURSES=OFF`, `LLDB_ENABLE_LZMA=OFF`), flang needs a Fortran compiler
  (winlibs ships `gfortran.exe`; the driver fails fast with a clear message if none
  exists).
* **`compiler-rt`/`libc`** — only in `everything`; they are awkward in a
  GCC-hosted first stage.

### Resource requirements

| scope | build dir | link RAM | 4 cores | 16 cores |
|---|---|---|---|---|
| minimal | ~10 GB | ~2 GB | ~1 h | ~20 min |
| toolchain | ~16 GB | ~4 GB | ~3 h | ~50 min |
| core | ~25 GB | ~6 GB | ~5 h | ~1.5 h |
| everything | ~45 GB | ~8 GB | 12 h+ | ~4 h |

`LLVM_PARALLEL_LINK_JOBS=1` is forced by default — linking `clang.exe` with
binutils `ld` is the single most common OOM in MinGW LLVM builds.

---

## Command reference

```
build.py <command> [options]

  doctor           environment, resources and network reachability
  info             resolve + print the newest LLVM and winlibs (--json for CI)
  fetch-toolchain  download, sha256-verify and extract winlibs GCC
  fetch-llvm       download and extract the LLVM sources (3 fallbacks)
  configure        cmake configure
  build            ninja build (optionally --build-targets a;b;c)
  install          cmake --install
  package          archive + .sha256/.sha512
  smoke-test       compile & run sanity checks
  all              every stage in order
```

Frequently used options:

| option | meaning |
|---|---|
| `--scope`, `--projects`, `--runtimes`, `--targets` | what to build |
| `--llvm-ref 23.1.2` / `--llvm-ref main` | pin LLVM, or track a branch |
| `--winlibs-tag 16.2.0posix-14.0.0-ucrt-r1` | pin the toolchain release |
| `--arch x86_64\|i686`, `--runtime ucrt\|msvcrt`, `--threads-model posix\|win32` | toolchain flavour |
| `--build-type`, `--assertions`, `--shared-libs` | optimisation / layout |
| `--jobs N`, `--link-jobs N` | parallelism (link-jobs 1 by default) |
| `--native` | use the host compiler instead of MinGW (validation) |
| `--toolchain-dir DIR` | use an already extracted toolchain |
| `--skip-download` | use cached archives only (air-gapped) |
| `--cmake-arg KEY=VALUE` | raw extra CMake flag, repeatable |
| `--archive zip,7z` | output formats |
| `--dry-run`, `--json`, `--quiet`, `--verbose` | plumbing |

Everything is reproducible: each configure writes `build/lmb-config.json` with the
fully resolved configuration, and `build/build.log` with the complete output.

---

## How it stays correct against a moving target

Building "latest" against a hard-coded flag list rots within one release, so the
driver defends itself:

1. **Live version resolution.** Newest non-prerelease LLVM tag and newest winlibs
   release are queried from the GitHub API at run time (`info` shows both).
2. **Option scanner** (`lmb/options.py`). Before configuring, the source tree is
   scanned for every `option()`, `set()` and — new in LLVM 23 — 
   `add_optional_dependency()` declaration, and generated `-D` flags that the tree
   does not declare are dropped and reported instead of being silently ignored.
   This is what keeps flags like `LLVM_ENABLE_TERMINFO`, `CLANG_ENABLE_ARCMT` and
   `FLANG_BUILD_NEW_DRIVER` (all removed in LLVM 23) from becoming no-ops.
   Your own `--cmake-arg` values are never filtered.
3. **Three-tier source fetch.** release asset → `codeload.github.com` tarball →
   shallow `git clone`. In networks where the release CDN is blocked (common in CI
   sandboxes) the build still gets its sources.
4. **Checksum-verified downloads.** winlibs `.sha256` sidecars are fetched and
   enforced; downloads are written to a temp file and only moved into place after
   verification, so a truncated download can never look valid.
5. **CMake 4 compatibility.** `CMAKE_POLICY_VERSION_MINIMUM=3.5` is passed
   automatically when CMake ≥ 4 is detected.

---

## Layout

```
build.py                     entry point (Windows / Linux / macOS)
build.sh, build.ps1          thin wrappers
lmb/
  cli.py                     argument parsing + command dispatch
  config.py                  scopes, defaults, CMake flag generation
  options.py                 discovers which CMake options the tree declares
  net.py                     GitHub API, resilient downloads, checksums
  toolchain.py               winlibs resolution / download / extraction / lookup
  llvm.py                    LLVM version resolution + source acquisition
  builder.py                 compiler resolution, configure, build, install
  packaging.py               zip/7z archives + sha256/sha512
  smoketest.py               toolchain and product sanity checks
cmake/
  toolchain-mingw-winlibs.cmake   CMake toolchain file (native + cross)
CMakePresets.json            presets for winlibs-core / -everything / -minimal
.github/workflows/           the Windows CI build
tests/                       42 unit tests (no network required)
Makefile                     convenience targets
```

---

## Troubleshooting

**`cannot download the winlibs toolchain`** — the archives are only served from
GitHub release assets, `winlibs.com` and SourceForge. If those hosts are blocked,
download the archive by hand, drop it into `work/downloads/` and re-run with
`--skip-download`, or point `--toolchain-dir` at an extracted copy. The error
message prints the exact file name and directory.

**`flang was requested but no Fortran compiler is available`** — install gfortran,
or drop flang: `--projects "clang;clang-tools-extra;lld;mlir"`.

**Out of memory while linking `clang.exe`** — keep `--link-jobs 1`, add
`--shared-libs` (single `libLLVM` shared library instead of static archives), or
build `--scope toolchain` first.

**Out of disk** — `make clean` between runs; use `--targets X86;ARM;AArch64`
instead of `all`; `--build-type MinSizeRel` shrinks objects.

**Windows: path too long** — enable long paths (`LongPathsEnabled=1`, the CI
workflow does this) and keep the prefix short, e.g. `C:\lmb`.

**Windows: build is inexplicably slow** — Windows Defender scanning every object
file. Add an exclusion for the build directory (the CI workflow does this).

**A `-D` flag seems ignored** — run `configure` and look for
`[options] dropped flags not declared by this LLVM tree`; it means LLVM renamed or
removed that option in this version.

---

## Validation status

The driver is exercised for real, not just written:

| what | where | result |
|---|---|---|
| 42 unit tests (scopes, flag generation, option scanner, checksums, archive extraction, packaging, PE detection, version parsing) | any host | pass |
| version resolution: LLVM 23.1.2, winlibs GCC 16.2.0 + MinGW-w64 14.0.0 UCRT r1 | GitHub API | pass |
| source fetch incl. CDN-blocked fallback to codeload | Linux sandbox | pass (277 MB, 23.1.2 verified) |
| CMake configure of the full `core` scope, all 20 targets | Linux sandbox, GCC 12 | pass, 20 s, 37 954 ninja targets |
| CMake configure of `everything` minus flang (lldb + polly + mlir) | Linux sandbox | pass, LLDB 23.1.2 detected |
| real compilation: `llvm-tblgen`, `FileCheck`, `llvm-config`, `count`, `not` | Linux sandbox | pass, 344 targets / 308 s; `llvm-config --version` → 23.1.2 |
| packaging + sha256/sha512 + smoke test | Linux sandbox | pass |
| the winlibs build itself | requires Windows | run it via the workflow above |

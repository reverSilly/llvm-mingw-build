"""Unit tests for the lmb build driver.

Run with::

    python3 -m unittest discover -s tests -t . -v

No network access and no LLVM sources are required (tests that benefit from a
real checkout skip themselves when it is absent).
"""

from __future__ import annotations

import io
import os
import struct
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lmb import llvm, net, options, packaging, smoketest, toolchain  # noqa: E402
from lmb.config import ALL_PROJECTS, ALL_RUNTIMES, SCOPES, BuildConfig  # noqa: E402


class TestScopes(unittest.TestCase):
    def test_presets_reference_known_components(self):
        for name, preset in SCOPES.items():
            for p in preset["projects"]:
                self.assertIn(p, ALL_PROJECTS, f"scope {name}: unknown project {p}")
            for r in preset["runtimes"]:
                self.assertIn(r, ALL_RUNTIMES, f"scope {name}: unknown runtime {r}")

    def test_unknown_scope_rejected(self):
        with self.assertRaises(ValueError):
            BuildConfig(scope="does-not-exist")

    def test_bad_build_type_rejected(self):
        with self.assertRaises(ValueError):
            BuildConfig(build_type="SuperFast")

    def test_bolt_excluded_for_windows(self):
        cfg = BuildConfig(scope="everything")
        self.assertNotIn("bolt", cfg.resolved_projects())

    def test_explicit_projects_override_scope(self):
        cfg = BuildConfig(scope="everything", projects=["clang"], runtimes=[])
        self.assertEqual(cfg.resolved_projects(), ["clang"])
        self.assertEqual(cfg.resolved_runtimes(), [])

    def test_target_triple(self):
        self.assertEqual(BuildConfig(arch="x86_64").target_triple, "x86_64-w64-mingw32")
        self.assertEqual(BuildConfig(arch="i686").target_triple, "i686-w64-mingw32")

    def test_exception_model_defaults(self):
        self.assertEqual(BuildConfig(arch="x86_64").exception_model, "seh")
        self.assertEqual(BuildConfig(arch="i686").exception_model, "dwarf")


class TestCMakeArgs(unittest.TestCase):
    def test_essential_flags_present(self):
        cfg = BuildConfig(scope="core")
        args = cfg.cmake_configure_args(source_dir="/s", build_dir="/b", cmake_version=(3, 31, 0))
        joined = " ".join(args)
        for needle in (
            "-DLLVM_ENABLE_PROJECTS=clang;clang-tools-extra;lld;mlir",
            "-DLLVM_TARGETS_TO_BUILD=all",
            "-DCMAKE_BUILD_TYPE=Release",
            "-DLLVM_ENABLE_ASSERTIONS=OFF",
            "-DLLVM_INCLUDE_TESTS=OFF",
            "-DLLVM_PARALLEL_LINK_JOBS=1",
            "-DLLVM_ENABLE_RTTI=ON",
            "-DLLVM_HOST_TRIPLE=x86_64-w64-mingw32",
            "-DCMAKE_EXE_LINKER_FLAGS=-static-libgcc -static-libstdc++",
        ):
            self.assertIn(needle, joined, f"missing {needle}")
        self.assertTrue(args[0] == "-S" and args[-1].endswith(os.path.join("/s", "llvm")))

    def test_no_known_dead_flags(self):
        """Flags removed upstream must never be generated again."""
        cfg = BuildConfig(scope="everything")
        args = cfg.cmake_configure_args(source_dir="/s", build_dir="/b")
        for dead in ("LLVM_ENABLE_TERMINFO", "CLANG_ENABLE_ARCMT", "FLANG_BUILD_NEW_DRIVER"):
            self.assertFalse(any(dead in a for a in args), f"{dead} should not be emitted")

    def test_cmake4_policy_shim_only_when_needed(self):
        cfg = BuildConfig()
        with4 = cfg.cmake_configure_args(source_dir="/s", build_dir="/b", cmake_version=(4, 4, 3))
        with3 = cfg.cmake_configure_args(source_dir="/s", build_dir="/b", cmake_version=(3, 31, 0))
        self.assertTrue(any("CMAKE_POLICY_VERSION_MINIMUM" in a for a in with4))
        self.assertFalse(any("CMAKE_POLICY_VERSION_MINIMUM" in a for a in with3))

    def test_lldb_flags_only_with_lldb(self):
        core = BuildConfig(scope="core").cmake_configure_args(source_dir="/s", build_dir="/b")
        every = BuildConfig(scope="everything").cmake_configure_args(source_dir="/s", build_dir="/b")
        self.assertFalse(any("LLDB_ENABLE_PYTHON" in a for a in core))
        self.assertTrue(any("-DLLDB_ENABLE_PYTHON=OFF" in a for a in every))

    def test_user_args_always_kept(self):
        cfg = BuildConfig(scope="core", extra_cmake_args=["-DTOTALLY_MADE_UP=1"])
        args = cfg.cmake_configure_args(source_dir="/s", build_dir="/b", declared={"LLVM_ENABLE_RTTI"})
        self.assertIn("-DTOTALLY_MADE_UP=1", args)

    def test_config_roundtrip(self):
        cfg = BuildConfig(scope="everything", arch="i686", runtime="msvcrt", assertions=True)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cfg.json")
            cfg.save(path)
            back = BuildConfig.load(path)
        self.assertEqual(back.scope, "everything")
        self.assertEqual(back.arch, "i686")
        self.assertEqual(back.runtime, "msvcrt")
        self.assertTrue(back.assertions)


class TestOptionFilter(unittest.TestCase):
    def test_undeclared_flags_dropped(self):
        declared = {"LLVM_ENABLE_RTTI", "LLVM_TARGETS_TO_BUILD"}
        dropped = []
        kept = options.filter_args(
            ["-S", "/s", "-DLLVM_ENABLE_RTTI=ON", "-DLLVM_ENABLE_TERMINFO=OFF",
             "-DCMAKE_BUILD_TYPE=Release", "-DLLVM_TARGETS_TO_BUILD=all"],
            declared, dropped_out=dropped,
        )
        self.assertEqual(dropped, ["LLVM_ENABLE_TERMINFO"])
        self.assertIn("-DLLVM_ENABLE_RTTI=ON", kept)
        self.assertIn("-DCMAKE_BUILD_TYPE=Release", kept)   # CMAKE_* always kept
        self.assertIn("-S", kept)

    def test_no_declared_set_means_no_filtering(self):
        args = ["-DWHATEVER=1"]
        self.assertEqual(options.filter_args(args, None), args)

    def test_scanner_finds_all_declaration_forms(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "llvm", "cmake", "modules"))
            with open(os.path.join(tmp, "llvm", "CMakeLists.txt"), "w") as fh:
                fh.write('set(LLVM_TARGETS_TO_BUILD "all" CACHE STRING "")\n'
                         'option(LLVM_ENABLE_RTTI "rtti" OFF)\n')
            with open(os.path.join(tmp, "llvm", "cmake", "modules", "X.cmake"), "w") as fh:
                fh.write('add_optional_dependency(LLDB_ENABLE_PYTHON "py" Python FOUND)\n')
            os.makedirs(os.path.join(tmp, "lldb", "cmake"))
            with open(os.path.join(tmp, "lldb", "CMakeLists.txt"), "w") as fh:
                fh.write('option(LLDB_INCLUDE_TESTS "tests" ON)\n')
            found = options.declared_options(tmp, ["lldb"], [])
        for name in ("LLVM_TARGETS_TO_BUILD", "LLVM_ENABLE_RTTI", "LLDB_ENABLE_PYTHON", "LLDB_INCLUDE_TESTS"):
            self.assertIn(name, found)

    @unittest.skipUnless(
        os.path.isfile(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "work", "src", "llvm-project", "llvm", "CMakeLists.txt")),
        "no local LLVM checkout",
    )
    def test_real_tree_known_options(self):
        src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "work", "src", "llvm-project")
        found = options.declared_options(src, ["clang", "lld", "mlir"], ["libcxx"])
        self.assertGreater(len(found), 500)
        for name in ("LLVM_ENABLE_RTTI", "LLVM_TARGETS_TO_BUILD", "CLANG_ENABLE_STATIC_ANALYZER"):
            self.assertIn(name, found)


class TestChecksums(unittest.TestCase):
    def test_sha256_and_sidecar_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = os.path.join(tmp, "blob.bin")
            with open(f, "wb") as fh:
                fh.write(b"llvm-mingw-build" * 100)
            digest = net.file_sha256(f)
            self.assertEqual(len(digest), 64)
            sidecar = os.path.join(tmp, "blob.bin.sha256")
            with open(sidecar, "w") as fh:
                fh.write(f"{digest}  blob.bin\n")
            self.assertEqual(net.parse_sha256_file(sidecar), digest)
            self.assertTrue(packaging.verify_checksum(f, sidecar))
            with open(f, "ab") as fh:
                fh.write(b"tampered")
            self.assertFalse(packaging.verify_checksum(f, sidecar))

    def test_download_rejects_wrong_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src.bin")
            with open(src, "wb") as fh:
                fh.write(b"payload")
            dest = os.path.join(tmp, "out.bin")
            with self.assertRaises(net.NetworkError):
                net.download(["file://" + src], dest, sha256="0" * 64, quiet=True, retries=1)
            self.assertFalse(os.path.exists(dest), "failed download must not leave a file behind")
            net.download(["file://" + src], dest, sha256=net.file_sha256(src), quiet=True, retries=1)
            self.assertTrue(os.path.exists(dest))

    def test_duplicate_urls_collapsed(self):
        url = "http://127.0.0.1:1/definitely-not-listening.bin"
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "x.bin")
            with self.assertRaises(net.NetworkError) as ctx:
                net.download([url, url, url], dest, retries=2, quiet=True)
        msg = str(ctx.exception)
        self.assertEqual(msg.count(url), 1, f"url should be reported once, got:\n{msg}")
        self.assertIn("(x2)", msg)


class TestWinlibsResolution(unittest.TestCase):
    API_PAYLOAD = {
        "tag_name": "16.2.0posix-14.0.0-ucrt-r1",
        "published_at": "2026-08-09T18:08:51Z",
        "assets": [
            {"name": "winlibs-x86_64-posix-seh-gcc-16.2.0-mingw-w64ucrt-14.0.0-r1.zip",
             "size": 287309000,
             "browser_download_url": "https://example.invalid/x86_64-ucrt.zip"},
            {"name": "winlibs-x86_64-posix-seh-gcc-16.2.0-mingw-w64ucrt-14.0.0-r1.zip.sha256",
             "browser_download_url": "https://example.invalid/x86_64-ucrt.zip.sha256"},
            {"name": "winlibs-x86_64-posix-seh-gcc-16.2.0-mingw-w64ucrt-14.0.0-r1.7z",
             "browser_download_url": "https://example.invalid/x86_64-ucrt.7z"},
            {"name": "winlibs-i686-posix-dwarf-gcc-16.2.0-mingw-w64ucrt-14.0.0-r1.zip",
             "browser_download_url": "https://example.invalid/i686-ucrt.zip"},
        ],
    }

    def test_tag_parsing(self):
        rel = toolchain.WinlibsRelease.from_api(self.API_PAYLOAD)
        self.assertIsNotNone(rel)
        self.assertEqual(rel.gcc_version, "16.2.0")
        self.assertEqual(rel.mingw_version, "14.0.0")
        self.assertEqual(rel.runtime, "ucrt")
        self.assertEqual(rel.threads, "posix")
        self.assertEqual(rel.release, 1)

    def test_unparseable_tag_ignored(self):
        self.assertIsNone(toolchain.WinlibsRelease.from_api({"tag_name": "8.4.1-snapshot20210401-r1",
                                                             "assets": []}))

    def test_archive_selection_prefers_zip(self):
        rel = toolchain.WinlibsRelease.from_api(self.API_PAYLOAD)
        cfg = BuildConfig(arch="x86_64", runtime="ucrt")
        self.assertEqual(
            toolchain.select_archive(cfg, rel),
            "winlibs-x86_64-posix-seh-gcc-16.2.0-mingw-w64ucrt-14.0.0-r1.zip",
        )
        cfg32 = BuildConfig(arch="i686", runtime="ucrt")
        self.assertEqual(
            toolchain.select_archive(cfg32, rel),
            "winlibs-i686-posix-dwarf-gcc-16.2.0-mingw-w64ucrt-14.0.0-r1.zip",
        )

    def test_missing_arch_raises_with_available_list(self):
        rel = toolchain.WinlibsRelease.from_api(self.API_PAYLOAD)
        with self.assertRaises(net.NetworkError) as ctx:
            toolchain.select_archive(BuildConfig(arch="aarch64"), rel)
        self.assertIn("winlibs-x86_64-posix-seh", str(ctx.exception))

    def test_asset_url_and_size(self):
        rel = toolchain.WinlibsRelease.from_api(self.API_PAYLOAD)
        name = "winlibs-x86_64-posix-seh-gcc-16.2.0-mingw-w64ucrt-14.0.0-r1.zip"
        self.assertEqual(rel.asset_url(name), "https://example.invalid/x86_64-ucrt.zip")
        self.assertEqual(rel.asset_size(name), 287309000)
        self.assertIsNone(rel.asset_url("nope.zip"))


class TestToolchainExtraction(unittest.TestCase):
    """Build a synthetic winlibs-shaped archive and run the real code on it."""

    def _make_zip(self, path: str, top: str = "mingw64") -> None:
        with zipfile.ZipFile(path, "w") as zf:
            for name in ("bin/gcc.exe", "bin/g++.exe", "bin/windres.exe", "bin/ar.exe",
                         "bin/ranlib.exe", "bin/gcc-ar.exe", "bin/gcc-ranlib.exe",
                         "bin/gfortran.exe", "bin/dlltool.exe",
                         "bin/strip.exe", "bin/as.exe", "bin/ld.exe",
                         "lib/gcc/x86_64-w64-mingw32/16.2.0/include/README"):
                zf.writestr(f"{top}/{name}", b"stub\n")

    def test_extract_flattens_and_locates_compilers(self):
        cfg = BuildConfig(arch="x86_64")
        with tempfile.TemporaryDirectory() as tmp:
            zpath = os.path.join(tmp, "winlibs.zip")
            self._make_zip(zpath)
            root = os.path.join(tmp, "toolchain")
            toolchain.extract_archive(zpath, root, cfg)

            gcc = toolchain.locate_gcc(root, cfg)
            self.assertIsNotNone(gcc, "gcc.exe must be findable even from a non-Windows host")
            self.assertTrue(gcc.endswith("gcc.exe"))
            # the single top-level dir is stripped, so bin/ sits at the root
            self.assertTrue(gcc.endswith(os.path.join("bin", "gcc.exe")))
            self.assertEqual(toolchain.find_program(root, "g++", cfg),
                             os.path.join(root, "bin", "g++.exe"))
            self.assertIsNone(toolchain.find_program(root, "does-not-exist", cfg))

            comps = toolchain.compilers(root, cfg)
            for key in ("cc", "cxx", "rc", "fortran", "ar"):
                self.assertIn(key, comps)

            # the single top dir was stripped, so bin/ lives at the root
            paths = toolchain.bin_path(root, cfg)
            self.assertTrue(any(p.endswith("bin") and os.path.isdir(p) for p in paths), paths)

    def test_extract_keeps_layout_with_multiple_top_dirs(self):
        """Real winlibs archives may ship extra top-level dirs; do not strip then."""
        cfg = BuildConfig(arch="x86_64")
        with tempfile.TemporaryDirectory() as tmp:
            zpath = os.path.join(tmp, "winlibs.zip")
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("mingw64/bin/gcc.exe", b"stub\n")
                zf.writestr("mingw64/bin/g++.exe", b"stub\n")
                zf.writestr("readme.txt", b"winlibs\n")
            root = os.path.join(tmp, "toolchain")
            toolchain.extract_archive(zpath, root, cfg)
            self.assertTrue(os.path.isfile(os.path.join(root, "mingw64", "bin", "gcc.exe")))
            self.assertTrue(os.path.isfile(os.path.join(root, "readme.txt")))
            gcc = toolchain.locate_gcc(root, cfg)
            self.assertIsNotNone(gcc)
            self.assertTrue(gcc.endswith(os.path.join("mingw64", "bin", "gcc.exe")))

    def test_path_traversal_rejected(self):
        cfg = BuildConfig()
        with tempfile.TemporaryDirectory() as tmp:
            zpath = os.path.join(tmp, "evil.zip")
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("mingw64/bin/../../../evil.txt", b"pwn")
            with self.assertRaises(RuntimeError):
                toolchain.extract_archive(zpath, os.path.join(tmp, "out"), cfg)

    def test_unsupported_archive_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "x.rar")
            open(p, "wb").close()
            with self.assertRaises(RuntimeError):
                toolchain.extract_archive(p, os.path.join(tmp, "out"), None)


class TestLlvmSource(unittest.TestCase):
    def test_version_new_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "cmake", "Modules"))
            with open(os.path.join(tmp, "cmake", "Modules", "LLVMVersion.cmake"), "w") as fh:
                fh.write("if(NOT DEFINED LLVM_VERSION_MAJOR)\n  set(LLVM_VERSION_MAJOR 23)\nendif()\n"
                         "if(NOT DEFINED LLVM_VERSION_MINOR)\n  set(LLVM_VERSION_MINOR 1)\nendif()\n"
                         "if(NOT DEFINED LLVM_VERSION_PATCH)\n  set(LLVM_VERSION_PATCH 2)\nendif()\n")
            self.assertEqual(llvm.source_version(tmp), "23.1.2")

    def test_version_old_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "llvm"))
            with open(os.path.join(tmp, "llvm", "CMakeLists.txt"), "w") as fh:
                fh.write("set(LLVM_VERSION_MAJOR 19)\nset(LLVM_VERSION_MINOR 1)\n"
                         "set(LLVM_VERSION_PATCH 7)\n")
            self.assertEqual(llvm.source_version(tmp), "19.1.7")

    def test_version_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(llvm.source_version(tmp))

    def test_source_is_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(llvm.source_is_valid(tmp))
            os.makedirs(os.path.join(tmp, "llvm", "cmake", "modules"))
            open(os.path.join(tmp, "llvm", "CMakeLists.txt"), "w").close()
            self.assertTrue(llvm.source_is_valid(tmp))

    def test_resolve_ref_forms(self):
        cfg = BuildConfig()
        self.assertEqual(llvm.resolve_ref(cfg, "23.1.2").tag, "llvmorg-23.1.2")
        self.assertEqual(llvm.resolve_ref(cfg, "llvmorg-22.1.8").tag, "llvmorg-22.1.8")
        self.assertTrue(llvm.resolve_ref(cfg, "main").is_branch)
        self.assertTrue(llvm.resolve_ref(cfg, "release/23.x").is_branch)

    def test_git_head_does_not_escape_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            # tmp is inside (or outside) a git repo; either way there is no .git
            self.assertIsNone(llvm.git_head(tmp))


class TestPackaging(unittest.TestCase):
    def test_package_name_winlibs_style(self):
        cfg = BuildConfig(arch="x86_64", runtime="ucrt", build_type="Release")
        name = packaging.package_name(cfg, "23.1.2", "16.2.0", "14.0.0", "zip")
        self.assertEqual(name, "llvm-23.1.2-gcc-16.2.0-mingw-w64ucrt-14.0.0-x86_64-posix-seh-release.zip")
        cfg32 = BuildConfig(arch="i686", runtime="msvcrt", build_type="RelWithDebInfo")
        self.assertEqual(
            packaging.package_name(cfg32, "23.1.2", "16.2.0", "14.0.0", "7z"),
            "llvm-23.1.2-gcc-16.2.0-mingw-w64msvcrt-14.0.0-i686-posix-dwarf-relwithdebinfo.7z",
        )

    def test_package_name_override(self):
        cfg = BuildConfig(package_name="my-llvm")
        self.assertEqual(packaging.package_name(cfg, "23.1.2", None, None, "zip"), "my-llvm.zip")

    def test_zip_roundtrip_and_checksums(self):
        cfg = BuildConfig(arch="x86_64", native=True)
        with tempfile.TemporaryDirectory() as tmp:
            install = os.path.join(tmp, "install")
            os.makedirs(os.path.join(install, "bin"))
            os.makedirs(os.path.join(install, "lib", "cmake", "llvm"))
            with open(os.path.join(install, "bin", "clang.exe"), "wb") as fh:
                # DOS header: 'MZ', pad to e_lfanew at 0x3C, point it at 0x40,
                # then the PE signature.
                fh.write(b"MZ" + b"\x00" * 58 + struct.pack("<I", 0x40) + b"PE\x00\x00" + b"\x00" * 100)
            with open(os.path.join(install, "lib", "cmake", "llvm", "LLVMConfig.cmake"), "w") as fh:
                fh.write("# config\n")

            cfg.prefix = tmp
            cfg.install_dir = install
            cfg.dist_dir = os.path.join(tmp, "dist")
            cfg.archive_formats = ["zip"]

            files = packaging.package_install(cfg, "23.1.2", "16.2.0", "14.0.0", quiet=True)
            self.assertEqual(len(files), 3, files)  # zip + .sha256 + .sha512
            archive = files[0]
            self.assertTrue(archive.endswith(".zip"))
            self.assertTrue(packaging.verify_checksum(archive, archive + ".sha256"))

            with zipfile.ZipFile(archive) as zf:
                names = zf.namelist()
            self.assertIn("bin/clang.exe", names)
            self.assertIn("lib/cmake/llvm/LLVMConfig.cmake", names)

            # PE detection on the crafted header
            self.assertTrue(smoketest.is_pe(os.path.join(install, "bin", "clang.exe")))

    def test_package_empty_install_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = BuildConfig()
            cfg.prefix = tmp
            cfg.install_dir = os.path.join(tmp, "empty")
            os.makedirs(cfg.install_dir)
            with self.assertRaises(RuntimeError):
                packaging.package_install(cfg, "23.1.2", quiet=True)

    def test_unknown_format_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = BuildConfig()
            cfg.prefix = tmp
            cfg.install_dir = os.path.join(tmp, "install")
            os.makedirs(os.path.join(cfg.install_dir, "bin"))
            with open(os.path.join(cfg.install_dir, "bin", "x"), "w") as fh:
                fh.write("x")
            cfg.archive_formats = ["rar"]
            with self.assertRaises(ValueError):
                packaging.package_install(cfg, "1.0", quiet=True)


class TestSmokeTestHelpers(unittest.TestCase):
    def test_is_pe_rejects_non_pe(self):
        with tempfile.TemporaryDirectory() as tmp:
            elf = os.path.join(tmp, "elf")
            with open(elf, "wb") as fh:
                fh.write(b"\x7fELF" + b"\x00" * 200)
            self.assertFalse(smoketest.is_pe(elf))
            self.assertFalse(smoketest.is_pe(os.path.join(tmp, "missing")))

    def test_scope_aware_expectations(self):
        cfg = BuildConfig(scope="llvm-only", native=True)
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "bin"))
            cfg.install_dir = tmp
            results = smoketest.test_products(cfg)
            names = " ".join(r[0] for r in results)
            self.assertNotIn("clang", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestCompilerResolution(unittest.TestCase):
    """Guards the winlibs lookup path - the primary one on Windows."""

    def _fake_toolchain(self, root: str, layout: str = "flat") -> str:
        bindir = os.path.join(root, "bin") if layout == "flat" else os.path.join(root, "mingw64", "bin")
        os.makedirs(bindir, exist_ok=True)
        for name in ("gcc.exe", "g++.exe", "windres.exe", "ar.exe", "ranlib.exe",
                     "gcc-ar.exe", "gcc-ranlib.exe"):
            with open(os.path.join(bindir, name), "wb") as fh:
                fh.write(b"stub\n")
        return root

    def test_winlibs_toolchain_is_used_for_windows_target(self):
        from lmb import builder

        for layout in ("flat", "mingw64"):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as tmp:
                root = self._fake_toolchain(tmp, layout)
                cfg = BuildConfig(scope="core", toolchain_dir=root)
                comps = builder.resolve_compilers(cfg)
                self.assertTrue(comps["_source"].startswith("winlibs"), comps["_source"])
                self.assertTrue(comps["cc"].endswith("gcc.exe"))
                self.assertTrue(comps["cxx"].endswith("g++.exe"))
                self.assertTrue(comps["rc"].endswith("windres.exe"))

    def test_unusable_toolchain_raises_instead_of_silently_falling_back(self):
        from lmb import builder

        with tempfile.TemporaryDirectory() as tmp:
            empty = os.path.join(tmp, "empty-toolchain")
            os.makedirs(empty)
            cfg = BuildConfig(scope="core", toolchain_dir=empty)
            with self.assertRaises(RuntimeError) as ctx:
                builder.resolve_compilers(cfg)
            self.assertIn("unusable", str(ctx.exception))

    def test_explicit_cc_cxx_wins(self):
        from lmb import builder

        cfg = BuildConfig(scope="core", cc="/usr/bin/mycc", cxx="/usr/bin/mycxx")
        comps = builder.resolve_compilers(cfg)
        self.assertEqual(comps["_source"], "explicit")
        self.assertEqual(comps["cc"], "/usr/bin/mycc")

    def test_native_uses_host_compiler(self):
        from lmb import builder

        cfg = BuildConfig(scope="core", native=True)
        comps = builder.resolve_compilers(cfg)
        self.assertTrue(comps["_source"].startswith("host"), comps["_source"])

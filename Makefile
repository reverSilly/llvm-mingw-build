# Convenience targets around build.py.
#
#   make help                       show what is available
#   make info                       latest LLVM + winlibs that would be used
#   make all SCOPE=core             fetch, build, install, package, smoke test
#   make validate                   quick native build to prove the wiring works
#   make test                       run the unit tests
#
# Overridables: SCOPE TARGETS BUILD_TYPE PREFIX JOBS LINK_JOBS ARCH RUNTIME
#               LLVM_REF WINLIBS_TAG ARCHIVE
SHELL := /bin/bash

SCOPE       ?= core
TARGETS     ?= all
BUILD_TYPE  ?= Release
PREFIX      ?= work
JOBS        ?= $(shell nproc 2>/dev/null || echo 2)
LINK_JOBS   ?= 1
ARCH        ?= x86_64
RUNTIME     ?= ucrt
ARCHIVE     ?= zip

PYTHON ?= $(shell command -v python3 || command -v python)

# Optional pins, e.g. make all LLVM_REF=22.1.8 WINLIBS_TAG=16.1.0posix-14.0.0-ucrt-r4
PIN := $(if $(LLVM_REF),--llvm-ref $(LLVM_REF)) $(if $(WINLIBS_TAG),--winlibs-tag $(WINLIBS_TAG))
COMMON := --prefix $(PREFIX) --scope $(SCOPE) --targets $(TARGETS) --build-type $(BUILD_TYPE) \
          --arch $(ARCH) --runtime $(RUNTIME) $(PIN)

.DEFAULT_GOAL := help

.PHONY: help doctor info fetch fetch-toolchain fetch-llvm configure build install \
        package smoke-test all validate test lint clean distclean

help:
	@echo "llvm-mingw-build - build LLVM for Windows with the winlibs MinGW-w64 GCC"
	@echo
	@echo "Targets:"
	@echo "  doctor           environment + network health check"
	@echo "  info             resolve latest LLVM / winlibs and show the plan"
	@echo "  fetch-toolchain  download, verify and extract winlibs GCC"
	@echo "  fetch-llvm       download and extract the LLVM sources"
	@echo "  fetch            both of the above"
	@echo "  configure        cmake configure only"
	@echo "  build            compile (JOBS=$(JOBS), LINK_JOBS=$(LINK_JOBS))"
	@echo "  install          install into $(PREFIX)/install"
	@echo "  package          archive + sha256/sha512 into $(PREFIX)/dist"
	@echo "  smoke-test       compile/run sanity checks"
	@echo "  all              every stage in order"
	@echo "  validate         fast native build of small tools (no Windows needed)"
	@echo "  test             run the unit tests"
	@echo "  clean            remove the build tree (keeps downloads and sources)"
	@echo "  distclean        remove everything under $(PREFIX)"
	@echo
	@echo "Current settings: SCOPE=$(SCOPE) TARGETS=$(TARGETS) BUILD_TYPE=$(BUILD_TYPE) ARCH=$(ARCH)"
	@echo "Example: make all SCOPE=everything JOBS=16 LLVM_REF=23.1.2"

doctor:
	$(PYTHON) build.py doctor $(COMMON)

info:
	$(PYTHON) build.py info $(COMMON)

fetch-toolchain:
	$(PYTHON) build.py fetch-toolchain $(COMMON)

fetch-llvm:
	$(PYTHON) build.py fetch-llvm $(COMMON)

fetch: fetch-toolchain fetch-llvm

configure:
	$(PYTHON) build.py configure $(COMMON) --jobs $(JOBS) --link-jobs $(LINK_JOBS)

build:
	$(PYTHON) build.py build $(COMMON) --jobs $(JOBS) --link-jobs $(LINK_JOBS)

install:
	$(PYTHON) build.py install $(COMMON)

package:
	$(PYTHON) build.py package $(COMMON) --archive $(ARCHIVE)

smoke-test:
	$(PYTHON) build.py smoke-test $(COMMON)

all:
	$(PYTHON) build.py all $(COMMON) --jobs $(JOBS) --link-jobs $(LINK_JOBS) --archive $(ARCHIVE)

# Builds a handful of small LLVM tools with the host compiler.  Proves the
# CMake wiring, flag generation, build, packaging and smoke-test paths work
# without needing Windows or a full multi-hour build.
validate:
	$(PYTHON) build.py configure --native --prefix $(PREFIX) --build-dir $(PREFIX)/build-validate \
		--scope core --targets all --build-type Release
	$(PYTHON) build.py build --native --prefix $(PREFIX) --build-dir $(PREFIX)/build-validate \
		--scope core --targets all --build-type Release \
		--build-targets "llvm-tblgen;FileCheck;llvm-config;count;not" --jobs $(JOBS)
	@echo "validate: build tree at $(PREFIX)/build-validate/bin"

test:
	$(PYTHON) -m unittest discover -s tests -t . -v

lint:
	-$(PYTHON) -m py_compile build.py lmb/*.py tests/*.py && echo "py_compile: ok"
	-bash -n build.sh && echo "build.sh: ok"

clean:
	rm -rf $(PREFIX)/build $(PREFIX)/build-validate $(PREFIX)/build-core $(PREFIX)/install $(PREFIX)/dist

distclean:
	rm -rf $(PREFIX)

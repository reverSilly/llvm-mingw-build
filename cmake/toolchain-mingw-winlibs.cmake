# CMake toolchain file for building LLVM with a winlibs MinGW-w64 GCC toolchain.
#
# Two situations use it:
#
#   1. Native Windows build (the normal case).  winlibs ships PE executables in
#      <root>/mingw64/bin; point WINLIBS_ROOT at the extracted archive and CMake
#      will find gcc/g++/windres/ar/ranlib.
#
#   2. Cross build from Linux/macOS using a distro MinGW toolchain
#      (x86_64-w64-mingw32-gcc).  Set MINGW_TRIPLE, or let it default.
#
# Usage:
#   cmake -DCMAKE_TOOLCHAIN_FILE=cmake/toolchain-mingw-winlibs.cmake \
#         -DWINLIBS_ROOT=C:/toolchain/winlibs-gcc-16.2.0 ...
#
# Knobs:
#   WINLIBS_ROOT   extracted winlibs archive (contains mingw64/ or mingw32/)
#   MINGW_TRIPLE   e.g. x86_64-w64-mingw32 (default x86_64-w64-mingw32)
#   MINGW_SUBDIR   mingw64 | mingw32       (default derived from the triple)

set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_SYSTEM_PROCESSOR x86_64)

if(NOT DEFINED MINGW_TRIPLE)
  set(MINGW_TRIPLE "x86_64-w64-mingw32")
endif()

if(NOT DEFINED MINGW_SUBDIR)
  if(MINGW_TRIPLE MATCHES "^i686")
    set(MINGW_SUBDIR "mingw32")
  else()
    set(MINGW_SUBDIR "mingw64")
  endif()
endif()

# ---------------------------------------------------------------------------
# Locate the compilers
# ---------------------------------------------------------------------------
set(_candidates "")

if(WINLIBS_ROOT)
  # native winlibs layout: <root>/mingw64/bin/gcc.exe
  list(APPEND _candidates "${WINLIBS_ROOT}/${MINGW_SUBDIR}/bin")
  # cross layout sometimes shipped by third parties: <root>/bin
  list(APPEND _candidates "${WINLIBS_ROOT}/bin")
endif()

find_program(LMB_CC
  NAMES gcc "${MINGW_TRIPLE}-gcc"
  PATHS ${_candidates}
  PATH_SUFFIXES bin
)
find_program(LMB_CXX
  NAMES g++ "${MINGW_TRIPLE}-g++"
  PATHS ${_candidates}
  PATH_SUFFIXES bin
)

if(NOT LMB_CC OR NOT LMB_CXX)
  message(FATAL_ERROR
    "Could not find a MinGW-w64 GCC. Set -DWINLIBS_ROOT=<extracted winlibs dir> "
    "or install a ${MINGW_TRIPLE} cross toolchain and put it on PATH.")
endif()

get_filename_component(LMB_BINDIR "${LMB_CC}" DIRECTORY)

set(CMAKE_C_COMPILER   "${LMB_CC}")
set(CMAKE_CXX_COMPILER "${LMB_CXX}")

foreach(_tool windres ar ranlib dlltool strip objdump objcopy nm as ld)
  find_program(LMB_${_tool}
    NAMES "${MINGW_TRIPLE}-${_tool}" "${_tool}" "${_tool}.exe"
    PATHS "${LMB_BINDIR}" ${_candidates}
    NO_DEFAULT_PATH
  )
  if(NOT LMB_${_tool})
    find_program(LMB_${_tool}
      NAMES "${MINGW_TRIPLE}-${_tool}" "${_tool}" "${_tool}.exe"
      PATHS ${_candidates}
    )
  endif()
endforeach()

if(LMB_windres)
  set(CMAKE_RC_COMPILER "${LMB_windres}")
endif()
if(LMB_ar)
  set(CMAKE_AR "${LMB_ar}")
endif()
if(LMB_ranlib)
  set(CMAKE_RANLIB "${LMB_ranlib}")
endif()
if(LMB_strip)
  set(CMAKE_STRIP "${LMB_strip}")
endif()
if(LMB_dlltool)
  set(CMAKE_DLLTOOL "${LMB_dlltool}")
endif()

# gcc needs its own bin/ on PATH to locate as/ld when cross compiling.
list(APPEND CMAKE_PROGRAM_PATH "${LMB_BINDIR}")
if(WIN32)
  set(LMB_PATH_SEP ";")
else()
  set(LMB_PATH_SEP ":")
endif()
set(ENV{PATH} "${LMB_BINDIR}${LMB_PATH_SEP}$ENV{PATH}")

set(CMAKE_FIND_ROOT_PATH "${LMB_BINDIR}/..")
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)

# Static libgcc/libstdc++ so the shipped binaries have no MinGW DLL deps.
if(NOT DEFINED LMB_STATIC_RUNTIME OR LMB_STATIC_RUNTIME)
  string(APPEND CMAKE_EXE_LINKER_FLAGS_INIT    " -static-libgcc -static-libstdc++")
  string(APPEND CMAKE_SHARED_LINKER_FLAGS_INIT " -static-libgcc -static-libstdc++")
endif()

# Reduce peak memory of binutils ld when linking clang/lld.
string(APPEND CMAKE_EXE_LINKER_FLAGS_INIT " -Wl,--no-keep-memory")

message(STATUS "lmb toolchain: C   = ${CMAKE_C_COMPILER}")
message(STATUS "lmb toolchain: CXX = ${CMAKE_CXX_COMPILER}")
message(STATUS "lmb toolchain: RC  = ${CMAKE_RC_COMPILER}")

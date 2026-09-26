#!/usr/bin/env bash
# POSIX wrapper around build.py (works on Linux, macOS, WSL and Git-Bash).
#
#   ./build.sh all --scope core            # full winlibs build (run on Windows)
#   ./build.sh doctor                      # environment check
#   ./build.sh info                        # what would be built
#
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
  for cand in python3 python py; do
    if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
  done
fi
if [[ -z "$PY" ]]; then
  echo "build.sh: no python interpreter found (set PYTHON=/path/to/python3)" >&2
  exit 127
fi

# pip installs cmake/ninja into ~/.local/bin on Linux; make sure they're visible.
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) PATH="$HOME/.local/bin:$PATH" ;;
esac
export PATH

exec "$PY" "$here/build.py" "$@"

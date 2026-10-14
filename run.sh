#!/usr/bin/env bash
# One command: set up (first time only), run the offline demo, then the test suite (which runs the eval harness).
set -euo pipefail
cd "$(dirname "$0")"

# Use $PYTHON if set, otherwise the newest Python 3.9+ on this machine (macOS ships 3.9 as /usr/bin/python3).
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for cand in python3.14 python3.13 python3.12 python3.11 python3.10 python3 python3.9; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
      PY="$cand"
      break
    fi
  done
fi
if [ -z "$PY" ]; then
  echo "Python 3.9 or newer is needed (python.org, or 'brew install python')." >&2
  exit 1
fi
echo "Using $("$PY" --version 2>&1) ($(command -v "$PY"))"

if "$PY" -c "import pydantic, pytest" >/dev/null 2>&1 && \
   "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
  RUN="$PY"
else
  # first run: a local virtual environment with the two dependencies (plus a typing backport on Python 3.9)
  if [ ! -x .venv/bin/python ]; then
    "$PY" -m venv .venv
    .venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt
  fi
  RUN=.venv/bin/python
fi
"$RUN" -m creaition demo --out sample_outputs
"$RUN" -m pytest

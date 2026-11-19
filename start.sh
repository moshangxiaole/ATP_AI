#!/usr/bin/env sh
# ============================================================================
#  ATP Console - one-click launcher (macOS / Linux)
#  Windows users: double-click start.bat instead.
#
#    ./start.sh              start (or focus) the console
#    ./start.sh --port 9000  custom port
#    ./start.sh --stop       stop the background console
#    ./start.sh --status     show whether it is running
# ============================================================================
set -u
cd "$(dirname "$0")" || exit 1

# ---------- 1. system interpreter (used to bootstrap the venv if needed)
SYS="$(command -v python3 || command -v python || true)"
if [ -z "$SYS" ]; then
  echo "[!] Python 3.10+ not found. Install it, then run this again." >&2
  exit 1
fi

# ---------- 2. project venv, created on first run so we never dirty the OS
PY="venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "[*] First run - creating virtual environment ..."
  if ! "$SYS" -m venv venv; then
    echo "[!] Could not create venv, falling back to the system interpreter." >&2
    PY="$SYS"
  fi
fi

# ---------- 3. version gate
if ! "$PY" -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)'; then
  echo "[!] Python 3.10+ is required. Upgrade it, delete ./venv, retry." >&2
  exit 1
fi

# ---------- 4. dependencies
if ! "$PY" -c 'import pydantic,yaml,PIL' >/dev/null 2>&1; then
  echo "[*] Installing dependencies ..."
  if ! "$PY" -m pip install --disable-pip-version-check -q -r requirements.txt; then
    echo "[!] Dependency install failed. Try: $PY -m pip install -r requirements.txt" >&2
    exit 1
  fi
fi

# ---------- 5. hand over
exec "$PY" -m atp.cli web "$@"

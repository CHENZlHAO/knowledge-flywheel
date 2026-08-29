#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
hub_dir="$repo_dir/knowledge-hub"
venv_dir="${PYTHON_VENV_DIR:-$hub_dir/.venv}"
if [[ -n "${PYTHON_BIN:-}" ]]; then
  python_bin="$PYTHON_BIN"
else
  python_bin=""
  for candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      candidate_version="$($candidate -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
      candidate_major="${candidate_version%%.*}"
      candidate_minor="${candidate_version##*.}"
      if [[ "$candidate_major" -gt 3 || ( "$candidate_major" -eq 3 && "$candidate_minor" -ge 11 ) ]]; then
        python_bin="$candidate"
        break
      fi
    fi
  done
  if [[ -z "$python_bin" ]]; then
    python_bin="python3"
  fi
fi

if ! command -v "$python_bin" >/dev/null 2>&1; then
  echo "Python executable not found: $python_bin" >&2
  echo "Install Python 3.11+ and rerun this script." >&2
  exit 1
fi

python_version="$($python_bin -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
python_major="${python_version%%.*}"
python_minor="${python_version##*.}"
if [[ "$python_major" -lt 3 || ( "$python_major" -eq 3 && "$python_minor" -lt 11 ) ]]; then
  echo "Python 3.11+ is required; found $python_version" >&2
  exit 1
fi

if [[ ! -x "$venv_dir/bin/python" ]]; then
  echo "Creating virtual environment at $venv_dir"
  "$python_bin" -m venv "$venv_dir"
fi

venv_python="$venv_dir/bin/python"
"$venv_python" -m pip install --timeout 60 --retries 3 --upgrade pip
if ! "$venv_python" -m pip install --timeout 60 --retries 3 -r "$hub_dir/requirements-dev.txt"; then
  fallback_index="${PIP_FALLBACK_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"
  echo "Primary package index failed; retrying with $fallback_index"
  "$venv_python" -m pip install --timeout 60 --retries 3 -i "$fallback_index" -r "$hub_dir/requirements-dev.txt"
fi
(cd "$hub_dir" && "$venv_python" -m pytest -q tests)

echo
echo "Python environment ready: $venv_dir"
echo "Activate it with: source $venv_dir/bin/activate"

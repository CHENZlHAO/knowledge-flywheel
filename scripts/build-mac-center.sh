#!/usr/bin/env bash
# Build a standalone macOS center binary with PyInstaller (native arch).
#
# Output: knowledge-hub/dist/knowledge-center-darwin-arm64 (Apple Silicon)
#         knowledge-hub/dist/knowledge-center-darwin-amd64 (Intel)
#
# The artifact mirrors what .github/workflows/release.yml produces for the
# macos runners, so a local build and a CI release stay identical. Run it from
# anywhere: it resolves the repository root from its own location.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/knowledge-hub"

if [ ! -d .venv ]; then
  echo "==> Creating .venv (Python 3.12 recommended)"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Installing requirements + PyInstaller"
python -m pip install --upgrade pip
pip install -r requirements.txt pyinstaller

ARCH="$(uname -m)"
case "$ARCH" in
  arm64) ARTIFACT="knowledge-center-darwin-arm64" ;;
  x86_64) ARTIFACT="knowledge-center-darwin-amd64" ;;
  *)
    echo "Unsupported architecture: $ARCH (expected arm64 or x86_64)" >&2
    exit 1
    ;;
esac

echo "==> Building ${ARTIFACT}"
# macOS/Linux use ":" as the PyInstaller --add-data separator (Windows uses ";").
pyinstaller --onefile --name knowledge-center --add-data "app/console.html:app" standalone_center.py
mv -f dist/knowledge-center "dist/${ARTIFACT}"
chmod +x "dist/${ARTIFACT}"

echo "==> Artifact: $(pwd)/dist/${ARTIFACT}"
ls -lh "dist/${ARTIFACT}"

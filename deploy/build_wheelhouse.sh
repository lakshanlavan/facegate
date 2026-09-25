#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# build_wheelhouse.sh — Build an OFFLINE wheel bundle for CodeGloFix.
#
# Run on a KNOWN-GOOD machine (recommended: Ubuntu + Python 3.12) that matches
# the customer's OS/arch. It downloads every wheel for the production runtime so
# the customer PC can install with NO internet and a REPEATABLE, frozen set:
#
#     ./build_wheelhouse.sh                 # uses python3.12 + requirements.txt
#     ./build_wheelhouse.sh python3.12 requirements.txt
#
# The PyQt live-display deps (requirements-display.txt: PyQt6 + PyQt6-WebEngine)
# are the ONLY live view and are bundled automatically when that file exists, so
# an offline `install_customer_pc.sh --wheelhouse …` installs the display with no
# internet. Set INCLUDE_DISPLAY=0 to skip them (smaller bundle, but then the live
# display needs internet to install). To bundle ONLY display wheels, pass
# requirements-display.txt as the 2nd arg.
#
# Produces:
#     wheelhouse/                 — all .whl / .tar.gz packages
#     wheelhouse/requirements.lock.txt  — frozen exact versions (pip freeze)
#
# Then on the customer PC:
#     ./install_customer_pc.sh --python python3.12 --wheelhouse /path/to/wheelhouse
#
# IMPORTANT: build on the SAME Ubuntu major version + CPU arch as the target,
# because manylinux wheels (numpy, onnxruntime, opencv) are platform-specific.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PYBIN="${1:-python3.12}"
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$DEPLOY_DIR/.." && pwd)"
REQ="${2:-$PROJECT_DIR/requirements.txt}"
OUT="${OUT:-$PWD/wheelhouse}"

if ! command -v "$PYBIN" >/dev/null 2>&1; then
    echo "ERROR: '$PYBIN' not found. Install it (official repos) or pass another, e.g.:"
    echo "  $0 python3.12 $PROJECT_DIR/requirements.txt"
    exit 1
fi
VER="$("$PYBIN" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
echo "=== CodeGloFix wheelhouse build ==="
echo "  python       : $PYBIN ($VER)"
echo "  requirements : $REQ"
echo "  output       : $OUT"
echo "  (build on the same Ubuntu major + arch as the customer PC)"

mkdir -p "$OUT"
# Build in a throwaway venv so host site-packages don't taint the download.
TMPV="$(mktemp -d)/v"
"$PYBIN" -m venv "$TMPV"
"$TMPV/bin/pip" install --upgrade pip wheel >/dev/null

# Bundle pip + wheel too, so the offline install can self-upgrade if needed.
"$TMPV/bin/pip" download pip wheel -d "$OUT"
"$TMPV/bin/pip" download -r "$REQ" -d "$OUT"

# Also bundle the PyQt live-display wheels so an offline install has PyQt6 +
# PyQt6-WebEngine available (the live display is the only live view). Best-effort
# and skippable (INCLUDE_DISPLAY=0). Not added when the caller already pointed
# $REQ at requirements-display.txt (avoids downloading it twice).
DISPLAY_REQ="$PROJECT_DIR/requirements-display.txt"
if [ "${INCLUDE_DISPLAY:-1}" != "0" ] && [ -f "$DISPLAY_REQ" ] \
   && [ "$(cd "$(dirname "$REQ")" && pwd)/$(basename "$REQ")" != "$DISPLAY_REQ" ]; then
    echo "  + bundling PyQt live-display wheels ($DISPLAY_REQ)"
    "$TMPV/bin/pip" download -r "$DISPLAY_REQ" -d "$OUT" \
        || echo "  WARN: could not download display wheels (offline?) — PyQt install will need internet."
fi

# Record the exact resolved versions for audit/repeatability.
"$TMPV/bin/pip" install -r "$REQ" >/dev/null 2>&1 || true
"$TMPV/bin/pip" freeze > "$OUT/requirements.lock.txt" 2>/dev/null || true
rm -rf "$(dirname "$TMPV")"

echo
echo "=== Wheelhouse ready ==="
echo "  $(ls -1 "$OUT"/*.whl "$OUT"/*.tar.gz 2>/dev/null | wc -l) packages in $OUT"
echo "  Copy the whole '$OUT' folder to the customer PC, then:"
echo "    ./install_customer_pc.sh --python python$VER --wheelhouse $OUT"

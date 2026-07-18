#!/usr/bin/env bash
# Build WindNinja CLI (mass-conserving solver, no Qt GUI, no OpenFOAM) into tools/windninja.
# Idempotent: skips if tools/windninja/bin/WindNinja_cli already exists.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="$ROOT/tools/windninja"
BIN="$PREFIX/bin/WindNinja_cli"
SRC="$PREFIX/src/windninja"
BUILD="$PREFIX/build"
COMPAT_H="$PREFIX/gdal_cplisnan_compat.hpp"

if [[ -x "$BIN" ]]; then
  echo "[install-windninja] already present: $BIN"
  "$BIN" --help 2>&1 | head -3 || true
  exit 0
fi

echo "[install-windninja] installing brew deps (cmake boost gdal netcdf)…"
brew list cmake >/dev/null 2>&1 || brew install cmake
brew list boost >/dev/null 2>&1 || brew install boost
brew list gdal >/dev/null 2>&1 || brew install gdal
brew list netcdf >/dev/null 2>&1 || brew install netcdf

mkdir -p "$PREFIX/bin" "$PREFIX/src"

# Incomplete clones (network drop) leave an empty tree with only .git — redo.
if [[ -d "$SRC/.git" ]] && [[ ! -f "$SRC/CMakeLists.txt" ]]; then
  echo "[install-windninja] incomplete clone detected — removing…"
  rm -rf "$SRC"
fi

if [[ ! -f "$SRC/CMakeLists.txt" ]]; then
  echo "[install-windninja] cloning firelab/windninja @ 3.12.2…"
  rm -rf "$SRC"
  git clone --depth 1 --branch 3.12.2 https://github.com/firelab/windninja.git "$SRC" \
    || git clone --depth 1 https://github.com/firelab/windninja.git "$SRC"
fi

# GDAL ≥3.x only defines CPLIsNan for C (!__cplusplus). WindNinja still calls it from C++.
cat >"$COMPAT_H" <<'EOF'
#pragma once
#include <cmath>
#include <algorithm>
#ifndef CPLIsNan
inline int CPLIsNan(double x) { return std::isnan(x) ? 1 : 0; }
inline int CPLIsNan(float x) { return std::isnan(x) ? 1 : 0; }
#endif
#ifndef CPLIsInf
inline int CPLIsInf(double x) { return std::isinf(x) ? 1 : 0; }
inline int CPLIsInf(float x) { return std::isinf(x) ? 1 : 0; }
#endif
#ifndef CPLIsFinite
inline int CPLIsFinite(double x) { return std::isfinite(x) ? 1 : 0; }
inline int CPLIsFinite(float x) { return std::isfinite(x) ? 1 : 0; }
#endif
#ifndef MIN
#define MIN(a, b) (((a) < (b)) ? (a) : (b))
#endif
#ifndef MAX
#define MAX(a, b) (((a) > (b)) ? (a) : (b))
#endif
EOF

# GDAL 3.13+: OGRLayer::GetSpatialRef() returns const*. WindNinja 3.12 assigns to non-const*.
echo "[install-windninja] applying GDAL 3.13 source patches…"
python3 - "$SRC" <<'PY'
from pathlib import Path
import sys
root = Path(sys.argv[1])
replacements = [
    ("poSrcSRS = poLayer->GetSpatialRef();",
     "poSrcSRS = const_cast<OGRSpatialReference*>(poLayer->GetSpatialRef());"),
    ("poSrcSRS = poLayer->GetSpatialRef(); //shapefile CRS",
     "poSrcSRS = const_cast<OGRSpatialReference*>(poLayer->GetSpatialRef()); //shapefile CRS"),
    ("CPLJoinThread(threadHandles.front());",
     "CPLJoinThread(static_cast<CPLJoinableThread*>(threadHandles.front()));"),
    ("CPLJoinThread(threadHandles[i]);",
     "CPLJoinThread(static_cast<CPLJoinableThread*>(threadHandles[i]));"),
]
for path in root.rglob("*.cpp"):
    text = path.read_text(encoding="utf-8", errors="replace")
    orig = text
    for old, new in replacements:
        text = text.replace(old, new)
    if text != orig:
        path.write_text(text, encoding="utf-8")
        print(f"  patched {path.relative_to(root)}")
PY

echo "[install-windninja] configuring (CLI only, no Qt, no OpenFOAM)…"
rm -rf "$BUILD"
mkdir -p "$BUILD"
cd "$BUILD"

export GDAL_DIR="$(brew --prefix gdal)"
export CMAKE_PREFIX_PATH="$(brew --prefix):${CMAKE_PREFIX_PATH:-}"
CXX_COMPAT_FLAGS="-include ${COMPAT_H}"

cmake "$SRC" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$PREFIX" \
  -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
  -DCMAKE_CXX_FLAGS="${CXX_COMPAT_FLAGS}" \
  -DNINJA_CLI=ON \
  -DNINJA_QTGUI=OFF \
  -DNINJAFOAM=OFF \
  -DWITH_NOMADS_SUPPORT=OFF \
  -DWITH_LCP_CLIENT=OFF \
  -DGDAL_CONFIG="$GDAL_DIR/bin/gdal-config" \
  || cmake "$SRC" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
    -DCMAKE_CXX_FLAGS="${CXX_COMPAT_FLAGS}" \
    -DNINJA_CLI=ON \
    -DNINJA_QTGUI=OFF \
    -DNINJAFOAM=OFF \
    -DWITH_NOMADS_SUPPORT=OFF \
    -DWITH_LCP_CLIENT=OFF

echo "[install-windninja] compiling (this can take several minutes)…"
cmake --build . --config Release -j "$(sysctl -n hw.ncpu 2>/dev/null || echo 4)"

FOUND="$(find "$BUILD" -type f \( -name 'WindNinja_cli' -o -name 'WindNinja' \) -perm +111 2>/dev/null | head -1 || true)"
if [[ -z "$FOUND" ]]; then
  echo "[install-windninja] ERROR: CLI binary not found in build tree" >&2
  exit 1
fi
cp "$FOUND" "$BIN"
chmod +x "$BIN"

# Point WINDNINJA_DATA at the cloned share data (required at runtime).
DATA_LINK="$PREFIX/data"
if [[ ! -e "$DATA_LINK" ]]; then
  ln -sfn "$SRC/data" "$DATA_LINK"
fi

# Thin wrapper that injects WINDNINJA_DATA + GDAL_DATA.
WRAPPER="$PREFIX/bin/WindNinja"
cat >"$WRAPPER" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export WINDNINJA_DATA="\${WINDNINJA_DATA:-$DATA_LINK}"
if [[ -z "\${GDAL_DATA:-}" ]]; then
  if [[ -d /opt/homebrew/share/gdal ]]; then
    export GDAL_DATA=/opt/homebrew/share/gdal
  elif [[ -d /usr/local/share/gdal ]]; then
    export GDAL_DATA=/usr/local/share/gdal
  fi
fi
exec "$BIN" "\$@"
EOF
chmod +x "$WRAPPER"

echo "[install-windninja] installed $BIN"
echo "[install-windninja] wrapper  $WRAPPER"
echo "[install-windninja] data     $DATA_LINK"
WINDNINJA_DATA="$DATA_LINK" "$BIN" --version 2>&1 | head -5 || true

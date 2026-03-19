#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# check_deps_rocky9.sh  —  Verify all OpenDCC dependencies on Rocky Linux 9
#
# Usage:  bash scripts/check_deps_rocky9.sh [--prefix /opt/opendcc_deps]
# ═══════════════════════════════════════════════════════════════════════════════
set -uo pipefail

PREFIX="${1:-/opt/opendcc_deps}"
USD_PREFIX="${2:-/opt/usd}"
OPENDCC_PREFIX="${3:-/opt/opendcc}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

PASS=0
FAIL=0
WARN=0

check() {
    local name="$1"
    local path="$2"
    if [[ -e "$path" ]]; then
        echo -e "  ${GREEN}✓${NC}  $name  ($path)"
        ((PASS++))
    else
        echo -e "  ${RED}✗${NC}  $name  (expected: $path)"
        ((FAIL++))
    fi
}

check_cmd() {
    local name="$1"
    local cmd="$2"
    if command -v "$cmd" &>/dev/null; then
        local ver
        ver=$($cmd --version 2>&1 | head -1)
        echo -e "  ${GREEN}✓${NC}  $name  ($ver)"
        ((PASS++))
    else
        echo -e "  ${RED}✗${NC}  $name  (command not found: $cmd)"
        ((FAIL++))
    fi
}

check_py() {
    local name="$1"
    local module="$2"
    if python3 -c "import $module" 2>/dev/null; then
        echo -e "  ${GREEN}✓${NC}  $name  (python3 -c 'import $module')"
        ((PASS++))
    else
        echo -e "  ${YELLOW}⚠${NC}  $name  (python3 cannot import $module)"
        ((WARN++))
    fi
}

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  OpenDCC Dependency Check — Rocky Linux 9"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# ── System tools ──────────────────────────────────────────────────────────────
echo "System tools:"
check_cmd "GCC"      "gcc"
check_cmd "G++"      "g++"
check_cmd "CMake"    "cmake"
check_cmd "Ninja"    "ninja"
check_cmd "Python3"  "python3"
check_cmd "Git"      "git"
echo ""

# ── GCC version ───────────────────────────────────────────────────────────────
echo "GCC version check:"
GCC_VER=$(gcc -dumpversion 2>/dev/null || echo "0")
GCC_MAJOR="${GCC_VER%%.*}"
if [[ "$GCC_MAJOR" -ge 11 ]]; then
    echo -e "  ${GREEN}✓${NC}  GCC $GCC_VER (≥11 required for C++17)"
    ((PASS++))
else
    echo -e "  ${RED}✗${NC}  GCC $GCC_VER (≥11 required — run: source /opt/rh/gcc-toolset-12/enable)"
    ((FAIL++))
fi
echo ""

# ── Python version ────────────────────────────────────────────────────────────
echo "Python version check:"
PY_VER=$(python3 --version 2>/dev/null | awk '{print $2}')
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [[ "$PY_MAJOR" -eq 3 ]] && [[ "$PY_MINOR" -ge 10 ]]; then
    echo -e "  ${GREEN}✓${NC}  Python $PY_VER (≥3.10 recommended)"
    ((PASS++))
else
    echo -e "  ${YELLOW}⚠${NC}  Python $PY_VER (3.11 recommended)"
    ((WARN++))
fi
echo ""

# ── Third-party libraries ────────────────────────────────────────────────────
echo "Third-party dependencies (prefix: $PREFIX):"
check "Boost"              "$PREFIX/boost/lib/libboost_system.so"
check "Boost.Python"       "$PREFIX/boost/lib/libboost_python311.so"
check "OpenColorIO"        "$PREFIX/ocio/lib/libOpenColorIO.so"
check "OpenImageIO"        "$PREFIX/oiio/lib/libOpenImageIO.so"
check "OpenSubdiv"         "$PREFIX/osd/lib/libosdCPU.so"
check "Embree 3"           "$PREFIX/embree/lib/libembree3.so"
check "ADS (cmake)"        "$PREFIX/ads/lib/cmake/qtadvanceddocking"
check "Sentry (cmake)"     "$PREFIX/sentry/lib/cmake/sentry"
check "doctest (cmake)"    "$PREFIX/doctest/lib/cmake/doctest"
check "OpenMesh (cmake)"   "$PREFIX/openmesh/share/OpenMesh/cmake"
check "libigl"             "$PREFIX/igl/include/igl"
check "OSL"                "$PREFIX/osl/lib/liboslexec.so"
echo ""

# ── Qt 5.15 ──────────────────────────────────────────────────────────────────
echo "Qt 5.15:"
if [[ -f "$PREFIX/qt5/bin/qmake" ]]; then
    QT_VER=$("$PREFIX/qt5/bin/qmake" --version 2>/dev/null | tail -1)
    echo -e "  ${GREEN}✓${NC}  Qt from source  ($QT_VER)"
    ((PASS++))
elif command -v qmake-qt5 &>/dev/null; then
    QT_VER=$(qmake-qt5 --version 2>/dev/null | tail -1)
    echo -e "  ${GREEN}✓${NC}  System Qt  ($QT_VER)"
    ((PASS++))
else
    echo -e "  ${RED}✗${NC}  Qt 5.15 not found"
    ((FAIL++))
fi
echo ""

# ── System libraries ─────────────────────────────────────────────────────────
echo "System libraries:"
check "GLEW"               "/usr/include/GL/glew.h"
check "Eigen3"             "/usr/include/eigen3/Eigen/Core"
check "Bullet"             "/usr/include/bullet/btBulletDynamicsCommon.h"
check "ZeroMQ"             "/usr/include/zmq.h"
check "Graphviz"           "/usr/include/graphviz/gvc.h"
check "OpenEXR"            "/usr/include/OpenEXR/ImfRgbaFile.h"
check "Imath"              "/usr/include/Imath/ImathVec.h"
check "TBB"                "/usr/include/tbb/tbb.h"
echo ""

# ── OpenUSD ───────────────────────────────────────────────────────────────────
echo "OpenUSD (prefix: $USD_PREFIX):"
check "USD libs"           "$USD_PREFIX/lib"
check "USD Python"         "$USD_PREFIX/lib/python"
check "USD plugins"        "$USD_PREFIX/lib/usd"
echo ""

# ── Python packages ───────────────────────────────────────────────────────────
echo "Python packages:"
check_py "pybind11"    "pybind11"
check_py "PySide2"     "PySide2"
check_py "shiboken2"   "shiboken2"
check_py "fastapi"     "fastapi"
check_py "uvicorn"     "uvicorn"
echo ""

# ── OpenDCC install ──────────────────────────────────────────────────────────
echo "OpenDCC (prefix: $OPENDCC_PREFIX):"
if [[ -d "$OPENDCC_PREFIX" ]]; then
    check "bin/dcc_base"      "$OPENDCC_PREFIX/bin/dcc_base"
    check "lib/"              "$OPENDCC_PREFIX/lib"
    check "plugin/"           "$OPENDCC_PREFIX/plugin"
    check "configs/"          "$OPENDCC_PREFIX/configs"

    echo ""
    echo "Shared library check:"
    if [[ -f "$OPENDCC_PREFIX/bin/dcc_base" ]]; then
        MISSING=$(ldd "$OPENDCC_PREFIX/bin/dcc_base" 2>/dev/null | grep "not found" || true)
        if [[ -z "$MISSING" ]]; then
            echo -e "  ${GREEN}✓${NC}  All shared libraries resolve"
            ((PASS++))
        else
            echo -e "  ${RED}✗${NC}  Missing shared libraries:"
            echo "$MISSING" | sed 's/^/        /'
            ((FAIL++))
        fi
    fi
else
    echo -e "  ${YELLOW}⚠${NC}  OpenDCC not installed yet (build first)"
    ((WARN++))
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo -e "  Results:  ${GREEN}$PASS passed${NC}  ${RED}$FAIL failed${NC}  ${YELLOW}$WARN warnings${NC}"
echo "═══════════════════════════════════════════════════════════════"
echo ""

if [[ "$FAIL" -gt 0 ]]; then
    echo "Fix failed items before building OpenDCC."
    echo "See ROCKY9_BUILD.md for detailed instructions."
    exit 1
fi

exit 0

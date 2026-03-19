#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# bootstrap_rocky9.sh  —  Automated dependency build for Rocky Linux 9
#
# Usage:
#   sudo bash scripts/bootstrap_rocky9.sh [--prefix /opt/opendcc_deps] [--jobs 4]
#
# This script:
#   1. Installs system packages via dnf
#   2. Builds all third-party C++ dependencies from source
#   3. Builds OpenUSD 23.05
#   4. Prints the cmake command to build OpenDCC
#
# Estimated time: 2-4 hours depending on hardware and --jobs
# Estimated disk: ~40 GB (sources + builds + installs)
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
PREFIX="/opt/opendcc_deps"
USD_PREFIX="/opt/usd"
JOBS="$(nproc)"
SKIP_SYSTEM_PACKAGES=0
SKIP_USD=0
BUILD_QT_FROM_SOURCE=0

# ── Parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix)       PREFIX="$2";       shift 2 ;;
        --usd-prefix)   USD_PREFIX="$2";   shift 2 ;;
        --jobs|-j)      JOBS="$2";         shift 2 ;;
        --skip-system)  SKIP_SYSTEM_PACKAGES=1; shift ;;
        --skip-usd)     SKIP_USD=1;        shift ;;
        --build-qt)     BUILD_QT_FROM_SOURCE=1; shift ;;
        --help|-h)
            echo "Usage: $0 [--prefix DIR] [--usd-prefix DIR] [--jobs N]"
            echo "          [--skip-system] [--skip-usd] [--build-qt]"
            exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
TMPDIR="/tmp/opendcc_build_$$"

log()  { echo -e "\033[1;32m[bootstrap]\033[0m $*"; }
warn() { echo -e "\033[1;33m[bootstrap]\033[0m $*"; }
err()  { echo -e "\033[1;31m[bootstrap]\033[0m $*" >&2; }

cleanup() {
    if [[ -d "$TMPDIR" ]]; then
        log "Cleaning up $TMPDIR ..."
        rm -rf "$TMPDIR"
    fi
}
trap cleanup EXIT

mkdir -p "$TMPDIR" "$PREFIX"

# ═════════════════════════════════════════════════════════════════════════════
# 1. System packages
# ═════════════════════════════════════════════════════════════════════════════
if [[ "$SKIP_SYSTEM_PACKAGES" -eq 0 ]]; then
    log "Installing system packages..."

    dnf install -y epel-release
    dnf config-manager --set-enabled crb
    dnf update -y

    dnf groupinstall -y "Development Tools"

    dnf install -y \
        cmake ninja-build \
        gcc-toolset-12-gcc gcc-toolset-12-gcc-c++ \
        python3.11 python3.11-devel python3.11-pip \
        boost-devel tbb-devel glew-devel \
        mesa-libGL-devel mesa-libGLU-devel \
        openexr-devel imath-devel \
        zeromq-devel cppzmq-devel \
        eigen3-devel bullet-devel graphviz-devel \
        openssl-devel zlib-devel bzip2-devel xz-devel \
        libffi-devel readline-devel \
        libXrandr-devel libXinerama-devel libXcursor-devel libXi-devel \
        libXext-devel libXrender-devel libXfixes-devel \
        libXcomposite-devel libXdamage-devel \
        libxkbcommon-devel libxkbcommon-x11-devel \
        wayland-devel fontconfig-devel freetype-devel \
        xcb-util-devel xcb-util-wm-devel xcb-util-image-devel \
        xcb-util-keysyms-devel xcb-util-renderutil-devel \
        alsa-lib-devel pulseaudio-libs-devel \
        libsndfile-devel libjpeg-turbo-devel libpng-devel libtiff-devel \
        git curl wget patch which nasm autoconf automake libtool pkgconfig

    # Set python3.11 as default
    alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1
    alternatives --set python3 /usr/bin/python3.11
    python3 -m pip install --upgrade pip setuptools wheel
    python3 -m pip install pybind11

    log "System packages installed."
else
    log "Skipping system packages (--skip-system)"
fi

# ── Activate GCC 12 ──────────────────────────────────────────────────────────
source /opt/rh/gcc-toolset-12/enable
export CC=/opt/rh/gcc-toolset-12/root/usr/bin/gcc
export CXX=/opt/rh/gcc-toolset-12/root/usr/bin/g++
log "Using GCC $(gcc -dumpversion)"

# ═════════════════════════════════════════════════════════════════════════════
# 2. Qt 5.15
# ═════════════════════════════════════════════════════════════════════════════
QT_PREFIX="$PREFIX/qt5"
if [[ "$BUILD_QT_FROM_SOURCE" -eq 1 ]]; then
    if [[ ! -f "$QT_PREFIX/bin/qmake" ]]; then
        log "Building Qt 5.15.12 from source..."
        QT_VERSION=5.15.12
        cd "$TMPDIR"
        curl -fsSL "https://download.qt.io/archive/qt/5.15/${QT_VERSION}/single/qt-everywhere-opensource-src-${QT_VERSION}.tar.xz" \
            -o qt-src.tar.xz
        tar -xf qt-src.tar.xz
        cd qt-everywhere-src-${QT_VERSION}
        ./configure \
            -prefix "$QT_PREFIX" \
            -opensource -confirm-license \
            -release -shared \
            -nomake examples -nomake tests \
            -skip qt3d -skip qtandroidextras -skip qtcharts -skip qtdatavis3d \
            -skip qtdoc -skip qtgamepad -skip qtwebengine \
            -xcb -opengl desktop \
            -system-freetype -fontconfig -system-zlib \
            -system-libpng -system-libjpeg
        make -j"$JOBS"
        make install
        log "Qt 5.15 installed to $QT_PREFIX"
    else
        log "Qt 5.15 already found at $QT_PREFIX"
    fi
else
    log "Using system Qt 5.15 (install with: dnf install qt5-qtbase-devel qt5-qtsvg-devel qt5-qtmultimedia-devel qt5-qttools-devel)"
    dnf install -y qt5-qtbase-devel qt5-qtsvg-devel qt5-qtmultimedia-devel \
                   qt5-qttools-devel qt5-linguist 2>/dev/null || true
    QT_PREFIX="/usr/lib64/cmake/Qt5"
fi
export PATH="$QT_PREFIX/bin:$PATH"

# ═════════════════════════════════════════════════════════════════════════════
# 3. Boost 1.80 (with Python 3.11 bindings)
# ═════════════════════════════════════════════════════════════════════════════
BOOST_PREFIX="$PREFIX/boost"
if [[ ! -f "$BOOST_PREFIX/lib/libboost_python311.so" ]] && \
   [[ ! -f "$BOOST_PREFIX/lib/libboost_python3.so" ]]; then
    log "Building Boost 1.80.0..."
    cd "$TMPDIR"
    BOOST_VERSION=1.80.0
    BOOST_US=1_80_0
    curl -fsSL "https://boostorg.jfrog.io/artifactory/main/release/${BOOST_VERSION}/source/boost_${BOOST_US}.tar.bz2" \
        -o boost.tar.bz2
    tar -xf boost.tar.bz2
    cd boost_${BOOST_US}
    cat > user-config.jam <<'JAMEOF'
using python : 3.11 : /usr/bin/python3.11 : /usr/include/python3.11 : /usr/lib64 ;
JAMEOF
    ./bootstrap.sh --with-python=python3.11 --prefix="$BOOST_PREFIX"
    ./b2 --user-config=user-config.jam \
        --with-python --with-filesystem --with-system --with-thread \
        --with-program_options --with-regex --with-date_time \
        --with-serialization --with-iostreams --with-atomic \
        variant=release link=shared threading=multi \
        install --prefix="$BOOST_PREFIX" -j"$JOBS"
    log "Boost 1.80 installed to $BOOST_PREFIX"
else
    log "Boost already found at $BOOST_PREFIX"
fi

# ═════════════════════════════════════════════════════════════════════════════
# 4. OpenColorIO 2.2
# ═════════════════════════════════════════════════════════════════════════════
OCIO_PREFIX="$PREFIX/ocio"
if [[ ! -f "$OCIO_PREFIX/lib/libOpenColorIO.so" ]]; then
    log "Building OpenColorIO 2.2.1..."
    cd "$TMPDIR"
    git clone --depth 1 --branch v2.2.1 \
        https://github.com/AcademySoftwareFoundation/OpenColorIO.git ocio
    cmake -S ocio -B ocio/build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX="$OCIO_PREFIX" \
        -DOCIO_BUILD_APPS=OFF -DOCIO_BUILD_TESTS=OFF \
        -DOCIO_BUILD_GPU_TESTS=OFF -DOCIO_BUILD_PYTHON=OFF
    ninja -C ocio/build -j"$JOBS" install
    log "OCIO installed to $OCIO_PREFIX"
else
    log "OCIO already found at $OCIO_PREFIX"
fi

# ═════════════════════════════════════════════════════════════════════════════
# 5. OpenImageIO 2.4
# ═════════════════════════════════════════════════════════════════════════════
OIIO_PREFIX="$PREFIX/oiio"
if [[ ! -f "$OIIO_PREFIX/lib/libOpenImageIO.so" ]] && \
   [[ ! -f "$OIIO_PREFIX/lib64/libOpenImageIO.so" ]]; then
    log "Building OpenImageIO 2.4.17..."
    cd "$TMPDIR"
    git clone --depth 1 --branch v2.4.17.0 \
        https://github.com/AcademySoftwareFoundation/OpenImageIO.git oiio
    cmake -S oiio -B oiio/build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX="$OIIO_PREFIX" \
        -DCMAKE_PREFIX_PATH="$OCIO_PREFIX" \
        -DOIIO_BUILD_TESTS=OFF -DOIIO_BUILD_TOOLS=OFF \
        -DUSE_PYTHON=OFF -DUSE_QT=OFF
    ninja -C oiio/build -j"$JOBS" install
    log "OIIO installed to $OIIO_PREFIX"
else
    log "OIIO already found at $OIIO_PREFIX"
fi

# ═════════════════════════════════════════════════════════════════════════════
# 6. OpenSubdiv 3.5
# ═════════════════════════════════════════════════════════════════════════════
OSD_PREFIX="$PREFIX/osd"
if [[ ! -f "$OSD_PREFIX/lib/libosdCPU.so" ]] && \
   [[ ! -f "$OSD_PREFIX/lib64/libosdCPU.so" ]]; then
    log "Building OpenSubdiv 3.5.0..."
    cd "$TMPDIR"
    git clone --depth 1 --branch v3_5_0 \
        https://github.com/PixarAnimationStudios/OpenSubdiv.git osd
    cmake -S osd -B osd/build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX="$OSD_PREFIX" \
        -DNO_TUTORIALS=ON -DNO_EXAMPLES=ON -DNO_REGRESSION=ON \
        -DNO_DOC=ON -DNO_OMP=ON -DNO_CUDA=ON -DNO_OPENCL=ON -DNO_PTEX=ON
    ninja -C osd/build -j"$JOBS" install
    log "OpenSubdiv installed to $OSD_PREFIX"
else
    log "OpenSubdiv already found at $OSD_PREFIX"
fi

# ═════════════════════════════════════════════════════════════════════════════
# 7. Embree 3
# ═════════════════════════════════════════════════════════════════════════════
EMBREE_PREFIX="$PREFIX/embree"
if [[ ! -f "$EMBREE_PREFIX/lib/libembree3.so" ]] && \
   [[ ! -f "$EMBREE_PREFIX/lib64/libembree3.so" ]]; then
    log "Building Embree 3.13.5..."
    cd "$TMPDIR"
    git clone --depth 1 --branch v3.13.5 \
        https://github.com/embree/embree.git embree
    cmake -S embree -B embree/build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX="$EMBREE_PREFIX" \
        -DEMBREE_TUTORIALS=OFF -DEMBREE_ISPC_SUPPORT=OFF
    ninja -C embree/build -j"$JOBS" install
    log "Embree installed to $EMBREE_PREFIX"
else
    log "Embree already found at $EMBREE_PREFIX"
fi

# ═════════════════════════════════════════════════════════════════════════════
# 8. Qt Advanced Docking System
# ═════════════════════════════════════════════════════════════════════════════
ADS_PREFIX="$PREFIX/ads"
if [[ ! -d "$ADS_PREFIX/lib/cmake/qtadvanceddocking" ]]; then
    log "Building Qt Advanced Docking System 3.8.2..."
    cd "$TMPDIR"
    git clone --depth 1 --branch 3.8.2 \
        https://github.com/githubuser0xFFFF/Qt-Advanced-Docking-System.git ads
    cmake -S ads -B ads/build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_PREFIX_PATH="$QT_PREFIX" \
        -DCMAKE_INSTALL_PREFIX="$ADS_PREFIX" \
        -DBUILD_EXAMPLES=OFF
    ninja -C ads/build -j"$JOBS" install
    log "ADS installed to $ADS_PREFIX"
else
    log "ADS already found at $ADS_PREFIX"
fi

# ═════════════════════════════════════════════════════════════════════════════
# 9. Sentry Native
# ═════════════════════════════════════════════════════════════════════════════
SENTRY_PREFIX="$PREFIX/sentry"
if [[ ! -d "$SENTRY_PREFIX/lib/cmake/sentry" ]]; then
    log "Building Sentry Native 0.6.6..."
    cd "$TMPDIR"
    git clone --depth 1 --branch 0.6.6 --recurse-submodules \
        https://github.com/getsentry/sentry-native.git sentry
    cmake -S sentry -B sentry/build -G Ninja \
        -DCMAKE_BUILD_TYPE=RelWithDebInfo \
        -DCMAKE_INSTALL_PREFIX="$SENTRY_PREFIX" \
        -DSENTRY_BACKEND=inproc \
        -DSENTRY_BUILD_TESTS=OFF -DSENTRY_BUILD_EXAMPLES=OFF
    ninja -C sentry/build -j"$JOBS" install
    log "Sentry installed to $SENTRY_PREFIX"
else
    log "Sentry already found at $SENTRY_PREFIX"
fi

# ═════════════════════════════════════════════════════════════════════════════
# 10. OpenMesh
# ═════════════════════════════════════════════════════════════════════════════
OPENMESH_PREFIX="$PREFIX/openmesh"
if [[ ! -d "$OPENMESH_PREFIX/share/OpenMesh/cmake" ]]; then
    log "Building OpenMesh 9.0..."
    cd "$TMPDIR"
    curl -fsSL "https://www.graphics.rwth-aachen.de/media/openmesh_static/Releases/9.0/OpenMesh-9.0.tar.bz2" \
        | tar -xj
    cmake -S OpenMesh-9.0.0 -B openmesh_build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_APPS=OFF \
        -DCMAKE_INSTALL_PREFIX="$OPENMESH_PREFIX"
    ninja -C openmesh_build -j"$JOBS" install
    log "OpenMesh installed to $OPENMESH_PREFIX"
else
    log "OpenMesh already found at $OPENMESH_PREFIX"
fi

# ═════════════════════════════════════════════════════════════════════════════
# 11. doctest (header-only)
# ═════════════════════════════════════════════════════════════════════════════
DOCTEST_PREFIX="$PREFIX/doctest"
if [[ ! -d "$DOCTEST_PREFIX/lib/cmake/doctest" ]]; then
    log "Building doctest 2.4.11..."
    cd "$TMPDIR"
    git clone --depth 1 --branch v2.4.11 \
        https://github.com/doctest/doctest.git doctest
    cmake -S doctest -B doctest/build \
        -DDOCTEST_WITH_TESTS=OFF \
        -DCMAKE_INSTALL_PREFIX="$DOCTEST_PREFIX"
    cmake --build doctest/build --target install
    log "doctest installed to $DOCTEST_PREFIX"
else
    log "doctest already found at $DOCTEST_PREFIX"
fi

# ═════════════════════════════════════════════════════════════════════════════
# 12. libigl (header-only)
# ═════════════════════════════════════════════════════════════════════════════
IGL_DIR="$PREFIX/igl"
if [[ ! -d "$IGL_DIR/include/igl" ]]; then
    log "Cloning libigl (header-only)..."
    git clone --depth 1 https://github.com/libigl/libigl.git "$IGL_DIR/src"
    mkdir -p "$IGL_DIR/include"
    ln -sf "$IGL_DIR/src/include/igl" "$IGL_DIR/include/igl"
    log "libigl installed to $IGL_DIR"
else
    log "libigl already found at $IGL_DIR"
fi

# ═════════════════════════════════════════════════════════════════════════════
# 13. Open Shading Language (OSL)
# ═════════════════════════════════════════════════════════════════════════════
OSL_PREFIX="$PREFIX/osl"
if [[ ! -f "$OSL_PREFIX/lib/liboslexec.so" ]] && \
   [[ ! -f "$OSL_PREFIX/lib64/liboslexec.so" ]]; then
    log "Building OSL v1.12.14.0..."
    cd "$TMPDIR"
    git clone --depth 1 --branch v1.12.14.0 \
        https://github.com/AcademySoftwareFoundation/OpenShadingLanguage.git osl
    cmake -S osl -B osl/build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX="$OSL_PREFIX" \
        -DCMAKE_PREFIX_PATH="$OIIO_PREFIX;$OCIO_PREFIX;$BOOST_PREFIX" \
        -DOSL_BUILD_TESTS=OFF -DOSL_BUILD_PLUGINS=OFF -DUSE_QT=OFF
    ninja -C osl/build -j"$JOBS" install
    log "OSL installed to $OSL_PREFIX"
else
    log "OSL already found at $OSL_PREFIX"
fi

# ═════════════════════════════════════════════════════════════════════════════
# 14. PySide2 + Shiboken2
# ═════════════════════════════════════════════════════════════════════════════
log "Installing PySide2 / Shiboken2 via pip..."
python3 -m pip install --no-cache-dir PySide2 shiboken2 2>/dev/null || \
    warn "PySide2 pip wheel not available — you may need to build from source"

# ═════════════════════════════════════════════════════════════════════════════
# 15. OpenUSD 23.05
# ═════════════════════════════════════════════════════════════════════════════
if [[ "$SKIP_USD" -eq 0 ]]; then
    if [[ ! -f "$USD_PREFIX/lib/libusd_usd.so" ]] && \
       [[ ! -f "$USD_PREFIX/lib/libusd_ms.so" ]]; then
        log "Building OpenUSD 23.05 (this will take a while)..."
        cd "$TMPDIR"
        git clone --depth 1 --branch v23.05 \
            https://github.com/PixarAnimationStudios/OpenUSD.git usd_src

        # Set environment for build_usd.py to find deps
        export CMAKE_PREFIX_PATH="$BOOST_PREFIX:$OCIO_PREFIX:$OIIO_PREFIX:$OSD_PREFIX:$EMBREE_PREFIX:$QT_PREFIX"

        python3 usd_src/build_scripts/build_usd.py \
            --python \
            --usd-imaging \
            --opencolorio \
            --openimageio \
            --opensubdiv \
            --embree \
            --no-examples \
            --no-tutorials \
            --no-docs \
            --no-usdview \
            -j "$JOBS" \
            "$USD_PREFIX" || {
                warn "build_usd.py failed, trying manual cmake build..."
                cmake -S usd_src -B usd_build -G Ninja \
                    -DCMAKE_BUILD_TYPE=Release \
                    -DCMAKE_INSTALL_PREFIX="$USD_PREFIX" \
                    -DCMAKE_PREFIX_PATH="$CMAKE_PREFIX_PATH" \
                    -DPXR_BUILD_TESTS=OFF \
                    -DPXR_BUILD_EXAMPLES=OFF \
                    -DPXR_BUILD_TUTORIALS=OFF \
                    -DPXR_BUILD_USD_TOOLS=ON \
                    -DPXR_BUILD_IMAGING=ON \
                    -DPXR_BUILD_USD_IMAGING=ON \
                    -DPXR_BUILD_USDVIEW=OFF \
                    -DPXR_ENABLE_PYTHON_SUPPORT=ON \
                    -DPXR_USE_PYTHON_3=ON \
                    -DPython3_EXECUTABLE=/usr/bin/python3.11
                ninja -C usd_build -j"$JOBS" install
            }
        log "OpenUSD installed to $USD_PREFIX"
    else
        log "OpenUSD already found at $USD_PREFIX"
    fi
else
    log "Skipping USD build (--skip-usd)"
fi

# ═════════════════════════════════════════════════════════════════════════════
# Done — print cmake command
# ═════════════════════════════════════════════════════════════════════════════
log ""
log "════════════════════════════════════════════════════════════════"
log "  All dependencies built successfully!"
log "════════════════════════════════════════════════════════════════"
log ""
log "To build OpenDCC, run:"
log ""
cat <<CMAKEEOF

  source /opt/rh/gcc-toolset-12/enable

  cmake -S ${REPO_ROOT} -B ${REPO_ROOT}/build \\
    -G Ninja \\
    -DCMAKE_BUILD_TYPE=Release \\
    -DCMAKE_INSTALL_PREFIX=/opt/opendcc \\
    -DUSD_ROOT=${USD_PREFIX} \\
    -DCMAKE_PREFIX_PATH="${QT_PREFIX};${BOOST_PREFIX};${OCIO_PREFIX};${OIIO_PREFIX};${OSD_PREFIX};${EMBREE_PREFIX};${OSL_PREFIX};${USD_PREFIX}" \\
    -DPython3_ROOT_DIR=/usr \\
    -DPython3_EXECUTABLE=/usr/bin/python3.11 \\
    -DDCC_PYSIDE_CMAKE_FIND=OFF \\
    -DSHIBOKEN_CLANG_INSTALL_DIR=/usr \\
    -Dqtadvanceddocking_DIR=${ADS_PREFIX}/lib/cmake/qtadvanceddocking \\
    -Dsentry_DIR=${SENTRY_PREFIX}/lib/cmake/sentry \\
    -Ddoctest_DIR=${DOCTEST_PREFIX}/lib/cmake/doctest \\
    -DOpenMesh_DIR=${OPENMESH_PREFIX}/share/OpenMesh/cmake \\
    -DDCC_BUILD_ARNOLD_SUPPORT=OFF \\
    -DDCC_USD_FALLBACK_PROXY_BUILD_ARNOLD_USD=OFF \\
    -DDCC_USD_FALLBACK_PROXY_BUILD_CYCLES=OFF \\
    -DDCC_BUILD_RENDERMAN_SUPPORT=OFF \\
    -DDCC_EMBEDDED_PYTHON_HOME=ON \\
    -DDCC_BUILD_RENDER_VIEW=ON \\
    -DDCC_BUILD_TESTS=OFF

  ninja -C ${REPO_ROOT}/build -j\$(nproc) install

CMAKEEOF

# ── Generate env script ──────────────────────────────────────────────────────
ENV_SCRIPT="/opt/opendcc/opendcc_rocky9_env.sh"
if [[ -d "/opt/opendcc" ]]; then
    log "Generating environment script: $ENV_SCRIPT"
    cat > "$ENV_SCRIPT" <<ENVEOF
#!/bin/bash
# OpenDCC environment for Rocky Linux 9
# Source this before running OpenDCC: source $ENV_SCRIPT

source /opt/rh/gcc-toolset-12/enable

export OPENDCC_ROOT=/opt/opendcc
export PYTHONPATH="\${OPENDCC_ROOT}/lib/python3.11/site-packages:${USD_PREFIX}/lib/python:\${PYTHONPATH}"
export LD_LIBRARY_PATH="\${OPENDCC_ROOT}/lib:${USD_PREFIX}/lib:${QT_PREFIX}/lib:${BOOST_PREFIX}/lib:${OCIO_PREFIX}/lib:${OIIO_PREFIX}/lib:${OSD_PREFIX}/lib:${EMBREE_PREFIX}/lib:${OSL_PREFIX}/lib:${ADS_PREFIX}/lib:${SENTRY_PREFIX}/lib:${OPENMESH_PREFIX}/lib:\${LD_LIBRARY_PATH}"
export PXR_PLUGINPATH_NAME="\${OPENDCC_ROOT}/plugin/usd:\${OPENDCC_ROOT}/plugin/opendcc:${USD_PREFIX}/lib/usd"
export PATH="\${OPENDCC_ROOT}/bin:${QT_PREFIX}/bin:${USD_PREFIX}/bin:\${PATH}"
export LC_NUMERIC=C
ENVEOF
    chmod +x "$ENV_SCRIPT"
fi

log "Done! Total deps at: $PREFIX  USD at: $USD_PREFIX"

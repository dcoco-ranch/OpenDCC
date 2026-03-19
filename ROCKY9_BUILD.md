# OpenDCC – Rocky Linux 9 Build Guide

> **Status:** Rocky Linux 9 is the recommended Linux platform for OpenDCC,
> replacing the legacy CentOS 7 ASWF images. This guide covers both native
> builds and Docker-based workflows.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Install System Packages](#2-install-system-packages)
3. [Build Third-Party Dependencies](#3-build-third-party-dependencies)
4. [Build OpenUSD](#4-build-openusd)
5. [CMake Configuration](#5-cmake-configuration)
6. [Build & Install](#6-build--install)
7. [Running OpenDCC](#7-running-opendcc)
8. [Docker Quick Start](#8-docker-quick-start)
9. [GPU / Headless Rendering](#9-gpu--headless-rendering)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Prerequisites

- **Rocky Linux 9.x** (9.3+ recommended) — also works on AlmaLinux 9, RHEL 9
- **GCC 12** via `gcc-toolset-12` (C++17 required)
- **Python 3.11** (ships in Rocky 9 AppStream)
- **CMake 3.24+** and **Ninja**
- ~60 GB disk space for full build (deps + USD + OpenDCC)
- ~16 GB RAM recommended (8 GB minimum with `-j2`)

### VFX Reference Platform Alignment

| Component     | VFX Platform 2023 | This Build     |
|---------------|-------------------|----------------|
| GCC           | 11.x              | 12.x           |
| Python        | 3.10              | 3.11           |
| Qt            | 5.15              | 5.15.12        |
| Boost         | 1.80              | 1.80.0         |
| TBB           | 2020.3            | System (2020+) |
| OpenEXR       | 3.1               | System (3.1+)  |
| OpenColorIO   | 2.2               | 2.2.1          |
| OpenImageIO   | 2.4               | 2.4.17         |
| OpenSubdiv    | 3.5               | 3.5.0          |
| OpenUSD       | 23.05             | 23.05          |

> **Note:** Rocky 9 ships Python 3.9 by default. We use Python 3.11 from
> AppStream for better VFX Platform alignment and newer language features.

---

## 2. Install System Packages

```bash
# Enable EPEL and CRB (CodeReady Builder) repos
sudo dnf install -y epel-release
sudo dnf config-manager --set-enabled crb
sudo dnf update -y

# Development tools
sudo dnf groupinstall -y "Development Tools"
sudo dnf install -y cmake ninja-build

# GCC 12 (C++17 support)
sudo dnf install -y gcc-toolset-12-gcc gcc-toolset-12-gcc-c++

# Python 3.11
sudo dnf install -y python3.11 python3.11-devel python3.11-pip

# Core libraries
sudo dnf install -y \
    boost-devel \
    tbb-devel \
    glew-devel \
    mesa-libGL-devel \
    mesa-libGLU-devel \
    openexr-devel \
    imath-devel \
    zeromq-devel \
    cppzmq-devel \
    eigen3-devel \
    bullet-devel \
    graphviz-devel \
    openssl-devel \
    zlib-devel \
    bzip2-devel \
    xz-devel \
    libffi-devel \
    readline-devel

# X11 / Wayland / Qt deps
sudo dnf install -y \
    libXrandr-devel libXinerama-devel libXcursor-devel libXi-devel \
    libXext-devel libXrender-devel libXfixes-devel libXcomposite-devel \
    libXdamage-devel libxkbcommon-devel libxkbcommon-x11-devel \
    wayland-devel fontconfig-devel freetype-devel \
    xcb-util-devel xcb-util-wm-devel xcb-util-image-devel \
    xcb-util-keysyms-devel xcb-util-renderutil-devel

# Multimedia (for Qt Multimedia)
sudo dnf install -y alsa-lib-devel pulseaudio-libs-devel

# Misc build tools
sudo dnf install -y git curl wget patch which nasm
```

### Activate GCC 12

```bash
# For the current shell session:
source /opt/rh/gcc-toolset-12/enable

# Or add to ~/.bashrc for persistence:
echo 'source /opt/rh/gcc-toolset-12/enable' >> ~/.bashrc
```

Verify:
```bash
gcc --version   # Should show 12.x
g++ --version   # Should show 12.x
```

### Python 3.11 setup

```bash
# Make python3.11 the default python3
sudo alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1
sudo alternatives --set python3 /usr/bin/python3.11

# Install pip packages
python3 -m pip install --upgrade pip setuptools wheel
python3 -m pip install pybind11 PySide2 shiboken2
```

---

## 3. Build Third-Party Dependencies

All third-party deps are installed under `/opt/opendcc_deps`. Adjust
the prefix to your preference.

```bash
export DEPS_PREFIX=/opt/opendcc_deps
sudo mkdir -p $DEPS_PREFIX
```

### Qt 5.15

Rocky 9 ships Qt 5.15 in the repos, but for full VFX Platform control:

```bash
# Option A: System Qt (quick)
sudo dnf install -y qt5-qtbase-devel qt5-qtsvg-devel qt5-qtmultimedia-devel \
                    qt5-qttools-devel qt5-linguist

# Option B: Build from source (recommended for production)
QT_VERSION=5.15.12
curl -fsSL "https://download.qt.io/archive/qt/5.15/${QT_VERSION}/single/qt-everywhere-opensource-src-${QT_VERSION}.tar.xz" \
    -o /tmp/qt-src.tar.xz
cd /tmp && tar -xf qt-src.tar.xz
cd qt-everywhere-src-${QT_VERSION}
./configure \
    -prefix ${DEPS_PREFIX}/qt5 \
    -opensource -confirm-license \
    -release -shared \
    -nomake examples -nomake tests \
    -skip qt3d -skip qtandroidextras -skip qtcharts -skip qtdatavis3d \
    -skip qtdoc -skip qtgamepad -skip qtwebengine \
    -xcb -opengl desktop \
    -system-freetype -fontconfig -system-zlib
make -j$(nproc)
sudo make install
export PATH="${DEPS_PREFIX}/qt5/bin:$PATH"
```

### Boost (with Python 3.11 bindings)

```bash
BOOST_VERSION=1.80.0
BOOST_US=1_80_0
cd /tmp
curl -fsSL "https://archives.boost.io/release/${BOOST_VERSION}/source/boost_${BOOST_US}.tar.gz" \
    -o boost.tar.gz
tar -xzf boost.tar.gz && cd boost_${BOOST_US}

cat > user-config.jam <<'EOF'
using python : 3.11 : /usr/bin/python3.11 : /usr/include/python3.11 : /usr/lib64 ;
EOF

./bootstrap.sh --with-python=python3.11 --prefix=${DEPS_PREFIX}/boost
./b2 --user-config=user-config.jam \
    --with-python --with-filesystem --with-system --with-thread \
    --with-program_options --with-regex --with-date_time \
    --with-serialization --with-iostreams --with-atomic \
    variant=release link=shared threading=multi \
    install --prefix=${DEPS_PREFIX}/boost -j$(nproc)
```

### OpenColorIO 2.2

```bash
git clone --depth 1 --branch v2.2.1 \
    https://github.com/AcademySoftwareFoundation/OpenColorIO.git /tmp/ocio
cmake -S /tmp/ocio -B /tmp/ocio/build -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=${DEPS_PREFIX}/ocio \
    -DOCIO_BUILD_APPS=OFF -DOCIO_BUILD_TESTS=OFF \
    -DOCIO_BUILD_GPU_TESTS=OFF -DOCIO_BUILD_PYTHON=OFF
ninja -C /tmp/ocio/build -j$(nproc) install
```

### OpenImageIO 2.4

```bash
git clone --depth 1 --branch v2.4.17.0 \
    https://github.com/AcademySoftwareFoundation/OpenImageIO.git /tmp/oiio
cmake -S /tmp/oiio -B /tmp/oiio/build -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=${DEPS_PREFIX}/oiio \
    -DCMAKE_PREFIX_PATH="${DEPS_PREFIX}/ocio" \
    -DOIIO_BUILD_TESTS=OFF -DOIIO_BUILD_TOOLS=OFF \
    -DUSE_PYTHON=OFF -DUSE_QT=OFF
ninja -C /tmp/oiio/build -j$(nproc) install
```

### OpenSubdiv 3.5

```bash
git clone --depth 1 --branch v3_5_0 \
    https://github.com/PixarAnimationStudios/OpenSubdiv.git /tmp/osd
cmake -S /tmp/osd -B /tmp/osd/build -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=${DEPS_PREFIX}/osd \
    -DNO_TUTORIALS=ON -DNO_EXAMPLES=ON -DNO_REGRESSION=ON \
    -DNO_DOC=ON -DNO_OMP=ON -DNO_CUDA=ON -DNO_OPENCL=ON -DNO_PTEX=ON
ninja -C /tmp/osd/build -j$(nproc) install
```

### Embree 3

```bash
git clone --depth 1 --branch v3.13.5 \
    https://github.com/embree/embree.git /tmp/embree
cmake -S /tmp/embree -B /tmp/embree/build -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=${DEPS_PREFIX}/embree \
    -DEMBREE_TUTORIALS=OFF -DEMBREE_ISPC_SUPPORT=OFF
ninja -C /tmp/embree/build -j$(nproc) install
```

### Qt Advanced Docking System

```bash
git clone --depth 1 --branch 3.8.2 \
    https://github.com/githubuser0xFFFF/Qt-Advanced-Docking-System.git /tmp/ads
cmake -S /tmp/ads -B /tmp/ads/build -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_PREFIX_PATH="${DEPS_PREFIX}/qt5" \
    -DCMAKE_INSTALL_PREFIX=${DEPS_PREFIX}/ads \
    -DBUILD_EXAMPLES=OFF
ninja -C /tmp/ads/build -j$(nproc) install
```

### Sentry Native

```bash
git clone --depth 1 --branch 0.6.6 --recurse-submodules \
    https://github.com/getsentry/sentry-native.git /tmp/sentry
cmake -S /tmp/sentry -B /tmp/sentry/build -G Ninja \
    -DCMAKE_BUILD_TYPE=RelWithDebInfo \
    -DCMAKE_INSTALL_PREFIX=${DEPS_PREFIX}/sentry \
    -DSENTRY_BACKEND=inproc \
    -DSENTRY_BUILD_TESTS=OFF -DSENTRY_BUILD_EXAMPLES=OFF
ninja -C /tmp/sentry/build -j$(nproc) install
```

### OpenMesh

```bash
OPENMESH_VERSION=9.0
curl -fsSL "https://www.graphics.rwth-aachen.de/media/openmesh_static/Releases/${OPENMESH_VERSION}/OpenMesh-${OPENMESH_VERSION}.tar.bz2" \
    | tar -xj -C /tmp
cmake -S /tmp/OpenMesh-${OPENMESH_VERSION} -B /tmp/openmesh_build -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_APPS=OFF \
    -DCMAKE_INSTALL_PREFIX=${DEPS_PREFIX}/openmesh
ninja -C /tmp/openmesh_build -j$(nproc) install
```

### doctest (header-only)

```bash
git clone --depth 1 --branch v2.4.11 \
    https://github.com/doctest/doctest.git /tmp/doctest
cmake -S /tmp/doctest -B /tmp/doctest/build \
    -DDOCTEST_WITH_TESTS=OFF \
    -DCMAKE_INSTALL_PREFIX=${DEPS_PREFIX}/doctest
cmake --build /tmp/doctest/build --target install
```

### libigl (header-only)

```bash
git clone --depth 1 https://github.com/libigl/libigl.git ${DEPS_PREFIX}/igl_src
ln -s ${DEPS_PREFIX}/igl_src/include/igl ${DEPS_PREFIX}/include/igl 2>/dev/null || true
```

### Open Shading Language (OSL)

```bash
git clone --depth 1 --branch v1.12.14.0 \
    https://github.com/AcademySoftwareFoundation/OpenShadingLanguage.git /tmp/osl
cmake -S /tmp/osl -B /tmp/osl/build -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=${DEPS_PREFIX}/osl \
    -DCMAKE_PREFIX_PATH="${DEPS_PREFIX}/oiio;${DEPS_PREFIX}/ocio;${DEPS_PREFIX}/boost" \
    -DOSL_BUILD_TESTS=OFF -DOSL_BUILD_PLUGINS=OFF -DUSE_QT=OFF
ninja -C /tmp/osl/build -j$(nproc) install
```

---

## 4. Build OpenUSD

```bash
export USD_PREFIX=/opt/usd

git clone --depth 1 --branch v23.05 \
    https://github.com/PixarAnimationStudios/OpenUSD.git /tmp/usd_src

python3 /tmp/usd_src/build_scripts/build_usd.py \
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
    -j $(nproc) \
    ${USD_PREFIX}
```

If `build_usd.py` has issues finding deps, fall back to a manual CMake build:

```bash
cmake -S /tmp/usd_src -B /tmp/usd_build -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=${USD_PREFIX} \
    -DCMAKE_PREFIX_PATH="${DEPS_PREFIX}/boost;${DEPS_PREFIX}/ocio;${DEPS_PREFIX}/oiio;${DEPS_PREFIX}/osd;${DEPS_PREFIX}/embree;${DEPS_PREFIX}/qt5" \
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
ninja -C /tmp/usd_build -j$(nproc) install
```

---

## 5. CMake Configuration

```bash
export DEPS_PREFIX=/opt/opendcc_deps
export USD_PREFIX=/opt/usd

cd /path/to/OpenDCC
mkdir -p build && cd build

cmake .. \
    -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=/opt/opendcc \
    \
    -DUSD_ROOT=${USD_PREFIX} \
    \
    -DCMAKE_PREFIX_PATH="${DEPS_PREFIX}/qt5;${DEPS_PREFIX}/boost;${DEPS_PREFIX}/ocio;${DEPS_PREFIX}/oiio;${DEPS_PREFIX}/osd;${DEPS_PREFIX}/embree;${DEPS_PREFIX}/osl;${USD_PREFIX}" \
    \
    -DPython3_ROOT_DIR=/usr \
    -DPython3_EXECUTABLE=/usr/bin/python3.11 \
    \
    -DDCC_PYSIDE_CMAKE_FIND=OFF \
    -DSHIBOKEN_CLANG_INSTALL_DIR=/usr \
    \
    -Dqtadvanceddocking_DIR=${DEPS_PREFIX}/ads/lib/cmake/qtadvanceddocking \
    -Dsentry_DIR=${DEPS_PREFIX}/sentry/lib/cmake/sentry \
    -Ddoctest_DIR=${DEPS_PREFIX}/doctest/lib/cmake/doctest \
    -DOpenMesh_DIR=${DEPS_PREFIX}/openmesh/share/OpenMesh/cmake \
    \
    -DDCC_BUILD_ARNOLD_SUPPORT=OFF \
    -DDCC_USD_FALLBACK_PROXY_BUILD_ARNOLD_USD=OFF \
    -DDCC_USD_FALLBACK_PROXY_BUILD_CYCLES=OFF \
    -DDCC_BUILD_RENDERMAN_SUPPORT=OFF \
    \
    -DDCC_EMBEDDED_PYTHON_HOME=ON \
    -DDCC_BUILD_RENDER_VIEW=ON \
    -DDCC_BUILD_TESTS=OFF
```

---

## 6. Build & Install

```bash
ninja -j$(nproc)
ninja install
```

---

## 7. Running OpenDCC

### Create the environment setup script

```bash
cat > /opt/opendcc/opendcc_rocky9_env.sh << 'EOF'
#!/bin/bash
# OpenDCC environment for Rocky Linux 9

export DEPS_PREFIX=/opt/opendcc_deps
export USD_PREFIX=/opt/usd
export OPENDCC_ROOT=/opt/opendcc

# GCC 12
source /opt/rh/gcc-toolset-12/enable

# Python
export PYTHONPATH="${OPENDCC_ROOT}/lib/python3.11/site-packages:${USD_PREFIX}/lib/python:${PYTHONPATH}"

# Libraries
export LD_LIBRARY_PATH="${OPENDCC_ROOT}/lib:${USD_PREFIX}/lib:${DEPS_PREFIX}/qt5/lib:${DEPS_PREFIX}/boost/lib:${DEPS_PREFIX}/ocio/lib:${DEPS_PREFIX}/oiio/lib:${DEPS_PREFIX}/osd/lib:${DEPS_PREFIX}/embree/lib:${DEPS_PREFIX}/osl/lib:${DEPS_PREFIX}/ads/lib:${DEPS_PREFIX}/sentry/lib:${DEPS_PREFIX}/openmesh/lib:${LD_LIBRARY_PATH}"

# USD plugins
export PXR_PLUGINPATH_NAME="${OPENDCC_ROOT}/plugin/usd:${OPENDCC_ROOT}/plugin/opendcc:${USD_PREFIX}/lib/usd"

# PATH
export PATH="${OPENDCC_ROOT}/bin:${DEPS_PREFIX}/qt5/bin:${USD_PREFIX}/bin:${PATH}"
EOF

chmod +x /opt/opendcc/opendcc_rocky9_env.sh
```

### Launch (GUI mode)

```bash
source /opt/opendcc/opendcc_rocky9_env.sh
/opt/opendcc/bin/dcc_base
```

### Launch (headless web server)

```bash
source /opt/opendcc/opendcc_rocky9_env.sh
export QT_QPA_PLATFORM=offscreen
python3 /opt/opendcc/web/server.py --host 0.0.0.0 --port 8080
```

---

## 8. Docker Quick Start

### Web-only (fast, ~3-5 min build)

```bash
cd ShapeFX/
docker build -f OpenDCC/docker/Dockerfile.rocky9.web -t opendcc-web:rocky9 .
docker run -p 8080:8080 \
    -v $(pwd)/stages:/data/stages \
    opendcc-web:rocky9
# Open http://localhost:8080
```

### Full C++ build (~1-3 h)

```bash
cd ShapeFX/
docker build -f OpenDCC/docker/Dockerfile.rocky9 -t opendcc:rocky9 .
docker run -p 8080:8080 \
    -v $(pwd)/stages:/data/stages \
    opendcc:rocky9
```

### Docker Compose

```bash
cd OpenDCC/docker/

# Web-only
docker compose up rocky9-web

# Full build
docker compose build rocky9
docker compose up rocky9

# With GPU
docker compose up rocky9-gpu
```

---

## 9. GPU / Headless Rendering

### NVIDIA GPU Passthrough (Docker)

Install the NVIDIA Container Toolkit on your Rocky 9 host:

```bash
# Add NVIDIA repo
sudo dnf config-manager --add-repo \
    https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo
sudo dnf install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

Then use the `rocky9-gpu` service in docker-compose, or run manually:

```bash
docker run --gpus all -p 8080:8080 \
    -e NVIDIA_VISIBLE_DEVICES=all \
    -e NVIDIA_DRIVER_CAPABILITIES=all \
    -v $(pwd)/stages:/data/stages \
    opendcc:rocky9
```

### Native Headless (EGL / VirtualGL)

For headless GPU rendering without Docker:

```bash
# Install EGL support
sudo dnf install -y mesa-libEGL-devel

# Set up for headless rendering
export QT_QPA_PLATFORM=offscreen
export MESA_GL_VERSION_OVERRIDE=4.5
```

---

## 10. Troubleshooting

### Common Issues

| Issue | Solution |
|-------|----------|
| `gcc: command not found` | Run `source /opt/rh/gcc-toolset-12/enable` |
| `Python.h: No such file` | Install `python3.11-devel` |
| Boost.Python link error | Rebuild Boost with `user-config.jam` pointing to Python 3.11 |
| `libQt5Core.so.5: cannot open` | Add Qt lib dir to `LD_LIBRARY_PATH` |
| `PySide2 not found` | Try `pip3 install PySide2` or build from source |
| USD `build_usd.py` fails | Use the manual CMake fallback (see Section 4) |
| `GLEW: missing GL/glew.h` | Install `glew-devel` |
| `libzmq.so: cannot open` | Install `zeromq` (runtime) or `zeromq-devel` (build) |
| CMake can't find TBB | Install `tbb-devel`; Rocky 9 ships oneTBB 2021 |
| SELinux blocks shared libs | Run `sudo setsebool -P allow_execstack 1` or set context |
| Firewall blocks port 8080 | `sudo firewall-cmd --add-port=8080/tcp --permanent && sudo firewall-cmd --reload` |

### Verify Dependencies

```bash
# Check all shared libs resolve
source /opt/opendcc/opendcc_rocky9_env.sh
ldd /opt/opendcc/bin/dcc_base | grep "not found"

# Should print nothing. If libs are missing, add their paths to LD_LIBRARY_PATH.
```

### Rocky 9 vs CentOS 7 Key Differences

| Aspect | CentOS 7 (ASWF) | Rocky 9 |
|--------|------------------|---------|
| Package manager | `yum` | `dnf` |
| GCC | 4.8 (+ devtoolset) | 11.x (+ gcc-toolset-12) |
| Python | 2.7 / 3.6 | 3.9 / 3.11 |
| Kernel | 3.10 | 5.14 |
| glibc | 2.17 | 2.34 |
| OpenSSL | 1.0.2 | 3.0.x |
| EOL | June 2024 ❌ | May 2032 ✅ |
| SELinux | Permissive default | Enforcing default |

---

## License

Licensed under the **Apache 2.0 License**. See [LICENSE.txt](LICENSE.txt).

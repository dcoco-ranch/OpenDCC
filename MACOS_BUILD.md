# OpenDCC – macOS Build Guide

> **Status:** macOS support is In Progress upstream. This guide documents all
> required changes and dependencies to build on macOS (Apple Silicon / Intel).

---

## 1. Prerequisites

### Xcode Command Line Tools
```bash
xcode-select --install
```

### Homebrew packages
```bash
brew install cmake ninja python@3.10 boost tbb glew zeromq eigen graphviz \
             openexr imath openimageio opencolorio opensubdiv openmesh \
             bullet dylibbundler git-lfs
```

> **Note – Embree 3 on Apple Silicon:**
> Embree 3.x has limited ARM support. Use the Intel Homebrew tap or build from
> source with `cmake -DEMBREE_ISA_AVX=OFF -DEMBREE_ISA_AVX2=OFF …`.

### Qt 5.15.x
Download the Qt 5.15.x **macOS** online installer from https://www.qt.io/download
or via Homebrew:
```bash
brew install qt@5
echo 'export PATH="/opt/homebrew/opt/qt@5/bin:$PATH"' >> ~/.zshrc
```

### PySide2 + Shiboken2
PySide2 for Python 3.10 on macOS ARM can be installed from the community wheel:
```bash
pip3 install PySide2 shiboken2
```
If you need a custom build (e.g. linked against your Qt), see
https://wiki.qt.io/Qt_for_Python/GettingStarted.

### OpenUSD 22.05+
Clone and build (or use a pre-built release):
```bash
git clone https://github.com/PixarAnimationStudios/OpenUSD.git --branch v22.05 usd_src
python3 usd_src/build_scripts/build_usd.py \
    --python --no-imaging \   # add --imaging --opengl for Hydra/GL
    /opt/usd
```
For the full OpenDCC feature set (Hydra viewport) you need imaging:
```bash
python3 usd_src/build_scripts/build_usd.py \
    --python --imaging --opengl --opencolorio --openimageio \
    /opt/usd
```

### Qt Advanced Docking System (ADS)
```bash
git clone https://github.com/githubuser0xFFFF/Qt-Advanced-Docking-System.git ads
cmake -S ads -B ads/build -DCMAKE_PREFIX_PATH=/opt/homebrew/opt/qt@5 \
      -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/ads
cmake --build ads/build --target install -j$(sysctl -n hw.logicalcpu)
```

### Sentry Native (crash reporter)
```bash
git clone https://github.com/getsentry/sentry-native.git
cmake -S sentry-native -B sentry-native/build \
      -DCMAKE_BUILD_TYPE=RelWithDebInfo \
      -DCMAKE_INSTALL_PREFIX=/opt/sentry \
      -DSENTRY_BACKEND=crashpad
cmake --build sentry-native/build --target install
```

### doctest (header-only)
```bash
brew install doctest
# or manually: git clone https://github.com/doctest/doctest && copy include/
```

### pybind11
```bash
pip3 install pybind11
# or: brew install pybind11
```

---

## 2. CMake Configuration

```bash
mkdir build && cd build

cmake .. \
  -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX=/opt/opendcc \
  \
  # --- USD ---
  -DUSD_ROOT=/opt/usd \
  \
  # --- Qt ---
  -DCMAKE_PREFIX_PATH="/opt/homebrew/opt/qt@5;/opt/ads;/opt/sentry" \
  \
  # --- Python ---
  -DPython3_ROOT_DIR=$(python3 -c "import sys; print(sys.prefix)") \
  \
  # --- PySide2 / Shiboken ---
  -DDCC_PYSIDE_CMAKE_FIND=OFF \           # use PySideConfig.cmake discovery
  -DSHIBOKEN_CLANG_INSTALL_DIR=$(llvm-config --prefix) \
  \
  # --- Bullet ---
  -DBULLET_ROOT=/opt/homebrew \
  \
  # --- Disable unavailable optional deps ---
  -DDCC_BUILD_ARNOLD_SUPPORT=OFF \
  -DDCC_USD_FALLBACK_PROXY_BUILD_ARNOLD_USD=OFF \
  -DDCC_USD_FALLBACK_PROXY_BUILD_CYCLES=OFF \
  -DDCC_BUILD_RENDERMAN_SUPPORT=OFF \
  \
  # --- ADS ---
  -Dqtadvanceddocking_DIR=/opt/ads/lib/cmake/qtadvanceddocking \
  \
  # --- Sentry ---
  -Dsentry_DIR=/opt/sentry/lib/cmake/sentry
```

Then build and install:
```bash
ninja -j$(sysctl -n hw.logicalcpu)
ninja install
```

---

## 3. macOS-Specific CMake Patches Applied

### `cmake/macros/MakeTargets.cmake`
- `OS_LIBRARY_ENV_NAME` now correctly set to `DYLD_LIBRARY_PATH` on Apple
  (was `LD_LIBRARY_PATH`, which is a no-op on macOS)
- `get_usd_env()` now appends `$ENV{DYLD_LIBRARY_PATH}` and
  `$ENV{DYLD_FRAMEWORK_PATH}` on Apple
- All `execute_process` / `add_custom_command` calls that inject
  `LD_LIBRARY_PATH=…` now use the `${OS_LIBRARY_ENV_NAME}` variable

### `cmake/defaults/CXXDefaults.cmake` (already correct)
- `if(APPLE)` sets the same warning flags as GCC – no change needed.

### `src/bin/dcc_base/main.cpp` (already correct)
- `#ifdef OPENDCC_OS_MAC` → OpenGL 4.1 Core Profile (max on macOS)
- Config path walks into `.app/Contents/Resources/configs` when bundled

---

## 4. Known macOS Issues & Workarounds

| Issue | Status | Workaround |
|-------|--------|------------|
| `DYLD_LIBRARY_PATH` blocked by SIP | ⚠️ | Use `@rpath`; `make_osx_bundle.py` handles this for packaged builds |
| OpenGL deprecated (macOS 10.14+) | ⚠️ | Hydra/Storm still works via GL 4.1; Metal delegate is future work |
| Embree 3 on Apple Silicon (no AVX) | ⚠️ | Build with `EMBREE_ISA_AVX=OFF EMBREE_ISA_AVX2=OFF` |
| `OPENDCC_OS_MAC` define | ✅ | Derived from `__APPLE__` in `defines.h` – no CMake change needed |
| `Qt::DontUseNativeMenuBar` | ✅ | Already set in `main.cpp` |
| PySide2 arm64 wheels | ⚠️ | Use conda-forge `pyside2` if pip wheel is unavailable |
| `dylibbundler` for app bundle | ✅ | `brew install dylibbundler` |
| `macholib` Python package | ✅ | `pip3 install macholib` |
| Hardcoded Python 3.9 in `make_osx_bundle.py` | ⚠️ | Edit the script to match your Python version |
| Arnold / Cycles renderer | ℹ️ | Disabled by default (`DCC_BUILD_ARNOLD_SUPPORT=OFF`) |

---

## 5. Creating the .app Bundle (optional)

After `ninja install`:
```bash
pip3 install macholib
python3 cmake/macros/make_osx_bundle.py \
    /opt/opendcc \                          # install dir
    ~/Desktop/OpenDCC.app \                 # output .app
    /path/to/Info.plist \
    /path/to/extdeps \                      # where your external deps live
    icons/opendcc.png
```
> **Tip:** Update the hardcoded Python 3.9 path inside `make_osx_bundle.py`
> to match your Python version before running.

---

## 6. Running without a bundle

```bash
# source the env script (sets PYTHONPATH, DYLD_LIBRARY_PATH, PXR_PLUGINPATH_NAME, …)
source /opt/opendcc/opendcc_setup.sh

# launch
/opt/opendcc/bin/dcc_base
```

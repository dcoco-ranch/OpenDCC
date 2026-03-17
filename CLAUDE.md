# CLAUDE.md — OpenDCC Codebase Guide

This document provides guidance for AI assistants (Claude, Copilot, etc.) working in the OpenDCC codebase.

---

## Project Overview

**OpenDCC** is a production-grade Digital Content Creation (DCC) framework built on OpenUSD/Hydra with a Qt-based GUI. It targets the **VFX Reference Platform CY2022** (Python 3.9, USD 22.05+).

Key capabilities:
- Multi-renderer support (Arnold, RenderMan, Cycles, Storm/OpenGL)
- Plugin-based architecture via `package.toml` descriptors
- Full undo/redo with Python recording
- Live USD layer collaboration
- Embedded Python scripting/REPL
- Web-based headless rendering (FastAPI + WebSocket)
- Animation system (AnimX curves + graph editor)
- HydraOps procedural framework

---

## Repository Layout

```
OpenDCC/
├── src/
│   ├── lib/              # Core shared libraries
│   │   ├── opendcc/      # Application libraries (app, ui, usd, viewport, …)
│   │   └── usd/          # USD schema extensions & fallback proxy
│   ├── packages/         # Self-contained plugin packages (~33 packages)
│   ├── python/           # Python-only modules (startup, actions, plugin_manager, …)
│   └── bin/              # Executable entry points
├── cmake/
│   ├── defaults/         # Core CMake options, compiler flags, package finders
│   └── macros/           # CMake helper macros
├── configs/              # TOML application config templates
├── docker/               # Multi-stage Docker build files
├── web/                  # FastAPI web server + assets
├── icons/                # UI icons
├── i18n/                 # Internationalization files
├── CMakeLists.txt        # Root project (version 0.4.0.0)
├── .clang-format         # C++ formatting rules
├── .cmake-format         # CMake formatting rules
├── .pre-commit-config.yaml
└── pyproject.toml        # Black Python formatter config
```

---

## Technology Stack

| Layer | Technology |
|---|---|
| Scene / Data | OpenUSD 22.05+, Hydra |
| GUI | Qt 5.15, Qt Advanced Docking System |
| Python bindings (Qt) | PySide2, Shiboken2 |
| Python bindings (C++) | pybind11 |
| Build | CMake 3.18+, Ninja |
| Language standard | C++17 |
| Python | 3.9 (VFX CY2022) |
| Rendering | Arnold, RenderMan, Cycles, Storm/OpenGL |
| Physics | Bullet |
| Color management | OpenColorIO |
| Image I/O | OpenImageIO |
| IPC | ZeroMQ |
| Web server | FastAPI, uvicorn, WebSockets |
| Crash reporting | Sentry |
| C++ tests | doctest |
| Python formatter | Black (line-length 100) |
| C++ formatter | clang-format (Microsoft style, 150-col limit, C++17) |

---

## Building

### Prerequisites

- CMake 3.18+, Ninja
- USD 22.05+ installed (e.g. at `/opt/usd`)
- Qt 5.15 + PySide2 + Shiboken2
- Python 3.9
- Boost, TBB, OpenEXR, OCIO, OIIO, OpenSubdiv
- Optional: Arnold SDK, RenderMan SDK, Cycles

### Configure & Build

```bash
mkdir build && cd build
cmake .. -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DUSD_ROOT=/opt/usd \
  -DCMAKE_PREFIX_PATH="/opt/qt;/opt/ads;/opt/sentry"
ninja install
```

Default install prefix: `/opt/opendcc`

### Important CMake Options

| Option | Default | Description |
|---|---|---|
| `DCC_BUILD_TESTS` | OFF | Enable doctest unit tests |
| `DCC_BUILD_ANIM_ENGINE` | ON | Animation system |
| `DCC_BUILD_RENDER_VIEW` | ON | Standalone render viewer |
| `DCC_BUILD_ARNOLD_SUPPORT` | ON | Arnold renderer |
| `DCC_BUILD_RENDERMAN_SUPPORT` | OFF | RenderMan renderer |
| `DCC_BUILD_BULLET_PHYSICS` | ON | Bullet physics |
| `DCC_EMBEDDED_PYTHON_HOME` | ON | Use embedded Python |

### Build Types

- `Release` — optimized
- `RelWithDebInfo` — optimized + debug info
- `Debug` — unoptimized + debug info
- `Hybrid` — custom type (debug builds with optimization)

### Install Layout

```
/opt/opendcc/
├── bin/                         # dcc_base, render_view, usd_render, usd_ipc_broker, crash_reporter
├── lib/                         # Shared libraries
├── lib/python3.9/site-packages/ # Python packages
├── plugin/{usd,arnold,...}/     # USD and renderer plugins
└── opendcc_setup.sh             # Environment setup script (sets PYTHONPATH, LD_LIBRARY_PATH, PXR_PLUGINPATH_NAME)
```

Run before launching:
```bash
source /opt/opendcc/opendcc_setup.sh
```

---

## Running Tests

Tests use the **doctest** framework embedded in source files.

```bash
cmake .. -DDCC_BUILD_TESTS=ON
ninja install
# Run executables built with doctest; no separate test runner target yet
```

---

## Code Conventions

### C++ Naming

| Element | Convention | Example |
|---|---|---|
| Class | PascalCase | `ViewportGlWidget` |
| Function | camelCase | `getActiveSession()` |
| Private member | trailing underscore | `stage_` |
| Macro / constant | UPPER_CASE | `OPENDCC_API` |
| File | snake_case | `viewport_gl_widget.h` |

### Python Naming

| Element | Convention |
|---|---|
| Function / variable | snake_case |
| Class | PascalCase |
| Module | snake_case |

### Namespaces

All C++ code lives inside the `opendcc` namespace using the provided macros:

```cpp
#include "opendcc/base/defines.h"

OPENDCC_NAMESPACE_OPEN

class MyClass { ... };

OPENDCC_NAMESPACE_CLOSE
```

### Platform Guards

```cpp
#ifdef OPENDCC_OS_WINDOWS  // or OPENDCC_OS_LINUX, OPENDCC_OS_MAC
#ifdef OPENDCC_COMPILER_MSVC  // or OPENDCC_COMPILER_GCC
```

### Symbol Visibility / Exports

Each library exposes an export macro defined in its own `export.h`:

```cpp
#include "opendcc/app/core/export.h"

class OPENDCC_APP_CORE_API Application { ... };
```

On Windows this resolves to `__declspec(dllexport/dllimport)`; on Linux/macOS to `__attribute__((visibility(...)))`.

### Include Style

```cpp
// Third-party / system
#include <pxr/pxr.h>
#include <QWidget>

// Internal (always prefix with opendcc/)
#include "opendcc/app/core/application.h"
```

### CMake Conventions

- Use `cmake-format` (120-col limit); format is enforced by pre-commit.
- Target names mirror the C++ library: `opendcc_app_core`, `opendcc_viewport`, etc.
- Package metadata lives in `package.toml` alongside `CMakeLists.txt`.
- Find-package modules live in `cmake/modules/`.
- Helper macros live in `cmake/macros/`.

---

## Plugin / Package System

Plugins are self-contained directories under `src/packages/` with this structure:

```
opendcc.my_package/
├── package.toml           # Metadata: name, version, entry points
├── CMakeLists.txt
├── lib/                   # C++ source for this package
├── python/                # Python modules
└── pxr_plugins/           # USD plugInfo.json (optional)
```

`package.toml` example:

```toml
[package]
name = "opendcc.my_package"
version = "0.1.0"

[entry_points]
init = "opendcc.my_package.startup:init"
init_ui = "opendcc.my_package.startup:init_ui"
```

---

## Application Startup

1. `dcc_base` binary loads `configs/opendcc.usd_editor.toml` (generated from `.in` template).
2. The config calls `opendcc.startup.init()` (Python) → loads the plugin manager → discovers packages.
3. After core init, `opendcc.startup.init_ui()` registers panels via `PanelFactory`.
4. IPC command server starts on port 8000 (ZMQ).

---

## Key Source Locations

| Concern | Path |
|---|---|
| App singleton | `src/lib/opendcc/app/core/application.h` |
| Session management | `src/lib/opendcc/app/core/session.h` |
| Undo/redo | `src/lib/opendcc/app/core/undo/` |
| Main window | `src/lib/opendcc/app/ui/main_window.h` |
| Panel factory | `src/lib/opendcc/app/ui/panel_factory.h` |
| Viewport (OpenGL) | `src/lib/opendcc/app/viewport/viewport_gl_widget.h` |
| Hydra engine | `src/lib/opendcc/app/viewport/viewport_hydra_engine.h` |
| Python startup | `src/python/opendcc/startup.py` |
| Plugin manager | `src/python/opendcc/plugin_manager/` |
| Web server | `web/server.py` |
| Platform defines | `src/lib/opendcc/base/defines.h` |
| Export macros | `src/lib/opendcc/base/export.h` |

---

## Development Workflow

### Pre-commit Hooks

Install once:
```bash
pip install pre-commit
pre-commit install
```

Hooks run automatically on commit:
- **cmake-format** on `CMakeLists.txt` and `*.cmake`
- **black** on `*.py` (line-length 100, Python 3.9)
- **clang-format** on `*.cpp` / `*.h` (excludes `vendor/` and `thirdparty/`)

### Manual Formatting

```bash
# C++
clang-format -i src/lib/opendcc/app/core/application.cpp

# Python
black --line-length 100 src/python/opendcc/

# CMake
cmake-format -i src/packages/opendcc.my_package/CMakeLists.txt
```

### Docker Build

```bash
docker build -t opendcc .
docker run -p 8080:8080 opendcc
```

The runtime container exposes a FastAPI web server on port 8080 with WebSocket-based interactive rendering (`QT_QPA_PLATFORM=offscreen`).

---

## IPC & Remote Rendering

- **ZMQ command server** on port 8000 — accepts JSON commands; see `src/lib/opendcc/base/ipc_commands_api/`.
- **Web server** (FastAPI) on port 8080 — streams rendered frames over WebSocket; assets in `web/`.
- **USD delta sync** — live collaboration uses USD layer delta serialization from `src/lib/opendcc/usd/usd_live_share/`.

---

## Rendering Backends

| Backend | CMake Option | Plugin Dir |
|---|---|---|
| Storm (OpenGL) | always available | built-in |
| Arnold | `DCC_BUILD_ARNOLD_SUPPORT=ON` | `plugin/arnold/` |
| RenderMan | `DCC_BUILD_RENDERMAN_SUPPORT=ON` | `plugin/renderman/` |
| Cycles | `DCC_BUILD_CYCLES_SUPPORT=ON` | `plugin/cycles/` |

Renderer plugins implement the `HdRenderDelegate` interface from Hydra.

---

## Important Notes for AI Assistants

1. **Do not break VFX CY2022 compatibility** — target Python 3.9, USD 22.05, Qt 5.15. Do not use Python 3.10+ syntax.
2. **Always use the namespace macros** (`OPENDCC_NAMESPACE_OPEN/CLOSE`) — never write `namespace opendcc {` directly.
3. **Export macros are mandatory** for any public API class or function in a shared library.
4. **Pre-commit must pass** — run `pre-commit run --files <changed files>` before committing.
5. **Plugin registration** belongs in `package.toml`, not hardcoded elsewhere.
6. **Undo support** — all USD mutations should go through the undo system (`src/lib/opendcc/app/core/undo/`).
7. **Tests use doctest** — add `#include <doctest/doctest.h>` test cases in `.cpp` files; enable with `-DDCC_BUILD_TESTS=ON`.
8. **No new vendored code** without updating `.clang-format-ignore` and `THIRD_PARTY_LICENSES.txt`.
9. **Avoid `boost::noncopyable`** — use deleted copy constructors/operators (C++11 style) instead.
10. **Python style** — Black-formatted, 100-char line limit, must be Python 3.9-compatible (no walrus operator, no `match` statements, no `f-string =` debugging).

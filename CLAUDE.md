# CLAUDE.md — OpenDCC Codebase Guide

This document provides guidance for AI assistants (Claude, Copilot, etc.) working in the OpenDCC codebase.

---

## Project Overview

**OpenDCC** is an Apache 2.0 licensed, open-source Digital Content Creation (DCC) application framework
for building modular, production-grade 3D tools. It targets the **VFX Reference Platform CY2022**
(Python 3.9, USD 22.05+).

Key capabilities:
- Multi-renderer support (Arnold, RenderMan, Cycles, Storm/OpenGL)
- Plugin-based architecture via `package.toml` descriptors
- Full undo/redo with Python recording
- Live USD layer collaboration
- Embedded Python scripting/REPL
- Web-based headless rendering (FastAPI + WebSocket)
- Animation system (AnimX curves + graph editor)
- HydraOps procedural framework
- Viewport tools: sculpt, UV editor, paint primvar, texture paint, bezier, point instancer
- Node editor framework (generic + USD shader)
- Bullet physics simulation

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
│   ├── macros/           # CMake helper macros
│   └── modules/          # Find*.cmake scripts for all dependencies
├── configs/              # TOML application config templates
├── docker/               # Multi-stage Docker build files
├── web/                  # FastAPI web server + assets
├── icons/                # UI icons (1900+ PNG/SVG)
├── i18n/                 # Internationalization files (English TS)
├── CMakeLists.txt        # Root project (version 0.4.0.0)
├── .clang-format         # C++ formatting rules (Microsoft, 150-col, C++17)
├── .cmake-format         # CMake formatting rules (120-col)
├── .pre-commit-config.yaml
├── pyproject.toml        # Black Python formatter config
├── requirements.txt      # Python dependencies
├── MACOS_BUILD.md        # macOS-specific build notes
├── CONTRIBUTORS.md       # Contributor list
├── LICENSE.txt           # Apache 2.0
└── THIRD_PARTY_LICENSES.txt
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
- Qt Advanced Docking System (ADS)
- Sentry Native (crash reporter)
- ZeroMQ
- Optional: Arnold SDK, RenderMan SDK, Cycles, Bullet

### Configure & Build (Linux / Windows)

```bash
mkdir build && cd build
cmake .. -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DUSD_ROOT=/opt/usd \
  -DCMAKE_PREFIX_PATH="/opt/qt;/opt/ads;/opt/sentry"
ninja install
```

Default install prefix: `/opt/opendcc`

### CMake Options

| Option | Default | Description |
|---|---|---|
| `DCC_BUILD_TESTS` | OFF | Enable doctest unit tests |
| `DCC_BUILD_ANIM_ENGINE` | ON | Animation system (AnimX) |
| `DCC_BUILD_EXPRESSIONS_ENGINE` | ON | Expression evaluation system |
| `DCC_BUILD_RENDER_VIEW` | ON | Standalone render viewer |
| `DCC_RENDER_VIEW_WIN_GUI_EXECUTABLE` | ON | Render view as GUI app on Windows |
| `DCC_BUILD_ARNOLD_SUPPORT` | ON | Arnold renderer |
| `DCC_BUILD_RENDERMAN_SUPPORT` | OFF | RenderMan renderer |
| `DCC_BUILD_CYCLES_SUPPORT` | OFF | Cycles renderer |
| `DCC_BUILD_BULLET_PHYSICS` | ON | Bullet physics simulation |
| `DCC_BUILD_HYDRA_OP` | OFF | HydraOps procedural framework |
| `DCC_EMBEDDED_PYTHON_HOME` | ON | Use embedded Python |
| `DCC_NODE_EDITOR` | ON | Qt node editor framework |
| `DCC_USD_FALLBACK_PROXY_BUILD_ARNOLD_USD` | ON | Arnold USD fallback proxy |
| `DCC_USD_FALLBACK_PROXY_BUILD_CYCLES` | ON | Cycles fallback proxy |
| `DCC_USD_FALLBACK_PROXY_BUILD_MOONRAY` | OFF | MoonRay fallback proxy |
| `DCC_USE_PYTHON_3` | ON | Build with Python 3 |
| `DCC_USE_PTEX` | OFF | Enable Ptex texture support |
| `DCC_PYSIDE_CMAKE_FIND` | OFF | Use cmake exports for PySide2 |
| `DCC_USE_HYDRA_FRAMING_API` | OFF | Use Hydra Framing API (USD 21.08+) |
| `DCC_HOUDINI_SUPPORT` | OFF | Houdini integration |
| `DCC_KATANA_SUPPORT` | OFF | Katana integration |
| `DCC_DEBUG_BUILD` | OFF | Debug mode |
| `DCC_VERBOSE_SHIBOKEN_OUTPUT` | OFF | Verbose Shiboken generator output |
| `DCC_TESTS_USD_RENDER` | OFF | Out-of-process USD render test |
| `DCC_LANG` | `"all"` | Language to build with (`en`, `all`) |
| `DCC_DEFAULT_CONFIG` | `"opendcc.usd_editor.toml"` | Default TOML config file |
| `WITH_LINKER_GOLD` | ON (GCC) | Use ld.gold linker (GCC only) |
| `WITH_WINDOWS_SCCACHE` | OFF | Use sccache (Ninja + Windows) |

#### Per-Package Build Options

| Option | Default | Package |
|---|---|---|
| `DCC_PACKAGE_OPENDCC_USD_EDITOR_UV_EDITOR` | ON | UV editor |
| `DCC_PACKAGE_OPENDCC_USD_EDITOR_PAINT_PRIMVAR_TOOL` | ON | Paint primvar |
| `DCC_PACKAGE_OPENDCC_USD_EDITOR_SCULPT_TOOL` | ON | Sculpt tool |
| `DCC_PACKAGE_OPENDCC_USD_EDITOR_POINT_INSTANCER_TOOL` | ON | Point instancer |
| `DCC_PACKAGE_OPENDCC_USD_EDITOR_TEXTURE_PAINT_TOOL` | ON | Texture paint |
| `DCC_PACKAGE_OPENDCC_USD_EDITOR_BEZIER_TOOL` | ON | Bezier tool |
| `DCC_PACKAGE_OPENDCC_USD_EDITOR_LIGHT_OUTLINER` | ON | Light outliner |
| `DCC_PACKAGE_OPENDCC_USD_EDITOR_LIGHT_LINKING_EDITOR` | ON | Light linking editor |
| `DCC_PACKAGE_OPENDCC_USD_EDITOR_LIVE_SHARE` | ON | Live USD collaboration |
| `DCC_PACKAGE_OPENDCC_HYDRA_OP_SCENE_GRAPH` | ON | HydraOp scene graph UI |
| `DCC_PACKAGE_OPENDCC_HYDRA_OP_ATTRIBUTE_VIEW` | ON | HydraOp attribute view |

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
└── opendcc_setup.sh             # Environment setup script
```

Run before launching:
```bash
source /opt/opendcc/opendcc_setup.sh
```

The setup script sets `PYTHONPATH`, `LD_LIBRARY_PATH` (Linux) or `DYLD_LIBRARY_PATH` (macOS),
and `PXR_PLUGINPATH_NAME`.

---

## macOS Build

> **Status:** macOS support is in progress. See [MACOS_BUILD.md](MACOS_BUILD.md) for the full guide.

Key differences from Linux:
- Use `DYLD_LIBRARY_PATH` instead of `LD_LIBRARY_PATH` (handled automatically by CMake macros).
- OpenGL 4.1 Core Profile is the maximum on macOS.
- Embree 3 has limited ARM support — build with `EMBREE_ISA_AVX=OFF EMBREE_ISA_AVX2=OFF`.
- Arnold/RenderMan/Cycles must be disabled on macOS (`DCC_BUILD_ARNOLD_SUPPORT=OFF`, etc.).
- App bundle creation via `cmake/macros/make_osx_bundle.py` + `dylibbundler`.

```bash
cmake .. -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DUSD_ROOT=/opt/usd \
  -DCMAKE_PREFIX_PATH="/opt/homebrew/opt/qt@5;/opt/ads;/opt/sentry" \
  -DPython3_ROOT_DIR=$(python3 -c "import sys; print(sys.prefix)") \
  -DDCC_BUILD_ARNOLD_SUPPORT=OFF \
  -DDCC_USD_FALLBACK_PROXY_BUILD_ARNOLD_USD=OFF \
  -DDCC_USD_FALLBACK_PROXY_BUILD_CYCLES=OFF \
  -DDCC_BUILD_RENDERMAN_SUPPORT=OFF
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

On Windows this resolves to `__declspec(dllexport/dllimport)`; on Linux/macOS to
`__attribute__((visibility(...)))`.

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

### All Included Packages

**Animation:**

| Package | Description |
|---|---|
| `opendcc.anim_engine` | Main animation system |
| `opendcc.anim_engine.core` | Core animation engine |
| `opendcc.anim_engine.curve` | AnimX curve management |
| `opendcc.anim_engine.schema` | Animation schema |
| `opendcc.anim_engine.ui.graph_editor` | AnimX graph view |
| `opendcc.vendor.animx` | Vendored AnimX library |

**HydraOps Procedural Framework:**

| Package | Description |
|---|---|
| `opendcc.hydra_op` | Core HydraOp system (built on HydraSceneIndex) |
| `opendcc.hydra_op.schema` | HydraOp USD schema |
| `opendcc.hydra_op.translator` | Scene translation |
| `opendcc.hydra_op.render` | Rendering integration |
| `opendcc.hydra_op.ui.attribute_view` | Attribute editor UI |
| `opendcc.hydra_op.ui.node_editor` | Node graph editor |
| `opendcc.hydra_op.ui.scene_browser` | Scene browser |
| `opendcc.hydra_op.ui.scene_graph` | Scene graph view |

**USD Editor Tools:**

| Package | Description |
|---|---|
| `opendcc.usd_editor.common_cmds` | Common USD commands |
| `opendcc.usd_editor.common_tools` | Common USD tools |
| `opendcc.usd_editor.bezier_tool` | Bezier curve editing |
| `opendcc.usd_editor.bullet_physics` | Bullet physics layout tool |
| `opendcc.usd_editor.expression_variables_toolbar` | Expression variable UI |
| `opendcc.usd_editor.live_share` | Live USD collaboration |
| `opendcc.usd_editor.material_editor` | Material/shader editing |
| `opendcc.usd_editor.paint_primvar_tool` | Primvar painting |
| `opendcc.usd_editor.point_instancer_tool` | Point instancer editing |
| `opendcc.usd_editor.scene_indices` | Scene index management |
| `opendcc.usd_editor.sculpt_tool` | Point-based sculpting |
| `opendcc.usd_editor.texture_paint` | Texture painting |
| `opendcc.usd_editor.usd_node_editor` | USD shader node editor |
| `opendcc.usd_editor.uv_editor` | UV layout editor |

**UI Frameworks:**

| Package | Description |
|---|---|
| `opendcc.ui.code_editor` | Code editor widget |
| `opendcc.ui.node_editor` | Generic Qt node editor framework |
| `opendcc.ui.script_editor` | Python script editor / REPL |

**Other:**

| Package | Description |
|---|---|
| `opendcc.expression` | Expression evaluation system |
| `opendcc.external.graphviz` | Graphviz integration |

---

## Application Startup

1. `dcc_base` binary loads `configs/opendcc.usd_editor.toml` (generated from `.in` template).
2. The config calls `opendcc.startup.init()` (Python) → loads the plugin manager → discovers packages.
3. After core init, `opendcc.startup.init_ui()` registers panels via `PanelFactory`.
4. IPC command server starts on port 8000 (ZMQ).

---

## Key Source Locations

### Application Core (`src/lib/opendcc/app/`)

| Concern | Path |
|---|---|
| App singleton | `src/lib/opendcc/app/core/application.h` |
| Session management | `src/lib/opendcc/app/core/session.h` |
| Undo/redo | `src/lib/opendcc/app/core/undo/` |
| Main window | `src/lib/opendcc/app/ui/main_window.h` |
| Panel factory | `src/lib/opendcc/app/ui/panel_factory.h` |
| Viewport (OpenGL) | `src/lib/opendcc/app/viewport/viewport_gl_widget.h` |
| Hydra engine | `src/lib/opendcc/app/viewport/viewport_hydra_engine.h` |

### Base Infrastructure (`src/lib/opendcc/base/`)

| Concern | Path |
|---|---|
| Platform defines | `src/lib/opendcc/base/defines.h` |
| Export macros | `src/lib/opendcc/base/export.h` |
| App config (TOML) | `src/lib/opendcc/base/app_config/` |
| Command system | `src/lib/opendcc/base/commands_api/` |
| IPC commands (ZMQ) | `src/lib/opendcc/base/ipc_commands_api/` |
| Crash reporting | `src/lib/opendcc/base/crash_reporting/` |
| Logging | `src/lib/opendcc/base/logging/` |
| Plugin packaging | `src/lib/opendcc/base/packaging/` |
| Python utilities | `src/lib/opendcc/base/py_utils/` |
| pybind11 bridge | `src/lib/opendcc/base/pybind_bridge/` |
| Test runner | `src/lib/opendcc/base/test_runner/` |

### USD / Rendering (`src/lib/opendcc/usd/`, `src/lib/opendcc/render_system/`)

| Concern | Path |
|---|---|
| USD compositing | `src/lib/opendcc/usd/compositing/` |
| Hydra render sessions | `src/lib/opendcc/usd/hydra_render_session_api/` |
| Layer change tracking | `src/lib/opendcc/usd/layer_tree_watcher/` |
| Rendering backend | `src/lib/opendcc/usd/render/` |
| IPC serialization | `src/lib/opendcc/usd/usd_ipc_serialization/` |
| Live collaboration | `src/lib/opendcc/usd/usd_live_share/` |
| Renderer abstraction | `src/lib/opendcc/render_system/` |
| Display driver API | `src/lib/opendcc/render_view/display_driver_api/` |
| Image viewer | `src/lib/opendcc/render_view/image_view/` |

### UI Components (`src/lib/opendcc/ui/`)

| Concern | Path |
|---|---|
| Color theme system | `src/lib/opendcc/ui/color_theme/` |
| Common widgets (timeline, shelf) | `src/lib/opendcc/ui/common_widgets/` |
| Logger panel | `src/lib/opendcc/ui/logger_panel/` |
| OCIO color widgets | `src/lib/opendcc/ui/ocio_color_widgets/` |

### Python (`src/python/opendcc/`)

| Concern | Path |
|---|---|
| Python startup | `src/python/opendcc/startup.py` |
| Plugin manager | `src/python/opendcc/plugin_manager/` |
| Action system | `src/python/opendcc/actions/` |
| Preferences | `src/python/opendcc/preferences/` |
| Core Python APIs | `src/python/opendcc/core/` |
| Bake UI | `src/python/opendcc/bake_ui/` |
| Syntax highlighting | `src/python/opendcc/pygments_utils/` |

### Executables (`src/bin/`)

| Binary | Description |
|---|---|
| `dcc_base` | Main application |
| `render_view` | Standalone render viewer |
| `usd_render` | Headless USD/Hydra rendering |
| `usd_ipc_broker` | ZMQ IPC message broker |
| `crash_reporter` | Sentry crash reporter daemon |

---

## IPC & Remote Rendering

- **ZMQ command server** on port 8000 — accepts JSON commands; see `src/lib/opendcc/base/ipc_commands_api/`.
- **Web server** (FastAPI) on port 8080 — streams rendered frames over WebSocket; assets in `web/`.
- **USD delta sync** — live collaboration uses USD layer delta serialization from
  `src/lib/opendcc/usd/usd_live_share/`.

---

## Rendering Backends

| Backend | CMake Option | Notes |
|---|---|---|
| Storm (OpenGL) | always available | Built-in Hydra renderer |
| Arnold | `DCC_BUILD_ARNOLD_SUPPORT=ON` | Requires Arnold SDK |
| RenderMan | `DCC_BUILD_RENDERMAN_SUPPORT=ON` | Requires RenderMan SDK |
| Cycles | `DCC_BUILD_CYCLES_SUPPORT=ON` | Requires Cycles SDK |

Renderer plugins implement the `HdRenderDelegate` interface from Hydra.

---

## Docker

```bash
# Full application container
docker build -t opendcc .

# Web server only
docker build -t opendcc-web -f docker/Dockerfile.web .

# Run (exposes FastAPI on port 8080, Qt offscreen rendering)
docker run -p 8080:8080 opendcc
```

The runtime container uses `QT_QPA_PLATFORM=offscreen` and exposes the FastAPI web server
on port 8080 with WebSocket-based interactive rendering.

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

---

## Important Notes for AI Assistants

1. **Do not break VFX CY2022 compatibility** — target Python 3.9, USD 22.05, Qt 5.15.
   Do not use Python 3.10+ syntax.
2. **Always use the namespace macros** (`OPENDCC_NAMESPACE_OPEN/CLOSE`) — never write
   `namespace opendcc {` directly.
3. **Export macros are mandatory** for any public API class or function in a shared library.
4. **Pre-commit must pass** — run `pre-commit run --files <changed files>` before committing.
5. **Plugin registration** belongs in `package.toml`, not hardcoded elsewhere.
6. **Undo support** — all USD mutations should go through the undo system
   (`src/lib/opendcc/app/core/undo/`).
7. **Tests use doctest** — add `#include <doctest/doctest.h>` test cases in `.cpp` files;
   enable with `-DDCC_BUILD_TESTS=ON`.
8. **No new vendored code** without updating `.clang-format-ignore` and
   `THIRD_PARTY_LICENSES.txt`.
9. **Avoid `boost::noncopyable`** — use deleted copy constructors/operators (C++11 style)
   instead.
10. **Python style** — Black-formatted, 100-char line limit, must be Python 3.9-compatible
    (no walrus operator, no `match` statements, no `f-string =` debugging).
11. **macOS builds** require disabling Arnold/RenderMan/Cycles and using
    `DYLD_LIBRARY_PATH` — see [MACOS_BUILD.md](MACOS_BUILD.md).
12. **HydraOps** (`DCC_BUILD_HYDRA_OP=ON`) is an opt-in procedural framework built on
    HydraSceneIndex; packages under `opendcc.hydra_op.*` are only compiled when enabled.

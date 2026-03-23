"""
OpenDCC Web Server
==================
FastAPI backend that:
  1. Serves the needle-tools usd-wasm bindings at /usd  (WASM+JS)
  2. Exports the live USD stage as .usda at /api/stage/export.usda
  3. Broadcasts scene_changed over WebSocket so the browser reloads on edits
  4. Exposes REST endpoints for prim inspection, editing, and DCC commands

CRITICAL: SharedArrayBuffer (required by the WASM multi-threading) needs
  Cross-Origin-Embedder-Policy: require-corp
  Cross-Origin-Opener-Policy:   same-origin
  Both headers are added by the _CORPMiddleware on every response.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import io
import json
import logging
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

# ── Logging ───────────────────────────────────────────────────────────────────
logger = logging.getLogger("opendcc.web")
logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s  %(name)s  %(message)s")

# ── Path resolution ───────────────────────────────────────────────────────────
_HERE         = Path(__file__).parent
_STATIC_DIR   = _HERE / "static"
_REPO_ROOT    = _HERE.parent
_PARENT_ROOT  = _REPO_ROOT.parent

_USD_VIEWER   = Path(os.environ.get(
    "USD_VIEWER_PATH",
    str(_PARENT_ROOT / "usd-viewer")
))
_USD_WASM_SRC = _USD_VIEWER / "usd-wasm" / "src"
_USD_MODULES  = _USD_VIEWER / "public" / "modules"

# ── OpenDCC state ─────────────────────────────────────────────────────────────
_opendcc_available = False
_pxr_available     = False
_app_core  = None
_session   = None
_pxr_stage = None   # Used in pxr-only mode (no opendcc.core)
_pxr_stage_path: Optional[str] = None
_pxr_edit_layer = None
_pxr_edit_layer_path: Optional[str] = None

# Simple undo/redo for pxr mode (USDA snapshot stack)
_pxr_undo_stack: list = []
_pxr_redo_stack: list = []


def _pxr_snapshot_layer() -> Optional[str]:
    if not _pxr_stage:
        return None
    layer = _pxr_edit_layer if _pxr_edit_layer is not None else _pxr_stage.GetRootLayer()
    if not layer:
        return None
    return layer.ExportToString()


def _pxr_restore_layer(usda: str) -> bool:
    if not _pxr_stage or usda is None:
        return False
    layer = _pxr_edit_layer if _pxr_edit_layer is not None else _pxr_stage.GetRootLayer()
    if not layer:
        return False
    layer.ImportFromString(usda)
    try:
        _pxr_stage.SetEditTarget(layer)
    except Exception:
        pass
    return True


def _pxr_push_undo():
    """Save current pxr authored layer state for undo."""
    global _pxr_redo_stack
    if not _pxr_stage:
        return
    try:
        usda = _pxr_snapshot_layer()
        if usda is None:
            return
        _pxr_undo_stack.append(usda)
        _pxr_redo_stack = []  # clear redo on new action
        if len(_pxr_undo_stack) > 30:
            _pxr_undo_stack.pop(0)
    except Exception:
        pass


def _pxr_do_undo() -> bool:
    """Restore previous pxr authored layer state."""
    global _pxr_stage
    if not _pxr_undo_stack or not _pxr_stage:
        return False
    try:
        cur = _pxr_snapshot_layer()
        if cur is not None:
            _pxr_redo_stack.append(cur)
    except Exception:
        pass
    usda = _pxr_undo_stack.pop()
    return _pxr_restore_layer(usda)


def _pxr_do_redo() -> bool:
    """Restore next pxr authored layer state."""
    global _pxr_stage
    if not _pxr_redo_stack or not _pxr_stage:
        return False
    try:
        cur = _pxr_snapshot_layer()
        if cur is not None:
            _pxr_undo_stack.append(cur)
    except Exception:
        pass
    usda = _pxr_redo_stack.pop()
    return _pxr_restore_layer(usda)


def _init_opendcc() -> None:
    global _opendcc_available, _pxr_available, _app_core, _session
    # Try full OpenDCC C++ core first
    try:
        import opendcc.core as dcc_core
        from opendcc.app_config import ApplicationConfig

        opendcc_root = os.environ.get("OPENDCC_ROOT", "/opt/opendcc")
        cfg_path = os.path.join(opendcc_root, "configs", "opendcc.usd_editor.toml")

        cfg = ApplicationConfig(cfg_path)
        dcc_core.Application.set_app_config(cfg)
        dcc_core.Application.create_command_server()

        _app_core = dcc_core.Application.instance()
        _app_core.init_python([sys.argv[0]])
        _app_core.initialize_extensions()
        _app_core.run_startup_init()

        _session           = _app_core.get_session()
        _opendcc_available = True
        logger.info("OpenDCC initialised (headless mode)")
        return
    except (ImportError, Exception) as exc:
        logger.info("opendcc.core not available: %s", exc)

    # Fall back to pxr (USD Python bindings) directly
    try:
        from pxr import Usd, UsdGeom  # noqa: F401
        _pxr_available = True
        logger.info("pxr (OpenUSD Python) available — running in USD server mode")
        # Create a default stage
        _init_default_pxr_stage()
    except ImportError:
        logger.warning(
            "Neither opendcc.core nor pxr importable – running in STUB mode.\n"
            "Install OpenUSD or set PYTHONPATH to include USD Python bindings."
        )


def _init_default_pxr_stage() -> None:
    """Create a default in-memory USD stage with an explicit mesh cube.

    We use UsdGeomMesh (not UsdGeomCube) because the WASM Hydra render
    delegate (needle-tools) does not reliably tessellate implicit geometric
    prims like Cube/Sphere/Cylinder.  An explicit mesh with points and
    faceVertexIndices is guaranteed to render in all backends.
    """
    global _pxr_stage
    from pxr import Usd, UsdGeom, Gf, Vt, Sdf

    _pxr_stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(_pxr_stage, UsdGeom.Tokens.y)

    world = UsdGeom.Xform.Define(_pxr_stage, Sdf.Path("/World"))
    _pxr_stage.SetDefaultPrim(world.GetPrim())

    # -- Explicit cube mesh (8 vertices, 6 quad faces, CCW winding) ---------
    mesh = UsdGeom.Mesh.Define(_pxr_stage, Sdf.Path("/World/Cube"))
    mesh.GetPointsAttr().Set(Vt.Vec3fArray([
        Gf.Vec3f(-1, -1, -1), Gf.Vec3f( 1, -1, -1),
        Gf.Vec3f( 1,  1, -1), Gf.Vec3f(-1,  1, -1),
        Gf.Vec3f(-1, -1,  1), Gf.Vec3f( 1, -1,  1),
        Gf.Vec3f( 1,  1,  1), Gf.Vec3f(-1,  1,  1),
    ]))
    mesh.GetFaceVertexCountsAttr().Set(Vt.IntArray([4, 4, 4, 4, 4, 4]))
    mesh.GetFaceVertexIndicesAttr().Set(Vt.IntArray([
        0, 3, 2, 1,   # front  (-Z)
        4, 5, 6, 7,   # back   (+Z)
        0, 4, 7, 3,   # left   (-X)
        1, 2, 6, 5,   # right  (+X)
        0, 1, 5, 4,   # bottom (-Y)
        2, 3, 7, 6,   # top    (+Y)
    ]))
    mesh.GetExtentAttr().Set(Vt.Vec3fArray([
        Gf.Vec3f(-1, -1, -1), Gf.Vec3f(1, 1, 1),
    ]))
    mesh.GetSubdivisionSchemeAttr().Set("none")


# ── Init at module load (works with both `python server.py` and `uvicorn server:app`)
_init_opendcc()


# ═════════════════════════════════════════════════════════════════════════════
# In-memory stub stage
# Used when neither opendcc.core nor pxr is available (e.g. arm64 Docker).
# Stores a flat prim dict, serialises to valid USDA, supports undo/redo.
# ═════════════════════════════════════════════════════════════════════════════

class _StubStage:
    # Mesh data tuples ─────────────────────────────────────────────────────────
    _BOX_ATTRS = [
        'float3[] extent = [(-1,-1,-1),(1,1,1)]',
        'int[]    faceVertexCounts  = [4,4,4,4,4,4]',
        'int[]    faceVertexIndices = [0,3,2,1, 4,5,6,7, 0,4,7,3, 1,2,6,5, 0,1,5,4, 2,3,7,6]',
        'point3f[] points = [(-1,-1,-1),(1,-1,-1),(1,1,-1),(-1,1,-1),'
        '(-1,-1,1),(1,-1,1),(1,1,1),(-1,1,1)]',
        'uniform token subdivisionScheme = "none"',
    ]
    _PLANE_ATTRS = [
        'float3[] extent = [(-1,0,-1),(1,0,1)]',
        'int[]    faceVertexCounts  = [4]',
        'int[]    faceVertexIndices = [0,1,2,3]',
        'point3f[] points = [(-1,0,-1),(1,0,-1),(1,0,1),(-1,0,1)]',
        'normal3f[] normals = [(0,1,0),(0,1,0),(0,1,0),(0,1,0)]',
        'uniform token subdivisionScheme = "none"',
    ]

    # prim-type → (usd_type_name_in_usda, [attr_lines])
    _PRIM_DEFS: dict = {
        "Cube":          ("Mesh",            _BOX_ATTRS),
        "Plane":         ("Mesh",            _PLANE_ATTRS),
        "Sphere":        ("Sphere",          ["double radius = 1"]),
        "Cylinder":      ("Cylinder",        ["double radius = 1", "double height = 2"]),
        "Cone":          ("Cone",            ["double radius = 1", "double height = 2"]),
        "Capsule":       ("Capsule",         ["double radius = 0.5", "double height = 1"]),
        "Camera":        ("Camera",          ["float focalLength = 50"]),
        "Xform":         ("Xform",           []),
        "Scope":         ("Scope",           []),
        # UsdLux lights
        "CylinderLight": ("CylinderLight",   ["float inputs:intensity = 1000",
                                              "float inputs:radius = 0.5",
                                              "float inputs:length = 2"]),
        "DiskLight":     ("DiskLight",       ["float inputs:intensity = 1000",
                                              "float inputs:radius = 0.5"]),
        "DistantLight":  ("DistantLight",    ["float inputs:intensity = 2000",
                                              "float inputs:angle = 0.53"]),
        "DomeLight":     ("DomeLight",       ["float inputs:intensity = 1"]),
        "RectLight":     ("RectLight",       ["float inputs:intensity = 1000",
                                              "float inputs:width = 2",
                                              "float inputs:height = 2"]),
        "SphereLight":   ("SphereLight",     ["float inputs:intensity = 1000",
                                              "float inputs:radius = 0.5"]),
    }

    def __init__(self) -> None:
        self._prims:    dict = {}   # path → prim-data dict
        self._children: dict = {}   # path → [child-path, …]
        self._selection: list = []
        self._undo_stack: list = []
        self._redo_stack: list = []
        self._reset()

    # ── reset / init ──────────────────────────────────────────────────────────
    def _reset(self) -> None:
        self._prims = {
            "/World": {
                "name": "World", "type": "Xform", "usd_type": "Xform",
                "active": True, "visibility": "inherited", "attrs": [],
            },
            "/World/Cube": {
                "name": "Cube", "type": "Cube", "usd_type": "Mesh",
                "active": True, "visibility": "inherited",
                "attrs": list(self._BOX_ATTRS),
            },
        }
        self._children = {
            "/": ["/World"],
            "/World": ["/World/Cube"],
            "/World/Cube": [],
        }
        self._selection = []
        self._undo_stack.clear()
        self._redo_stack.clear()

    # ── undo / redo ───────────────────────────────────────────────────────────
    def _push_undo(self) -> None:
        self._undo_stack.append((
            copy.deepcopy(self._prims),
            copy.deepcopy(self._children),
            list(self._selection),
        ))
        self._redo_stack.clear()
        if len(self._undo_stack) > 50:
            self._undo_stack.pop(0)

    def undo(self) -> bool:
        if not self._undo_stack:
            return False
        self._redo_stack.append((
            copy.deepcopy(self._prims),
            copy.deepcopy(self._children),
            list(self._selection),
        ))
        self._prims, self._children, self._selection = self._undo_stack.pop()
        return True

    def redo(self) -> bool:
        if not self._redo_stack:
            return False
        self._undo_stack.append((
            copy.deepcopy(self._prims),
            copy.deepcopy(self._children),
            list(self._selection),
        ))
        self._prims, self._children, self._selection = self._redo_stack.pop()
        return True

    # ── helpers ───────────────────────────────────────────────────────────────
    def _parent_of(self, path: str) -> str:
        parts = path.rstrip("/").rsplit("/", 1)
        return parts[0] if len(parts) > 1 and parts[0] else "/"

    def _unique_name(self, parent_path: str, base: str) -> str:
        siblings = {self._prims[c]["name"]
                    for c in self._children.get(parent_path, [])
                    if c in self._prims}
        if base not in siblings:
            return base
        i = 1
        while f"{base}{i}" in siblings:
            i += 1
        return f"{base}{i}"

    def _ensure_world(self) -> None:
        if "/World" not in self._prims:
            self._prims["/World"] = {
                "name": "World", "type": "Xform", "usd_type": "Xform",
                "active": True, "visibility": "inherited", "attrs": [],
            }
            self._children.setdefault("/", [])
            if "/World" not in self._children["/"]:
                self._children["/"].append("/World")
            self._children.setdefault("/World", [])

    # ── CRUD ──────────────────────────────────────────────────────────────────
    def create_prim(self, prim_type: str, parent_path: str = "/World") -> str:
        self._ensure_world()
        if parent_path not in self._prims:
            parent_path = "/World"

        pdef      = self._PRIM_DEFS.get(prim_type, (prim_type, []))
        usd_type  = pdef[0]
        attrs     = list(pdef[1])
        name      = self._unique_name(parent_path, prim_type)
        path      = f"{parent_path.rstrip('/')}/{name}"

        self._prims[path] = {
            "name": name, "type": prim_type, "usd_type": usd_type,
            "active": True, "visibility": "inherited", "attrs": attrs,
        }
        self._children.setdefault(parent_path, [])
        self._children[parent_path].append(path)
        self._children[path] = []
        return path

    def delete_prim(self, path: str) -> None:
        for child in list(self._children.get(path, [])):
            self.delete_prim(child)
        parent = self._parent_of(path)
        if parent in self._children:
            self._children[parent] = [c for c in self._children[parent] if c != path]
        self._prims.pop(path, None)
        self._children.pop(path, None)
        self._selection = [s for s in self._selection if not s.startswith(path)]

    def duplicate_prim(self, path: str) -> str:
        prim = self._prims.get(path)
        if not prim:
            return ""
        parent   = self._parent_of(path)
        name     = self._unique_name(parent, prim["name"])
        new_path = f"{parent.rstrip('/')}/{name}"
        self._prims[new_path] = copy.deepcopy(prim)
        self._prims[new_path]["name"] = name
        self._children.setdefault(parent, [])
        self._children[parent].append(new_path)
        self._children[new_path] = []
        for child in self._children.get(path, []):
            self._dup_subtree(child, new_path)
        return new_path

    def _dup_subtree(self, old_path: str, new_parent: str) -> None:
        prim = self._prims.get(old_path)
        if not prim:
            return
        new_path = f"{new_parent.rstrip('/')}/{prim['name']}"
        self._prims[new_path] = copy.deepcopy(prim)
        self._children.setdefault(new_parent, [])
        self._children[new_parent].append(new_path)
        self._children[new_path] = []
        for child in self._children.get(old_path, []):
            self._dup_subtree(child, new_path)

    def group_prims(self, paths: list) -> str:
        if not paths:
            return ""
        parents = {self._parent_of(p) for p in paths}
        parent  = list(parents)[0] if len(parents) == 1 else "/World"
        self._ensure_world()
        name       = self._unique_name(parent, "Group")
        group_path = f"{parent.rstrip('/')}/{name}"
        self._prims[group_path] = {
            "name": name, "type": "Xform", "usd_type": "Xform",
            "active": True, "visibility": "inherited", "attrs": [],
        }
        self._children.setdefault(parent, [])
        self._children[parent].append(group_path)
        self._children[group_path] = []
        for p in paths:
            old_parent = self._parent_of(p)
            if old_parent in self._children:
                self._children[old_parent] = [c for c in self._children[old_parent] if c != p]
            self._children[group_path].append(p)
        return group_path

    def rename_prim(self, path: str, new_name: str) -> str:
        prim = self._prims.get(path)
        if not prim:
            return path
        parent   = self._parent_of(path)
        siblings = {self._prims[c]["name"]
                    for c in self._children.get(parent, []) if c != path and c in self._prims}
        if new_name in siblings:
            i = 1
            while f"{new_name}{i}" in siblings:
                i += 1
            new_name = f"{new_name}{i}"
        new_path = f"{parent.rstrip('/')}/{new_name}"
        data     = self._prims.pop(path)
        data["name"] = new_name
        self._prims[new_path] = data
        if parent in self._children:
            self._children[parent] = [new_path if c == path else c
                                       for c in self._children[parent]]
        children = self._children.pop(path, [])
        self._children[new_path] = children
        self._selection = [new_path if s == path else s for s in self._selection]
        return new_path

    def set_visibility(self, path: str, visible: bool) -> None:
        prim = self._prims.get(path)
        if prim:
            prim["visibility"] = "inherited" if visible else "invisible"

    def reparent(self, path: str, new_parent: str) -> None:
        old_parent = self._parent_of(path)
        if old_parent == new_parent:
            return
        if old_parent in self._children:
            self._children[old_parent] = [c for c in self._children[old_parent] if c != path]
        self._children.setdefault(new_parent, [])
        self._children[new_parent].append(path)

    # ── serialisation helpers (for /api/prims) ────────────────────────────────
    def prim_dict(self, path: str) -> dict:
        prim = self._prims.get(path, {})
        return {
            "path":       path,
            "name":       prim.get("name", path.rsplit("/", 1)[-1]),
            "type":       prim.get("type", "Xform"),
            "active":     prim.get("active", True),
            "visibility": prim.get("visibility", "inherited"),
            "children":   self._children.get(path, []),
        }

    def children_dicts(self, parent_path: str) -> list:
        result = []
        for child_path in self._children.get(parent_path, []):
            d = self.prim_dict(child_path)
            d["children"] = self._children.get(child_path, [])
            result.append(d)
        return result

    # ── USDA export ───────────────────────────────────────────────────────────
    def to_usda(self) -> str:
        lines = [
            '#usda 1.0',
            '(',
            '    upAxis = "Y"',
            '    doc = "OpenDCC web scene"',
            ')',
            '',
        ]
        for child_path in self._children.get("/", []):
            lines.extend(self._prim_lines(child_path, 0))
        return "\n".join(lines)

    def _prim_lines(self, path: str, depth: int) -> list:
        prim = self._prims.get(path)
        if not prim:
            return []
        ind      = "    " * depth
        usd_type = prim.get("usd_type", prim.get("type", "Xform"))
        name     = prim.get("name", path.rsplit("/", 1)[-1])
        vis      = prim.get("visibility", "inherited")
        active   = prim.get("active", True)

        lines: list = []
        act_str = "" if active else " (active = false)"
        lines.append(f'{ind}def {usd_type} "{name}"{act_str}')
        lines.append(f'{ind}{{')
        if vis == "invisible":
            lines.append(f'{ind}    token visibility = "invisible"')
        for attr in prim.get("attrs", []):
            lines.append(f'{ind}    {attr}')
        for child_path in self._children.get(path, []):
            lines.extend(self._prim_lines(child_path, depth + 1))
        lines.append(f'{ind}}}')
        lines.append('')
        return lines


# Global stub stage instance
_stub_stage = _StubStage()


# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(title="OpenDCC Web", version="0.1.0")


# ── COOP / COEP middleware ────────────────────────────────────────────────────
class _CORPMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
        response.headers["Cross-Origin-Opener-Policy"]   = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-site"
        return response


app.add_middleware(_CORPMiddleware)


# ── Static mounts ─────────────────────────────────────────────────────────────
if _USD_WASM_SRC.exists():
    app.mount("/usd", StaticFiles(directory=str(_USD_WASM_SRC)), name="usd-wasm")
    logger.info("usd-wasm served from %s", _USD_WASM_SRC)
else:
    logger.warning(
        "usd-wasm not found at %s.\n"
        "Clone https://github.com/needle-tools/usd-viewer next to OpenDCC,\n"
        "or set USD_VIEWER_PATH env var.", _USD_WASM_SRC
    )

if _USD_MODULES.exists():
    app.mount("/modules", StaticFiles(directory=str(_USD_MODULES)), name="modules")

if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ── WebSocket connection manager ──────────────────────────────────────────────
class _Connections:
    def __init__(self) -> None:
        self.active: list = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.active = [c for c in self.active if c is not ws]

    async def broadcast(self, msg: dict) -> None:
        text = json.dumps(msg)
        for ws in list(self.active):
            try:
                await ws.send_text(text)
            except Exception:
                self.disconnect(ws)


_conns = _Connections()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _current_stage():
    if _opendcc_available:
        return _session.get_stage() if _session else None
    if _pxr_available:
        return _pxr_stage
    return None


def _configure_pxr_edit_layer(stage, stage_path: str):
    """Configure a non-destructive sidecar edit layer for pxr mode.

    Edits are authored in a stronger layer (`*.opendcc_edits.usda`) referenced
    by the stage session layer. The imported root layer remains untouched.
    """
    from pxr import Sdf

    global _pxr_edit_layer, _pxr_edit_layer_path

    if not stage or not stage_path:
        _pxr_edit_layer = None
        _pxr_edit_layer_path = None
        return

    stage_dir = os.path.dirname(stage_path)
    base = os.path.splitext(os.path.basename(stage_path))[0]
    edit_path = os.path.join(stage_dir, f"{base}.opendcc_edits.usda")

    layer = Sdf.Layer.FindOrOpen(edit_path)
    if not layer:
        layer = Sdf.Layer.CreateNew(edit_path)

    session = stage.GetSessionLayer()
    rel = os.path.relpath(edit_path, stage_dir).replace("\\", "/")
    paths = list(session.subLayerPaths)
    if rel not in paths and edit_path not in paths:
        paths.insert(0, rel)
        session.subLayerPaths = paths

    stage.SetEditTarget(layer)

    _pxr_edit_layer = layer
    _pxr_edit_layer_path = edit_path


def _make_explicit_mesh(stage, prim_path, shape: str) -> None:
    """Set up explicit mesh points for Cube or Plane (no implicit prims)."""
    from pxr import UsdGeom, Gf, Vt
    mesh = UsdGeom.Mesh.Get(stage, prim_path)
    if not mesh:
        mesh = UsdGeom.Mesh.Define(stage, prim_path)
    if shape == "Cube":
        mesh.GetPointsAttr().Set(Vt.Vec3fArray([
            Gf.Vec3f(-1,-1,-1), Gf.Vec3f(1,-1,-1),
            Gf.Vec3f(1,1,-1),   Gf.Vec3f(-1,1,-1),
            Gf.Vec3f(-1,-1,1),  Gf.Vec3f(1,-1,1),
            Gf.Vec3f(1,1,1),    Gf.Vec3f(-1,1,1),
        ]))
        mesh.GetFaceVertexCountsAttr().Set(Vt.IntArray([4]*6))
        mesh.GetFaceVertexIndicesAttr().Set(Vt.IntArray([
            0,3,2,1, 4,5,6,7, 0,4,7,3, 1,2,6,5, 0,1,5,4, 2,3,7,6]))
        mesh.GetExtentAttr().Set(Vt.Vec3fArray([Gf.Vec3f(-1,-1,-1), Gf.Vec3f(1,1,1)]))
    elif shape == "Plane":
        mesh.GetPointsAttr().Set(Vt.Vec3fArray([
            Gf.Vec3f(-1,0,-1), Gf.Vec3f(1,0,-1),
            Gf.Vec3f(1,0,1),   Gf.Vec3f(-1,0,1),
        ]))
        mesh.GetFaceVertexCountsAttr().Set(Vt.IntArray([4]))
        mesh.GetFaceVertexIndicesAttr().Set(Vt.IntArray([0,1,2,3]))
        mesh.GetExtentAttr().Set(Vt.Vec3fArray([Gf.Vec3f(-1,0,-1), Gf.Vec3f(1,0,1)]))
    mesh.GetSubdivisionSchemeAttr().Set("none")


def _prim_to_dict(prim) -> dict:
    return {
        "path":     str(prim.GetPath()),
        "name":     prim.GetName(),
        "type":     prim.GetTypeName(),
        "active":   prim.IsActive(),
        "children": [str(c.GetPath()) for c in prim.GetChildren()],
    }


def _get_attr_interpolation_mode(attr) -> str:
    """Return curve interpolation mode stored in custom data.

    Supported: linear, step, bezier (bezier is display/edit hint for now).
    """
    try:
        mode = attr.GetCustomDataByKey("opendcc:interp")
        if isinstance(mode, str):
            mode = mode.strip().lower()
            if mode in {"linear", "step", "bezier"}:
                return mode
    except Exception:
        pass
    return "linear"


def _attr_to_dict(attr, time: Optional[float] = None) -> dict:
    interpolation = _get_attr_interpolation_mode(attr)
    try:
        if time is not None:
            from pxr import Usd
            # Step mode: evaluate at lower bracketing sample when possible
            if interpolation == "step":
                times = list(attr.GetTimeSamples() or [])
                if times:
                    lower, _upper = attr.GetBracketingTimeSamples(float(time))
                    val = attr.Get(Usd.TimeCode(float(lower)))
                else:
                    val = attr.Get(Usd.TimeCode(time))
            else:
                val = attr.Get(Usd.TimeCode(time))
            val = str(val)
        else:
            val = str(attr.Get())
    except Exception:
        val = "<error>"
    return {
        "name":        attr.GetName(),
        "type":        str(attr.GetTypeName()),
        "value":       val,
        "variability": str(attr.GetVariability()),
        "interpolation": interpolation,
    }


def _coerce_value(attr, raw_value: Any):
    try:
        from pxr import Gf, Vt
        type_name = str(attr.GetTypeName())
        t = type_name.lower()

        if isinstance(raw_value, list):
            if len(raw_value) >= 3:
                if "double3" in t or "vec3d" in t:
                    return Gf.Vec3d(*[float(x) for x in raw_value[:3]])
                if any(k in t for k in ["float3", "vec3f", "vec3", "color3", "normal3", "point3", "vector3"]):
                    return Gf.Vec3f(*[float(x) for x in raw_value[:3]])
            if len(raw_value) >= 2:
                if "double2" in t or "vec2d" in t:
                    return Gf.Vec2d(*[float(x) for x in raw_value[:2]])
                if "float2" in t or "vec2" in t:
                    return Gf.Vec2f(*[float(x) for x in raw_value[:2]])
            if len(raw_value) >= 4:
                if "double4" in t or "vec4d" in t:
                    return Gf.Vec4d(*[float(x) for x in raw_value[:4]])
                if "float4" in t or "vec4" in t:
                    return Gf.Vec4f(*[float(x) for x in raw_value[:4]])

            if "float" in t:
                return Vt.FloatArray([float(x) for x in raw_value])
            if "int" in t:
                return Vt.IntArray([int(x) for x in raw_value])

        if isinstance(raw_value, (int, float)):
            if "float" in t or "double" in t:
                return float(raw_value)
            if "int" in t:
                return int(raw_value)
            if "bool" in t:
                return bool(raw_value)

        return raw_value
    except Exception as exc:
        logger.debug("coerce_value failed: %s", exc)
        return raw_value


# ── Server-side GPU rendering ─────────────────────────────────────────────────

@app.get(
    "/api/render/snapshot",
    response_class=Response,
    responses={200: {"content": {"image/png": {}}}},
)
async def render_snapshot(
    width: int = 1280,
    height: int = 720,
    camera: str = "",
    time: float = None,
    eye: str = "",
    target: str = "",
    fov: float = 0,
    aspect: float = 0,
    near: float = 0,
    far: float = 0,
):
    """Render the current stage via Hydra Storm (GPU) and return a PNG.

    If eye/target/fov are provided, the snapshot matches the client viewport.
    Otherwise falls back to auto-framing from the bounding box.
    """
    stage = _current_stage()
    if not stage:
        raise HTTPException(status_code=400, detail="No stage loaded")

    # Clamp resolution to sane limits
    width  = max(64, min(width, 7680))
    height = max(64, min(height, 4320))

    try:
        from render_snapshot import render_stage_to_png
        from pxr import Usd

        tc = Usd.TimeCode(time) if time is not None else None

        # Build viewport camera dict if client sent eye/target/fov
        viewport_cam = None
        if eye and target and fov:
            try:
                viewport_cam = {
                    "eye":    [float(v) for v in eye.split(",")],
                    "target": [float(v) for v in target.split(",")],
                    "fov":    fov,
                    "aspect": aspect if aspect > 0 else (width / height),
                    "near":   near if near > 0 else 0.01,
                    "far":    far if far > 0 else 100000,
                }
            except (ValueError, IndexError):
                pass  # Fall back to auto-framing

        png_bytes = render_stage_to_png(
            stage, width, height, tc, camera,
            viewport_camera=viewport_cam,
        )

        if png_bytes is None:
            raise HTTPException(
                status_code=501,
                detail="GPU rendering not available — use client-side WASM viewport",
            )

        return Response(
            content=png_bytes,
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )
    except HTTPException:
        raise
    except ImportError as exc:
        raise HTTPException(status_code=501, detail=f"Render module not available: {exc}")
    except Exception as exc:
        logger.warning("Snapshot render failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Render failed: {exc}")


@app.get("/api/render/status")
async def render_status():
    """Check if server-side GPU rendering is available."""
    try:
        from render_snapshot import check_gpu_rendering_available
        return check_gpu_rendering_available()
    except ImportError:
        return {
            "frame_recorder": False,
            "imaging_gl": False,
            "egl": False,
            "reason": "render_snapshot module not importable",
        }


# ── Page ──────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index():
    html_path = _STATIC_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>OpenDCC Web</h1><p>static/index.html not found.</p>")


@app.get("/health")
async def health():
    result = {
        "status":         "ok",
        "opendcc":        _opendcc_available,
        "pxr":            _pxr_available,
        "usd_wasm_found": _USD_WASM_SRC.exists(),
    }

    # GPU info — written by gpu-entrypoint.sh at container startup
    gpu_info_path = Path("/tmp/gpu-info.json")
    if gpu_info_path.exists():
        try:
            result["gpu"] = json.loads(gpu_info_path.read_text())
        except Exception:
            result["gpu"] = {"gpu_available": False, "error": "parse_failed"}
    else:
        # Not running in GPU container, or no entrypoint ran
        result["gpu"] = {
            "gpu_available": False,
            "render_mode": os.environ.get("OPENDCC_RENDER_MODE", "client_wasm"),
        }

    # ── Verify actual render pipeline (not just GPU hardware presence) ─────
    # gpu_available only means nvidia-smi found a card.  snapshot_available
    # is True only when a test render actually succeeds end-to-end.
    try:
        from render_snapshot import check_gpu_rendering_available
        render_info = check_gpu_rendering_available()
        result["gpu"]["render_status"] = render_info
        result["gpu"]["snapshot_available"] = render_info.get("test_render_ok", False)
        if not result["gpu"]["snapshot_available"]:
            result["gpu"]["snapshot_reason"] = render_info.get(
                "reason", "GPU render pipeline not functional")
    except ImportError:
        result["gpu"]["snapshot_available"] = False
        result["gpu"]["snapshot_reason"] = "render_snapshot module not importable"

    return result


# ── USD stage export ──────────────────────────────────────────────────────────

@app.get(
    "/api/stage/export.usda",
    response_class=Response,
    responses={200: {"content": {"model/vnd.usda": {}}}},
)
async def stage_export_usda(flatten: bool = False):
    """Export the current stage as USDA.

    By default exports the root layer (preserving composition arcs).
    Set flatten=true to resolve composed opinions into one layer.
    """
    stage = _current_stage()
    if stage:
        try:
            if flatten:
                flat_layer = stage.Flatten()
                usda = flat_layer.ExportToString()
            else:
                usda = stage.GetRootLayer().ExportToString()
            return Response(
                content=usda,
                media_type="model/vnd.usda",
                headers={"Cache-Control": "no-store"},
            )
        except Exception as exc:
            logger.warning("Export failed: %s", exc)
    # Fallback to stub
    return Response(
        content=_stub_stage.to_usda(),
        media_type="model/vnd.usda",
        headers={"Cache-Control": "no-store"},
    )


# ── Stage asset tree (for WASM composition) ──────────────────────────────────

@app.get("/api/stage/assets")
async def stage_assets():
    """List USD layers required for WASM composition preload.

    Uses stage.GetUsedLayers() when available (faster than scanning whole tree).
    In pxr edit-layer mode, returns `composeLayers=[edit, root]` so frontend can
    open a tiny synthetic root that composes non-destructive overrides.
    """
    import os

    stage = _current_stage()
    if not stage:
        return {"rootLayer": None, "assets": []}

    real_path = stage.GetRootLayer().realPath
    if not real_path or not os.path.exists(real_path):
        # In-memory stage — no files to serve
        return {"rootLayer": None, "assets": []}

    stage_dir = os.path.dirname(real_path)
    root_name = os.path.basename(real_path)

    assets_set = set()
    texture_exts = {".png", ".jpg", ".jpeg", ".tga", ".exr", ".hdr", ".tx", ".bmp", ".tif", ".tiff", ".webp"}

    # Preferred: only preload actually used layers.
    try:
        for layer in stage.GetUsedLayers():
            lp = layer.realPath or ""
            if not lp:
                continue
            if not os.path.exists(lp):
                continue
            # Keep only files under stage dir (same trust model as /api/stage/asset)
            if not os.path.normpath(lp).startswith(os.path.normpath(stage_dir)):
                continue
            ext = os.path.splitext(lp)[1].lower()
            if ext not in (".usd", ".usda", ".usdc", ".usdz", ".mtlx"):
                continue
            rel = os.path.relpath(lp, stage_dir).replace("\\", "/")
            assets_set.add(rel)
    except Exception:
        # Fallback: old behavior (directory scan)
        for dirpath, _dirs, filenames in os.walk(stage_dir):
            for f in filenames:
                full = os.path.join(dirpath, f)
                rel = os.path.relpath(full, stage_dir).replace("\\", "/")
                ext = os.path.splitext(f)[1].lower()
                if ext in (".usd", ".usda", ".usdc", ".usdz", ".mtlx"):
                    assets_set.add(rel)

    assets_set.add(root_name)

    # MaterialX texture support: preload image files near used .mtlx files.
    # This enables usd-wasm delegate texture loading via driver.getFile().
    mtlx_rel_paths = [p for p in assets_set if p.lower().endswith('.mtlx')]
    for mrel in mtlx_rel_paths:
        try:
            mabs = os.path.normpath(os.path.join(stage_dir, mrel))
            if not os.path.exists(mabs):
                continue
            mdir = os.path.dirname(mabs)

            # Scan material directory (and child dirs such as ./tex)
            for dirpath, _dirs, filenames in os.walk(mdir):
                for f in filenames:
                    ext = os.path.splitext(f)[1].lower()
                    if ext not in texture_exts:
                        continue
                    full = os.path.join(dirpath, f)
                    if not os.path.normpath(full).startswith(os.path.normpath(stage_dir)):
                        continue
                    rel = os.path.relpath(full, stage_dir).replace('\\', '/')
                    assets_set.add(rel)
        except Exception:
            continue

    compose_layers = None
    if _pxr_available and stage is _pxr_stage and _pxr_edit_layer_path:
        try:
            if os.path.exists(_pxr_edit_layer_path):
                edit_rel = os.path.relpath(_pxr_edit_layer_path, stage_dir).replace("\\", "/")
                assets_set.add(edit_rel)
                # Strongest first
                compose_layers = [edit_rel, root_name]
        except Exception:
            pass

    assets = sorted(assets_set)
    result = {
        "rootLayer": root_name,
        "stageDir": stage_dir,
        "assets": assets,
        "count": len(assets),
    }
    if compose_layers:
        result["composeLayers"] = compose_layers
        result["compositionMode"] = "synthetic_root"
        result["editLayer"] = _pxr_edit_layer_path
    return result


@app.get("/api/stage/asset")
async def stage_asset(path: str):
    """Serve a specific asset file from the current stage directory."""
    import os
    from pathlib import Path as P

    stage = _current_stage()
    if not stage:
        raise HTTPException(status_code=400, detail="No stage")

    real_path = stage.GetRootLayer().realPath
    if not real_path:
        raise HTTPException(status_code=400, detail="In-memory stage")

    stage_dir = os.path.dirname(real_path)
    resolved = os.path.normpath(os.path.join(stage_dir, path))
    
    # Security: must stay within stage directory
    if not resolved.startswith(stage_dir):
        raise HTTPException(status_code=403, detail="Path traversal denied")

    if not os.path.exists(resolved):
        raise HTTPException(status_code=404, detail=f"Asset not found: {path}")

    content = P(resolved).read_bytes()

    # Normalize MaterialX references for usd-wasm compatibility when
    # serving textual USDA layers: strip SDF_FORMAT_ARGS from .mtlx refs,
    # because some wasm builds treat the full token as a literal filename.
    ext = os.path.splitext(resolved)[1].lower()
    if ext in (".usd", ".usda") and content.startswith(b"#usda"):
        try:
            txt = content.decode("utf-8")

            def _fix_mtlx_ref(m):
                ref_path = m.group(1)
                return f"@{ref_path}@"

            patched = re.sub(
                r"@([^@\n\r]*?\.mtlx)(:SDF_FORMAT_ARGS:[^@\n\r]*)?@",
                _fix_mtlx_ref,
                txt,
                flags=re.IGNORECASE,
            )
            if patched != txt:
                content = patched.encode("utf-8")
        except Exception:
            pass

    media = {
        ".usda": "model/vnd.usda",
        ".usd": "application/octet-stream",
        ".usdc": "application/octet-stream",
        ".usdz": "application/zip",
        ".mtlx": "application/xml",
    }.get(ext, "application/octet-stream")

    return Response(
        content=content,
        media_type=media,
        headers={"Cache-Control": "max-age=3600"},
    )


# ── Stage dependencies (for WASM composition resolution) ──────────────────────

@app.get("/api/stage/dependencies")
async def stage_dependencies():
    """List all external dependencies (sublayers, references, payloads) of the stage."""
    stage = _current_stage()
    if not stage:
        return {"sublayers": [], "references": [], "payloads": []}

    try:
        from pxr import UsdUtils
        sublayers = []
        references = []
        payloads = []
        root_path = stage.GetRootLayer().realPath or ""
        UsdUtils.ExtractExternalReferences(root_path, sublayers, references, payloads)
        return {
            "sublayers": sublayers,
            "references": references,
            "payloads": payloads,
            "rootLayer": root_path,
        }
    except Exception as exc:
        # Fallback: enumerate used layers from the stage
        layers = []
        try:
            for layer in stage.GetUsedLayers():
                layers.append(layer.identifier)
        except Exception:
            pass
        return {"layers": layers, "error": str(exc)}


@app.get("/api/stage/layer")
async def stage_layer(path: str):
    """Serve a specific layer/asset file for WASM composition resolution."""
    import os
    from pathlib import Path as P

    # Security: only serve from allowed roots
    stages_root = os.environ.get("OPENDCC_STAGES_ROOT", "/data/stages")
    allowed_roots = [stages_root, "/opt/opendcc", "/tmp"]

    # Resolve the path
    resolved = os.path.abspath(path)
    if not any(resolved.startswith(root) for root in allowed_roots):
        raise HTTPException(status_code=403, detail="Access denied")

    if not os.path.exists(resolved):
        raise HTTPException(status_code=404, detail=f"File not found: {path}")

    content = P(resolved).read_bytes()
    ext = os.path.splitext(resolved)[1].lower()
    media = {
        ".usda": "model/vnd.usda",
        ".usdc": "application/octet-stream",
        ".usdz": "application/zip",
        ".usd":  "application/octet-stream",
        ".mtlx": "application/xml",
        ".png":  "image/png",
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
        ".hdr":  "image/vnd.radiance",
        ".exr":  "image/x-exr",
    }.get(ext, "application/octet-stream")

    return Response(content=content, media_type=media,
                    headers={"Cache-Control": "max-age=3600"})


# ── GLB export (for Three.js GLTFLoader in browser) ──────────────────────────

@app.get(
    "/api/stage/export.glb",
    response_class=Response,
    responses={200: {"content": {"model/gltf-binary": {}}}},
)
async def stage_export_glb():
    """Export the current stage as binary glTF (.glb) for Three.js."""
    # Try full USD → glTF conversion first (works with opendcc.core OR pxr)
    stage = _current_stage()

    if stage:
        try:
            from usd_to_gltf import stage_to_glb
            glb = stage_to_glb(stage)
            return Response(
                content=glb,
                media_type="model/gltf-binary",
                headers={"Cache-Control": "no-store"},
            )
        except Exception as exc:
            logger.warning("glTF export failed: %s", exc)

    # Fallback: try opening stub USDA via pxr if available
    try:
        from pxr import Usd
        from usd_to_gltf import stage_to_glb
        usda = _stub_stage.to_usda()
        tmp_stage = Usd.Stage.CreateInMemory()
        tmp_stage.GetRootLayer().ImportFromString(usda)
        glb = stage_to_glb(tmp_stage)
        return Response(
            content=glb,
            media_type="model/gltf-binary",
            headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:
        logger.warning("Stub glTF export failed: %s", exc)

    # Last resort: empty glTF
    import struct, json as _json
    empty_gltf = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": []}], "nodes": []}
    j = _json.dumps(empty_gltf).encode()
    pad = (4 - len(j) % 4) % 4
    j += b" " * pad
    header = struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(j))
    chunk = struct.pack("<II", len(j), 0x4E4F534A) + j
    return Response(
        content=header + chunk,
        media_type="model/gltf-binary",
        headers={"Cache-Control": "no-store"},
    )


# ── Stage management ──────────────────────────────────────────────────────────

@app.get("/api/stages")
async def list_stages():
    """List available USD stages in the stages directory."""
    import os
    stages_root = os.environ.get("OPENDCC_STAGES_ROOT", "/data/stages")
    if not os.path.exists(stages_root):
        return {"stages": [], "root": stages_root}

    stages = []
    for entry in sorted(os.listdir(stages_root)):
        full = os.path.join(stages_root, entry)
        if os.path.isfile(full) and entry.endswith(('.usd', '.usda', '.usdc', '.usdz')):
            size = os.path.getsize(full)
            stages.append({"name": entry, "path": entry, "size": size, "type": "file"})
        elif os.path.isdir(full):
            # Look for a root USD file inside
            for f in os.listdir(full):
                if f.endswith(('.usd', '.usda', '.usdc')) and not f.startswith('.'):
                    fpath = os.path.join(entry, f)
                    size = os.path.getsize(os.path.join(full, f))
                    stages.append({"name": f"{entry}/{f}", "path": fpath,
                                   "size": size, "type": "dir"})
    return {"stages": stages, "root": stages_root}

class _StageOpenReq(BaseModel):
    path: str


@app.post("/api/stage/open")
async def stage_open(req: _StageOpenReq):
    global _pxr_stage, _pxr_stage_path, _pxr_edit_layer, _pxr_edit_layer_path
    stages_root = os.environ.get("OPENDCC_STAGES_ROOT", "/data/stages")
    p = req.path if os.path.isabs(req.path) else os.path.join(stages_root, req.path)

    if _opendcc_available:
        try:
            _session.open_stage(p)
            await _conns.broadcast({"event": "stage_opened", "path": p})
            await _conns.broadcast({"event": "scene_changed"})
            return {"ok": True, "path": p}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    if _pxr_available:
        try:
            from pxr import Usd, Ar
            if not os.path.exists(p):
                raise HTTPException(status_code=404, detail=f"File not found: {p}")
            # Open with LoadNone first (fast), then load all
            _pxr_stage = Usd.Stage.Open(p, Usd.Stage.LoadAll)
            if not _pxr_stage:
                raise HTTPException(status_code=400, detail=f"Failed to open: {p}")

            _pxr_stage_path = p
            _configure_pxr_edit_layer(_pxr_stage, p)
            _pxr_undo_stack.clear()
            _pxr_redo_stack.clear()

            prim_count = sum(1 for _ in _pxr_stage.TraverseAll())
            logger.info(
                "Opened stage: %s (%d prims) | edit layer: %s",
                p,
                prim_count,
                _pxr_edit_layer_path or "<none>",
            )
            await _conns.broadcast({"event": "stage_opened", "path": p})
            await _conns.broadcast({"event": "scene_changed"})
            return {
                "ok": True,
                "path": p,
                "edit_layer": _pxr_edit_layer_path,
                "non_destructive": bool(_pxr_edit_layer_path),
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # Stub mode
    await _conns.broadcast({"event": "stage_opened", "path": p})
    await _conns.broadcast({"event": "scene_changed"})
    return {"ok": True, "stub": True, "path": p}


@app.post("/api/stage/new")
async def stage_new():
    global _pxr_stage, _pxr_stage_path, _pxr_edit_layer, _pxr_edit_layer_path
    if _opendcc_available:
        _session.new_stage()
    elif _pxr_available:
        _init_default_pxr_stage()
        _pxr_stage_path = None
        _pxr_edit_layer = None
        _pxr_edit_layer_path = None
        _pxr_undo_stack.clear()
        _pxr_redo_stack.clear()
    else:
        _stub_stage._reset()
    await _conns.broadcast({"event": "stage_new"})
    await _conns.broadcast({"event": "scene_changed"})
    return {"ok": True}


@app.post("/api/stage/save")
async def stage_save():
    if _opendcc_available:
        try:
            _session.save_stage()
            return {"ok": True}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    if _pxr_available and _pxr_stage:
        try:
            if _pxr_edit_layer is not None:
                _pxr_edit_layer.Save()
                return {
                    "ok": True,
                    "saved": "edit_layer",
                    "layer_path": _pxr_edit_layer_path,
                    "non_destructive": True,
                }

            _pxr_stage.GetRootLayer().Save()
            return {"ok": True, "saved": "root_layer", "non_destructive": False}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    return {"ok": True, "stub": True}


class _StageSaveEditsAsReq(BaseModel):
    path: str
    switch_to_exported_layer: bool = False


@app.post("/api/stage/save_edits_as")
async def stage_save_edits_as(req: _StageSaveEditsAsReq):
    """Export current authored edit layer opinions to a dedicated USDA file."""
    global _pxr_edit_layer, _pxr_edit_layer_path

    if _opendcc_available:
        raise HTTPException(status_code=501, detail="save_edits_as is currently implemented for pxr mode")

    if not (_pxr_available and _pxr_stage and _pxr_edit_layer):
        raise HTTPException(status_code=400, detail="No active pxr edit layer")

    out_path = str(req.path or "").strip()
    if not out_path:
        raise HTTPException(status_code=400, detail="Missing destination path")

    base_dir = os.path.dirname(_pxr_stage_path or "") if _pxr_stage_path else os.environ.get("OPENDCC_STAGES_ROOT", "/data/stages")
    stages_root = os.environ.get("OPENDCC_STAGES_ROOT", "/data/stages")

    if os.path.isabs(out_path):
        resolved = out_path
    else:
        resolved = os.path.normpath(os.path.join(base_dir, out_path))
        # Convenience: treat "Kitchen_set/foo.usda" as stages-root-relative,
        # not relative to ".../Kitchen_set".
        try:
            first = out_path.replace("\\", "/").split("/", 1)[0]
            if first and first == os.path.basename(base_dir.rstrip("/\\")):
                resolved = os.path.normpath(os.path.join(stages_root, out_path))
        except Exception:
            pass

    out_dir = os.path.dirname(resolved)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    try:
        ok = _pxr_edit_layer.Export(resolved)
        if ok is False:
            raise RuntimeError("Layer export returned false")

        if req.switch_to_exported_layer:
            from pxr import Sdf
            layer = Sdf.Layer.FindOrOpen(resolved)
            if not layer:
                raise RuntimeError(f"Failed to open exported layer: {resolved}")
            _pxr_stage.SetEditTarget(layer)
            _pxr_edit_layer = layer
            _pxr_edit_layer_path = resolved

        return {
            "ok": True,
            "path": resolved,
            "switched": bool(req.switch_to_exported_layer),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/stage/info")
async def stage_info():
    stage = _current_stage()
    if not stage:
        return {"stage": None}
    from pxr import UsdGeom
    edit_layer = None
    try:
        et = stage.GetEditTarget().GetLayer()
        if et:
            edit_layer = et.identifier
    except Exception:
        pass

    return {
        "stage": {
            "identifier": stage.GetRootLayer().identifier,
            "prim_count":  sum(1 for _ in stage.TraverseAll()),
            "up_axis":     str(UsdGeom.GetStageUpAxis(stage)),
            "edit_layer":  edit_layer,
        }
    }


# ── Find prim by name ─────────────────────────────────────────────────────────

@app.get("/api/prims/find")
async def prims_find(name: str):
    """Find prim paths matching a given name (last path segment)."""
    stage = _current_stage()
    if stage:
        matches = []
        for prim in stage.TraverseAll():
            if prim.GetName() == name:
                matches.append(str(prim.GetPath()))
        return {"matches": matches}
    # Stub fallback
    matches = [p for p in _stub_stage._prims if p.rsplit("/", 1)[-1] == name]
    return {"matches": matches}


# ── Prim tree ─────────────────────────────────────────────────────────────────

@app.get("/api/prims")
async def prims_list(path: str = "/"):
    stage = _current_stage()
    if stage:
        from pxr import Sdf
        prim = stage.GetPrimAtPath(Sdf.Path(path))
        if not prim or not prim.IsValid():
            raise HTTPException(status_code=404, detail=f"Prim not found: {path}")
        return {
            "path":     path,
            "children": [_prim_to_dict(c) for c in prim.GetChildren()],
        }

    return {"path": path, "children": _stub_stage.children_dicts(path)}


@app.get("/api/prim/{prim_path:path}")
async def prim_detail(prim_path: str, time: Optional[float] = None):
    full_path = "/" + prim_path.lstrip("/")

    stage = _current_stage()
    if stage:
        from pxr import Sdf
        prim = stage.GetPrimAtPath(Sdf.Path(full_path))
        if not prim or not prim.IsValid():
            raise HTTPException(status_code=404, detail=f"Prim not found: {full_path}")
        return {
            **_prim_to_dict(prim),
            "query_time": time,
            "attributes": [_attr_to_dict(a, time=time) for a in prim.GetAttributes()],
        }

    # Stub fallback
    d = _stub_stage.prim_dict(full_path)
    prim = _stub_stage._prims.get(full_path, {})
    attrs = []
    for attr_line in prim.get("attrs", []):
        parts = attr_line.strip().split(" = ", 1)
        if len(parts) == 2:
            left  = parts[0].strip().rsplit(" ", 1)
            aname = left[-1] if left else attr_line
            atype = left[0]  if len(left) > 1 else "token"
            attrs.append({"name": aname, "type": atype,
                           "value": parts[1], "variability": "varying"})
    return {**d, "query_time": time, "attributes": attrs}


@app.get("/api/prim_spatial/{prim_path:path}")
async def prim_spatial(prim_path: str, time: Optional[float] = None):
    """Return lightweight spatial data for a prim (pivot + world bbox).

    Used by viewport gizmo/frame fallbacks when no direct Three.js mesh mapping
    is available for selected Xform/Scope prims.
    """
    full_path = "/" + prim_path.lstrip("/")

    stage = _current_stage()
    if not stage:
        return {"ok": False, "path": full_path, "error": "no stage"}

    if not _pxr_available:
        return {"ok": False, "path": full_path, "error": "spatial query requires pxr mode"}

    from pxr import Sdf, Usd, UsdGeom

    prim = stage.GetPrimAtPath(Sdf.Path(full_path))
    if not prim or not prim.IsValid():
        raise HTTPException(status_code=404, detail=f"Prim not found: {full_path}")

    tc = Usd.TimeCode.Default() if time is None else Usd.TimeCode(float(time))

    pivot = None
    try:
        xcache = UsdGeom.XformCache(tc)
        m = xcache.GetLocalToWorldTransform(prim)
        t = m.ExtractTranslation()
        pivot = [float(t[0]), float(t[1]), float(t[2])]
    except Exception:
        pivot = None

    bbox_data = None
    try:
        purposes = [
            UsdGeom.Tokens.default_,
            UsdGeom.Tokens.render,
            UsdGeom.Tokens.proxy,
            UsdGeom.Tokens.guide,
        ]
        bcache = UsdGeom.BBoxCache(tc, purposes, useExtentsHint=True, ignoreVisibility=False)
        wb = bcache.ComputeWorldBound(prim)
        rng = wb.GetRange()
        if rng and not rng.IsEmpty():
            mn = rng.GetMin()
            mx = rng.GetMax()
            cx = (float(mn[0]) + float(mx[0])) * 0.5
            cy = (float(mn[1]) + float(mx[1])) * 0.5
            cz = (float(mn[2]) + float(mx[2])) * 0.5
            sx = float(mx[0]) - float(mn[0])
            sy = float(mx[1]) - float(mn[1])
            sz = float(mx[2]) - float(mn[2])
            radius = max(1e-6, (sx * sx + sy * sy + sz * sz) ** 0.5 * 0.5)
            bbox_data = {
                "min": [float(mn[0]), float(mn[1]), float(mn[2])],
                "max": [float(mx[0]), float(mx[1]), float(mx[2])],
                "center": [cx, cy, cz],
                "size": [sx, sy, sz],
                "radius": radius,
            }
            if pivot is None:
                pivot = [cx, cy, cz]
    except Exception:
        bbox_data = None

    return {
        "ok": True,
        "path": full_path,
        "type": str(prim.GetTypeName() or ""),
        "pivot": pivot,
        "bbox": bbox_data,
    }


def _uv_pair_from_value(v: Any) -> Optional[list[float]]:
    try:
        if hasattr(v, "__len__") and len(v) >= 2:
            return [float(v[0]), float(v[1])]
    except Exception:
        pass
    try:
        return [float(v[0]), float(v[1])]
    except Exception:
        return None


def _resolve_mesh_prim_for_uv(prim):
    """Return a mesh prim for UV extraction.

    If the selected prim is not a mesh (Xform/Scope), walk descendants first,
    then a short parent fallback chain.
    """
    from pxr import UsdGeom

    if prim and prim.IsValid() and prim.IsA(UsdGeom.Mesh):
        return prim

    queue = []
    if prim and prim.IsValid():
        queue.append(prim)

    seen: set[str] = set()
    visit_limit = 6000

    while queue and len(seen) < visit_limit:
        cur = queue.pop(0)
        key = str(cur.GetPath())
        if key in seen:
            continue
        seen.add(key)

        if cur.IsA(UsdGeom.Mesh):
            return cur

        try:
            for c in cur.GetChildren():
                if c and c.IsValid():
                    queue.append(c)
        except Exception:
            pass

    cur = prim
    hops = 0
    while cur and cur.IsValid() and hops < 8:
        try:
            cur = cur.GetParent()
        except Exception:
            break
        hops += 1
        if cur and cur.IsValid() and cur.IsA(UsdGeom.Mesh):
            return cur

    return None


@app.get("/api/prim_uv/{prim_path:path}")
async def prim_uv(prim_path: str, primvar: str = "st", time: Optional[float] = None, max_faces: int = 8000):
    """Extract UV face loops for a mesh (UV Editor MVP viewport).

    Returns lightweight polyline data ready for Canvas2D rendering.
    """
    full_path = "/" + prim_path.lstrip("/")

    stage = _current_stage()
    if not stage:
        return {"ok": False, "path": full_path, "error": "no stage"}

    from pxr import Sdf, Usd, UsdGeom

    prim = stage.GetPrimAtPath(Sdf.Path(full_path))
    if not prim or not prim.IsValid():
        raise HTTPException(status_code=404, detail=f"Prim not found: {full_path}")

    mesh_prim = _resolve_mesh_prim_for_uv(prim)
    if not mesh_prim:
        return {"ok": False, "path": full_path, "error": "No mesh found under selected prim"}

    mesh = UsdGeom.Mesh(mesh_prim)
    tc = Usd.TimeCode.Default() if time is None else Usd.TimeCode(float(time))

    counts = list(mesh.GetFaceVertexCountsAttr().Get(tc) or [])
    fvi = list(mesh.GetFaceVertexIndicesAttr().Get(tc) or [])
    if not counts or not fvi:
        return {
            "ok": False,
            "path": full_path,
            "meshPath": str(mesh_prim.GetPath()),
            "error": "Mesh has no face topology",
        }

    max_faces = max(1, min(int(max_faces), 50000))

    primvar_name = str(primvar or "").strip() or "st"
    pva = UsdGeom.PrimvarsAPI(mesh_prim)
    pv = pva.GetPrimvar(primvar_name)

    def _is_valid_primvar(pv_obj) -> bool:
        try:
            return bool(pv_obj and pv_obj.GetAttr() and pv_obj.GetAttr().IsValid())
        except Exception:
            return False

    if not _is_valid_primvar(pv):
        for cand in ("st", "uv", "map1"):
            cpv = pva.GetPrimvar(cand)
            if _is_valid_primvar(cpv):
                pv = cpv
                primvar_name = cand
                break

    if not _is_valid_primvar(pv):
        return {
            "ok": False,
            "path": full_path,
            "meshPath": str(mesh_prim.GetPath()),
            "error": f"No UV primvar found (requested: {primvar_name})",
        }

    try:
        uv_values = list(pv.Get(tc) or [])
    except Exception:
        uv_values = []

    if not uv_values:
        return {
            "ok": False,
            "path": full_path,
            "meshPath": str(mesh_prim.GetPath()),
            "primvar": primvar_name,
            "error": "UV primvar has no values",
        }

    idx_values: list[int] = []
    try:
        if pv.IsIndexed():
            idx_values = list(pv.GetIndices(tc) or [])
    except Exception:
        idx_values = []

    interpolation = str(pv.GetInterpolation() or "")
    interp_l = interpolation.lower()

    total_fv = sum(max(0, int(c or 0)) for c in counts)
    max_vidx = max([int(i) for i in fvi], default=-1)

    if interp_l in {"facevarying", "face_varying"}:
        mode = "faceVarying"
    elif interp_l in {"vertex", "varying"}:
        mode = "vertex"
    elif interp_l == "uniform":
        mode = "uniform"
    elif interp_l == "constant":
        mode = "constant"
    else:
        if len(uv_values) == total_fv:
            mode = "faceVarying"
        elif len(uv_values) > max_vidx >= 0:
            mode = "vertex"
        elif len(uv_values) >= len(counts):
            mode = "uniform"
        else:
            mode = "constant"

    def _uv_for_raw_index(raw_idx: Optional[int]) -> Optional[list[float]]:
        if raw_idx is None:
            return None
        try:
            i = int(raw_idx)
        except Exception:
            return None
        if i < 0:
            return None

        if idx_values:
            if i >= len(idx_values):
                return None
            try:
                i = int(idx_values[i])
            except Exception:
                return None

        if i < 0 or i >= len(uv_values):
            return None
        return _uv_pair_from_value(uv_values[i])

    faces: list[list[float]] = []
    cursor = 0
    min_u = float("inf")
    max_u = float("-inf")
    min_v = float("inf")
    max_v = float("-inf")

    for fi, count in enumerate(counts):
        n = int(count or 0)
        if n <= 0:
            continue

        if len(faces) >= max_faces:
            break

        poly: list[float] = []
        for j in range(n):
            fv_idx = cursor + j

            raw_idx: Optional[int]
            if mode == "faceVarying":
                raw_idx = fv_idx
            elif mode == "vertex":
                raw_idx = int(fvi[fv_idx]) if fv_idx < len(fvi) else None
            elif mode == "uniform":
                raw_idx = fi
            else:  # constant
                raw_idx = 0

            uv = _uv_for_raw_index(raw_idx)
            if not uv:
                continue

            u = float(uv[0])
            v = float(uv[1])
            poly.extend([u, v])

            if u < min_u:
                min_u = u
            if u > max_u:
                max_u = u
            if v < min_v:
                min_v = v
            if v > max_v:
                max_v = v

        if len(poly) >= 6:
            faces.append(poly)

        cursor += n

    if not faces:
        return {
            "ok": False,
            "path": full_path,
            "meshPath": str(mesh_prim.GetPath()),
            "primvar": primvar_name,
            "error": "Failed to decode UV face loops",
        }

    if not all(map(lambda x: x != float("inf") and x != float("-inf"), [min_u, max_u, min_v, max_v])):
        min_u, max_u, min_v, max_v = 0.0, 1.0, 0.0, 1.0

    return {
        "ok": True,
        "path": full_path,
        "meshPath": str(mesh_prim.GetPath()),
        "primvar": primvar_name,
        "interpolation": mode,
        "faceCount": len(counts),
        "drawFaceCount": len(faces),
        "truncated": len(faces) < len(counts),
        "bounds": {
            "minU": float(min_u),
            "maxU": float(max_u),
            "minV": float(min_v),
            "maxV": float(max_v),
        },
        "faces": faces,
    }


# ── Prim time samples (Curve Editor support) ──────────────────────────────────

@app.get("/api/prim_samples/{prim_path:path}")
async def prim_time_samples(prim_path: str, max_samples: int = 400):
    full_path = "/" + prim_path.lstrip("/")

    stage = _current_stage()
    if stage:
        from pxr import Sdf, Usd
        prim = stage.GetPrimAtPath(Sdf.Path(full_path))
        if not prim or not prim.IsValid():
            raise HTTPException(status_code=404, detail=f"Prim not found: {full_path}")

        max_samples = max(1, min(int(max_samples), 2000))
        animated = []

        for attr in prim.GetAttributes():
            try:
                times = list(attr.GetTimeSamples() or [])
            except Exception:
                times = []

            if not times:
                continue

            samples = []
            for t in times[:max_samples]:
                try:
                    v = attr.Get(Usd.TimeCode(t))
                    samples.append({
                        "time": float(t),
                        "value": str(v) if v is not None else None,
                    })
                except Exception:
                    samples.append({"time": float(t), "value": "<error>"})

            animated.append({
                "name": attr.GetName(),
                "type": str(attr.GetTypeName()),
                "sample_count": len(times),
                "interpolation": _get_attr_interpolation_mode(attr),
                "samples": samples,
            })

        return {
            "path": full_path,
            "count": len(animated),
            "start_time_code": stage.GetStartTimeCode(),
            "end_time_code": stage.GetEndTimeCode(),
            "time_codes_per_second": stage.GetTimeCodesPerSecond(),
            "animated": animated,
        }

    return {"path": full_path, "count": 0, "animated": []}


# ── Material info / editing / binding ────────────────────────────────────────


def _sanitize_ident(name: str, fallback: str = "Mat") -> str:
    raw = str(name or fallback)
    out = []
    for ch in raw:
        if ch.isalnum() or ch in {"_", "-"}:
            out.append(ch)
        else:
            out.append("_")
    s = "".join(out).strip("_")
    return s or fallback


def _unique_child_path(stage, parent_path, base_name: str):
    from pxr import Sdf

    parent = Sdf.Path(str(parent_path))
    base = _sanitize_ident(base_name, "Mat")
    cand = parent.AppendChild(base)
    i = 1
    while True:
        p = stage.GetPrimAtPath(cand)
        if not p or not p.IsValid():
            return cand
        cand = parent.AppendChild(f"{base}_{i}")
        i += 1


def _find_preview_surface_shader(mat_prim, create_if_missing: bool = False):
    from pxr import Usd, UsdShade, Sdf

    for shader_prim in Usd.PrimRange(mat_prim):
        shader = UsdShade.Shader(shader_prim)
        if not shader:
            continue
        sid = shader.GetIdAttr().Get() if shader.GetIdAttr() else None
        if sid == "UsdPreviewSurface":
            return shader

    if not create_if_missing:
        return None

    stage = mat_prim.GetStage()
    shader_path = mat_prim.GetPath().AppendChild("PreviewSurface")
    shader = UsdShade.Shader.Define(stage, shader_path)
    shader.CreateIdAttr("UsdPreviewSurface")
    shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set((0.18, 0.18, 0.18))
    shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.5)
    shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(0.0)
    shader.CreateInput("opacity", Sdf.ValueTypeNames.Float).Set(1.0)
    return shader


def _connect_material_surface(material, shader):
    out = material.CreateSurfaceOutput()
    # Signature differences across USD versions
    try:
        if out.ConnectToSource(shader.ConnectableAPI(), "surface"):
            return
    except Exception:
        pass
    try:
        if out.ConnectToSource(shader, "surface"):
            return
    except Exception:
        pass
    # Last-resort silent fallback; caller can still bind material.


def _infer_preview_input_type(param: str):
    from pxr import Sdf

    n = str(param or "").lower()
    if "color" in n or "emissive" in n or "normal" in n:
        return Sdf.ValueTypeNames.Color3f
    return Sdf.ValueTypeNames.Float


def _coerce_preview_value(inp, value):
    from pxr import Gf, Sdf

    t = str(inp.GetTypeName()).lower()

    if "asset" in t:
        return Sdf.AssetPath(str(value))

    if "color3" in t or "float3" in t or "vector3" in t or "normal3" in t:
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            return Gf.Vec3f(float(value[0]), float(value[1]), float(value[2]))

        if isinstance(value, str):
            txt = value.replace("(", " ").replace(")", " ").replace("[", " ").replace("]", " ")
            nums = []
            for tok in txt.replace(",", " ").split():
                try:
                    nums.append(float(tok))
                except Exception:
                    pass
            if len(nums) >= 3:
                return Gf.Vec3f(nums[0], nums[1], nums[2])

    if isinstance(value, (int, float)):
        return float(value)

    return value


def _material_texture_info(inp):
    try:
        src = inp.GetConnectedSource()
    except Exception:
        src = None
    if not src:
        return None

    try:
        src_api, source_name, _source_type = src
        src_prim = src_api.GetPrim()
    except Exception:
        return None

    from pxr import UsdShade

    shader = UsdShade.Shader(src_prim)
    sid = shader.GetIdAttr().Get() if shader and shader.GetIdAttr() else None

    asset_path = None
    if shader:
        for key in ("file", "filename", "tex", "inputs:file"):
            i = shader.GetInput(key)
            if not i:
                continue
            try:
                v = i.Get()
                if v is None:
                    continue
                asset_path = v.path if hasattr(v, "path") else str(v)
                if asset_path:
                    break
            except Exception:
                continue

    return {
        "shaderPath": str(src_prim.GetPath()),
        "shaderId": str(sid) if sid is not None else None,
        "output": str(source_name),
        "assetPath": asset_path,
    }


def _purpose_to_token_name(purpose: Optional[str]) -> str:
    p = str(purpose or "allPurpose").strip()
    if not p:
        return "allPurpose"
    valid = {"allPurpose", "preview", "full"}
    return p if p in valid else "allPurpose"


def _strength_to_token_name(strength: Optional[str]) -> str:
    s = str(strength or "weakerThanDescendants").strip()
    valid = {"weakerThanDescendants", "strongerThanDescendants"}
    return s if s in valid else "weakerThanDescendants"


def _usdshade_token(tokens, name: str):
    try:
        return getattr(tokens, name)
    except Exception:
        return name


def _bind_material_with_options(binding_api, material, purpose: Optional[str], strength: Optional[str]) -> tuple[str, str]:
    """Bind material with purpose/strength, with compatibility fallbacks."""
    from pxr import UsdShade

    p = _purpose_to_token_name(purpose)
    s = _strength_to_token_name(strength)

    p_tok = _usdshade_token(UsdShade.Tokens, p)
    s_tok = _usdshade_token(UsdShade.Tokens, s)

    # Most complete signature
    try:
        binding_api.Bind(material, s_tok, p_tok)
        return p, s
    except Exception:
        pass

    # Fallback signature(s)
    try:
        binding_api.Bind(material, s_tok)
        return p, s
    except Exception:
        pass

    try:
        binding_api.Bind(material)
        # If purpose != allPurpose, author direct rel explicitly.
        if p != "allPurpose":
            try:
                rel = binding_api.GetDirectBindingRel(p_tok)
                if rel:
                    rel.SetTargets([material.GetPath()])
                    rel.SetMetadata("bindMaterialAs", s_tok)
            except Exception:
                pass
        return p, s
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Bind failed: {exc}")


def _unbind_material_with_options(binding_api, purpose: Optional[str], all_purposes: bool = False) -> dict:
    from pxr import UsdShade

    if all_purposes:
        try:
            binding_api.UnbindAllBindings()
            return {"ok": True, "all_purposes": True}
        except Exception:
            # Fallback: clear known direct binding rels
            for pn in ("allPurpose", "preview", "full"):
                try:
                    tok = _usdshade_token(UsdShade.Tokens, pn)
                    rel = binding_api.GetDirectBindingRel(tok) if pn != "allPurpose" else binding_api.GetDirectBindingRel()
                    if rel:
                        rel.SetTargets([])
                except Exception:
                    pass
            return {"ok": True, "all_purposes": True}

    p = _purpose_to_token_name(purpose)
    p_tok = _usdshade_token(UsdShade.Tokens, p)

    try:
        if p == "allPurpose":
            binding_api.UnbindDirectBinding()
        else:
            binding_api.UnbindDirectBinding(p_tok)
        return {"ok": True, "purpose": p}
    except Exception:
        # Fallback: clear relation targets
        try:
            rel = binding_api.GetDirectBindingRel(p_tok) if p != "allPurpose" else binding_api.GetDirectBindingRel()
            if rel:
                rel.SetTargets([])
            return {"ok": True, "purpose": p}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Unbind failed: {exc}")


@app.get("/api/material/{prim_path:path}")
async def prim_material(prim_path: str, time: Optional[float] = None):
    """Get material binding + shader summary + editable UsdPreviewSurface params."""
    full_path = "/" + prim_path.lstrip("/")
    stage = _current_stage()
    if not stage:
        return {
            "bound": False,
            "supported": {
                "usd_preview_surface": True,
                "materialx": True,
                "vendor_shaders": True,
            },
        }

    from pxr import Sdf, UsdShade, Usd

    prim = stage.GetPrimAtPath(Sdf.Path(full_path))
    if not prim or not prim.IsValid():
        return {"bound": False}

    binding = UsdShade.MaterialBindingAPI(prim)

    bound_purpose = "allPurpose"
    mat, rel = None, None
    try:
        mat, rel = binding.ComputeBoundMaterial()
    except Exception:
        mat, rel = None, None

    if not mat:
        for pn in ("preview", "full"):
            try:
                tok = _usdshade_token(UsdShade.Tokens, pn)
                m, r = binding.ComputeBoundMaterial(tok)
                if m:
                    mat, rel = m, r
                    bound_purpose = pn
                    break
            except Exception:
                continue

    if not mat:
        return {
            "bound": False,
            "primPath": full_path,
            "supported": {
                "usd_preview_surface": True,
                "materialx": True,
                "vendor_shaders": True,
            },
            "binding": {
                "isDirect": False,
                "purpose": "allPurpose",
                "strength": None,
                "relation": None,
                "direct": {},
            },
        }

    mat_path = str(mat.GetPath())
    binding_info = {
        "isDirect": False,
        "purpose": bound_purpose,
        "strength": None,
        "relation": str(rel.GetPath()) if rel else None,
        "direct": {},
    }

    try:
        if rel:
            rel_name = rel.GetName()
            if rel_name.endswith(":preview"):
                binding_info["purpose"] = "preview"
            elif rel_name.endswith(":full"):
                binding_info["purpose"] = "full"

            st = rel.GetMetadata("bindMaterialAs")
            if st is not None:
                binding_info["strength"] = str(st)

            binding_info["isDirect"] = str(rel.GetPrim().GetPath()) == full_path
    except Exception:
        pass

    for pn in ("allPurpose", "preview", "full"):
        try:
            tok = _usdshade_token(UsdShade.Tokens, pn)
            drel = binding.GetDirectBindingRel(tok) if pn != "allPurpose" else binding.GetDirectBindingRel()
            if not drel:
                continue
            targets = drel.GetTargets()
            if not targets:
                continue
            entry = {"materialPath": str(targets[0])}
            bst = drel.GetMetadata("bindMaterialAs")
            if bst is not None:
                entry["strength"] = str(bst)
            binding_info["direct"][pn] = entry
        except Exception:
            continue

    result = {
        "bound": True,
        "primPath": full_path,
        "materialPath": mat_path,
        "query_time": time,
        "params": {},
        "textures": {},
        "shaderIds": [],
        "editablePreviewSurface": False,
        "binding": binding_info,
        "terminals": [],
        "readOnlyNodes": [],
    }

    # Terminals (including render-context outputs like surface@mtlx)
    for mout in _node_iter_material_outputs(mat):
        item = {
            "name": str(mout.get("uiName") or mout.get("terminal") or "surface"),
            "terminal": str(mout.get("terminal") or "surface"),
            "context": str(mout.get("context") or ""),
            "sourceNode": None,
            "sourceOutput": None,
        }
        out = mout.get("output")
        if out:
            try:
                src = out.GetConnectedSource()
            except Exception:
                src = None
            if src:
                try:
                    src_api, src_name, _st = src
                    src_prim = src_api.GetPrim()
                    item["sourceNode"] = str(src_prim.GetPath())
                    item["sourceOutput"] = str(src_name)
                except Exception:
                    pass
        result["terminals"].append(item)

    preview_shader = None
    shader_ids = []

    for shader_prim in Usd.PrimRange(mat.GetPrim()):
        shader = UsdShade.Shader(shader_prim)
        if not shader:
            continue
        sid = shader.GetIdAttr().Get() if shader.GetIdAttr() else None
        sid_str = str(sid) if sid is not None else None
        if sid_str is not None:
            shader_ids.append(sid_str)
        if sid == "UsdPreviewSurface" and preview_shader is None:
            preview_shader = shader

        # Read-only shader graph info (used for MaterialX/vendor accessibility)
        ro_node = {
            "path": str(shader_prim.GetPath()),
            "name": shader_prim.GetName() or str(shader_prim.GetPath()).rsplit("/", 1)[-1],
            "shaderId": sid_str,
            "inputs": [],
            "outputs": [],
        }
        try:
            for out in shader.GetOutputs():
                ro_node["outputs"].append({
                    "name": out.GetBaseName(),
                    "type": str(out.GetTypeName()),
                })
        except Exception:
            pass

        try:
            tc = Usd.TimeCode(time) if time is not None else None
            for inp in shader.GetInputs():
                ent = {
                    "name": inp.GetBaseName(),
                    "type": str(inp.GetTypeName()),
                    "value": None,
                    "sourceNode": None,
                    "sourceOutput": None,
                }
                try:
                    v = inp.Get(tc) if tc is not None else inp.Get()
                    if v is not None:
                        ent["value"] = str(v)
                except Exception:
                    pass
                try:
                    src = inp.GetConnectedSource()
                except Exception:
                    src = None
                if src:
                    try:
                        src_api, src_name, _st = src
                        src_prim = src_api.GetPrim()
                        ent["sourceNode"] = str(src_prim.GetPath())
                        ent["sourceOutput"] = str(src_name)
                    except Exception:
                        pass
                ro_node["inputs"].append(ent)
        except Exception:
            pass

        result["readOnlyNodes"].append(ro_node)

    result["shaderIds"] = sorted(set(shader_ids))

    lower_ids = [s.lower() for s in result["shaderIds"]]
    result["hasMaterialX"] = any(s.startswith("nd_") or "mtlx" in s for s in lower_ids)
    result["hasVendorShaders"] = any(
        s not in {"usdpreviewsurface", "usduvtexture", "usdprimvarreader_float2"}
        and not s.startswith("nd_")
        for s in lower_ids
    )

    if not preview_shader:
        return result

    result["editablePreviewSurface"] = True

    tc = Usd.TimeCode(time) if time is not None else None
    for inp in preview_shader.GetInputs():
        name = inp.GetBaseName()
        try:
            val = inp.Get(tc) if tc is not None else inp.Get()
            result["params"][name] = str(val) if val is not None else None
        except Exception:
            pass

        tex = _material_texture_info(inp)
        if tex:
            result["textures"][name] = tex

    return result


@app.get("/api/material_proxy/{mat_path:path}")
async def material_proxy(mat_path: str):
    """Extract a viewport-friendly texture proxy from MaterialX/UsdShade graph."""
    stage = _current_stage()
    if not stage:
        return {"ok": False, "error": "no stage"}

    from pxr import Sdf, UsdShade

    full = "/" + mat_path.lstrip("/")
    prim = stage.GetPrimAtPath(Sdf.Path(full))
    if not prim or not prim.IsValid():
        raise HTTPException(status_code=404, detail=f"Material not found: {full}")

    mat = UsdShade.Material(prim)
    if not mat:
        raise HTTPException(status_code=400, detail="Path is not a UsdShade material")

    proxy = _extract_materialx_proxy(stage, mat)
    proxy["materialPath"] = full
    return proxy


def _compute_bound_material(binding_api):
    """Resolve computed bound material with purpose fallback.

    Returns: (material, relation, purpose_name)
    """
    from pxr import UsdShade

    mat, rel = None, None
    purpose = "allPurpose"

    try:
        mat, rel = binding_api.ComputeBoundMaterial()
    except Exception:
        mat, rel = None, None

    if not mat:
        for pn in ("preview", "full"):
            try:
                tok = _usdshade_token(UsdShade.Tokens, pn)
                m, r = binding_api.ComputeBoundMaterial(tok)
                if m:
                    mat, rel = m, r
                    purpose = pn
                    break
            except Exception:
                continue

    return mat, rel, purpose


def _sanitize_asset_token(raw: Any) -> str:
    s = str(raw or "").strip()
    if s.startswith("@") and s.endswith("@"):
        s = s[1:-1]
    s = re.sub(r":SDF_FORMAT_ARGS:[^?#]+", "", s, flags=re.IGNORECASE)
    s = s.replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    if s.startswith("/"):
        s = s[1:]
    return s


def _asset_value_to_string(v: Any) -> str:
    try:
        if hasattr(v, "path"):
            return str(v.path)
    except Exception:
        pass
    return str(v)


def _resolve_texture_rel_path(stage, stage_dir: Optional[str], token: str) -> Optional[str]:
    import os

    t = _sanitize_asset_token(token)
    if not t:
        return None

    if not stage_dir:
        return t

    stage_dir_norm = os.path.normpath(stage_dir)

    # 1) direct under stage dir
    cand = os.path.normpath(os.path.join(stage_dir_norm, t))
    if cand.startswith(stage_dir_norm) and os.path.exists(cand):
        return os.path.relpath(cand, stage_dir_norm).replace("\\", "/")

    # 2) relative to used .mtlx layer directories
    try:
        for layer in stage.GetUsedLayers():
            lp = layer.realPath or ""
            if not lp or not lp.lower().endswith(".mtlx"):
                continue
            base = os.path.dirname(lp)
            c2 = os.path.normpath(os.path.join(base, t))
            if c2.startswith(stage_dir_norm) and os.path.exists(c2):
                return os.path.relpath(c2, stage_dir_norm).replace("\\", "/")
    except Exception:
        pass

    # 3) basename fallback search in stage dir (prefer /tex/)
    bn = os.path.basename(t).lower()
    if not bn:
        return None

    best = None
    best_key = None
    for dirpath, _dirs, files in os.walk(stage_dir_norm):
        for f in files:
            if f.lower() != bn:
                continue
            full = os.path.join(dirpath, f)
            if not os.path.normpath(full).startswith(stage_dir_norm):
                continue
            rel = os.path.relpath(full, stage_dir_norm).replace("\\", "/")
            key = (0 if "/tex/" in rel.lower() else 1, len(rel), rel)
            if best_key is None or key < best_key:
                best_key = key
                best = rel

    return best


def _resolve_texture_from_source_prim(source_prim, source_output_name: str, visited: set[tuple[str, str]]) -> Optional[str]:
    from pxr import UsdShade

    if not source_prim or not source_prim.IsValid():
        return None

    key = (str(source_prim.GetPath()), str(source_output_name or ""))
    if key in visited:
        return None
    visited.add(key)

    # Shader case
    shader = UsdShade.Shader(source_prim)
    if shader:
        sid = ""
        try:
            sidv = shader.GetIdAttr().Get() if shader.GetIdAttr() else None
            sid = str(sidv or "")
        except Exception:
            sid = ""

        sid_l = sid.lower()
        if sid_l.startswith("nd_image"):
            fin = shader.GetInput("file")
            if fin:
                try:
                    vv = fin.Get()
                    if vv is not None:
                        return _asset_value_to_string(vv)
                except Exception:
                    pass

        for nm in ("in", "input", "normal", "tex", "file"):
            inp = shader.GetInput(nm)
            if not inp:
                continue
            try:
                src = inp.GetConnectedSource()
            except Exception:
                src = None
            if not src:
                continue
            try:
                src_api, src_name, _st = src
                sp = src_api.GetPrim()
                r = _resolve_texture_from_source_prim(sp, str(src_name), visited)
                if r:
                    return r
            except Exception:
                continue

        # Generic fallback: walk any connected input
        try:
            for inp in shader.GetInputs():
                try:
                    src = inp.GetConnectedSource()
                except Exception:
                    src = None
                if not src:
                    continue
                try:
                    src_api, src_name, _st = src
                    sp = src_api.GetPrim()
                    r = _resolve_texture_from_source_prim(sp, str(src_name), visited)
                    if r:
                        return r
                except Exception:
                    continue
        except Exception:
            pass

    # NodeGraph case: trace output to source
    ng = UsdShade.NodeGraph(source_prim)
    if ng:
        out = None
        try:
            out = ng.GetOutput(str(source_output_name or ""))
        except Exception:
            out = None

        if not out:
            try:
                for o in ng.GetOutputs():
                    if o.GetBaseName() == str(source_output_name or ""):
                        out = o
                        break
            except Exception:
                out = None

        if out:
            try:
                src = out.GetConnectedSource()
            except Exception:
                src = None
            if src:
                try:
                    src_api, src_name, _st = src
                    sp = src_api.GetPrim()
                    return _resolve_texture_from_source_prim(sp, str(src_name), visited)
                except Exception:
                    pass

    return None


def _extract_materialx_proxy(stage, mat) -> dict[str, Any]:
    from pxr import UsdShade, Usd

    stage_dir = None
    try:
        rp = stage.GetRootLayer().realPath
        if rp:
            stage_dir = os.path.dirname(rp)
    except Exception:
        stage_dir = None

    main_shader = None
    for p in Usd.PrimRange(mat.GetPrim()):
        sh = UsdShade.Shader(p)
        if not sh:
            continue
        sid = None
        try:
            sid = sh.GetIdAttr().Get() if sh.GetIdAttr() else None
        except Exception:
            sid = None
        if sid and "nd_standard_surface" in str(sid).lower():
            main_shader = sh
            break

    if not main_shader:
        return {"ok": True, "textures": {}}

    def _resolve_from_input(inp_name: str) -> Optional[str]:
        inp = main_shader.GetInput(inp_name)
        if not inp:
            return None
        try:
            src = inp.GetConnectedSource()
        except Exception:
            src = None
        if src:
            try:
                src_api, src_name, _st = src
                sp = src_api.GetPrim()
                tok = _resolve_texture_from_source_prim(sp, str(src_name), set())
                if tok:
                    return _resolve_texture_rel_path(stage, stage_dir, tok)
            except Exception:
                pass
        try:
            vv = inp.Get()
            if vv is not None:
                return _resolve_texture_rel_path(stage, stage_dir, _asset_value_to_string(vv))
        except Exception:
            pass
        return None

    tex = {}
    for out_name, in_names in {
        "baseColor": ["base_color", "diffuseColor"],
        "normal": ["normal"],
        "roughness": ["specular_roughness", "roughness"],
        "metallic": ["metalness", "metallic"],
        "emissive": ["emission_color", "emissiveColor"],
    }.items():
        for nm in in_names:
            r = _resolve_from_input(nm)
            if r:
                tex[out_name] = r
                break

    return {
        "ok": True,
        "textures": tex,
    }


_NODE_SHADER_LIBRARY = [
    {
        "id": "UsdPreviewSurface",
        "label": "Preview Surface",
        "base_name": "PreviewSurface",
        "inputs": [
            {"name": "diffuseColor", "type": "Color3f", "default": [0.18, 0.18, 0.18]},
            {"name": "roughness", "type": "Float", "default": 0.5},
            {"name": "metallic", "type": "Float", "default": 0.0},
            {"name": "opacity", "type": "Float", "default": 1.0},
        ],
        "outputs": [
            {"name": "surface", "type": "Token"},
        ],
    },
    {
        "id": "UsdUVTexture",
        "label": "UV Texture",
        "base_name": "UVTexture",
        "inputs": [
            {"name": "file", "type": "Asset"},
            {"name": "st", "type": "Float2"},
        ],
        "outputs": [
            {"name": "rgb", "type": "Float3"},
            {"name": "r", "type": "Float"},
        ],
    },
    {
        "id": "UsdPrimvarReader_float2",
        "label": "Primvar Reader (float2)",
        "base_name": "PrimvarReader2",
        "inputs": [
            {"name": "varname", "type": "Token", "default": "st"},
        ],
        "outputs": [
            {"name": "result", "type": "Float2"},
        ],
    },
    {
        "id": "UsdPrimvarReader_float",
        "label": "Primvar Reader (float)",
        "base_name": "PrimvarReader1",
        "inputs": [
            {"name": "varname", "type": "Token"},
        ],
        "outputs": [
            {"name": "result", "type": "Float"},
        ],
    },
]


def _node_shader_def(shader_id: str) -> dict:
    sid = str(shader_id or "").strip()
    for d in _NODE_SHADER_LIBRARY:
        if d["id"] == sid:
            return d
    return {
        "id": sid or "UsdPreviewSurface",
        "label": sid or "Shader",
        "base_name": _sanitize_ident((sid or "Shader").split("_")[0], "Shader"),
        "inputs": [],
        "outputs": [],
    }


def _node_type_from_name(Sdf, type_name: str):
    name = str(type_name or "Token")
    return getattr(Sdf.ValueTypeNames, name, Sdf.ValueTypeNames.Token)


def _node_apply_shader_ports(shader, shader_id: str):
    from pxr import Sdf

    spec = _node_shader_def(shader_id)
    shader.CreateIdAttr(spec["id"])

    for inp_def in spec.get("inputs", []):
        name = str(inp_def.get("name") or "")
        if not name:
            continue
        t = _node_type_from_name(Sdf, inp_def.get("type", "Token"))
        inp = shader.GetInput(name)
        if not inp:
            inp = shader.CreateInput(name, t)
        if "default" in inp_def:
            try:
                cur = inp.Get()
                if cur is None:
                    inp.Set(_coerce_preview_value(inp, inp_def["default"]))
            except Exception:
                pass

    for out_def in spec.get("outputs", []):
        name = str(out_def.get("name") or "")
        if not name:
            continue
        t = _node_type_from_name(Sdf, out_def.get("type", "Token"))
        out = shader.GetOutput(name)
        if not out:
            shader.CreateOutput(name, t)


def _node_iter_material_outputs(mat):
    """Yield material terminal outputs with context-aware UI names.

    uiName examples: surface, displacement, volume, surface@mtlx
    """
    out_items = []
    seen = set()

    # Generic outputs include render-context terminals (e.g. outputs:mtlx:surface)
    try:
        all_outs = list(mat.GetOutputs())
    except Exception:
        all_outs = []

    for out in all_outs:
        try:
            attr = out.GetAttr()
            attr_name = str(attr.GetName()) if attr else ""
            parts = attr_name.split(":") if attr_name else []

            terminal = out.GetBaseName() if out else ""
            context = ""
            if len(parts) >= 3 and parts[0] == "outputs":
                context = ":".join(parts[1:-1]).strip()
                terminal = parts[-1].strip() or terminal
            elif len(parts) == 2 and parts[0] == "outputs":
                terminal = parts[1].strip() or terminal

            if not terminal:
                continue

            ui_name = terminal if not context else f"{terminal}@{context}"
            out_key = ""
            try:
                attr2 = out.GetAttr()
                out_key = str(attr2.GetPath()) if attr2 else ""
            except Exception:
                out_key = ""
            key = (ui_name, out_key or terminal)
            if key in seen:
                continue
            seen.add(key)

            out_items.append({
                "uiName": ui_name,
                "terminal": terminal,
                "context": context,
                "output": out,
            })
        except Exception:
            continue

    # Ensure classic terminals exist in list (for PreviewSurface workflows)
    for terminal, fn_name in (("surface", "GetSurfaceOutput"), ("displacement", "GetDisplacementOutput"), ("volume", "GetVolumeOutput")):
        try:
            fn = getattr(mat, fn_name, None)
            out = fn() if callable(fn) else None
        except Exception:
            out = None
        if not out:
            continue
        ui_name = terminal
        out_key = ""
        try:
            attr2 = out.GetAttr()
            out_key = str(attr2.GetPath()) if attr2 else ""
        except Exception:
            out_key = ""
        key = (ui_name, out_key or terminal)
        if key in seen:
            continue
        seen.add(key)
        out_items.append({
            "uiName": ui_name,
            "terminal": terminal,
            "context": "",
            "output": out,
        })

    return out_items


def _node_material_output(mat, output_name: str, create: bool = False):
    from pxr import UsdShade

    raw = str(output_name or "surface").strip()
    out_name = raw.lower()

    # Match existing outputs first (including render-context terminals)
    for item in _node_iter_material_outputs(mat):
        if item["uiName"].lower() == out_name or item["terminal"].lower() == out_name:
            return item["output"], item["uiName"]

    # Parse requested context form: terminal@context
    terminal = out_name
    context = ""
    if "@" in out_name:
        terminal, context = out_name.split("@", 1)
        terminal = terminal.strip() or "surface"
        context = context.strip()

    # Create classic terminal outputs when requested
    fn = {
        "surface": ("CreateSurfaceOutput", "GetSurfaceOutput"),
        "displacement": ("CreateDisplacementOutput", "GetDisplacementOutput"),
        "volume": ("CreateVolumeOutput", "GetVolumeOutput"),
    }.get(terminal, ("CreateSurfaceOutput", "GetSurfaceOutput"))

    if create:
        creator = getattr(mat, fn[0], None)
        if callable(creator):
            try:
                # Render-context overload when supported (e.g. "mtlx")
                if context:
                    try:
                        tok = _usdshade_token(UsdShade.Tokens, context)
                        out = creator(tok)
                    except Exception:
                        out = creator(context)
                    if out:
                        return out, f"{terminal}@{context}"
                else:
                    out = creator()
                    if out:
                        return out, terminal
            except Exception:
                pass

    getter = getattr(mat, fn[1], None)
    if callable(getter):
        try:
            out = getter()
            if out:
                return out, terminal
        except Exception:
            pass

    return None, (f"{terminal}@{context}" if context else terminal)


def _node_connect_input(inp, source_shader, source_output_name: str):
    out_name = str(source_output_name or "").strip()
    if not out_name:
        raise HTTPException(status_code=400, detail="Missing source output name")
    try:
        inp.ConnectToSource(source_shader.ConnectableAPI(), out_name)
        return
    except Exception:
        pass
    try:
        inp.ConnectToSource(source_shader, out_name)
        return
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Connect failed: {exc}")


class _NodeCreateReq(BaseModel):
    prim_path: str
    shader_id: str
    node_name: Optional[str] = None


class _NodeDeleteReq(BaseModel):
    node_path: str


class _NodeConnectReq(BaseModel):
    source_node_path: str
    source_output: str
    target_node_path: Optional[str] = None
    target_input: Optional[str] = None
    material_path: Optional[str] = None
    material_output: Optional[str] = None


class _NodeDisconnectReq(BaseModel):
    target_node_path: Optional[str] = None
    target_input: Optional[str] = None
    material_path: Optional[str] = None
    material_output: Optional[str] = None


class _NodeSetInputReq(BaseModel):
    node_path: str
    input_name: str
    value: Any


@app.get("/api/node_graph/library")
async def node_graph_library():
    return {"ok": True, "items": _NODE_SHADER_LIBRARY}


@app.get("/api/node_graph/{prim_path:path}")
async def node_graph(prim_path: str):
    """Minimal node graph view for bound UsdShade material network."""
    full_path = "/" + prim_path.lstrip("/")
    stage = _current_stage()
    if not stage:
        return {
            "ok": True,
            "primPath": full_path,
            "bound": False,
            "nodes": [],
            "edges": [],
            "library": _NODE_SHADER_LIBRARY,
            "editable": False,
        }

    from pxr import Sdf, Usd, UsdShade

    prim = stage.GetPrimAtPath(Sdf.Path(full_path))
    if not prim or not prim.IsValid():
        raise HTTPException(status_code=404, detail=f"Prim not found: {full_path}")

    binding = UsdShade.MaterialBindingAPI(prim)
    mat, rel, purpose = _compute_bound_material(binding)

    if not mat:
        return {
            "ok": True,
            "primPath": full_path,
            "bound": False,
            "nodes": [],
            "edges": [],
            "material": None,
            "library": _NODE_SHADER_LIBRARY,
            "editable": False,
        }

    mat_prim = mat.GetPrim()
    mat_path = str(mat_prim.GetPath())

    nodes: list[dict[str, Any]] = []
    nodes_by_id: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    edge_keys: set[tuple[str, str, str, str]] = set()

    def ensure_node_from_prim(p):
        node_id = str(p.GetPath())
        if node_id in nodes_by_id:
            return nodes_by_id[node_id]

        node = {
            "id": node_id,
            "path": node_id,
            "name": p.GetName() or node_id.rsplit("/", 1)[-1],
            "type": str(p.GetTypeName() or "Prim"),
            "shaderId": None,
            "inputs": [],
            "outputs": [],
        }
        nodes.append(node)
        nodes_by_id[node_id] = node
        return node

    for shader_prim in Usd.PrimRange(mat_prim):
        shader = UsdShade.Shader(shader_prim)
        if not shader:
            continue

        node = ensure_node_from_prim(shader_prim)
        node["type"] = "Shader"

        try:
            sid = shader.GetIdAttr().Get() if shader.GetIdAttr() else None
            if sid is not None:
                node["shaderId"] = str(sid)
        except Exception:
            pass

        try:
            node["outputs"] = [
                {"name": out.GetBaseName(), "type": str(out.GetTypeName())}
                for out in shader.GetOutputs()
            ]
        except Exception:
            node["outputs"] = []

        node_inputs = []
        for inp in shader.GetInputs():
            entry = {
                "name": inp.GetBaseName(),
                "type": str(inp.GetTypeName()),
                "value": None,
            }

            try:
                v = inp.Get()
                if v is not None:
                    entry["value"] = str(v)
            except Exception:
                pass

            try:
                src = inp.GetConnectedSource()
            except Exception:
                src = None

            if src:
                try:
                    src_api, src_name, _src_type = src
                    src_prim = src_api.GetPrim()
                    src_id = str(src_prim.GetPath())
                    ensure_node_from_prim(src_prim)

                    src_out = str(src_name)
                    dst_in = inp.GetBaseName()
                    entry["sourceNode"] = src_id
                    entry["sourceOutput"] = src_out

                    ekey = (src_id, src_out, node["id"], dst_in)
                    if ekey not in edge_keys:
                        edge_keys.add(ekey)
                        edges.append({
                            "source": src_id,
                            "sourceOutput": src_out,
                            "target": node["id"],
                            "targetInput": dst_in,
                        })
                except Exception:
                    pass

            node_inputs.append(entry)

        node["inputs"] = node_inputs

    material_node = nodes_by_id.get(mat_path)
    if not material_node:
        material_node = {
            "id": mat_path,
            "path": mat_path,
            "name": mat_prim.GetName() or mat_path.rsplit("/", 1)[-1],
            "type": "Material",
            "shaderId": None,
            "inputs": [],
            "outputs": [],
        }
        nodes.append(material_node)
        nodes_by_id[mat_path] = material_node
    else:
        material_node["type"] = "Material"
        material_node.setdefault("outputs", [])

    for mout in _node_iter_material_outputs(mat):
        label = str(mout.get("uiName") or mout.get("terminal") or "surface")
        out = mout.get("output")
        if not out:
            continue

        material_node["outputs"].append({
            "name": label,
            "type": "output",
            "terminal": mout.get("terminal") or label,
            "context": mout.get("context") or "",
        })

        try:
            src = out.GetConnectedSource()
        except Exception:
            src = None

        if not src:
            continue

        try:
            src_api, src_name, _src_type = src
            src_prim = src_api.GetPrim()
            src_id = str(src_prim.GetPath())
            ensure_node_from_prim(src_prim)
            ekey = (src_id, str(src_name), mat_path, label)
            if ekey not in edge_keys:
                edge_keys.add(ekey)
                edges.append({
                    "source": src_id,
                    "sourceOutput": str(src_name),
                    "target": mat_path,
                    "targetInput": label,
                })
        except Exception:
            continue

    # Stable output order
    nodes.sort(key=lambda n: (0 if n.get("type") == "Material" else 1, n.get("path", "")))
    edges.sort(key=lambda e: (e.get("source", ""), e.get("target", ""), e.get("sourceOutput", ""), e.get("targetInput", "")))

    return {
        "ok": True,
        "primPath": full_path,
        "bound": True,
        "editable": True,
        "material": {
            "path": mat_path,
            "bindingRelation": str(rel.GetPath()) if rel else None,
            "purpose": purpose,
            "isDirect": bool(rel and str(rel.GetPrim().GetPath()) == full_path),
        },
        "nodes": nodes,
        "edges": edges,
        "library": _NODE_SHADER_LIBRARY,
    }


@app.post("/api/node_graph/create_node")
async def node_graph_create_node(req: _NodeCreateReq):
    stage = _current_stage()
    if not stage:
        return {"ok": False, "error": "no stage"}

    from pxr import Sdf, UsdShade

    full_path = "/" + req.prim_path.lstrip("/")
    prim = stage.GetPrimAtPath(Sdf.Path(full_path))
    if not prim or not prim.IsValid():
        raise HTTPException(status_code=404, detail=f"Prim not found: {full_path}")

    binding = UsdShade.MaterialBindingAPI(prim)
    mat, _rel, _purpose = _compute_bound_material(binding)
    if not mat:
        raise HTTPException(status_code=400, detail="No bound material on selected prim")

    spec = _node_shader_def(req.shader_id)
    _pxr_push_undo()

    mat_path = mat.GetPrim().GetPath()
    base = req.node_name or spec.get("base_name") or "Shader"
    node_path = _unique_child_path(stage, mat_path, base)

    shader = UsdShade.Shader.Define(stage, node_path)
    _node_apply_shader_ports(shader, spec["id"])

    await _conns.broadcast({
        "event": "node_graph_changed",
        "primPath": full_path,
        "materialPath": str(mat.GetPath()),
        "action": "create_node",
        "nodePath": str(node_path),
    })
    await _conns.broadcast({
        "event": "material_changed",
        "primPath": full_path,
        "materialPath": str(mat.GetPath()),
        "node_graph": True,
    })

    return {
        "ok": True,
        "primPath": full_path,
        "materialPath": str(mat.GetPath()),
        "nodePath": str(node_path),
        "shaderId": spec["id"],
    }


@app.post("/api/node_graph/delete_node")
async def node_graph_delete_node(req: _NodeDeleteReq):
    stage = _current_stage()
    if not stage:
        return {"ok": False, "error": "no stage"}

    from pxr import Sdf, UsdShade

    node_path = "/" + req.node_path.lstrip("/")
    prim = stage.GetPrimAtPath(Sdf.Path(node_path))
    if not prim or not prim.IsValid():
        raise HTTPException(status_code=404, detail=f"Node not found: {node_path}")

    shader = UsdShade.Shader(prim)
    if not shader:
        raise HTTPException(status_code=400, detail="Only shader nodes can be deleted")

    parent = prim.GetParent()
    mat_path = None
    if parent and parent.IsValid() and UsdShade.Material(parent):
        mat_path = str(parent.GetPath())

    _pxr_push_undo()
    ok = stage.RemovePrim(Sdf.Path(node_path))
    if not ok:
        raise HTTPException(status_code=400, detail=f"Failed to delete node: {node_path}")

    await _conns.broadcast({
        "event": "node_graph_changed",
        "materialPath": mat_path,
        "action": "delete_node",
        "nodePath": node_path,
    })
    await _conns.broadcast({
        "event": "material_changed",
        "materialPath": mat_path,
        "node_graph": True,
    })

    return {"ok": True, "nodePath": node_path, "materialPath": mat_path}


@app.post("/api/node_graph/connect")
async def node_graph_connect(req: _NodeConnectReq):
    stage = _current_stage()
    if not stage:
        return {"ok": False, "error": "no stage"}

    from pxr import Sdf, UsdShade

    src_path = "/" + req.source_node_path.lstrip("/")
    src_prim = stage.GetPrimAtPath(Sdf.Path(src_path))
    if not src_prim or not src_prim.IsValid():
        raise HTTPException(status_code=404, detail=f"Source node not found: {src_path}")

    src_shader = UsdShade.Shader(src_prim)
    if not src_shader:
        raise HTTPException(status_code=400, detail="Source node must be a shader")

    src_out_name = str(req.source_output or "").strip()
    if not src_out_name:
        raise HTTPException(status_code=400, detail="Missing source output")

    src_out = src_shader.GetOutput(src_out_name)
    if not src_out:
        src_out = src_shader.CreateOutput(src_out_name, _node_type_from_name(Sdf, "Token"))

    to_shader = bool(req.target_node_path)
    to_material = bool(req.material_path)
    if to_shader == to_material:
        raise HTTPException(status_code=400, detail="Specify either target_node_path or material_path")

    _pxr_push_undo()

    material_path = None
    target_path = None
    target_port = None

    if to_shader:
        target_path = "/" + str(req.target_node_path).lstrip("/")
        tgt_prim = stage.GetPrimAtPath(Sdf.Path(target_path))
        if not tgt_prim or not tgt_prim.IsValid():
            raise HTTPException(status_code=404, detail=f"Target node not found: {target_path}")

        tgt_shader = UsdShade.Shader(tgt_prim)
        if not tgt_shader:
            raise HTTPException(status_code=400, detail="Target node must be a shader")

        target_port = str(req.target_input or "").strip()
        if not target_port:
            raise HTTPException(status_code=400, detail="Missing target_input")

        inp = tgt_shader.GetInput(target_port)
        if not inp:
            out_type = src_out.GetTypeName() if src_out else _node_type_from_name(Sdf, "Token")
            inp = tgt_shader.CreateInput(target_port, out_type)

        _node_connect_input(inp, src_shader, src_out_name)

        parent = tgt_prim.GetParent()
        if parent and parent.IsValid() and UsdShade.Material(parent):
            material_path = str(parent.GetPath())

    else:
        material_path = "/" + str(req.material_path).lstrip("/")
        mat_prim = stage.GetPrimAtPath(Sdf.Path(material_path))
        if not mat_prim or not mat_prim.IsValid():
            raise HTTPException(status_code=404, detail=f"Material not found: {material_path}")

        mat = UsdShade.Material(mat_prim)
        if not mat:
            raise HTTPException(status_code=400, detail="Target material path is not a UsdShade material")

        out, out_name = _node_material_output(mat, req.material_output or "surface", create=True)
        if not out:
            raise HTTPException(status_code=400, detail="Cannot resolve material output")

        target_port = out_name
        _node_connect_input(out, src_shader, src_out_name)

    await _conns.broadcast({
        "event": "node_graph_changed",
        "action": "connect",
        "sourceNodePath": src_path,
        "sourceOutput": src_out_name,
        "targetPath": target_path or material_path,
        "targetPort": target_port,
        "materialPath": material_path,
    })
    await _conns.broadcast({
        "event": "material_changed",
        "materialPath": material_path,
        "node_graph": True,
    })

    return {
        "ok": True,
        "sourceNodePath": src_path,
        "sourceOutput": src_out_name,
        "targetPath": target_path or material_path,
        "targetPort": target_port,
        "materialPath": material_path,
    }


@app.post("/api/node_graph/disconnect")
async def node_graph_disconnect(req: _NodeDisconnectReq):
    stage = _current_stage()
    if not stage:
        return {"ok": False, "error": "no stage"}

    from pxr import Sdf, UsdShade

    from_shader = bool(req.target_node_path)
    from_material = bool(req.material_path)
    if from_shader == from_material:
        raise HTTPException(status_code=400, detail="Specify either target_node_path or material_path")

    _pxr_push_undo()

    material_path = None
    target_path = None
    target_port = None

    if from_shader:
        target_path = "/" + str(req.target_node_path).lstrip("/")
        tgt_prim = stage.GetPrimAtPath(Sdf.Path(target_path))
        if not tgt_prim or not tgt_prim.IsValid():
            raise HTTPException(status_code=404, detail=f"Target node not found: {target_path}")

        tgt_shader = UsdShade.Shader(tgt_prim)
        if not tgt_shader:
            raise HTTPException(status_code=400, detail="Target node must be a shader")

        target_port = str(req.target_input or "").strip()
        if not target_port:
            raise HTTPException(status_code=400, detail="Missing target_input")

        inp = tgt_shader.GetInput(target_port)
        if not inp:
            return {"ok": True, "targetPath": target_path, "targetPort": target_port, "materialPath": None}

        try:
            inp.DisconnectSource()
        except Exception:
            pass

        parent = tgt_prim.GetParent()
        if parent and parent.IsValid() and UsdShade.Material(parent):
            material_path = str(parent.GetPath())

    else:
        material_path = "/" + str(req.material_path).lstrip("/")
        mat_prim = stage.GetPrimAtPath(Sdf.Path(material_path))
        if not mat_prim or not mat_prim.IsValid():
            raise HTTPException(status_code=404, detail=f"Material not found: {material_path}")

        mat = UsdShade.Material(mat_prim)
        if not mat:
            raise HTTPException(status_code=400, detail="Target material path is not a UsdShade material")

        out, out_name = _node_material_output(mat, req.material_output or "surface", create=False)
        target_port = out_name
        if out:
            try:
                out.DisconnectSource()
            except Exception:
                pass

    await _conns.broadcast({
        "event": "node_graph_changed",
        "action": "disconnect",
        "targetPath": target_path or material_path,
        "targetPort": target_port,
        "materialPath": material_path,
    })
    await _conns.broadcast({
        "event": "material_changed",
        "materialPath": material_path,
        "node_graph": True,
    })

    return {
        "ok": True,
        "targetPath": target_path or material_path,
        "targetPort": target_port,
        "materialPath": material_path,
    }


@app.post("/api/node_graph/set_input")
async def node_graph_set_input(req: _NodeSetInputReq):
    stage = _current_stage()
    if not stage:
        return {"ok": False, "error": "no stage"}

    from pxr import Sdf, UsdShade

    node_path = "/" + req.node_path.lstrip("/")
    prim = stage.GetPrimAtPath(Sdf.Path(node_path))
    if not prim or not prim.IsValid():
        raise HTTPException(status_code=404, detail=f"Node not found: {node_path}")

    shader = UsdShade.Shader(prim)
    if not shader:
        raise HTTPException(status_code=400, detail="Only shader nodes are editable")

    input_name = str(req.input_name or "").strip()
    if not input_name:
        raise HTTPException(status_code=400, detail="Missing input_name")

    _pxr_push_undo()

    inp = shader.GetInput(input_name)
    if not inp:
        inp = shader.CreateInput(input_name, _node_type_from_name(Sdf, "Token"))

    try:
        inp.DisconnectSource()
    except Exception:
        pass

    try:
        inp.Set(_coerce_preview_value(inp, req.value))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Set input failed: {exc}")

    material_path = None
    parent = prim.GetParent()
    if parent and parent.IsValid() and UsdShade.Material(parent):
        material_path = str(parent.GetPath())

    await _conns.broadcast({
        "event": "node_graph_changed",
        "action": "set_input",
        "nodePath": node_path,
        "input": input_name,
        "materialPath": material_path,
    })
    await _conns.broadcast({
        "event": "material_changed",
        "materialPath": material_path,
        "node_graph": True,
    })

    return {
        "ok": True,
        "nodePath": node_path,
        "input": input_name,
        "materialPath": material_path,
    }


class _MatBindReq(BaseModel):
    prim_path: str
    material_path: Optional[str] = None
    create_if_missing: bool = True
    material_name: Optional[str] = None
    looks_scope: str = "/World/Looks"
    purpose: str = "allPurpose"
    binding_strength: str = "weakerThanDescendants"


@app.post("/api/material/bind")
async def material_bind(req: _MatBindReq):
    """Bind an existing material to a prim, or create+bind a default UsdPreviewSurface material."""
    stage = _current_stage()
    if not stage:
        return {"ok": False, "error": "no stage"}

    from pxr import Sdf, UsdShade

    prim_path = "/" + req.prim_path.lstrip("/")
    prim = stage.GetPrimAtPath(Sdf.Path(prim_path))
    if not prim or not prim.IsValid():
        raise HTTPException(status_code=404, detail=f"Prim not found: {prim_path}")

    _pxr_push_undo()

    created = False
    mat = None

    if req.material_path:
        mat_path = "/" + req.material_path.lstrip("/")
        mat_prim = stage.GetPrimAtPath(Sdf.Path(mat_path))
        if not mat_prim or not mat_prim.IsValid():
            raise HTTPException(status_code=404, detail=f"Material not found: {mat_path}")
        mat = UsdShade.Material(mat_prim)
    elif req.create_if_missing:
        looks_path = Sdf.Path("/" + req.looks_scope.lstrip("/"))
        if str(looks_path) == "/":
            looks_path = Sdf.Path("/World/Looks")
        stage.DefinePrim(looks_path, "Scope")

        base_name = req.material_name or f"{prim.GetName()}_Mat"
        new_path = _unique_child_path(stage, looks_path, base_name)
        mat = UsdShade.Material.Define(stage, new_path)
        shader = _find_preview_surface_shader(mat.GetPrim(), create_if_missing=True)
        if shader:
            _connect_material_surface(mat, shader)
        created = True
    else:
        raise HTTPException(status_code=400, detail="No material_path provided and create_if_missing=false")

    if not mat:
        raise HTTPException(status_code=400, detail="Failed to resolve or create material")

    try:
        binding_api = UsdShade.MaterialBindingAPI.Apply(prim)
    except Exception:
        binding_api = UsdShade.MaterialBindingAPI(prim)

    purpose, strength = _bind_material_with_options(
        binding_api,
        mat,
        req.purpose,
        req.binding_strength,
    )

    await _conns.broadcast({
        "event": "material_changed",
        "primPath": prim_path,
        "materialPath": str(mat.GetPath()),
        "bind": True,
        "created": created,
        "purpose": purpose,
        "binding_strength": strength,
    })
    return {
        "ok": True,
        "created": created,
        "primPath": prim_path,
        "materialPath": str(mat.GetPath()),
        "purpose": purpose,
        "binding_strength": strength,
    }


class _MatUnbindReq(BaseModel):
    prim_path: str
    purpose: str = "allPurpose"
    all_purposes: bool = False


@app.post("/api/material/unbind")
async def material_unbind(req: _MatUnbindReq):
    """Remove direct material binding opinions on a prim."""
    stage = _current_stage()
    if not stage:
        return {"ok": False, "error": "no stage"}

    from pxr import Sdf, UsdShade

    prim_path = "/" + req.prim_path.lstrip("/")
    prim = stage.GetPrimAtPath(Sdf.Path(prim_path))
    if not prim or not prim.IsValid():
        raise HTTPException(status_code=404, detail=f"Prim not found: {prim_path}")

    _pxr_push_undo()

    try:
        binding_api = UsdShade.MaterialBindingAPI.Apply(prim)
    except Exception:
        binding_api = UsdShade.MaterialBindingAPI(prim)

    out = _unbind_material_with_options(binding_api, req.purpose, req.all_purposes)

    await _conns.broadcast({
        "event": "material_changed",
        "primPath": prim_path,
        "unbind": True,
        "purpose": out.get("purpose", req.purpose),
        "all_purposes": bool(req.all_purposes),
    })

    return {
        "ok": True,
        "primPath": prim_path,
        "purpose": out.get("purpose", req.purpose),
        "all_purposes": bool(req.all_purposes),
    }


class _MatParamReq(BaseModel):
    param: str
    value: Any


@app.post("/api/material/{mat_path:path}/set")
async def material_set_param(mat_path: str, req: _MatParamReq):
    """Set (or create then set) a UsdPreviewSurface parameter on a material."""
    full_path = "/" + mat_path.lstrip("/")
    stage = _current_stage()
    if not stage:
        return {"ok": False, "error": "no stage"}

    from pxr import Sdf, UsdShade

    _pxr_push_undo()

    mat_prim = stage.GetPrimAtPath(Sdf.Path(full_path))
    if not mat_prim or not mat_prim.IsValid():
        raise HTTPException(status_code=404, detail=f"Material not found: {full_path}")

    shader = _find_preview_surface_shader(mat_prim, create_if_missing=True)
    if not shader:
        raise HTTPException(status_code=404, detail="No UsdPreviewSurface shader found")

    inp = shader.GetInput(req.param)
    if not inp:
        inp = shader.CreateInput(req.param, _infer_preview_input_type(req.param))

    try:
        inp.Set(_coerce_preview_value(inp, req.value))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await _conns.broadcast({
        "event": "material_changed",
        "materialPath": full_path,
        "param": req.param,
    })
    return {"ok": True}


class _MatTextureReq(BaseModel):
    param: str
    asset_path: str = ""
    st_primvar: str = "st"


@app.post("/api/material/{mat_path:path}/texture")
async def material_set_texture(mat_path: str, req: _MatTextureReq):
    """Connect/clear a texture input on a UsdPreviewSurface parameter."""
    full_path = "/" + mat_path.lstrip("/")
    stage = _current_stage()
    if not stage:
        return {"ok": False, "error": "no stage"}

    from pxr import Sdf, UsdShade

    _pxr_push_undo()

    mat_prim = stage.GetPrimAtPath(Sdf.Path(full_path))
    if not mat_prim or not mat_prim.IsValid():
        raise HTTPException(status_code=404, detail=f"Material not found: {full_path}")

    shader = _find_preview_surface_shader(mat_prim, create_if_missing=True)
    if not shader:
        raise HTTPException(status_code=404, detail="No UsdPreviewSurface shader found")

    inp = shader.GetInput(req.param)
    if not inp:
        inp = shader.CreateInput(req.param, _infer_preview_input_type(req.param))

    asset_path = str(req.asset_path or "").strip()
    if not asset_path:
        try:
            inp.DisconnectSource()
        except Exception:
            pass
        await _conns.broadcast({
            "event": "material_changed",
            "materialPath": full_path,
            "param": req.param,
            "texture": True,
            "cleared": True,
        })
        return {"ok": True, "cleared": True}

    base = _sanitize_ident(req.param, "tex")
    tex_shader = UsdShade.Shader.Define(stage, mat_prim.GetPath().AppendChild(f"{base}_tex"))
    tex_shader.CreateIdAttr("UsdUVTexture")
    tex_shader.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath(asset_path))
    tex_shader.CreateOutput("rgb", Sdf.ValueTypeNames.Float3)
    tex_shader.CreateOutput("r", Sdf.ValueTypeNames.Float)

    st_reader = UsdShade.Shader.Define(stage, mat_prim.GetPath().AppendChild("stReader"))
    st_reader.CreateIdAttr("UsdPrimvarReader_float2")
    st_reader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set(req.st_primvar or "st")
    st_reader.CreateOutput("result", Sdf.ValueTypeNames.Float2)

    try:
        tex_shader.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(st_reader.ConnectableAPI(), "result")
    except Exception:
        try:
            tex_shader.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(st_reader, "result")
        except Exception:
            pass

    type_name = str(inp.GetTypeName()).lower()
    out_name = "rgb" if ("color3" in type_name or "float3" in type_name or "vector3" in type_name or "normal3" in type_name) else "r"

    try:
        inp.ConnectToSource(tex_shader.ConnectableAPI(), out_name)
    except Exception:
        try:
            inp.ConnectToSource(tex_shader, out_name)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Texture connect failed: {exc}")

    await _conns.broadcast({
        "event": "material_changed",
        "materialPath": full_path,
        "param": req.param,
        "texture": True,
        "asset_path": asset_path,
    })
    return {"ok": True, "asset_path": asset_path, "output": out_name}


# ── Attribute editing ─────────────────────────────────────────────────────────

class _AttrSetReq(BaseModel):
    attribute: str
    value:     Any
    time:      Optional[float] = None


@app.patch("/api/prim/{prim_path:path}")
async def prim_set_attr(prim_path: str, req: _AttrSetReq):
    full_path = "/" + prim_path.lstrip("/")

    stage = _current_stage()
    if stage:
        _pxr_push_undo()
        from pxr import Sdf, Usd
        prim = stage.GetPrimAtPath(Sdf.Path(full_path))
        if not prim or not prim.IsValid():
            raise HTTPException(status_code=404, detail=f"Prim not found: {full_path}")

        attr = prim.GetAttribute(req.attribute)
        if not attr or not attr.IsValid():
            raise HTTPException(status_code=404, detail=f"Attribute not found: {req.attribute}")

        try:
            typed_val = _coerce_value(attr, req.value)
            if req.time is not None:
                attr.Set(typed_val, Usd.TimeCode(req.time))
            else:
                attr.Set(typed_val)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Set failed: {exc}")

        await _conns.broadcast({
            "event": "scene_changed", "primPath": full_path, "attribute": req.attribute,
        })
        return {"ok": True}

    return {"ok": True, "stub": True}


# ── Keyframe editing (Curve Editor) ──────────────────────────────────────────

class _KeyframeSetReq(BaseModel):
    attribute: str
    time: float
    value: Any


class _KeyframeDeleteReq(BaseModel):
    attribute: str
    time: float


class _KeyframeMoveReq(BaseModel):
    attribute: str
    from_time: float
    to_time: float


class _KeyframeInterpolationReq(BaseModel):
    attribute: str
    mode: str


@app.post("/api/prim/{prim_path:path}/keyframe/interpolation")
async def prim_set_keyframe_interpolation(prim_path: str, req: _KeyframeInterpolationReq):
    full_path = "/" + prim_path.lstrip("/")

    mode = str(req.mode or "").strip().lower()
    if mode not in {"linear", "step", "bezier"}:
        raise HTTPException(status_code=400, detail=f"Unsupported interpolation mode: {req.mode}")

    stage = _current_stage()
    if stage:
        _pxr_push_undo()
        from pxr import Sdf

        prim = stage.GetPrimAtPath(Sdf.Path(full_path))
        if not prim or not prim.IsValid():
            raise HTTPException(status_code=404, detail=f"Prim not found: {full_path}")

        attr = prim.GetAttribute(req.attribute)
        if not attr or not attr.IsValid():
            raise HTTPException(status_code=404, detail=f"Attribute not found: {req.attribute}")

        try:
            attr.SetCustomDataByKey("opendcc:interp", mode)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Set interpolation failed: {exc}")

        await _conns.broadcast({
            "event": "scene_changed", "primPath": full_path, "attribute": req.attribute,
        })
        return {"ok": True, "path": full_path, "attribute": req.attribute, "mode": mode}

    return {"ok": True, "stub": True, "mode": mode}


@app.post("/api/prim/{prim_path:path}/keyframe/set")
async def prim_set_keyframe(prim_path: str, req: _KeyframeSetReq):
    full_path = "/" + prim_path.lstrip("/")

    stage = _current_stage()
    if stage:
        _pxr_push_undo()
        from pxr import Sdf, Usd

        prim = stage.GetPrimAtPath(Sdf.Path(full_path))
        if not prim or not prim.IsValid():
            raise HTTPException(status_code=404, detail=f"Prim not found: {full_path}")

        attr = prim.GetAttribute(req.attribute)
        if not attr or not attr.IsValid():
            raise HTTPException(status_code=404, detail=f"Attribute not found: {req.attribute}")

        try:
            typed_val = _coerce_value(attr, req.value)
            tc = Usd.TimeCode(float(req.time))
            attr.Set(typed_val, tc)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Set keyframe failed: {exc}")

        await _conns.broadcast({
            "event": "scene_changed", "primPath": full_path, "attribute": req.attribute,
        })
        return {"ok": True, "path": full_path, "attribute": req.attribute, "time": req.time}

    return {"ok": True, "stub": True}


@app.post("/api/prim/{prim_path:path}/keyframe/delete")
async def prim_delete_keyframe(prim_path: str, req: _KeyframeDeleteReq):
    full_path = "/" + prim_path.lstrip("/")

    stage = _current_stage()
    if stage:
        _pxr_push_undo()
        from pxr import Sdf, Usd

        prim = stage.GetPrimAtPath(Sdf.Path(full_path))
        if not prim or not prim.IsValid():
            raise HTTPException(status_code=404, detail=f"Prim not found: {full_path}")

        attr = prim.GetAttribute(req.attribute)
        if not attr or not attr.IsValid():
            raise HTTPException(status_code=404, detail=f"Attribute not found: {req.attribute}")

        try:
            tc = Usd.TimeCode(float(req.time))
            attr.ClearAtTime(tc)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Delete keyframe failed: {exc}")

        await _conns.broadcast({
            "event": "scene_changed", "primPath": full_path, "attribute": req.attribute,
        })
        return {"ok": True, "path": full_path, "attribute": req.attribute, "time": req.time}

    return {"ok": True, "stub": True}


@app.post("/api/prim/{prim_path:path}/keyframe/move")
async def prim_move_keyframe(prim_path: str, req: _KeyframeMoveReq):
    full_path = "/" + prim_path.lstrip("/")

    stage = _current_stage()
    if stage:
        _pxr_push_undo()
        from pxr import Sdf, Usd

        prim = stage.GetPrimAtPath(Sdf.Path(full_path))
        if not prim or not prim.IsValid():
            raise HTTPException(status_code=404, detail=f"Prim not found: {full_path}")

        attr = prim.GetAttribute(req.attribute)
        if not attr or not attr.IsValid():
            raise HTTPException(status_code=404, detail=f"Attribute not found: {req.attribute}")

        from_tc = Usd.TimeCode(float(req.from_time))
        to_tc = Usd.TimeCode(float(req.to_time))

        try:
            authored_times = list(attr.GetTimeSamples() or [])
            if not any(abs(float(t) - float(req.from_time)) < 1e-6 for t in authored_times):
                raise HTTPException(status_code=404, detail=f"No authored keyframe at time {req.from_time}")

            value = attr.Get(from_tc)
            if value is None:
                raise HTTPException(status_code=404, detail=f"No keyframe value at time {req.from_time}")

            attr.Set(value, to_tc)
            attr.ClearAtTime(from_tc)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Move keyframe failed: {exc}")

        await _conns.broadcast({
            "event": "scene_changed", "primPath": full_path, "attribute": req.attribute,
        })
        return {
            "ok": True,
            "path": full_path,
            "attribute": req.attribute,
            "from_time": req.from_time,
            "to_time": req.to_time,
        }

    return {"ok": True, "stub": True}


# ═════════════════════════════════════════════════════════════════════════════
# DCC commands  (mirrors opendcc.cmds / opendcc.actions)
# ═════════════════════════════════════════════════════════════════════════════

# ── Selection ─────────────────────────────────────────────────────────────────

@app.get("/api/selection")
async def get_selection():
    if not _opendcc_available:
        return {"paths": _stub_stage._selection}
    return {"paths": [str(p) for p in _app_core.get_prim_selection()]}


class _SelectionReq(BaseModel):
    paths:   List[str]
    replace: bool = True


@app.post("/api/selection")
async def set_selection(req: _SelectionReq):
    if not _opendcc_available:
        if req.replace:
            _stub_stage._selection = list(req.paths)
        else:
            for p in req.paths:
                if p not in _stub_stage._selection:
                    _stub_stage._selection.append(p)
        return {"ok": True, "paths": _stub_stage._selection}
    import opendcc.core as dcc_core
    import opendcc.cmds as cmds
    from pxr import Sdf
    sel = dcc_core.SelectionList([Sdf.Path(p) for p in req.paths])
    cmds.select(sel, replace=req.replace)
    return {"ok": True}


# ── Create prim ───────────────────────────────────────────────────────────────

class _CreatePrimReq(BaseModel):
    type:   str
    parent: str = "/World"


@app.post("/api/prims/create")
async def cmd_create_prim(req: _CreatePrimReq):
    # ── pxr mode (USD Python bindings) ────────────────────────────────────
    if _pxr_available and not _opendcc_available:
        _pxr_push_undo()
        stage = _current_stage()
        if stage:
            from pxr import Sdf, UsdGeom, UsdLux
            parent_path = Sdf.Path(req.parent)
            parent_prim = stage.GetPrimAtPath(parent_path)
            if not parent_prim or not parent_prim.IsValid():
                parent_path = Sdf.Path("/World")

            # Find unique name
            base_name = req.type
            existing = {c.GetName() for c in stage.GetPrimAtPath(parent_path).GetChildren()} \
                       if stage.GetPrimAtPath(parent_path).IsValid() else set()
            name = base_name
            i = 1
            while name in existing:
                name = f"{base_name}{i}"
                i += 1

            prim_path = parent_path.AppendChild(name)

            # Map type → USD define
            _TYPE_MAP = {
                "Cube":     ("UsdGeom", "Mesh"),
                "Sphere":   ("UsdGeom", "Sphere"),
                "Cylinder": ("UsdGeom", "Cylinder"),
                "Cone":     ("UsdGeom", "Cone"),
                "Capsule":  ("UsdGeom", "Capsule"),
                "Plane":    ("UsdGeom", "Mesh"),
                "Xform":    ("UsdGeom", "Xform"),
                "Scope":    ("UsdGeom", "Scope"),
                "Camera":   ("UsdGeom", "Camera"),
                # Lights
                "RectLight":     ("UsdLux", "RectLight"),
                "SphereLight":   ("UsdLux", "SphereLight"),
                "DiskLight":     ("UsdLux", "DiskLight"),
                "CylinderLight": ("UsdLux", "CylinderLight"),
                "DistantLight":  ("UsdLux", "DistantLight"),
                "DomeLight":     ("UsdLux", "DomeLight"),
            }

            entry = _TYPE_MAP.get(req.type)
            if entry:
                mod_name, cls_name = entry
                mod = UsdGeom if mod_name == "UsdGeom" else UsdLux
                cls = getattr(mod, cls_name, None)
                if cls:
                    prim_def = cls.Define(stage, prim_path)
                    # Cube/Plane as explicit Mesh
                    if req.type in ("Cube", "Plane"):
                        _make_explicit_mesh(stage, prim_path, req.type)
            else:
                # Generic prim
                stage.DefinePrim(prim_path, req.type)

            path_str = str(prim_path)
            await _conns.broadcast({"event": "scene_changed"})
            return {"ok": True, "path": path_str}

    # ── Stub mode ─────────────────────────────────────────────────────────
    if not _opendcc_available:
        _stub_stage._push_undo()
        path = _stub_stage.create_prim(req.type, req.parent)
        _stub_stage._selection = [path]
        await _conns.broadcast({"event": "scene_changed"})
        return {"ok": True, "path": path}

    import opendcc.cmds as cmds
    res  = cmds.create_prim(req.type, req.type)
    path = res.get_result() if res.is_successful() else None
    if path:
        await _conns.broadcast({"event": "scene_changed"})
    return {"ok": bool(path), "path": str(path) if path else None}


# ── Delete prims ──────────────────────────────────────────────────────────────

class _DeleteReq(BaseModel):
    paths: Optional[List[str]] = None


@app.post("/api/prims/delete")
async def cmd_delete_prims(req: _DeleteReq):
    # ── pxr mode ──────────────────────────────────────────────────────────
    if _pxr_available and not _opendcc_available:
        _pxr_push_undo()
        stage = _current_stage()
        if stage:
            from pxr import Sdf
            paths = req.paths or []
            if not paths:
                return {"ok": False, "error": "nothing selected"}
            for p in paths:
                stage.RemovePrim(Sdf.Path(p))
            await _conns.broadcast({"event": "scene_changed"})
            return {"ok": True, "deleted": paths}

    # ── Stub mode ─────────────────────────────────────────────────────────
    if not _opendcc_available:
        paths = req.paths or list(_stub_stage._selection)
        if not paths:
            return {"ok": False, "error": "nothing selected"}
        _stub_stage._push_undo()
        for p in list(paths):
            _stub_stage.delete_prim(p)
        await _conns.broadcast({"event": "scene_changed"})
        return {"ok": True, "deleted": paths}

    import opendcc.core as dcc_core
    from pxr import Sdf
    from opendcc.usd_ui_utils import remove_prims
    from opendcc.undo import UsdEditsUndoBlock
    paths = req.paths or [str(p) for p in _app_core.get_prim_selection()]
    stage = _current_stage()
    with Sdf.ChangeBlock(), UsdEditsUndoBlock():
        remove_prims(paths, stage, ask=False)
    await _conns.broadcast({"event": "scene_changed"})
    return {"ok": True}


# ── Duplicate prims ───────────────────────────────────────────────────────────

class _DuplicateReq(BaseModel):
    paths: Optional[List[str]] = None


@app.post("/api/prims/duplicate")
async def cmd_duplicate_prims(req: _DuplicateReq):
    # ── pxr mode ──────────────────────────────────────────────────────────
    if _pxr_available and not _opendcc_available:
        _pxr_push_undo()
        stage = _current_stage()
        if stage:
            from pxr import Sdf, Usd
            paths = req.paths or []
            if not paths:
                return {"ok": False, "error": "nothing selected"}
            new_paths = []
            for p in paths:
                src_prim = stage.GetPrimAtPath(Sdf.Path(p))
                if not src_prim or not src_prim.IsValid():
                    continue
                parent = Sdf.Path(p).GetParentPath()
                base = src_prim.GetName()
                existing = {c.GetName() for c in stage.GetPrimAtPath(parent).GetChildren()}
                name, i = base + "_copy", 1
                while name in existing:
                    name = f"{base}_copy{i}"; i += 1
                new_path = parent.AppendChild(name)
                Sdf.CopySpec(stage.GetRootLayer(),
                             Sdf.Path(p), stage.GetRootLayer(), new_path)
                new_paths.append(str(new_path))
            await _conns.broadcast({"event": "scene_changed"})
            return {"ok": True, "new_paths": new_paths}

    # ── Stub mode ─────────────────────────────────────────────────────────
    if not _opendcc_available:
        paths = req.paths or list(_stub_stage._selection)
        if not paths:
            return {"ok": False, "error": "nothing selected"}
        _stub_stage._push_undo()
        new_paths = [_stub_stage.duplicate_prim(p) for p in paths]
        _stub_stage._selection = [p for p in new_paths if p]
        await _conns.broadcast({"event": "scene_changed"})
        return {"ok": True, "new_paths": new_paths}

    import opendcc.cmds as cmds
    cmds.duplicate_prim()
    await _conns.broadcast({"event": "scene_changed"})
    return {"ok": True}


# ── Group prims ───────────────────────────────────────────────────────────────

class _GroupReq(BaseModel):
    paths: Optional[List[str]] = None


@app.post("/api/prims/group")
async def cmd_group_prims(req: _GroupReq):
    # ── pxr mode ──────────────────────────────────────────────────────────
    if _pxr_available and not _opendcc_available:
        _pxr_push_undo()
        stage = _current_stage()
        if stage:
            from pxr import Sdf, UsdGeom
            paths = req.paths or []
            if not paths:
                return {"ok": False, "error": "nothing selected"}
            parent = Sdf.Path(paths[0]).GetParentPath()
            existing = {c.GetName() for c in stage.GetPrimAtPath(parent).GetChildren()}
            name, i = "Group", 1
            while name in existing:
                name = f"Group{i}"; i += 1
            group_path = parent.AppendChild(name)
            UsdGeom.Xform.Define(stage, group_path)
            # Reparent prims under group (simplified — no transform preservation)
            for p in paths:
                src = Sdf.Path(p)
                dst = group_path.AppendChild(src.name)
                Sdf.CopySpec(stage.GetRootLayer(), src, stage.GetRootLayer(), dst)
                stage.RemovePrim(src)
            await _conns.broadcast({"event": "scene_changed"})
            return {"ok": True, "path": str(group_path)}

    # ── Stub mode ─────────────────────────────────────────────────────────
    if not _opendcc_available:
        paths = req.paths or list(_stub_stage._selection)
        if not paths:
            return {"ok": False, "error": "nothing selected"}
        _stub_stage._push_undo()
        group_path = _stub_stage.group_prims(paths)
        _stub_stage._selection = [group_path]
        await _conns.broadcast({"event": "scene_changed"})
        return {"ok": True, "path": group_path}

    import opendcc.cmds as cmds
    cmds.group_prim()
    await _conns.broadcast({"event": "scene_changed"})
    return {"ok": True}


# ── Transform prim (from gizmo) ───────────────────────────────────────────────

class _XformReq(BaseModel):
    path:      str
    translate: Optional[List[float]] = None
    rotate:    Optional[List[float]] = None   # degrees (XYZ euler)
    scale:     Optional[List[float]] = None


@app.post("/api/prims/xform")
async def cmd_xform_prim(req: _XformReq):
    """Apply translate/rotate/scale to a prim's xformOps."""
    _pxr_push_undo()
    stage = _current_stage()
    if not stage:
        return {"ok": False, "error": "no stage"}

    from pxr import Sdf, UsdGeom, Gf

    prim = stage.GetPrimAtPath(Sdf.Path(req.path))
    if not prim or not prim.IsValid():
        raise HTTPException(status_code=404, detail=f"Prim not found: {req.path}")

    xformable = UsdGeom.Xformable(prim)

    # Clear existing xform ops and set fresh TRS
    xformable.ClearXformOpOrder()

    if req.translate:
        xformable.AddTranslateOp().Set(Gf.Vec3d(*req.translate))
    if req.rotate:
        xformable.AddRotateXYZOp().Set(Gf.Vec3f(*req.rotate))
    if req.scale and req.scale != [1, 1, 1]:
        xformable.AddScaleOp().Set(Gf.Vec3f(*req.scale))

    await _conns.broadcast({"event": "scene_changed"})
    return {"ok": True, "path": req.path}


# ── Rename prim ───────────────────────────────────────────────────────────────

class _RenameReq(BaseModel):
    path:     str
    new_name: str


@app.post("/api/prims/rename")
async def cmd_rename_prim(req: _RenameReq):
    # ── pxr mode ──────────────────────────────────────────────────────────
    if _pxr_available and not _opendcc_available:
        _pxr_push_undo()
        stage = _current_stage()
        if stage:
            from pxr import Sdf
            src = Sdf.Path(req.path)
            parent = src.GetParentPath()
            new_path = parent.AppendChild(req.new_name)
            Sdf.CopySpec(stage.GetRootLayer(), src, stage.GetRootLayer(), new_path)
            stage.RemovePrim(src)
            await _conns.broadcast({"event": "scene_changed"})
            return {"ok": True, "new_path": str(new_path)}

    # ── Stub mode ─────────────────────────────────────────────────────────
    if not _opendcc_available:
        _stub_stage._push_undo()
        new_path = _stub_stage.rename_prim(req.path, req.new_name)
        await _conns.broadcast({"event": "scene_changed"})
        return {"ok": True, "new_path": new_path}

    import opendcc.cmds as cmds
    from pxr import Sdf
    cmds.rename_prim(Sdf.Path(req.path), req.new_name)
    await _conns.broadcast({"event": "scene_changed"})
    return {"ok": True}


# ── Visibility ────────────────────────────────────────────────────────────────

class _VisibilityReq(BaseModel):
    paths:   Optional[List[str]] = None
    visible: bool = True


@app.post("/api/prims/visibility")
async def cmd_set_visibility(req: _VisibilityReq):
    # ── pxr mode ──────────────────────────────────────────────────────────
    if _pxr_available and not _opendcc_available:
        _pxr_push_undo()
        stage = _current_stage()
        if stage:
            from pxr import Sdf, UsdGeom
            paths = req.paths or []
            token = UsdGeom.Tokens.inherited if req.visible else UsdGeom.Tokens.invisible
            for p in paths:
                prim = stage.GetPrimAtPath(Sdf.Path(p))
                if prim and prim.IsValid():
                    UsdGeom.Imageable(prim).GetVisibilityAttr().Set(token)
            await _conns.broadcast({"event": "scene_changed"})
            return {"ok": True}

    # ── Stub mode ─────────────────────────────────────────────────────────
    if not _opendcc_available:
        paths = req.paths or list(_stub_stage._selection)
        _stub_stage._push_undo()
        for p in paths:
            _stub_stage.set_visibility(p, req.visible)
        await _conns.broadcast({"event": "scene_changed"})
        return {"ok": True}

    import opendcc.core as dcc_core
    from pxr import Sdf, UsdGeom
    from opendcc.undo import UsdEditsUndoBlock
    paths = req.paths or [str(p) for p in _app_core.get_prim_selection()]
    stage = _current_stage()
    token = UsdGeom.Tokens.inherited if req.visible else UsdGeom.Tokens.invisible
    with Sdf.ChangeBlock(), UsdEditsUndoBlock():
        for path in paths:
            prim = stage.GetPrimAtPath(Sdf.Path(path))
            if prim and prim.HasAttribute("visibility"):
                prim.GetAttribute("visibility").Set(token)
    await _conns.broadcast({"event": "scene_changed"})
    return {"ok": True}


# ── Parent / unparent ─────────────────────────────────────────────────────────

class _ParentReq(BaseModel):
    parent_path:        str
    paths:              Optional[List[str]] = None
    preserve_transform: bool = True


@app.post("/api/prims/parent")
async def cmd_parent_prims(req: _ParentReq):
    if not _opendcc_available:
        paths = req.paths or list(_stub_stage._selection)
        _stub_stage._push_undo()
        for p in paths:
            _stub_stage.reparent(p, req.parent_path)
        await _conns.broadcast({"event": "scene_changed"})
        return {"ok": True}

    import opendcc.cmds as cmds
    from pxr import Sdf
    paths = req.paths or [str(p) for p in _app_core.get_prim_selection()]
    cmds.parent_prim(
        Sdf.Path(req.parent_path),
        paths=[Sdf.Path(p) for p in paths],
        preserve_transform=req.preserve_transform,
    )
    await _conns.broadcast({"event": "scene_changed"})
    return {"ok": True}


# ── Undo / Redo ───────────────────────────────────────────────────────────────

@app.post("/api/undo")
async def cmd_undo():
    if _pxr_available and not _opendcc_available:
        ok = _pxr_do_undo()
        if ok:
            await _conns.broadcast({"event": "scene_changed"})
        return {"ok": ok}
    if not _opendcc_available:
        ok = _stub_stage.undo()
        if ok:
            await _conns.broadcast({"event": "scene_changed"})
        return {"ok": ok}
    _app_core.get_undo_stack().undo()
    await _conns.broadcast({"event": "scene_changed"})
    return {"ok": True}


@app.post("/api/redo")
async def cmd_redo():
    if _pxr_available and not _opendcc_available:
        ok = _pxr_do_redo()
        if ok:
            await _conns.broadcast({"event": "scene_changed"})
        return {"ok": ok}
    if not _opendcc_available:
        ok = _stub_stage.redo()
        if ok:
            await _conns.broadcast({"event": "scene_changed"})
        return {"ok": ok}
    _app_core.get_undo_stack().redo()
    await _conns.broadcast({"event": "scene_changed"})
    return {"ok": True}


# ── Python script execution ───────────────────────────────────────────────────

class _ExecReq(BaseModel):
    code: str


@app.post("/api/execute")
async def execute_python(req: _ExecReq):
    output_lines: list = []

    class _Cap(io.StringIO):
        def write(self, s):
            output_lines.append(s)
            return len(s)

    cap = _Cap()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = cap

    try:
        ctx: dict = {}
        if _opendcc_available:
            import opendcc.core as dcc_core
            ctx["app"]     = dcc_core.Application.instance()
            ctx["session"] = _session
            stage = _current_stage()
            if stage:
                ctx["stage"] = stage
        exec(req.code, ctx)
        result = {"ok": True, "output": "".join(output_lines)}
    except Exception:
        result = {"ok": False, "output": "".join(output_lines),
                  "error": traceback.format_exc()}
    finally:
        sys.stdout, sys.stderr = old_out, old_err

    if result["ok"]:
        await _conns.broadcast({"event": "scene_changed"})
    await _conns.broadcast({"event": "script_executed", "ok": result["ok"]})
    return result


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await _conns.connect(ws)
    await ws.send_text(json.dumps({
        "event":    "connected",
        "opendcc":  _opendcc_available or _pxr_available,
        "usd_wasm": _USD_WASM_SRC.exists(),
    }))
    try:
        while True:
            data = await ws.receive_text()
            msg  = json.loads(data)
            if msg.get("cmd") == "ping":
                await ws.send_text(json.dumps({"event": "pong"}))
    except WebSocketDisconnect:
        _conns.disconnect(ws)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OpenDCC Web Server")
    parser.add_argument("--host",   default="0.0.0.0")
    parser.add_argument("--port",   type=int, default=8080)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    _init_opendcc()

    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )

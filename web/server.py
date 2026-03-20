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


def _prim_to_dict(prim) -> dict:
    return {
        "path":     str(prim.GetPath()),
        "name":     prim.GetName(),
        "type":     prim.GetTypeName(),
        "active":   prim.IsActive(),
        "children": [str(c.GetPath()) for c in prim.GetChildren()],
    }


def _attr_to_dict(attr) -> dict:
    try:
        val = str(attr.Get())
    except Exception:
        val = "<error>"
    return {
        "name":        attr.GetName(),
        "type":        str(attr.GetTypeName()),
        "value":       val,
        "variability": str(attr.GetVariability()),
    }


def _coerce_value(attr, raw_value: Any):
    try:
        from pxr import Gf, Vt
        type_name = str(attr.GetTypeName())
        if isinstance(raw_value, list):
            if "Vec3"   in type_name: return Gf.Vec3f(*[float(x) for x in raw_value[:3]])
            if "Vec2"   in type_name: return Gf.Vec2f(*[float(x) for x in raw_value[:2]])
            if "Vec4"   in type_name: return Gf.Vec4f(*[float(x) for x in raw_value[:4]])
            if "float"  in type_name.lower(): return Vt.FloatArray([float(x) for x in raw_value])
            if "int"    in type_name.lower():  return Vt.IntArray([int(x)   for x in raw_value])
        if isinstance(raw_value, (int, float)):
            if "float"  in type_name.lower() or "double" in type_name.lower(): return float(raw_value)
            if "int"    in type_name.lower():  return int(raw_value)
            if "bool"   in type_name.lower():  return bool(raw_value)
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
):
    """Render the current stage via Hydra Storm (GPU) and return a PNG.

    Falls back to 501 if GPU rendering is not available — the client should
    then use WASM-based rendering (already the default in the viewport).
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
        png_bytes = render_stage_to_png(stage, width, height, tc, camera)

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
async def stage_export_usda():
    stage = _current_stage()
    if stage:
        try:
            # stage.Flatten() resolves ALL composition arcs:
            # references, payloads, sublayers, variants → single layer
            flat_layer = stage.Flatten()
            usda = flat_layer.ExportToString()
            return Response(
                content=usda,
                media_type="model/vnd.usda",
                headers={"Cache-Control": "no-store"},
            )
        except Exception as exc:
            logger.warning("Flatten failed: %s", exc)
            try:
                usda = stage.GetRootLayer().ExportToString()
                return Response(
                    content=usda,
                    media_type="model/vnd.usda",
                    headers={"Cache-Control": "no-store"},
                )
            except Exception:
                pass
    # Fallback to stub
    return Response(
        content=_stub_stage.to_usda(),
        media_type="model/vnd.usda",
        headers={"Cache-Control": "no-store"},
    )


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

class _StageOpenReq(BaseModel):
    path: str


@app.post("/api/stage/open")
async def stage_open(req: _StageOpenReq):
    global _pxr_stage
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
            prim_count = sum(1 for _ in _pxr_stage.TraverseAll())
            logger.info("Opened stage: %s (%d prims)", p, prim_count)
            await _conns.broadcast({"event": "stage_opened", "path": p})
            await _conns.broadcast({"event": "scene_changed"})
            return {"ok": True, "path": p}
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
    global _pxr_stage
    if _opendcc_available:
        _session.new_stage()
    elif _pxr_available:
        _init_default_pxr_stage()
    else:
        _stub_stage._reset()
    await _conns.broadcast({"event": "stage_new"})
    await _conns.broadcast({"event": "scene_changed"})
    return {"ok": True}


@app.post("/api/stage/save")
async def stage_save():
    if not _opendcc_available:
        return {"ok": True, "stub": True}
    try:
        _session.save_stage()
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/stage/info")
async def stage_info():
    stage = _current_stage()
    if not stage:
        return {"stage": None}
    from pxr import UsdGeom
    return {
        "stage": {
            "identifier": stage.GetRootLayer().identifier,
            "prim_count":  sum(1 for _ in stage.TraverseAll()),
            "up_axis":     str(UsdGeom.GetStageUpAxis(stage)),
        }
    }


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
async def prim_detail(prim_path: str):
    full_path = "/" + prim_path.lstrip("/")

    stage = _current_stage()
    if stage:
        from pxr import Sdf
        prim = stage.GetPrimAtPath(Sdf.Path(full_path))
        if not prim or not prim.IsValid():
            raise HTTPException(status_code=404, detail=f"Prim not found: {full_path}")
        return {
            **_prim_to_dict(prim),
            "attributes": [_attr_to_dict(a) for a in prim.GetAttributes()],
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
    return {**d, "attributes": attrs}


# ── Attribute editing ─────────────────────────────────────────────────────────

class _AttrSetReq(BaseModel):
    attribute: str
    value:     Any
    time:      Optional[float] = None


@app.patch("/api/prim/{prim_path:path}")
async def prim_set_attr(prim_path: str, req: _AttrSetReq):
    full_path = "/" + prim_path.lstrip("/")

    if not _opendcc_available:
        return {"ok": True, "stub": True}

    stage = _current_stage()
    if not stage:
        raise HTTPException(status_code=400, detail="No stage loaded")

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


# ── Rename prim ───────────────────────────────────────────────────────────────

class _RenameReq(BaseModel):
    path:     str
    new_name: str


@app.post("/api/prims/rename")
async def cmd_rename_prim(req: _RenameReq):
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

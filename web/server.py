"""
OpenDCC Web Server
==================
FastAPI backend that:
  1. Serves the needle-tools usd-wasm bindings at /usd  (WASM+JS)
  2. Exports the live USD stage as .usda at /api/stage/export.usda
     → the browser's HdWebSyncDriver fetches it and renders via Hydra-WASM
  3. Broadcasts scene_changed over WebSocket so the browser reloads on edits
  4. Exposes REST endpoints for prim inspection and attribute editing

CRITICAL: SharedArrayBuffer (required by the WASM multi-threading) needs
  Cross-Origin-Embedder-Policy: require-corp
  Cross-Origin-Opener-Policy:   same-origin
  Both headers are added by the _CORPMiddleware on every response.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Optional

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
_HERE         = Path(__file__).parent                          # …/web
_STATIC_DIR   = _HERE / "static"
_REPO_ROOT    = _HERE.parent                                   # …/OpenDCC
_PARENT_ROOT  = _REPO_ROOT.parent                              # …/ShapeFX

# needle-tools/usd-viewer must be cloned as a sibling of OpenDCC.
# Override with env var USD_VIEWER_PATH if needed.
_USD_VIEWER   = Path(os.environ.get(
    "USD_VIEWER_PATH",
    str(_PARENT_ROOT / "usd-viewer")
))
_USD_WASM_SRC = _USD_VIEWER / "usd-wasm" / "src"              # emHdBindings.* + ThreeJsRenderDelegate.js
_USD_MODULES  = _USD_VIEWER / "public" / "modules"            # es-module-shims

# ── OpenDCC state ─────────────────────────────────────────────────────────────
_opendcc_available = False
_app_core  = None
_session   = None


def _init_opendcc() -> None:
    global _opendcc_available, _app_core, _session
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
    except ImportError:
        logger.warning(
            "opendcc.core not importable – running in STUB mode.\n"
            "PYTHONPATH must include the OpenDCC site-packages."
        )


# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(title="OpenDCC Web", version="0.1.0")


# ── COOP / COEP middleware ────────────────────────────────────────────────────
class _CORPMiddleware(BaseHTTPMiddleware):
    """
    SharedArrayBuffer (used by the WASM worker threads) requires
    Cross-Origin-Embedder-Policy and Cross-Origin-Opener-Policy to be set
    on EVERY response – including the static JS/WASM assets.
    """
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
        response.headers["Cross-Origin-Opener-Policy"]   = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-site"
        return response


app.add_middleware(_CORPMiddleware)


# ── Static mounts ─────────────────────────────────────────────────────────────
# Order matters: more-specific prefixes first.

if _USD_WASM_SRC.exists():
    # Serves:
    #   /usd/bindings/emHdBindings.js    (WASM glue, sets globalThis["NEEDLE:USD:GET"])
    #   /usd/bindings/emHdBindings.wasm  (16 MB OpenUSD compiled to WASM)
    #   /usd/bindings/emHdBindings.data  (USD schema data)
    #   /usd/bindings/emHdBindings.worker.js
    #   /usd/hydra/ThreeJsRenderDelegate.js  (Hydra → Three.js bridge)
    app.mount("/usd", StaticFiles(directory=str(_USD_WASM_SRC)), name="usd-wasm")
    logger.info("usd-wasm served from %s", _USD_WASM_SRC)
else:
    logger.warning(
        "usd-wasm not found at %s.\n"
        "Clone https://github.com/needle-tools/usd-viewer next to OpenDCC,\n"
        "or set USD_VIEWER_PATH env var.", _USD_WASM_SRC
    )

if _USD_MODULES.exists():
    # Serves /modules/es-module-shims@1.8.0.js (import-map polyfill)
    app.mount("/modules", StaticFiles(directory=str(_USD_MODULES)), name="modules")

if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ── WebSocket connection manager ──────────────────────────────────────────────
class _Connections:
    def __init__(self) -> None:
        self.active: list[WebSocket] = []

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
    if not _opendcc_available:
        return None
    return _session.get_stage() if _session else None


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
    """Convert JSON-decoded value to a typed USD value for attr.Set()."""
    try:
        from pxr import Gf, Vt
        type_name = str(attr.GetTypeName())

        if isinstance(raw_value, list):
            if "Vec3" in type_name:
                return Gf.Vec3f(*[float(x) for x in raw_value[:3]])
            if "Vec2" in type_name:
                return Gf.Vec2f(*[float(x) for x in raw_value[:2]])
            if "Vec4" in type_name:
                return Gf.Vec4f(*[float(x) for x in raw_value[:4]])
            if "Matrix4" in type_name:
                return Gf.Matrix4d(*[float(x) for x in raw_value[:16]])
            if "float" in type_name.lower():
                return Vt.FloatArray([float(x) for x in raw_value])
            if "int" in type_name.lower():
                return Vt.IntArray([int(x) for x in raw_value])

        if isinstance(raw_value, (int, float)):
            if "float" in type_name.lower() or "double" in type_name.lower():
                return float(raw_value)
            if "int" in type_name.lower():
                return int(raw_value)
            if "bool" in type_name.lower():
                return bool(raw_value)

        return raw_value
    except Exception as exc:
        logger.debug("coerce_value failed: %s", exc)
        return raw_value


# ── Page ──────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index():
    html_path = _STATIC_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse(
        "<h1>OpenDCC Web</h1>"
        "<p>static/index.html not found. "
        "Make sure web/static/index.html exists.</p>"
    )


@app.get("/health")
async def health():
    return {
        "status":          "ok",
        "opendcc":         _opendcc_available,
        "usd_wasm_found":  _USD_WASM_SRC.exists(),
    }


# ── USD stage export (consumed by HdWebSyncDriver in the browser) ─────────────

# Minimal placeholder stage returned when no real stage is loaded.
_STUB_USDA = """\
#usda 1.0
(
    upAxis = "Y"
    doc = "OpenDCC stub scene – open a USD file to see your content here"
)

def Xform "World"
{
    def Mesh "Cube"
    {
        float3[] extent = [(-1, -1, -1), (1, 1, 1)]
        int[]    faceVertexCounts  = [4, 4, 4, 4, 4, 4]
        int[]    faceVertexIndices = [0,1,2,3, 4,5,6,7, 0,4,7,1, 2,6,5,3, 0,3,5,4, 1,7,6,2]
        point3f[] points = [(-1,-1,-1),(1,-1,-1),(1,1,-1),(-1,1,-1),
                            (-1,-1, 1),(1,-1, 1),(1,1, 1),(-1,1, 1)]
        uniform token subdivisionScheme = "none"
    }
}
"""


@app.get(
    "/api/stage/export.usda",
    response_class=Response,
    responses={200: {"content": {"model/vnd.usda": {}}}},
)
async def stage_export_usda():
    """
    Return the current USD stage as flat USDA text.
    The browser's HdWebSyncDriver fetches this URL and renders it
    using the Hydra/WASM pipeline without any server-side geometry conversion.
    """
    if not _opendcc_available:
        return Response(
            content=_STUB_USDA,
            media_type="model/vnd.usda",
            headers={"Cache-Control": "no-store"},
        )

    stage = _current_stage()
    if not stage:
        return Response(
            content=_STUB_USDA,
            media_type="model/vnd.usda",
            headers={"Cache-Control": "no-store"},
        )

    try:
        # Flatten all sublayers into one .usda so that all references are
        # self-contained and the browser doesn't need to resolve external paths.
        from pxr import UsdUtils
        flat_layer = UsdUtils.FlattenLayerStack(stage)
        usda = flat_layer.ExportToString()
    except Exception:
        # Fallback: export just the root layer (may have unresolved references)
        usda = stage.GetRootLayer().ExportToString()

    return Response(
        content=usda,
        media_type="model/vnd.usda",
        headers={"Cache-Control": "no-store"},
    )


# ── Stage management ──────────────────────────────────────────────────────────

class _StageOpenReq(BaseModel):
    path: str


@app.post("/api/stage/open")
async def stage_open(req: _StageOpenReq):
    stages_root = os.environ.get("OPENDCC_STAGES_ROOT", "/data/stages")
    p = req.path if os.path.isabs(req.path) else os.path.join(stages_root, req.path)

    if not _opendcc_available:
        await _conns.broadcast({"event": "stage_opened", "path": p})
        await _conns.broadcast({"event": "scene_changed"})
        return {"ok": True, "stub": True, "path": p}

    try:
        _session.open_stage(p)
        await _conns.broadcast({"event": "stage_opened",  "path": p})
        await _conns.broadcast({"event": "scene_changed"})
        return {"ok": True, "path": p}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/stage/new")
async def stage_new():
    if not _opendcc_available:
        return {"ok": True, "stub": True}
    _session.new_stage()
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

_STUB_TREE: dict = {
    "/": [
        {"path": "/World", "name": "World", "type": "Xform",
         "active": True, "children": ["/World/Cube"]},
    ],
    "/World": [
        {"path": "/World/Cube", "name": "Cube", "type": "Mesh",
         "active": True, "children": []},
    ],
    "/World/Cube": [],
}

@app.get("/api/prims")
async def prims_list(path: str = "/"):
    if not _opendcc_available:
        return {"path": path, "children": _STUB_TREE.get(path, [])}

    stage = _current_stage()
    if not stage:
        return {"path": path, "children": []}

    from pxr import Sdf
    prim = stage.GetPrimAtPath(Sdf.Path(path))
    if not prim or not prim.IsValid():
        raise HTTPException(status_code=404, detail=f"Prim not found: {path}")

    return {
        "path":     path,
        "children": [_prim_to_dict(c) for c in prim.GetChildren()],
    }


@app.get("/api/prim/{prim_path:path}")
async def prim_detail(prim_path: str):
    full_path = "/" + prim_path.lstrip("/")

    if not _opendcc_available:
        return {
            "path": full_path, "name": full_path.split("/")[-1],
            "type": "Mesh",
            "attributes": [
                {"name": "points",           "type": "point3f[]",
                 "value": "[]",              "variability": "varying"},
                {"name": "xformOp:translate","type": "double3",
                 "value": "(0, 0, 0)",       "variability": "varying"},
            ],
        }

    stage = _current_stage()
    if not stage:
        raise HTTPException(status_code=400, detail="No stage loaded")

    from pxr import Sdf
    prim = stage.GetPrimAtPath(Sdf.Path(full_path))
    if not prim or not prim.IsValid():
        raise HTTPException(status_code=404, detail=f"Prim not found: {full_path}")

    return {
        **_prim_to_dict(prim),
        "attributes": [_attr_to_dict(a) for a in prim.GetAttributes()],
    }


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
        raise HTTPException(status_code=404,
                            detail=f"Attribute not found: {req.attribute}")

    try:
        typed_val = _coerce_value(attr, req.value)
        if req.time is not None:
            attr.Set(typed_val, Usd.TimeCode(req.time))
        else:
            attr.Set(typed_val)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Set failed: {exc}")

    await _conns.broadcast({
        "event":     "scene_changed",
        "primPath":  full_path,
        "attribute": req.attribute,
    })
    return {"ok": True}


# ── Python script execution ───────────────────────────────────────────────────

class _ExecReq(BaseModel):
    code: str


@app.post("/api/execute")
async def execute_python(req: _ExecReq):
    output_lines: list[str] = []

    class _Cap(io.StringIO):
        def write(self, s):
            output_lines.append(s)
            return len(s)

    cap = _Cap()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = cap

    try:
        ctx: dict[str, Any] = {}
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
        "event":     "connected",
        "opendcc":   _opendcc_available,
        "usd_wasm":  _USD_WASM_SRC.exists(),
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
    parser.add_argument("--reload", action="store_true",
                        help="uvicorn auto-reload (dev mode)")
    args = parser.parse_args()

    _init_opendcc()

    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )

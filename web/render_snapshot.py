"""
render_snapshot.py
==================
Server-side Hydra Storm rendering → PNG snapshot.

Uses OpenUSD's UsdAppUtils.FrameRecorder (or fallback UsdImagingGL) to
render the current stage to an image via the GPU (EGL headless) or
CPU (OSMesa fallback).

The rendered PNG is served at /api/render/snapshot for quick previews
without requiring client-side WASM rendering.
"""

from __future__ import annotations

import io
import logging
import os
import struct
import tempfile
import zlib
from pathlib import Path

log = logging.getLogger("opendcc.render")

# Minimal PNG encoder (no Pillow dependency) ──────────────────────────────────

def _encode_png(pixels: bytes, width: int, height: int) -> bytes:
    """Encode raw RGBA pixels to PNG (minimal, no compression tricks)."""

    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        c = chunk_type + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    header = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA

    # Build raw scanlines (filter byte 0 = None for each row)
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)  # filter byte
        row_start = y * stride
        raw.extend(pixels[row_start:row_start + stride])

    compressed = zlib.compress(bytes(raw), 6)

    return (header
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", compressed)
            + _chunk(b"IEND", b""))


def render_stage_to_png(stage, width: int = 1280, height: int = 720,
                        time_code=None, camera_path: str = "") -> bytes | None:
    """
    Render *stage* to PNG bytes using Hydra Storm (GL).

    Tries three approaches in order:
      1. UsdAppUtils.FrameRecorder (cleanest, requires UsdAppUtils)
      2. UsdImagingGL.Engine (lower level, widely available)
      3. Returns None (caller falls back to WASM client-side)

    Parameters
    ----------
    stage : Usd.Stage
    width, height : render resolution
    time_code : Usd.TimeCode or None (default)
    camera_path : SdfPath string of camera prim (empty = free camera)

    Returns
    -------
    bytes (PNG) or None if rendering is not available.
    """
    if stage is None:
        return None

    # ── Try UsdAppUtils.FrameRecorder ─────────────────────────────────────
    try:
        return _render_via_frame_recorder(stage, width, height, time_code, camera_path)
    except Exception as exc:
        log.debug("FrameRecorder not available: %s", exc)

    # ── Try UsdImagingGL.Engine ───────────────────────────────────────────
    try:
        return _render_via_imaging_gl(stage, width, height, time_code, camera_path)
    except Exception as exc:
        log.debug("UsdImagingGL not available: %s", exc)

    log.warning("No GPU rendering backend available — snapshot not supported")
    return None


def _render_via_frame_recorder(stage, width, height, time_code, camera_path) -> bytes:
    """Render using UsdAppUtils.FrameRecorder → temp file → PNG bytes."""
    from pxr import Usd, UsdAppUtils, Sdf

    tc = time_code if time_code is not None else Usd.TimeCode.Default()

    recorder = UsdAppUtils.FrameRecorder()
    recorder.SetImageWidth(width)

    if camera_path:
        cam_prim = stage.GetPrimAtPath(Sdf.Path(camera_path))
        if cam_prim and cam_prim.IsValid():
            recorder.SetCameraPath(Sdf.Path(camera_path))

    # FrameRecorder writes to a file
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        recorder.Record(stage, tc, tmp_path)
        return Path(tmp_path).read_bytes()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _render_via_imaging_gl(stage, width, height, time_code, camera_path) -> bytes:
    """Render using UsdImagingGL.Engine → raw pixels → PNG."""
    from pxr import Usd, UsdImagingGL, UsdGeom, Gf, Sdf, CameraUtil

    tc = time_code if time_code is not None else Usd.TimeCode.Default()

    engine = UsdImagingGL.Engine()

    # Render params
    params = UsdImagingGL.RenderParams()
    params.frame = tc
    params.clearColor = Gf.Vec4f(0.12, 0.12, 0.12, 1.0)

    # Camera setup
    if camera_path:
        cam_prim = stage.GetPrimAtPath(Sdf.Path(camera_path))
        if cam_prim and cam_prim.IsValid():
            cam = UsdGeom.Camera(cam_prim)
            gf_cam = cam.GetCamera(tc)
            frustum = gf_cam.GetFrustum()
            engine.SetCameraState(
                frustum.ComputeViewMatrix(),
                frustum.ComputeProjectionMatrix()
            )

    engine.SetRendererAov("color")
    engine.SetRenderViewport(Gf.Vec4d(0, 0, width, height))

    # Render
    root = stage.GetPseudoRoot()
    engine.Render(root, params)

    # Read pixels
    try:
        import ctypes
        from OpenGL import GL
        pixels = GL.glReadPixels(0, 0, width, height, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE)
        if pixels:
            # OpenGL returns bottom-up; flip vertically
            stride = width * 4
            flipped = bytearray()
            for y in range(height - 1, -1, -1):
                flipped.extend(pixels[y * stride:(y + 1) * stride])
            return _encode_png(bytes(flipped), width, height)
    except Exception as exc:
        log.warning("glReadPixels failed: %s", exc)

    return None


def check_gpu_rendering_available() -> dict:
    """Check if GPU rendering is actually functional (not just importable).

    Goes beyond import checks: tries to initialise an EGL display and,
    if UsdAppUtils is present, attempts a tiny 1×1 test render so that
    /health can report *real* capability.
    """
    result = {
        "frame_recorder": False,
        "imaging_gl": False,
        "egl": False,
        "egl_initialised": False,
        "test_render_ok": False,
        "reason": "",
    }

    # ── Module import checks ──────────────────────────────────────────────
    try:
        from pxr import UsdAppUtils  # noqa: F401
        result["frame_recorder"] = True
    except ImportError:
        pass

    try:
        from pxr import UsdImagingGL  # noqa: F401
        result["imaging_gl"] = True
    except ImportError:
        pass

    if not result["frame_recorder"] and not result["imaging_gl"]:
        result["reason"] = "No USD imaging module available (UsdAppUtils or UsdImagingGL)"
        return result

    # ── EGL display: load + initialise (not just GetDisplay) ──────────────
    try:
        import ctypes
        egl = ctypes.CDLL("libEGL.so.1")

        EGL_DEFAULT_DISPLAY = 0
        EGL_NO_DISPLAY = 0
        display = egl.eglGetDisplay(EGL_DEFAULT_DISPLAY)
        result["egl"] = display != EGL_NO_DISPLAY

        if result["egl"]:
            major, minor = ctypes.c_int(0), ctypes.c_int(0)
            ok = egl.eglInitialize(display, ctypes.byref(major), ctypes.byref(minor))
            result["egl_initialised"] = bool(ok)
            if ok:
                result["egl_version"] = f"{major.value}.{minor.value}"
            else:
                result["reason"] = "EGL display found but eglInitialize() failed"
                return result
        else:
            result["reason"] = "eglGetDisplay returned EGL_NO_DISPLAY — no GPU accessible"
            return result
    except Exception as exc:
        result["reason"] = f"EGL not available: {exc}"
        return result

    # ── Tiny test render (catches driver/context failures early) ──────────
    if result["frame_recorder"]:
        try:
            from pxr import Usd, UsdGeom, UsdAppUtils, Sdf
            stage = Usd.Stage.CreateInMemory()
            UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
            UsdGeom.Xform.Define(stage, Sdf.Path("/Test"))
            stage.SetDefaultPrim(stage.GetPrimAtPath(Sdf.Path("/Test")))

            recorder = UsdAppUtils.FrameRecorder()
            recorder.SetImageWidth(1)

            tmp_path = "/tmp/_opendcc_gpu_test.png"
            recorder.Record(stage, Usd.TimeCode.Default(), tmp_path)

            if os.path.exists(tmp_path):
                result["test_render_ok"] = True
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            else:
                result["reason"] = "FrameRecorder.Record() produced no output"
        except Exception as exc:
            result["reason"] = f"Test render failed: {exc}"
    else:
        # imaging_gl only — we validated EGL init, that's the best we can do
        result["test_render_ok"] = result["egl_initialised"]

    if not result["test_render_ok"] and not result["reason"]:
        result["reason"] = "GPU render pipeline not functional"

    return result

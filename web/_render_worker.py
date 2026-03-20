#!/usr/bin/env python3
"""
_render_worker.py
=================
Isolated subprocess for GPU rendering.  Called by render_snapshot.py.

Two modes:
  --probe           Print JSON GPU capability report to stdout, then exit.
  --usda + --output Render a stage USDA to a PNG file via FrameRecorder.

This runs in its own process so that any HgiGL cleanup crash ("double free")
never affects the main FastAPI server.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


# ═════════════════════════════════════════════════════════════════════════════
# EGL headless context (must happen BEFORE any pxr imaging import)
# ═════════════════════════════════════════════════════════════════════════════

def _init_egl():
    """Create a headless EGL OpenGL 4.5 Core context on NVIDIA platform device.

    Returns (success: bool, info: dict).
    """
    import ctypes

    info = {"egl": False, "egl_initialised": False, "gl_version": "", "gl_renderer": ""}

    try:
        egl = ctypes.CDLL("libEGL.so.1")
    except OSError:
        info["reason"] = "libEGL.so.1 not found"
        return False, info

    EGLint       = ctypes.c_int
    EGLDeviceEXT = ctypes.c_void_p

    _get_proc = egl.eglGetProcAddress
    _get_proc.restype  = ctypes.c_void_p
    _get_proc.argtypes = [ctypes.c_char_p]

    # Function pointers
    QUERY_T = ctypes.CFUNCTYPE(ctypes.c_int, EGLint,
                                ctypes.POINTER(EGLDeviceEXT),
                                ctypes.POINTER(EGLint))
    GETPLAT_T = ctypes.CFUNCTYPE(ctypes.c_void_p, EGLint,
                                  ctypes.c_void_p, ctypes.c_void_p)

    ptr_q = _get_proc(b"eglQueryDevicesEXT")
    ptr_p = _get_proc(b"eglGetPlatformDisplay") or _get_proc(b"eglGetPlatformDisplayEXT")
    if not ptr_q or not ptr_p:
        info["reason"] = "EGL platform device extensions not available"
        return False, info

    query_devices        = QUERY_T(ptr_q)
    get_platform_display = GETPLAT_T(ptr_p)

    # Enumerate devices
    EGL_PLATFORM_DEVICE_EXT = 0x313F
    num = EGLint(0)
    query_devices(0, None, ctypes.byref(num))
    if num.value == 0:
        info["reason"] = "No EGL devices found"
        return False, info

    devices = (EGLDeviceEXT * num.value)()
    query_devices(num.value, devices, ctypes.byref(num))
    info["egl"] = True

    # Find first initialisable device
    major, minor = EGLint(0), EGLint(0)
    display = None
    for i in range(num.value):
        d = get_platform_display(EGL_PLATFORM_DEVICE_EXT, devices[i], None)
        if d and egl.eglInitialize(ctypes.c_void_p(d),
                                    ctypes.byref(major), ctypes.byref(minor)):
            display = d
            info["egl_initialised"] = True
            info["egl_version"] = f"{major.value}.{minor.value}"
            break

    if not display:
        info["reason"] = "No EGL device could be initialised"
        return False, info

    # Bind OpenGL, get config, create PBuffer + context
    EGL_OPENGL_API = 0x30A2
    EGL_NONE       = 0x3038
    egl.eglBindAPI(EGL_OPENGL_API)

    EGLConfig = ctypes.c_void_p
    num_cfg = EGLint(0)
    egl.eglGetConfigs.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                   EGLint, ctypes.POINTER(EGLint)]
    egl.eglGetConfigs(ctypes.c_void_p(display), None, 0, ctypes.byref(num_cfg))
    if num_cfg.value == 0:
        info["reason"] = "No EGL configs"
        return False, info

    configs = (EGLConfig * num_cfg.value)()
    egl.eglGetConfigs(ctypes.c_void_p(display), configs, num_cfg.value,
                       ctypes.byref(num_cfg))
    config = configs[0]

    # PBuffer
    pbuf = (EGLint * 5)(0x3057, 4096, 0x3056, 4096, EGL_NONE)  # W, H
    egl.eglCreatePbufferSurface.restype  = ctypes.c_void_p
    egl.eglCreatePbufferSurface.argtypes = [ctypes.c_void_p, EGLConfig,
                                             ctypes.c_void_p]
    surface = egl.eglCreatePbufferSurface(ctypes.c_void_p(display), config, pbuf)

    # GL 4.5 Core
    ctx_attr = (EGLint * 7)(0x3098, 4, 0x30FB, 5, 0x30FD, 0x01, EGL_NONE)
    egl.eglCreateContext.restype  = ctypes.c_void_p
    egl.eglCreateContext.argtypes = [ctypes.c_void_p, EGLConfig,
                                      ctypes.c_void_p, ctypes.c_void_p]
    context = egl.eglCreateContext(ctypes.c_void_p(display), config, None, ctx_attr)
    if not context:
        info["reason"] = "eglCreateContext failed"
        return False, info

    # Make current
    egl.eglMakeCurrent.argtypes = [ctypes.c_void_p] * 4
    ok = egl.eglMakeCurrent(ctypes.c_void_p(display),
                             ctypes.c_void_p(surface) if surface else None,
                             ctypes.c_void_p(surface) if surface else None,
                             ctypes.c_void_p(context))
    if not ok:
        info["reason"] = "eglMakeCurrent failed"
        return False, info

    # Query GL
    try:
        gl = ctypes.CDLL("libOpenGL.so.0")
        gl.glGetString.restype  = ctypes.c_char_p
        gl.glGetString.argtypes = [ctypes.c_uint]
        info["gl_version"]  = (gl.glGetString(0x1F02) or b"").decode()
        info["gl_renderer"] = (gl.glGetString(0x1F01) or b"").decode()
    except Exception:
        pass

    return True, info


# ═════════════════════════════════════════════════════════════════════════════
# Probe mode (--probe)
# ═════════════════════════════════════════════════════════════════════════════

def _probe():
    """Test EGL + FrameRecorder, print JSON result to stdout."""
    ok, info = _init_egl()

    result = {
        "egl":              info.get("egl", False),
        "egl_initialised":  info.get("egl_initialised", False),
        "gl_version":       info.get("gl_version", ""),
        "gl_renderer":      info.get("gl_renderer", ""),
        "test_render_ok":   False,
        "reason":           info.get("reason", ""),
    }

    if not ok:
        print(json.dumps(result), flush=True)
        return

    # Try a tiny test render
    try:
        from pxr import Usd, UsdGeom, UsdAppUtils, Sdf, Gf, Vt

        stage = Usd.Stage.CreateInMemory()
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
        xf = UsdGeom.Xform.Define(stage, Sdf.Path("/Test"))
        stage.SetDefaultPrim(xf.GetPrim())

        # Minimal mesh
        mesh = UsdGeom.Mesh.Define(stage, Sdf.Path("/Test/Box"))
        mesh.GetPointsAttr().Set(Vt.Vec3fArray([
            Gf.Vec3f(-1, -1, -1), Gf.Vec3f(1, -1, -1),
            Gf.Vec3f(1, 1, -1),   Gf.Vec3f(-1, 1, -1),
            Gf.Vec3f(-1, -1, 1),  Gf.Vec3f(1, -1, 1),
            Gf.Vec3f(1, 1, 1),    Gf.Vec3f(-1, 1, 1),
        ]))
        mesh.GetFaceVertexCountsAttr().Set(Vt.IntArray([4] * 6))
        mesh.GetFaceVertexIndicesAttr().Set(Vt.IntArray([
            0,3,2,1, 4,5,6,7, 0,4,7,3, 1,2,6,5, 0,1,5,4, 2,3,7,6]))
        mesh.GetExtentAttr().Set(Vt.Vec3fArray([
            Gf.Vec3f(-1,-1,-1), Gf.Vec3f(1,1,1)]))
        mesh.GetSubdivisionSchemeAttr().Set("none")

        cam = UsdGeom.Camera.Define(stage, Sdf.Path("/Test/Cam"))
        cam.GetFocalLengthAttr().Set(50.0)
        UsdGeom.Xformable(cam.GetPrim()).AddTranslateOp().Set(Gf.Vec3d(5, 5, 5))

        recorder = UsdAppUtils.FrameRecorder()
        recorder.SetImageWidth(64)

        tmp = "/tmp/_opendcc_probe_render.png"
        usd_cam = UsdGeom.Camera(stage.GetPrimAtPath(Sdf.Path("/Test/Cam")))

        try:
            recorder.Record(stage, usd_cam, Usd.TimeCode.Default(), tmp)
        except Exception:
            pass  # GL errors raised as exceptions — but file may still exist

        if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            result["test_render_ok"] = True
            os.unlink(tmp)
        else:
            result["reason"] = "FrameRecorder produced no output"

    except Exception as exc:
        result["reason"] = f"Test render failed: {exc}"

    print(json.dumps(result), flush=True)


# ═════════════════════════════════════════════════════════════════════════════
# Render mode (--usda + --output)
# ═════════════════════════════════════════════════════════════════════════════

def _render(usda_path: str, output_path: str, width: int, height: int,
            camera_path: str = "", time_val: str = ""):
    """Render a USDA file to PNG."""
    ok, info = _init_egl()
    if not ok:
        print(f"EGL init failed: {info.get('reason', '?')}", file=sys.stderr)
        sys.exit(1)

    from pxr import Usd, UsdGeom, UsdAppUtils, Sdf

    stage = Usd.Stage.Open(usda_path)
    if not stage:
        print(f"Cannot open stage: {usda_path}", file=sys.stderr)
        sys.exit(1)

    tc = Usd.TimeCode.Default()
    if time_val:
        tc = Usd.TimeCode(float(time_val))

    recorder = UsdAppUtils.FrameRecorder()
    recorder.SetImageWidth(width)

    # Find or create a camera
    usd_cam = None
    if camera_path:
        cam_prim = stage.GetPrimAtPath(Sdf.Path(camera_path))
        if cam_prim and cam_prim.IsValid():
            usd_cam = UsdGeom.Camera(cam_prim)

    if usd_cam is None:
        # Create a default camera looking at the stage bounding box
        from pxr import Gf, UsdGeom as UG
        bbox_cache = UG.BBoxCache(tc, [UG.Tokens.default_])
        root_prim = stage.GetDefaultPrim() or stage.GetPseudoRoot()
        bbox = bbox_cache.ComputeWorldBound(root_prim)
        rng = bbox.ComputeAlignedRange()
        center = (rng.GetMin() + rng.GetMax()) / 2.0
        size = (rng.GetMax() - rng.GetMin()).GetLength()
        if size < 0.001:
            size = 10.0

        cam_path = Sdf.Path("/_SnapshotCam")
        cam_prim_def = UsdGeom.Camera.Define(stage, cam_path)
        cam_prim_def.GetFocalLengthAttr().Set(50.0)

        dist = size * 2.0
        eye = Gf.Vec3d(center[0] + dist * 0.6,
                       center[1] + dist * 0.4,
                       center[2] + dist * 0.6)
        xf = UsdGeom.Xformable(cam_prim_def.GetPrim())
        xf.AddTranslateOp().Set(eye)

        usd_cam = cam_prim_def

    try:
        recorder.Record(stage, usd_cam, tc, output_path)
    except Exception:
        pass  # GL errors may throw, but file may still be written

    if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        print(f"OK {os.path.getsize(output_path)} bytes", file=sys.stderr)
    else:
        print("Render produced no output", file=sys.stderr)
        sys.exit(1)


# ═════════════════════════════════════════════════════════════════════════════
# Entry point
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OpenDCC GPU render worker")
    parser.add_argument("--probe",  action="store_true", help="Test GPU and exit")
    parser.add_argument("--usda",   type=str, default="", help="Input USDA path")
    parser.add_argument("--output", type=str, default="", help="Output PNG path")
    parser.add_argument("--width",  type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--camera", type=str, default="")
    parser.add_argument("--time",   type=str, default="")
    args = parser.parse_args()

    if args.probe:
        _probe()
    elif args.usda and args.output:
        _render(args.usda, args.output, args.width, args.height,
                args.camera, args.time)
    else:
        parser.print_help()
        sys.exit(1)

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

    # GL 4.5+ Compatibility profile (NOT core — USD HgiGL uses legacy
    # GL state queries like GL_POLYGON_SMOOTH that are invalid in core)
    EGL_CONTEXT_OPENGL_COMPAT_BIT = 0x00000002
    ctx_attr = (EGLint * 7)(0x3098, 4, 0x30FB, 5, 0x30FD, EGL_CONTEXT_OPENGL_COMPAT_BIT, EGL_NONE)
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
            camera_path: str = "", time_val: str = "",
            viewport_camera: dict = None):
    """Render a USDA file to PNG."""
    ok, info = _init_egl()
    if not ok:
        print(f"EGL init failed: {info.get('reason', '?')}", file=sys.stderr)
        sys.exit(1)

    from pxr import Usd, UsdGeom, UsdAppUtils, Sdf, UsdLux

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

    # ── Viewport camera (matches client Three.js view) ────────────────────
    if usd_cam is None and viewport_camera:
        from pxr import Gf
        try:
            eye_vals    = list(viewport_camera["eye"])
            target_vals = list(viewport_camera["target"])
            vfov_deg    = viewport_camera.get("fov", 27)
            aspect      = viewport_camera.get("aspect", width / height)
            near_clip   = viewport_camera.get("near", 0.01)
            far_clip    = viewport_camera.get("far", 100000)

            # Three.js applies rotation.x = -π/2 to USD root when stage is
            # Z-up (to convert Z-up → Y-up for Three.js).  The camera
            # coordinates from Three.js are therefore in the ROTATED space.
            # We must undo that rotation before placing the USD camera.
            #
            # Inverse of Rx(-90°) is Rx(+90°):
            #   (x, y, z) → (x, -z, y)
            up_axis = str(UsdGeom.GetStageUpAxis(stage)).upper()
            if up_axis == "Z":
                # Undo the -90°X that Three.js applied:  Rx(+90°)
                # (x, y, z) → (x, -z, y)
                ex, ey, ez = eye_vals
                eye_vals    = [ex, -ez, ey]
                tx, ty, tz = target_vals
                target_vals = [tx, -tz, ty]
                up = Gf.Vec3d(0, 0, 1)  # Z-up in USD stage
                print(f"Z-up stage: rotated camera coords back", file=sys.stderr)
            else:
                up = Gf.Vec3d(0, 1, 0)  # Y-up

            eye    = Gf.Vec3d(*eye_vals)
            target = Gf.Vec3d(*target_vals)

            # Three.js PerspectiveCamera.fov is VERTICAL fov in degrees.
            # USD Camera uses horizontalAperture + focalLength.
            # Formula: focalLength = (hAperture / 2) / tan(hfov / 2)
            # hfov = 2 * atan(tan(vfov/2) * aspect)
            import math
            vfov_rad = math.radians(vfov_deg)
            hfov_rad = 2.0 * math.atan(math.tan(vfov_rad / 2.0) * aspect)

            h_aperture = 36.0  # standard 35mm film (mm)
            focal_length = (h_aperture / 2.0) / math.tan(hfov_rad / 2.0)

            cam_path_s = Sdf.Path("/_SnapshotCam")
            cam_def = UsdGeom.Camera.Define(stage, cam_path_s)
            cam_def.GetFocalLengthAttr().Set(float(focal_length))
            cam_def.GetHorizontalApertureAttr().Set(float(h_aperture))
            cam_def.GetVerticalApertureAttr().Set(float(h_aperture / aspect))
            cam_def.GetClippingRangeAttr().Set(
                Gf.Vec2f(float(near_clip), float(far_clip)))

            # Look-at transform
            look_at = Gf.Matrix4d()
            look_at.SetLookAt(eye, target, up)
            cam_xform = look_at.GetInverse()

            xf = UsdGeom.Xformable(cam_def.GetPrim())
            xf.AddTransformOp().Set(cam_xform)

            usd_cam = cam_def
            print(f"Viewport cam: eye={eye_vals} target={target_vals} "
                  f"vfov={vfov_deg:.1f}° focal={focal_length:.1f}mm",
                  file=sys.stderr)
        except Exception as exc:
            print(f"Viewport camera setup failed: {exc}", file=sys.stderr)
            usd_cam = None

    # ── Fallback: auto-frame from bounding box ───────────────────────────
    if usd_cam is None:
        from pxr import Gf, UsdGeom as UG
        bbox_cache = UG.BBoxCache(tc, [UG.Tokens.default_])
        root_prim = stage.GetDefaultPrim() or stage.GetPseudoRoot()
        bbox = bbox_cache.ComputeWorldBound(root_prim)
        rng = bbox.ComputeAlignedRange()
        center = Gf.Vec3d((rng.GetMin() + rng.GetMax()) / 2.0)
        size = (rng.GetMax() - rng.GetMin()).GetLength()
        if size < 0.001:
            size = 10.0

        up_axis = str(UsdGeom.GetStageUpAxis(stage)).upper()

        cam_path_s = Sdf.Path("/_SnapshotCam")
        cam_def = UsdGeom.Camera.Define(stage, cam_path_s)
        cam_def.GetFocalLengthAttr().Set(35.0)
        cam_def.GetClippingRangeAttr().Set(Gf.Vec2f(0.1, size * 20))

        dist = size * 1.8
        if up_axis == "Z":
            eye = Gf.Vec3d(center[0] + dist * 0.6,
                           center[1] - dist * 0.6,
                           center[2] + dist * 0.45)
            up = Gf.Vec3d(0, 0, 1)
        else:
            eye = Gf.Vec3d(center[0] + dist * 0.6,
                           center[1] + dist * 0.45,
                           center[2] + dist * 0.6)
            up = Gf.Vec3d(0, 1, 0)

        look_at = Gf.Matrix4d()
        look_at.SetLookAt(eye, center, up)

        xf = UsdGeom.Xformable(cam_def.GetPrim())
        xf.AddTransformOp().Set(look_at.GetInverse())

        usd_cam = cam_def

    # ── Ensure at least one light exists ──────────────────────────────────
    has_light = False
    for prim in stage.TraverseAll():
        if prim.IsA(UsdLux.BoundableLightBase) if hasattr(UsdLux, 'BoundableLightBase') else prim.GetTypeName().endswith("Light"):
            has_light = True
            break
    if not has_light:
        from pxr import UsdLux as _UL
        # Subtle dome light — avoids washing out the scene
        dome = _UL.DomeLight.Define(stage, Sdf.Path("/_SnapshotDome"))
        dome.GetIntensityAttr().Set(0.35)

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
    parser.add_argument("--viewport-camera", type=str, default="",
                        help="JSON dict: {eye, target, fov, aspect, near, far}")
    args = parser.parse_args()

    if args.probe:
        _probe()
    elif args.usda and args.output:
        vc = None
        if args.viewport_camera:
            vc = json.loads(args.viewport_camera)
        _render(args.usda, args.output, args.width, args.height,
                args.camera, args.time, viewport_camera=vc)
    else:
        parser.print_help()
        sys.exit(1)

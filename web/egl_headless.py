"""
egl_headless.py
===============
Headless EGL context setup for NVIDIA GPU rendering without X11.

Uses EGL_EXT_platform_device to find the NVIDIA GPU and create an
OpenGL 4.5+ Compatibility Profile context with a PBuffer surface — required
by USD's HgiGL / Hydra Storm before any rendering call.

Call ``ensure_egl_context()`` once at process startup (before importing
any pxr imaging module).
"""

from __future__ import annotations

import ctypes
import logging
import os

log = logging.getLogger("opendcc.egl")

# ── EGL constants ─────────────────────────────────────────────────────────────
EGL_PLATFORM_DEVICE_EXT = 0x313F
EGL_DEFAULT_DISPLAY     = 0
EGL_NO_DISPLAY          = 0
EGL_OPENGL_API          = 0x30A2
EGL_NONE                = 0x3038
EGL_CONTEXT_MAJOR_VERSION        = 0x3098
EGL_CONTEXT_MINOR_VERSION        = 0x30FB
EGL_CONTEXT_OPENGL_PROFILE_MASK  = 0x30FD
EGL_CONTEXT_OPENGL_CORE_PROFILE_BIT = 0x00000001
EGL_WIDTH  = 0x3057
EGL_HEIGHT = 0x3056

# ── State ─────────────────────────────────────────────────────────────────────
_egl_ready = False
_egl_display = None
_egl_context = None
_egl_surface = None


def _load_egl():
    """Load libEGL and helper function pointers."""
    egl = ctypes.CDLL("libEGL.so.1")

    EGLint       = ctypes.c_int
    EGLDeviceEXT = ctypes.c_void_p
    EGLDisplay   = ctypes.c_void_p
    EGLContext   = ctypes.c_void_p
    EGLSurface   = ctypes.c_void_p
    EGLConfig    = ctypes.c_void_p

    _get_proc = egl.eglGetProcAddress
    _get_proc.restype  = ctypes.c_void_p
    _get_proc.argtypes = [ctypes.c_char_p]

    # eglQueryDevicesEXT
    QUERY_DEVICES_T = ctypes.CFUNCTYPE(
        ctypes.c_int, EGLint, ctypes.POINTER(EGLDeviceEXT), ctypes.POINTER(EGLint))
    ptr = _get_proc(b"eglQueryDevicesEXT")
    if not ptr:
        raise RuntimeError("eglQueryDevicesEXT not available")
    query_devices = QUERY_DEVICES_T(ptr)

    # eglGetPlatformDisplay(EXT)
    GET_PLATFORM_T = ctypes.CFUNCTYPE(EGLDisplay, EGLint, ctypes.c_void_p, ctypes.c_void_p)
    ptr = _get_proc(b"eglGetPlatformDisplay") or _get_proc(b"eglGetPlatformDisplayEXT")
    if not ptr:
        raise RuntimeError("eglGetPlatformDisplay not available")
    get_platform_display = GET_PLATFORM_T(ptr)

    return egl, EGLint, EGLDeviceEXT, EGLConfig, query_devices, get_platform_display


def ensure_egl_context(width: int = 4096, height: int = 4096) -> bool:
    """Initialise a headless EGL OpenGL 4.5 Core context.

    Safe to call multiple times — only initialises once.
    Returns True if the context is active, False on failure.
    """
    global _egl_ready, _egl_display, _egl_context, _egl_surface

    if _egl_ready:
        return True

    try:
        egl, EGLint, EGLDeviceEXT, EGLConfig, query_devices, get_platform_display = _load_egl()
    except Exception as exc:
        log.warning("EGL load failed: %s", exc)
        return False

    # ── Find first working NVIDIA EGL device ──────────────────────────────
    num = EGLint(0)
    query_devices(0, None, ctypes.byref(num))
    if num.value == 0:
        log.warning("No EGL devices found")
        return False

    devices = (EGLDeviceEXT * num.value)()
    query_devices(num.value, devices, ctypes.byref(num))

    major, minor = EGLint(0), EGLint(0)
    display = None
    for i in range(num.value):
        d = get_platform_display(EGL_PLATFORM_DEVICE_EXT, devices[i], None)
        if d and egl.eglInitialize(ctypes.c_void_p(d), ctypes.byref(major), ctypes.byref(minor)):
            display = d
            log.info("EGL device %d: EGL %d.%d", i, major.value, minor.value)
            break

    if not display:
        log.warning("No EGL device could be initialised")
        return False

    # ── Bind OpenGL API ───────────────────────────────────────────────────
    if not egl.eglBindAPI(EGL_OPENGL_API):
        log.warning("eglBindAPI(OPENGL) failed")
        return False

    # ── Pick first config (all 65 support PBuffer+OpenGL on NVIDIA) ───────
    num_cfg = EGLint(0)
    egl.eglGetConfigs.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                   EGLint, ctypes.POINTER(EGLint)]
    egl.eglGetConfigs(ctypes.c_void_p(display), None, 0, ctypes.byref(num_cfg))
    if num_cfg.value == 0:
        log.warning("No EGL configs available")
        return False

    configs = (EGLConfig * num_cfg.value)()
    egl.eglGetConfigs(ctypes.c_void_p(display), configs, num_cfg.value,
                       ctypes.byref(num_cfg))
    config = configs[0]

    # ── PBuffer surface ───────────────────────────────────────────────────
    pbuf_attribs = (EGLint * 5)(EGL_WIDTH, width, EGL_HEIGHT, height, EGL_NONE)
    egl.eglCreatePbufferSurface.restype  = ctypes.c_void_p
    egl.eglCreatePbufferSurface.argtypes = [ctypes.c_void_p, EGLConfig,
                                             ctypes.c_void_p]
    surface = egl.eglCreatePbufferSurface(ctypes.c_void_p(display), config,
                                           pbuf_attribs)
    if not surface:
        log.warning("eglCreatePbufferSurface failed")
        return False

    # ── OpenGL 4.5+ Compatibility Profile context ──────────────────────────
    # IMPORTANT: USD's HgiGL uses legacy GL state queries (GL_POLYGON_SMOOTH
    # etc.) that trigger invalid-enum in core profile → draw failures.
    # Compatibility profile provides GL 4.6 on NVIDIA and avoids all this.
    EGL_CONTEXT_OPENGL_COMPAT_BIT = 0x00000002
    ctx_attribs = (EGLint * 7)(
        EGL_CONTEXT_MAJOR_VERSION, 4,
        EGL_CONTEXT_MINOR_VERSION, 5,
        EGL_CONTEXT_OPENGL_PROFILE_MASK, EGL_CONTEXT_OPENGL_COMPAT_BIT,
        EGL_NONE,
    )
    egl.eglCreateContext.restype  = ctypes.c_void_p
    egl.eglCreateContext.argtypes = [ctypes.c_void_p, EGLConfig,
                                      ctypes.c_void_p, ctypes.c_void_p]
    context = egl.eglCreateContext(ctypes.c_void_p(display), config, None,
                                    ctx_attribs)
    if not context:
        log.warning("eglCreateContext failed")
        return False

    # ── Make current ──────────────────────────────────────────────────────
    egl.eglMakeCurrent.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_void_p, ctypes.c_void_p]
    if not egl.eglMakeCurrent(ctypes.c_void_p(display),
                               ctypes.c_void_p(surface),
                               ctypes.c_void_p(surface),
                               ctypes.c_void_p(context)):
        log.warning("eglMakeCurrent failed")
        return False

    _egl_display = display
    _egl_context = context
    _egl_surface = surface
    _egl_ready   = True

    # ── Verify GL ─────────────────────────────────────────────────────────
    try:
        gl = ctypes.CDLL("libOpenGL.so.0")
        gl.glGetString.restype  = ctypes.c_char_p
        gl.glGetString.argtypes = [ctypes.c_uint]
        gl_ver   = (gl.glGetString(0x1F02) or b"").decode()
        renderer = (gl.glGetString(0x1F01) or b"").decode()
        log.info("GL ready: %s — %s", gl_ver, renderer)
    except Exception:
        pass

    return True

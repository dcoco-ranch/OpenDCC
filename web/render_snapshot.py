"""
render_snapshot.py
==================
Server-side Hydra Storm rendering → PNG snapshot.

Strategy
--------
1. Export the current stage to a temp USDA file.
2. Spawn a **subprocess** that:
   a. Initialises a headless EGL context (NVIDIA platform device)
   b. Opens the stage with pxr
   c. Renders via UsdAppUtils.FrameRecorder
   d. Writes a PNG and exits

The subprocess approach is deliberate: USD's HgiGL on NVIDIA 595+ drivers
triggers non-fatal GL errors and occasionally crashes during cleanup
("double free").  By isolating the render in a child process the FastAPI
server is never affected.

The rendered PNG is served at ``/api/render/snapshot``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

log = logging.getLogger("opendcc.render")


# ═════════════════════════════════════════════════════════════════════════════
# Public API (called by server.py)
# ═════════════════════════════════════════════════════════════════════════════

def render_stage_to_png(stage, width: int = 1280, height: int = 720,
                        time_code=None, camera_path: str = "",
                        viewport_camera: dict = None) -> bytes | None:
    """Render *stage* to PNG bytes via a GPU subprocess.

    Parameters
    ----------
    viewport_camera : dict, optional
        {"eye": [x,y,z], "target": [x,y,z], "fov": float,
         "aspect": float, "near": float, "far": float}
        When provided the snapshot matches the client's Three.js viewport.

    Returns PNG bytes on success, or None if rendering is unavailable.
    """
    if stage is None:
        return None

    # ── Export stage to temp USDA ─────────────────────────────────────────
    try:
        flat = stage.Flatten()
        usda = flat.ExportToString()
    except Exception:
        try:
            usda = stage.GetRootLayer().ExportToString()
        except Exception as exc:
            log.warning("Cannot export stage: %s", exc)
            return None

    fd_usda, usda_path = tempfile.mkstemp(suffix=".usda", prefix="opendcc_snap_")
    fd_png,  png_path  = tempfile.mkstemp(suffix=".png",  prefix="opendcc_snap_")
    os.close(fd_png)

    try:
        with os.fdopen(fd_usda, "w") as f:
            f.write(usda)

        # ── Determine time code ───────────────────────────────────────────
        tc_arg = ""
        if time_code is not None:
            from pxr import Usd
            if isinstance(time_code, Usd.TimeCode):
                if not time_code.IsDefault():
                    tc_arg = str(time_code.GetValue())
            else:
                tc_arg = str(time_code)

        # ── Spawn render subprocess ───────────────────────────────────────
        worker = Path(__file__).parent / "_render_worker.py"
        cmd = [
            sys.executable, str(worker),
            "--usda",   usda_path,
            "--output", png_path,
            "--width",  str(width),
            "--height", str(height),
        ]
        if camera_path:
            cmd += ["--camera", camera_path]
        if tc_arg:
            cmd += ["--time", tc_arg]
        if viewport_camera:
            cmd += ["--viewport-camera", json.dumps(viewport_camera)]

        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=30,
            env={**os.environ, "PYOPENGL_PLATFORM": "egl"},
        )

        # Worker may crash during cleanup (double-free) but still produce
        # valid output.  Check the PNG file regardless of return code.
        if os.path.exists(png_path) and os.path.getsize(png_path) > 0:
            png_bytes = Path(png_path).read_bytes()
            log.info("Snapshot rendered: %d bytes (%dx%d)", len(png_bytes), width, height)
            return png_bytes

        # No output — log worker stderr for diagnostics
        stderr = result.stderr.decode(errors="replace")[-500:]
        log.warning("Render worker produced no output (rc=%d): %s",
                     result.returncode, stderr)
        return None

    except subprocess.TimeoutExpired:
        log.warning("Render worker timed out (30s)")
        return None
    except Exception as exc:
        log.warning("Render subprocess failed: %s", exc)
        return None
    finally:
        for p in (usda_path, png_path):
            try:
                os.unlink(p)
            except OSError:
                pass


def check_gpu_rendering_available() -> dict:
    """Check if GPU rendering is actually functional.

    Spawns a small subprocess that initialises EGL and does a 1×1 test
    render.  Returns a status dict consumed by ``/health``.
    """
    result = {
        "frame_recorder": False,
        "imaging_gl": False,
        "egl": False,
        "egl_initialised": False,
        "test_render_ok": False,
        "reason": "",
    }

    # ── Module import checks (in current process — safe) ──────────────────
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
        result["reason"] = "No USD imaging module available"
        return result

    # ── EGL + test render in subprocess (safe — can't crash server) ───────
    worker = Path(__file__).parent / "_render_worker.py"
    try:
        proc = subprocess.run(
            [sys.executable, str(worker), "--probe"],
            capture_output=True,
            timeout=15,
            env={**os.environ, "PYOPENGL_PLATFORM": "egl"},
        )
        stdout = proc.stdout.decode(errors="replace").strip()
        if stdout:
            probe = json.loads(stdout)
            result.update(probe)
    except subprocess.TimeoutExpired:
        result["reason"] = "GPU probe subprocess timed out"
    except Exception as exc:
        result["reason"] = f"GPU probe failed: {exc}"

    return result

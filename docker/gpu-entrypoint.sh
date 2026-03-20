#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# gpu-entrypoint.sh — GPU detection + graceful fallback for OpenDCC container
#
# Runs at container startup:
#   1. Detects if NVIDIA GPU is accessible
#   2. Writes GPU info to /tmp/gpu-info.json (read by /health endpoint)
#   3. Falls back gracefully if no GPU (WASM client-side rendering)
#   4. Exec's the CMD (server.py)
# ═══════════════════════════════════════════════════════════════════════════════
set -uo pipefail

GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[gpu]${NC} $*"; }
warn() { echo -e "${YELLOW}[gpu]${NC} $*"; }
info() { echo -e "${CYAN}[gpu]${NC} $*"; }

GPU_INFO_FILE="/tmp/gpu-info.json"

echo ""
log "OpenDCC GPU Container — Starting..."
echo ""

# ── Detect NVIDIA GPU ─────────────────────────────────────────────────────────
if command -v nvidia-smi &>/dev/null; then
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo "")
    GPU_MEM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader 2>/dev/null | head -1 || echo "")
    DRIVER_VER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 || echo "")
    CUDA_VER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 || echo "")
    GPU_COUNT=$(nvidia-smi --list-gpus 2>/dev/null | wc -l || echo "0")

    if [[ -n "$GPU_NAME" && "$GPU_NAME" != "" ]]; then
        info "GPU detected:"
        info "  Name:    ${GPU_NAME}"
        info "  VRAM:    ${GPU_MEM}"
        info "  Driver:  ${DRIVER_VER}"
        info "  Count:   ${GPU_COUNT}"
        echo ""

        # Write GPU info for the /health endpoint
        cat > "$GPU_INFO_FILE" << EOF
{
    "gpu_available": true,
    "gpu_name": "${GPU_NAME}",
    "gpu_memory": "${GPU_MEM}",
    "gpu_driver": "${DRIVER_VER}",
    "gpu_count": ${GPU_COUNT},
    "render_mode": "server_gpu"
}
EOF
        log "GPU rendering available — Hydra Storm (server-side)"
        export OPENDCC_GPU_ENABLED=1
        export OPENDCC_RENDER_MODE=server_gpu
    else
        warn "nvidia-smi found but no GPU detected"
        echo '{"gpu_available": false, "render_mode": "client_wasm"}' > "$GPU_INFO_FILE"
        export OPENDCC_GPU_ENABLED=0
        export OPENDCC_RENDER_MODE=client_wasm
    fi
else
    warn "nvidia-smi not found — no GPU available"
    warn "Rendering will use client-side WASM (browser)"
    echo '{"gpu_available": false, "render_mode": "client_wasm"}' > "$GPU_INFO_FILE"
    export OPENDCC_GPU_ENABLED=0
    export OPENDCC_RENDER_MODE=client_wasm
fi

# ── Test EGL availability ─────────────────────────────────────────────────────
if command -v eglinfo &>/dev/null; then
    if eglinfo 2>/dev/null | grep -q "EGL version"; then
        info "EGL: available ✓"
    else
        warn "EGL: not functional (headless GPU may not work)"
    fi
fi

echo ""
log "Starting OpenDCC web server..."
echo ""

# ── Exec the CMD ──────────────────────────────────────────────────────────────
exec "$@"

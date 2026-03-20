#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# setup_nvidia_docker.sh — Install & verify NVIDIA Container Toolkit on Rocky 9
#
# Prerequisites (host):
#   - NVIDIA GPU with driver ≥ 525 (for CUDA 12+)
#   - Docker CE or Podman installed
#
# Usage:
#   sudo bash scripts/setup_nvidia_docker.sh
#
# What it does:
#   1. Detects NVIDIA driver on host
#   2. Installs nvidia-container-toolkit from NVIDIA repo
#   3. Configures Docker runtime
#   4. Tests GPU access inside a container
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[setup]${NC} $*"; }
warn() { echo -e "${YELLOW}[setup]${NC} $*"; }
fail() { echo -e "${RED}[setup]${NC} $*"; exit 1; }
info() { echo -e "${CYAN}[setup]${NC} $*"; }

# ── Must be root ──────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    fail "This script must be run as root (sudo)."
fi

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  NVIDIA Container Toolkit — Setup for OpenDCC GPU Docker"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# ── Step 1: Detect NVIDIA driver ─────────────────────────────────────────────
log "Step 1: Detecting NVIDIA driver..."

if ! command -v nvidia-smi &>/dev/null; then
    fail "nvidia-smi not found. Install the NVIDIA driver first:
    dnf install -y nvidia-driver nvidia-driver-cuda"
fi

DRIVER_VER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)
GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)
GPU_MEM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader | head -1)
GPU_COUNT=$(nvidia-smi --query-gpu=count --format=csv,noheader | head -1)

info "  GPU:    ${GPU_NAME} (${GPU_MEM})"
info "  Driver: ${DRIVER_VER}"
info "  Count:  ${GPU_COUNT}"

DRIVER_MAJOR="${DRIVER_VER%%.*}"
if [[ "$DRIVER_MAJOR" -lt 525 ]]; then
    warn "Driver ${DRIVER_VER} is old. Recommend ≥ 525 for CUDA 12+ / Hydra Storm."
fi

echo ""

# ── Step 2: Install nvidia-container-toolkit ──────────────────────────────────
log "Step 2: Installing nvidia-container-toolkit..."

if rpm -q nvidia-container-toolkit &>/dev/null; then
    NCT_VER=$(rpm -q nvidia-container-toolkit)
    info "  Already installed: ${NCT_VER}"
else
    # Add NVIDIA container toolkit repo
    if [[ ! -f /etc/yum.repos.d/nvidia-container-toolkit.repo ]]; then
        log "  Adding NVIDIA container toolkit repository..."
        curl -fsSL https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo \
            | tee /etc/yum.repos.d/nvidia-container-toolkit.repo > /dev/null
    fi

    log "  Installing..."
    dnf install -y nvidia-container-toolkit

    NCT_VER=$(rpm -q nvidia-container-toolkit)
    info "  Installed: ${NCT_VER}"
fi

echo ""

# ── Step 3: Configure Docker runtime ─────────────────────────────────────────
log "Step 3: Configuring Docker runtime..."

if command -v docker &>/dev/null; then
    # Configure the NVIDIA runtime for Docker
    nvidia-ctk runtime configure --runtime=docker 2>/dev/null || true

    # Restart Docker to pick up the new runtime
    if systemctl is-active --quiet docker; then
        log "  Restarting Docker daemon..."
        systemctl restart docker
        sleep 2
    fi

    # Verify runtime is registered
    if docker info 2>/dev/null | grep -q "nvidia"; then
        info "  Docker NVIDIA runtime: configured ✓"
    else
        warn "  Docker NVIDIA runtime not detected in 'docker info'."
        warn "  You may need to manually add to /etc/docker/daemon.json:"
        warn '    {"runtimes": {"nvidia": {"path": "nvidia-container-runtime"}}}'
    fi
else
    warn "  Docker not found. If using Podman, configure CDI manually:"
    warn "    nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml"
fi

echo ""

# ── Step 4: Test GPU access in container ──────────────────────────────────────
log "Step 4: Testing GPU access inside a container..."

if command -v docker &>/dev/null; then
    TEST_OUTPUT=$(docker run --rm --gpus all rockylinux:9-minimal \
        bash -c "cat /proc/driver/nvidia/version 2>/dev/null && echo 'GPU_OK'" 2>&1) || true

    if echo "$TEST_OUTPUT" | grep -q "GPU_OK"; then
        info "  Container GPU access: working ✓"
        echo "$TEST_OUTPUT" | grep -v "GPU_OK" | head -2 | sed 's/^/    /'
    else
        warn "  Container GPU test failed. Output:"
        echo "$TEST_OUTPUT" | head -5 | sed 's/^/    /'
        warn "  Try: docker run --rm --gpus all nvidia/cuda:12.3.2-base-rockylinux9 nvidia-smi"
    fi
else
    warn "  Skipping container test (Docker not available)."
fi

echo ""

# ── Step 5: Test nvidia-smi in container ──────────────────────────────────────
log "Step 5: Full nvidia-smi test..."

if command -v docker &>/dev/null; then
    if docker run --rm --gpus all nvidia/cuda:12.3.2-base-rockylinux9 nvidia-smi 2>/dev/null; then
        info "  nvidia-smi in container: working ✓"
    else
        warn "  nvidia-smi test failed. This may be a driver/toolkit version mismatch."
    fi
fi

echo ""

# ── Summary ───────────────────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════════════"
log "Setup complete!"
echo ""
info "Next steps:"
info "  1. Build the OpenDCC image:    cd docker && bash build.sh rocky9"
info "  2. Start the GPU service:      docker compose up rocky9-gpu"
info "  3. Verify GPU in container:    docker exec opendcc-rocky9-gpu nvidia-smi"
info "  4. Check health endpoint:      curl http://localhost:8081/health"
echo "═══════════════════════════════════════════════════════════════"
echo ""

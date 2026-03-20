# OpenDCC — GPU Docker Setup Guide

## Prerequisites

### Host Requirements
- **NVIDIA GPU** (Turing or newer recommended — RTX 2000+, Tesla T4+)
- **NVIDIA Driver** ≥ 525 (for CUDA 12+ / Hydra Storm)
- **Docker CE** with compose plugin
- **Rocky Linux 9** (or compatible EL9 host)

### Quick Check
```bash
# Verify GPU is visible
nvidia-smi

# Verify Docker is running
docker info
```

---

## 1. Install NVIDIA Container Toolkit

Run the automated setup script:
```bash
sudo bash scripts/setup_nvidia_docker.sh
```

This will:
1. Detect your NVIDIA driver and GPU
2. Install `nvidia-container-toolkit` from NVIDIA's repo
3. Configure the Docker runtime
4. Test GPU access inside a container

### Manual Installation (if script fails)
```bash
# Add NVIDIA repo
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo \
    | sudo tee /etc/yum.repos.d/nvidia-container-toolkit.repo

# Install
sudo dnf install -y nvidia-container-toolkit

# Configure Docker
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# Test
docker run --rm --gpus all nvidia/cuda:12.3.2-base-rockylinux9 nvidia-smi
```

---

## 2. Build & Run

```bash
cd ShapeFX/OpenDCC/docker

# Step 1: Build the base image (if not already built, ~1h first time)
bash build.sh rocky9

# Step 2: Build the GPU image (fast, ~2min — extends rocky9)
bash build.sh rocky9-gpu

# Step 3: Start
docker compose up -d rocky9-gpu
```

The GPU service runs on **port 8081**: http://localhost:8081

---

## 3. Verify

### Health Endpoint
```bash
curl -s http://localhost:8081/health | python3 -m json.tool
```

Expected output:
```json
{
    "status": "ok",
    "opendcc": false,
    "pxr": true,
    "usd_wasm_found": true,
    "gpu": {
        "gpu_available": true,
        "gpu_name": "NVIDIA RTX A4000",
        "gpu_memory": "16384 MiB",
        "gpu_driver": "535.183.01",
        "gpu_count": 1,
        "render_mode": "server_gpu"
    }
}
```

### Render Status
```bash
curl -s http://localhost:8081/api/render/status | python3 -m json.tool
```

### Server-Side Snapshot
```bash
# Render the current stage as PNG (1280×720)
curl -o snapshot.png "http://localhost:8081/api/render/snapshot?width=1280&height=720"
```

### Container GPU Access
```bash
docker exec opendcc-rocky9-gpu nvidia-smi
```

---

## 4. Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Browser (any OS)                                       │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │  Three.js    │  │ USD WASM     │  │ Outliner /    │  │
│  │  Viewport    │  │ Hydra        │  │ Properties    │  │
│  │  (client)    │  │ (client)     │  │ (REST+WS)     │  │
│  └──────┬───────┘  └──────┬───────┘  └───────┬───────┘  │
│         │                 │                   │          │
│    ─────┴─────────────────┴───────────────────┴────      │
│                    WebSocket + REST                       │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────┴────────────────────────────────┐
│  Docker Container (Rocky 9 + GPU)                        │
│  ┌──────────────────┐  ┌──────────────────────────────┐  │
│  │  FastAPI Server   │  │  OpenUSD + Hydra Storm       │  │
│  │  (server.py)      │  │  (EGL headless GPU render)   │  │
│  │                   │  │                              │  │
│  │  /api/render/     │──│  FrameRecorder → PNG         │  │
│  │    snapshot       │  │  UsdImagingGL → pixels        │  │
│  │                   │  │                              │  │
│  │  /api/stage/      │  │  Stage manipulation          │  │
│  │    export.usda    │  │  (pxr Python bindings)       │  │
│  └──────────────────┘  └──────────────────────────────┘  │
│                                                          │
│  NVIDIA GPU (passthrough via nvidia-container-toolkit)    │
└──────────────────────────────────────────────────────────┘
```

### Rendering Modes

| Mode | When | Rendering |
|---|---|---|
| **Server GPU** | GPU container + NVIDIA | Hydra Storm via EGL → PNG snapshots |
| **Client WASM** | Any container, any browser | USD WASM + Three.js (default viewport) |
| **Hybrid** | GPU container | WASM viewport (interactive) + server snapshots (high-quality) |

The system auto-detects GPU availability. If no GPU is found, it falls back
gracefully to client-side WASM rendering with no user action required.

---

## 5. Troubleshooting

### "GPU not available" in /health
```bash
# Check host GPU
nvidia-smi

# Check container runtime
docker info | grep -i nvidia

# Check container GPU access
docker run --rm --gpus all rockylinux:9-minimal nvidia-smi
```

### "EGL not available"
```bash
# Inside container
docker exec opendcc-rocky9-gpu eglinfo
docker exec opendcc-rocky9-gpu ls /usr/share/glvnd/egl_vendor.d/
```

### Render snapshot returns 501
This means GPU rendering modules are not importable. Check:
```bash
docker exec opendcc-rocky9-gpu python3 -c "from pxr import UsdImagingGL; print('OK')"
docker exec opendcc-rocky9-gpu python3 -c "from pxr import UsdAppUtils; print('OK')"
```

---

## 6. Smoke Tests

Run the automated test suite:
```bash
# Test web-only (no GPU needed)
bash scripts/test_docker.sh rocky9-web

# Test full USD backend
bash scripts/test_docker.sh rocky9

# Test GPU variant
bash scripts/test_docker.sh rocky9-gpu
```

#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# build.sh  —  Docker build wrapper for OpenDCC
#
# Creates stub directories for optional dependencies (usd-viewer) if they
# don't exist, then runs docker compose build.
#
# Usage:
#   cd OpenDCC/docker/
#   bash build.sh [service...]        # e.g. bash build.sh rocky9-web
#   bash build.sh --all               # build everything
#
# The build context is ShapeFX/ (two levels up from this script).
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTEXT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

log()  { echo -e "\033[1;32m[build]\033[0m $*"; }
warn() { echo -e "\033[1;33m[build]\033[0m $*"; }

# ── Ensure usd-viewer stub dirs exist in the build context ────────────────────
# Docker COPY fails if the source path doesn't exist at all, even with wildcards.
# We create empty stubs so the Dockerfiles can COPY src* / module* safely.
USD_WASM_SRC="$CONTEXT_DIR/usd-viewer/usd-wasm/src"
USD_MODULES="$CONTEXT_DIR/usd-viewer/public/modules"

CREATED_STUBS=0
if [[ ! -d "$USD_WASM_SRC" ]]; then
    warn "usd-viewer/usd-wasm/src not found — creating empty stub"
    warn "(Clone https://github.com/needle-tools/usd-viewer next to OpenDCC for WASM viewport)"
    mkdir -p "$USD_WASM_SRC"
    CREATED_STUBS=1
fi
if [[ ! -d "$USD_MODULES" ]]; then
    warn "usd-viewer/public/modules not found — creating empty stub"
    mkdir -p "$USD_MODULES"
    CREATED_STUBS=1
fi

if [[ "$CREATED_STUBS" -eq 1 ]]; then
    log "Stub directories created. WASM viewport will not be available."
    log "To enable it, clone usd-viewer:"
    log "  cd $CONTEXT_DIR && git clone https://github.com/needle-tools/usd-viewer.git"
    echo ""
fi

# ── Ensure stages directory exists for volume mounts ──────────────────────────
mkdir -p "$SCRIPT_DIR/stages"

# ── Run docker compose ────────────────────────────────────────────────────────
cd "$SCRIPT_DIR"

if [[ "${1:-}" == "--all" ]]; then
    log "Building all services..."
    docker compose build
elif [[ $# -gt 0 ]]; then
    log "Building: $*"
    docker compose build "$@"
else
    log "Building rocky9-web (default)..."
    docker compose build rocky9-web
fi

log "Done! Run 'docker compose up <service>' to start."

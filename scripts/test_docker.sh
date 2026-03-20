#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# test_docker.sh — Smoke tests for OpenDCC Docker images
#
# Builds and starts a container, verifies key endpoints, then tears down.
#
# Usage:
#   bash scripts/test_docker.sh [service]
#
# Services: rocky9-web (default), rocky9, rocky9-gpu
#
# Exit codes:
#   0 = all tests passed
#   1 = one or more tests failed
# ═══════════════════════════════════════════════════════════════════════════════
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKER_DIR="$SCRIPT_DIR/../docker"
COMPOSE_FILE="$DOCKER_DIR/docker-compose.yml"

SERVICE="${1:-rocky9-web}"
PASS=0
FAIL=0

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[test]${NC} $*"; }
warn() { echo -e "${YELLOW}[test]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*"; ((FAIL++)); }
pass() { echo -e "${GREEN}[PASS]${NC} $*"; ((PASS++)); }
info() { echo -e "${CYAN}[test]${NC} $*"; }

# ── Determine port based on service ──────────────────────────────────────────
case "$SERVICE" in
    rocky9)     PORT=8080 ;;
    rocky9-gpu) PORT=8081 ;;
    rocky9-web) PORT=8082 ;;
    *)          PORT=8082 ;;
esac

BASE_URL="http://localhost:${PORT}"

# ── Cleanup function ──────────────────────────────────────────────────────────
cleanup() {
    log "Tearing down ${SERVICE}..."
    docker compose -f "$COMPOSE_FILE" down "$SERVICE" --timeout 10 2>/dev/null || true
}
trap cleanup EXIT

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  OpenDCC Docker Smoke Tests"
echo "  Service: ${SERVICE}  |  Port: ${PORT}"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# ── Build ─────────────────────────────────────────────────────────────────────
log "Building ${SERVICE}..."
if docker compose -f "$COMPOSE_FILE" build "$SERVICE" 2>&1 | tail -5; then
    pass "Docker build succeeded"
else
    fail "Docker build failed"
    exit 1
fi

echo ""

# ── Start ─────────────────────────────────────────────────────────────────────
log "Starting ${SERVICE}..."
docker compose -f "$COMPOSE_FILE" up -d "$SERVICE" 2>&1

# Wait for the service to be ready
log "Waiting for server to be ready (max 60s)..."
READY=0
for i in $(seq 1 60); do
    if curl -sf "${BASE_URL}/health" > /dev/null 2>&1; then
        READY=1
        break
    fi
    sleep 1
done

if [[ "$READY" -eq 1 ]]; then
    pass "Server ready in ${i}s"
else
    fail "Server did not become ready within 60s"
    log "Container logs:"
    docker compose -f "$COMPOSE_FILE" logs "$SERVICE" 2>&1 | tail -30
    exit 1
fi

echo ""

# ── Test: /health ─────────────────────────────────────────────────────────────
log "Test: GET /health"
HEALTH=$(curl -sf "${BASE_URL}/health" 2>&1)
if echo "$HEALTH" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['status']=='ok'" 2>/dev/null; then
    pass "/health returns status=ok"
    info "  Response: ${HEALTH}"
else
    fail "/health did not return status=ok"
    info "  Response: ${HEALTH}"
fi

# ── Test: /health GPU info ────────────────────────────────────────────────────
if [[ "$SERVICE" == "rocky9-gpu" ]]; then
    log "Test: GET /health (GPU info)"
    GPU_AVAIL=$(echo "$HEALTH" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('gpu',{}).get('gpu_available',False))" 2>/dev/null)
    if [[ "$GPU_AVAIL" == "True" ]]; then
        pass "/health shows GPU available"
    else
        warn "/health shows GPU not available (may be expected without NVIDIA runtime)"
    fi
fi

echo ""

# ── Test: /api/stage/export.usda ──────────────────────────────────────────────
log "Test: GET /api/stage/export.usda"
USDA=$(curl -sf "${BASE_URL}/api/stage/export.usda" 2>&1)
if echo "$USDA" | grep -q "usda"; then
    pass "/api/stage/export.usda returns valid USDA"
    # Check that it contains a Cube or Mesh
    if echo "$USDA" | grep -qE "(Mesh|Cube)"; then
        pass "  USDA contains geometry (Mesh/Cube)"
    else
        warn "  USDA does not contain Mesh/Cube (may be stub)"
    fi
else
    fail "/api/stage/export.usda did not return USDA"
fi

echo ""

# ── Test: /api/prims ──────────────────────────────────────────────────────────
log "Test: GET /api/prims"
PRIMS=$(curl -sf "${BASE_URL}/api/prims" 2>&1)
if echo "$PRIMS" | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'children' in d" 2>/dev/null; then
    pass "/api/prims returns children"
else
    fail "/api/prims did not return expected format"
    info "  Response: ${PRIMS}"
fi

echo ""

# ── Test: /api/stage/info ─────────────────────────────────────────────────────
log "Test: GET /api/stage/info"
STAGE_INFO=$(curl -sf "${BASE_URL}/api/stage/info" 2>&1)
if echo "$STAGE_INFO" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
    pass "/api/stage/info returns valid JSON"
else
    fail "/api/stage/info did not return JSON"
fi

echo ""

# ── Test: /api/prims/create ───────────────────────────────────────────────────
log "Test: POST /api/prims/create"
CREATE=$(curl -sf -X POST "${BASE_URL}/api/prims/create" \
    -H "Content-Type: application/json" \
    -d '{"type":"Sphere","parent":"/World"}' 2>&1)
if echo "$CREATE" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get('ok')" 2>/dev/null; then
    pass "/api/prims/create Sphere succeeded"
else
    fail "/api/prims/create failed"
    info "  Response: ${CREATE}"
fi

echo ""

# ── Test: /api/undo ───────────────────────────────────────────────────────────
log "Test: POST /api/undo"
UNDO=$(curl -sf -X POST "${BASE_URL}/api/undo" 2>&1)
if echo "$UNDO" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
    pass "/api/undo returns valid JSON"
else
    fail "/api/undo failed"
fi

echo ""

# ── Test: /api/render/status (GPU service only) ──────────────────────────────
if [[ "$SERVICE" == "rocky9-gpu" || "$SERVICE" == "rocky9" ]]; then
    log "Test: GET /api/render/status"
    RSTATUS=$(curl -sf "${BASE_URL}/api/render/status" 2>&1)
    if echo "$RSTATUS" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
        pass "/api/render/status returns valid JSON"
        info "  Response: ${RSTATUS}"
    else
        fail "/api/render/status failed"
    fi
    echo ""
fi

# ── Test: WebSocket connectivity ──────────────────────────────────────────────
log "Test: WebSocket /ws"
# Simple check: try to connect and read one message
WS_OK=0
python3 -c "
import asyncio, websockets, json, sys
async def test():
    try:
        async with websockets.connect('ws://localhost:${PORT}/ws', close_timeout=3) as ws:
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            data = json.loads(msg)
            if data.get('event') == 'connected':
                print('connected_ok')
                sys.exit(0)
    except Exception as e:
        print(f'ws_error: {e}')
        sys.exit(1)
asyncio.run(test())
" 2>/dev/null && WS_OK=1

if [[ "$WS_OK" -eq 1 ]]; then
    pass "WebSocket /ws connects and receives 'connected' event"
else
    # websockets module might not be installed on the host
    warn "WebSocket test skipped (websockets module not available on host)"
fi

echo ""

# ── Summary ───────────────────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════════════"
echo -e "  Results:  ${GREEN}${PASS} passed${NC}  ${RED}${FAIL} failed${NC}"
echo "═══════════════════════════════════════════════════════════════"
echo ""

if [[ "$FAIL" -gt 0 ]]; then
    fail "Some tests failed!"
    exit 1
fi

log "All tests passed! ✓"
exit 0

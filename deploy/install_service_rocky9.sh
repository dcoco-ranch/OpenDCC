#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# install_service_rocky9.sh  —  Install OpenDCC as a systemd service
#
# Usage:  sudo bash deploy/install_service_rocky9.sh
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log()  { echo -e "\033[1;32m[install]\033[0m $*"; }
err()  { echo -e "\033[1;31m[install]\033[0m $*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || err "Must run as root (sudo)"

# ── Create service user ───────────────────────────────────────────────────────
if ! id opendcc &>/dev/null; then
    log "Creating system user 'opendcc'..."
    useradd --system --shell /sbin/nologin --home-dir /opt/opendcc \
            --comment "OpenDCC Web Server" opendcc
fi

# ── Create data directories ──────────────────────────────────────────────────
log "Creating data directories..."
mkdir -p /data/stages
chown opendcc:opendcc /data/stages

# ── Create sysconfig override file ────────────────────────────────────────────
if [[ ! -f /etc/sysconfig/opendcc ]]; then
    log "Creating /etc/sysconfig/opendcc (environment overrides)..."
    cat > /etc/sysconfig/opendcc <<'EOF'
# Override OpenDCC environment variables here.
# These are loaded by the systemd service.
#
# OPENDCC_STAGES_ROOT=/data/stages
# USD_VIEWER_PATH=/opt/usd-viewer
EOF
fi

# ── Install systemd unit ─────────────────────────────────────────────────────
log "Installing systemd unit..."
cp "$SCRIPT_DIR/opendcc-web.service" /etc/systemd/system/opendcc-web.service
chmod 644 /etc/systemd/system/opendcc-web.service
systemctl daemon-reload

# ── Firewall ──────────────────────────────────────────────────────────────────
if command -v firewall-cmd &>/dev/null; then
    log "Opening port 8080 in firewall..."
    firewall-cmd --permanent --add-port=8080/tcp 2>/dev/null || true
    firewall-cmd --reload 2>/dev/null || true
fi

# ── SELinux ───────────────────────────────────────────────────────────────────
if command -v getenforce &>/dev/null && [[ "$(getenforce)" != "Disabled" ]]; then
    log "Configuring SELinux..."
    # Allow httpd-like network binding
    setsebool -P httpd_can_network_connect 1 2>/dev/null || true
    # Label the data directory
    semanage fcontext -a -t httpd_sys_rw_content_t "/data/stages(/.*)?" 2>/dev/null || true
    restorecon -Rv /data/stages 2>/dev/null || true
fi

# ── Enable & start ────────────────────────────────────────────────────────────
log "Enabling and starting opendcc-web.service..."
systemctl enable opendcc-web.service
systemctl start opendcc-web.service

log ""
log "════════════════════════════════════════════════════════════════"
log "  OpenDCC Web Server installed as systemd service"
log ""
log "  Status:   systemctl status opendcc-web"
log "  Logs:     journalctl -u opendcc-web -f"
log "  Restart:  systemctl restart opendcc-web"
log "  URL:      http://$(hostname -I | awk '{print $1}'):8080"
log "════════════════════════════════════════════════════════════════"

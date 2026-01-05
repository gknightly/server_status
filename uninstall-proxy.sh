#!/bin/bash
#
# uninstall-proxy.sh - Remove the nftables-proxy service
#
# Usage: sudo ./uninstall-proxy.sh
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1"
    exit 1
}

# Check for root
if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root (use sudo)"
fi

# Stop the service
info "Stopping nftables-proxy service..."
systemctl stop nftables-proxy 2>/dev/null || true

# Disable the service
info "Disabling nftables-proxy service..."
systemctl disable nftables-proxy 2>/dev/null || true

# Remove systemd service file
info "Removing systemd service file..."
rm -f /etc/systemd/system/nftables-proxy.service

# Reload systemd
info "Reloading systemd..."
systemctl daemon-reload

# Remove installation directory
info "Removing installation directory..."
rm -rf /opt/nftables-proxy

# Clean up nftables table
info "Cleaning up nftables rules..."
nft delete table ip minecraft_proxy 2>/dev/null || true

# Restore DOCKER-USER chain to default (just a return rule)
info "Restoring DOCKER-USER chain to default..."
if nft list chain ip filter DOCKER-USER &>/dev/null; then
    nft flush chain ip filter DOCKER-USER
    nft add rule ip filter DOCKER-USER counter return
    info "DOCKER-USER chain restored"
fi

# Remove socket directory if it exists
rm -rf /run/nftables-proxy

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Uninstallation complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "The nftables-proxy service has been removed."
echo ""
echo "Don't forget to:"
echo "  1. Set PROXY_MODE to false in config.json"
echo "  2. Remove the socket mount from docker-compose.yml (optional)"
echo "  3. Restart the Discord bot if it's running"
echo ""

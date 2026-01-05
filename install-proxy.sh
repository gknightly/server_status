#!/bin/bash
#
# install-proxy.sh - Install the nftables-proxy service for Minecraft server routing
#
# This script installs a minimal privileged service that manages nftables rules
# for proxying Minecraft traffic. The Discord bot communicates with this service
# via a Unix socket to update routing when servers start/stop.
#
# Usage: sudo ./install-proxy.sh
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

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROXY_DIR="$SCRIPT_DIR/proxy"

# Check that proxy files exist
if [[ ! -f "$PROXY_DIR/nftables-proxy.py" ]]; then
    error "Cannot find proxy/nftables-proxy.py. Run this script from the project directory."
fi

if [[ ! -f "$PROXY_DIR/nftables-proxy.service" ]]; then
    error "Cannot find proxy/nftables-proxy.service. Run this script from the project directory."
fi

# Check dependencies
info "Checking dependencies..."

if ! command -v python3 &> /dev/null; then
    error "Python 3 is required but not installed"
fi

if ! command -v nft &> /dev/null; then
    error "nftables is required but not installed. Install with: apt install nftables"
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
info "Found Python $PYTHON_VERSION"

# Create installation directory
INSTALL_DIR="/opt/nftables-proxy"
info "Creating installation directory: $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"

# Copy the proxy script
info "Installing nftables-proxy.py..."
cp "$PROXY_DIR/nftables-proxy.py" "$INSTALL_DIR/nftables-proxy.py"
chmod 755 "$INSTALL_DIR/nftables-proxy.py"

# Install systemd service
info "Installing systemd service..."
cp "$PROXY_DIR/nftables-proxy.service" /etc/systemd/system/nftables-proxy.service

# Reload systemd
info "Reloading systemd..."
systemctl daemon-reload

# Enable the service
info "Enabling nftables-proxy service..."
systemctl enable nftables-proxy

# Start the service
info "Starting nftables-proxy service..."
systemctl start nftables-proxy

# Check status
if systemctl is-active --quiet nftables-proxy; then
    info "Service started successfully!"
else
    error "Service failed to start. Check: journalctl -u nftables-proxy"
fi

# Verify socket was created with correct permissions
SOCKET_PATH="/run/nftables-proxy/proxy.sock"
info "Verifying socket at $SOCKET_PATH..."
sleep 1  # Brief wait for socket creation

if [[ -S "$SOCKET_PATH" ]]; then
    info "Socket created successfully"
    # The service handles group ownership via --group flag
    if ! getent group docker > /dev/null 2>&1; then
        warn "Docker group not found. The bot may not be able to connect to the socket."
        warn "Create the group or use --group flag to specify a different group."
    fi
else
    warn "Socket not found at $SOCKET_PATH. Check: journalctl -u nftables-proxy"
fi

# Print summary
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Installation complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "The nftables-proxy service is now running."
echo ""
echo "Next steps:"
echo "  1. Set PROXY_MODE to true in config.json"
echo "  2. Restart the Discord bot: docker compose restart"
echo ""
echo "Useful commands:"
echo "  - Check status:  systemctl status nftables-proxy"
echo "  - View logs:     journalctl -u nftables-proxy -f"
echo "  - Restart:       systemctl restart nftables-proxy"
echo "  - Test socket:   echo 'STATUS' | nc -U /run/nftables-proxy/proxy.sock"
echo ""

#!/bin/bash
#
# docker-forward.sh - Configure DOCKER-USER chain for proxy forwarding
#
# Configures nftables rules in Docker's DOCKER-USER chain to allow proxied
# Minecraft traffic to be forwarded to backend servers.
#

set -e

# Wait briefly for DOCKER-USER chain (Docker creates it during startup)
for i in {1..10}; do
    if nft list chain ip filter DOCKER-USER &>/dev/null; then
        break
    fi
    sleep 0.5
done

if ! nft list chain ip filter DOCKER-USER &>/dev/null; then
    echo "DOCKER-USER chain not found" >&2
    exit 1
fi

nft flush chain ip filter DOCKER-USER
nft add rule ip filter DOCKER-USER ct state established,related counter accept
nft add rule ip filter DOCKER-USER tcp dport 25565 counter accept
nft add rule ip filter DOCKER-USER udp dport 24454 counter accept
nft add rule ip filter DOCKER-USER counter return

#!/usr/bin/env python3
"""
nftables-proxy: A minimal privileged service for managing Minecraft proxy routing.

This service listens on a Unix socket and accepts commands to update nftables
DNAT rules for proxying Minecraft traffic. It runs as root but only accepts
a limited set of validated commands.

Protocol (line-based over Unix socket):
    SET <ip> <port>  - Set DNAT destination
    CLEAR            - Remove DNAT rule (reject connections)
    GET              - Get current destination (returns "ip:port" or "none")
    STATUS           - Get service status

Responses:
    OK               - Command succeeded
    OK <data>        - Command succeeded with data
    ERR <message>    - Command failed
"""
from __future__ import annotations

import argparse
import asyncio
import grp
import ipaddress
import logging
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Configuration
DEFAULT_SOCKET_PATH = "/run/nftables-proxy/proxy.sock"
DEFAULT_SOCKET_GROUP = "docker"
DEFAULT_LISTEN_PORT = 25565
TABLE_NAME = "minecraft_proxy"
CHAIN_PREROUTING = "prerouting"
CHAIN_POSTROUTING = "postrouting"


@dataclass
class ProxyState:
    """Current proxy routing state."""

    destination_ip: str | None = None
    destination_port: int | None = None

    @property
    def is_active(self) -> bool:
        return self.destination_ip is not None


def validate_ip(ip: str) -> bool:
    """Validate an IPv4 address."""
    try:
        addr = ipaddress.ip_address(ip)
        # Only allow IPv4, non-loopback, non-multicast
        return (
            isinstance(addr, ipaddress.IPv4Address)
            and not addr.is_loopback
            and not addr.is_multicast
            and not addr.is_reserved
        )
    except ValueError:
        return False


def validate_port(port: str) -> int | None:
    """Validate a port number. Returns port as int or None if invalid."""
    try:
        p = int(port)
        if 1 <= p <= 65535:
            return p
    except ValueError:
        pass
    return None


def run_nft(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run an nft command."""
    cmd = ["nft"] + args
    logging.debug(f"Running: {' '.join(cmd)}")
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def ensure_table_exists(listen_port: int) -> None:
    """Create the nftables table and chains if they don't exist."""
    # Check if table exists
    result = run_nft(["list", "table", "ip", TABLE_NAME], check=False)
    if result.returncode == 0:
        logging.info(f"Table {TABLE_NAME} already exists")
        return

    logging.info(f"Creating nftables table {TABLE_NAME}")

    # Create table and chains atomically
    nft_script = f"""
table ip {TABLE_NAME} {{
    chain {CHAIN_PREROUTING} {{
        type nat hook prerouting priority dstnat; policy accept;
        tcp dport {listen_port} reject with tcp reset
    }}

    chain {CHAIN_POSTROUTING} {{
        type nat hook postrouting priority srcnat; policy accept;
        masquerade
    }}
}}
"""
    proc = subprocess.run(
        ["nft", "-f", "-"],
        input=nft_script,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        logging.error(f"Failed to create table: {proc.stderr}")
        raise RuntimeError(f"Failed to create nftables table: {proc.stderr}")


def set_destination(ip: str, port: int, listen_port: int) -> None:
    """Set the DNAT destination."""
    logging.info(f"Setting destination to {ip}:{port}")

    # Replace the prerouting chain with the new rule
    nft_script = f"""
flush chain ip {TABLE_NAME} {CHAIN_PREROUTING}
add rule ip {TABLE_NAME} {CHAIN_PREROUTING} tcp dport {listen_port} dnat to {ip}:{port}
"""
    proc = subprocess.run(
        ["nft", "-f", "-"],
        input=nft_script,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        logging.error(f"Failed to set destination: {proc.stderr}")
        raise RuntimeError(f"Failed to set destination: {proc.stderr}")


def clear_destination(listen_port: int) -> None:
    """Clear the DNAT destination (reject connections)."""
    logging.info("Clearing destination (rejecting connections)")

    nft_script = f"""
flush chain ip {TABLE_NAME} {CHAIN_PREROUTING}
add rule ip {TABLE_NAME} {CHAIN_PREROUTING} tcp dport {listen_port} reject with tcp reset
"""
    proc = subprocess.run(
        ["nft", "-f", "-"],
        input=nft_script,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        logging.error(f"Failed to clear destination: {proc.stderr}")
        raise RuntimeError(f"Failed to clear destination: {proc.stderr}")


def enable_ip_forwarding() -> None:
    """Enable IP forwarding if not already enabled."""
    forwarding_path = Path("/proc/sys/net/ipv4/ip_forward")
    current = forwarding_path.read_text().strip()
    if current != "1":
        logging.info("Enabling IP forwarding")
        forwarding_path.write_text("1")
    else:
        logging.debug("IP forwarding already enabled")


def recover_state_from_nftables(listen_port: int) -> ProxyState:
    """Recover proxy state by parsing current nftables rules."""
    result = run_nft(["list", "chain", "ip", TABLE_NAME, CHAIN_PREROUTING], check=False)
    if result.returncode != 0:
        logging.debug("Could not read nftables rules, assuming no active destination")
        return ProxyState()

    # Look for DNAT rule: "dnat to 1.2.3.4:25565"
    match = re.search(r"dnat to (\d+\.\d+\.\d+\.\d+):(\d+)", result.stdout)
    if match:
        ip, port = match.group(1), int(match.group(2))
        logging.info(f"Recovered existing destination from nftables: {ip}:{port}")
        return ProxyState(destination_ip=ip, destination_port=port)

    logging.debug("No active DNAT rule found")
    return ProxyState()


class NFTablesProxyServer:
    """Unix socket server for nftables proxy commands."""

    def __init__(
        self,
        socket_path: str,
        listen_port: int,
        socket_group: str | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.listen_port = listen_port
        self.socket_group = socket_group
        self.state = ProxyState()
        self._server: asyncio.Server | None = None

    async def handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a client connection."""
        peer = writer.get_extra_info("peername") or "unknown"
        logging.debug(f"Client connected: {peer}")

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break

                command = line.decode("utf-8").strip()
                if not command:
                    continue

                logging.debug(f"Received command: {command}")
                response = self.process_command(command)
                logging.debug(f"Response: {response}")

                writer.write((response + "\n").encode("utf-8"))
                await writer.drain()

        except Exception as e:
            logging.error(f"Error handling client: {e}")
        finally:
            writer.close()
            await writer.wait_closed()
            logging.debug(f"Client disconnected: {peer}")

    def process_command(self, command: str) -> str:
        """Process a command and return the response."""
        parts = command.split()
        if not parts:
            return "ERR empty command"

        cmd = parts[0].upper()

        try:
            if cmd == "SET":
                if len(parts) != 3:
                    return "ERR usage: SET <ip> <port>"

                ip = parts[1]
                if not validate_ip(ip):
                    return f"ERR invalid IP address: {ip}"

                port = validate_port(parts[2])
                if port is None:
                    return f"ERR invalid port: {parts[2]}"

                set_destination(ip, port, self.listen_port)
                self.state.destination_ip = ip
                self.state.destination_port = port
                return "OK"

            elif cmd == "CLEAR":
                clear_destination(self.listen_port)
                self.state.destination_ip = None
                self.state.destination_port = None
                return "OK"

            elif cmd == "GET":
                if self.state.is_active:
                    return f"OK {self.state.destination_ip}:{self.state.destination_port}"
                return "OK none"

            elif cmd == "STATUS":
                status = "active" if self.state.is_active else "inactive"
                dest = (
                    f"{self.state.destination_ip}:{self.state.destination_port}"
                    if self.state.is_active
                    else "none"
                )
                return f"OK status={status} destination={dest} listen_port={self.listen_port}"

            else:
                return f"ERR unknown command: {cmd}"

        except Exception as e:
            logging.exception(f"Command failed: {command}")
            return f"ERR {e}"

    async def start(self) -> None:
        """Start the server."""
        socket_path = Path(self.socket_path)

        # Ensure parent directory exists
        socket_path.parent.mkdir(parents=True, exist_ok=True)

        # Remove existing socket file
        if socket_path.exists():
            socket_path.unlink()

        # Initialize nftables
        enable_ip_forwarding()
        ensure_table_exists(self.listen_port)

        # Recover state from existing nftables rules (handles service restart)
        self.state = recover_state_from_nftables(self.listen_port)

        # If no active destination, ensure reject rule is in place
        if not self.state.is_active:
            clear_destination(self.listen_port)

        # Create Unix socket server
        self._server = await asyncio.start_unix_server(
            self.handle_client,
            path=self.socket_path,
        )

        # Set socket permissions (allow group access)
        os.chmod(self.socket_path, 0o660)

        # Set socket group ownership if specified
        if self.socket_group:
            try:
                gid = grp.getgrnam(self.socket_group).gr_gid
                os.chown(self.socket_path, -1, gid)
                logging.info(f"Socket group set to {self.socket_group}")
            except KeyError:
                logging.warning(
                    f"Group '{self.socket_group}' not found, socket will use default group"
                )

        logging.info(f"Listening on {self.socket_path}")

    async def stop(self) -> None:
        """Stop the server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()

        # Clean up socket file
        socket_path = Path(self.socket_path)
        if socket_path.exists():
            socket_path.unlink()

        logging.info("Server stopped")

    async def run_forever(self) -> None:
        """Run the server until interrupted."""
        await self.start()

        # Set up signal handlers
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        def signal_handler() -> None:
            logging.info("Received shutdown signal")
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, signal_handler)

        try:
            await stop_event.wait()
        finally:
            await self.stop()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="nftables proxy service for Minecraft server routing",
    )
    parser.add_argument(
        "--socket",
        default=DEFAULT_SOCKET_PATH,
        help=f"Unix socket path (default: {DEFAULT_SOCKET_PATH})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_LISTEN_PORT,
        help=f"Port to listen on for proxying (default: {DEFAULT_LISTEN_PORT})",
    )
    parser.add_argument(
        "--group",
        default=DEFAULT_SOCKET_GROUP,
        help=f"Group for socket ownership (default: {DEFAULT_SOCKET_GROUP})",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    # Check for root
    if os.geteuid() != 0:
        logging.error("This service must run as root")
        sys.exit(1)

    # Run the server
    server = NFTablesProxyServer(args.socket, args.port, args.group)
    try:
        asyncio.run(server.run_forever())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

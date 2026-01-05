# services/nftables.py
"""Client for the nftables-proxy service."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from errors import ProxyError

DEFAULT_SOCKET_PATH = "/run/nftables-proxy/proxy.sock"


@dataclass
class ProxyStatus:
    """Status information from the proxy service."""

    active: bool
    destination_ip: str | None
    destination_port: int | None
    listen_port: int

    @property
    def destination(self) -> str | None:
        """Return destination as 'ip:port' or None."""
        if self.destination_ip and self.destination_port:
            return f"{self.destination_ip}:{self.destination_port}"
        return None


class NFTablesClient:
    """Async client for communicating with the nftables-proxy service."""

    def __init__(self, socket_path: str = DEFAULT_SOCKET_PATH) -> None:
        self.socket_path = socket_path

    async def _send_command(self, command: str) -> str:
        """Send a command to the proxy service and return the response."""
        socket_path = Path(self.socket_path)
        if not socket_path.exists():
            raise ProxyError(
                f"Proxy socket not found at {self.socket_path}. "
                "Is the nftables-proxy service running?"
            )

        try:
            reader, writer = await asyncio.open_unix_connection(self.socket_path)
        except (ConnectionRefusedError, PermissionError) as e:
            raise ProxyError(f"Cannot connect to proxy service: {e}") from e

        try:
            writer.write((command + "\n").encode("utf-8"))
            await writer.drain()

            response = await asyncio.wait_for(reader.readline(), timeout=5.0)
            return response.decode("utf-8").strip()
        except asyncio.TimeoutError as e:
            raise ProxyError("Proxy service did not respond in time") from e
        finally:
            writer.close()
            await writer.wait_closed()

    def _parse_response(self, response: str) -> str:
        """Parse a response and return the data part, or raise on error."""
        if response.startswith("OK"):
            # "OK" or "OK <data>"
            if len(response) > 3:
                return response[3:]
            return ""
        elif response.startswith("ERR"):
            error_msg = response[4:] if len(response) > 4 else "Unknown error"
            raise ProxyError(f"Proxy error: {error_msg}")
        else:
            raise ProxyError(f"Unexpected response: {response}")

    async def set_destination(self, ip: str, port: int = 25565) -> None:
        """Set the proxy destination to route traffic to the given IP and port."""
        logging.info(f"Setting proxy destination to {ip}:{port}")
        response = await self._send_command(f"SET {ip} {port}")
        self._parse_response(response)
        logging.info(f"Proxy destination set to {ip}:{port}")

    async def clear_destination(self) -> None:
        """Clear the proxy destination (connections will be rejected)."""
        logging.info("Clearing proxy destination")
        response = await self._send_command("CLEAR")
        self._parse_response(response)
        logging.info("Proxy destination cleared")

    async def get_destination(self) -> tuple[str, int] | None:
        """Get the current proxy destination, or None if not set."""
        response = await self._send_command("GET")
        data = self._parse_response(response)

        if data == "none":
            return None

        try:
            ip, port_str = data.split(":")
            return (ip, int(port_str))
        except ValueError as e:
            raise ProxyError(f"Invalid destination format: {data}") from e

    async def get_status(self) -> ProxyStatus:
        """Get the full status of the proxy service."""
        response = await self._send_command("STATUS")
        data = self._parse_response(response)

        # Parse "status=active destination=1.2.3.4:25565 listen_port=25565"
        parts = dict(p.split("=") for p in data.split())

        active = parts.get("status") == "active"
        dest = parts.get("destination", "none")
        listen_port = int(parts.get("listen_port", "25565"))

        if dest == "none":
            return ProxyStatus(
                active=False,
                destination_ip=None,
                destination_port=None,
                listen_port=listen_port,
            )

        ip, port_str = dest.split(":")
        return ProxyStatus(
            active=True,
            destination_ip=ip,
            destination_port=int(port_str),
            listen_port=listen_port,
        )

    async def is_available(self) -> bool:
        """Check if the proxy service is available and responding."""
        try:
            await self.get_status()
            return True
        except ProxyError:
            return False

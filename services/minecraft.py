# services/minecraft.py
"""Minecraft server status checking service."""
from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from mcstatus import JavaServer

from constants import (
    MC_STATUS_TIMEOUT_SEC,
    MC_READY_TIMEOUT_SEC,
    MC_POLL_INITIAL_DELAY_SEC,
    MC_POLL_MAX_DELAY_SEC,
    MC_POLL_BACKOFF_FACTOR,
)


@dataclass
class MCStatus:
    """Status of a Minecraft server."""

    online: bool
    players: int
    player_names: list[str]
    version: str | None
    motd: str | None
    max_players: int | None

    @staticmethod
    def offline() -> MCStatus:
        """Create an offline status."""
        return MCStatus(
            online=False,
            players=0,
            player_names=[],
            version=None,
            motd=None,
            max_players=None,
        )


class MinecraftService:
    """Service for checking Minecraft server status."""

    @staticmethod
    async def check_status(
        ip: str,
        timeout: float = MC_STATUS_TIMEOUT_SEC,
    ) -> MCStatus:
        """
        Check the status of a Minecraft server.

        Args:
            ip: Server IP or hostname (optionally with :port)
            timeout: Timeout in seconds for the status check

        Returns:
            MCStatus with server information, or offline status on failure
        """
        try:
            server = JavaServer.lookup(ip)

            # Run the blocking status call in a thread with timeout
            status = await asyncio.wait_for(
                asyncio.to_thread(server.status),
                timeout=timeout,
            )

            player_names = [p.name for p in (status.players.sample or [])]

            return MCStatus(
                online=True,
                players=status.players.online,
                player_names=player_names,
                version=status.version.name,
                motd=str(status.description) if status.description else None,
                max_players=status.players.max,
            )

        except asyncio.TimeoutError:
            logging.debug(f"MC status timeout for {ip} after {timeout}s")
            return MCStatus.offline()
        except (OSError, socket.error, ConnectionError) as e:
            # Network-related errors
            logging.debug(f"MC network error for {ip}: {e}")
            return MCStatus.offline()
        except ValueError as e:
            # Invalid response from server
            logging.debug(f"MC invalid response for {ip}: {e}")
            return MCStatus.offline()
        except Exception as e:
            # Unexpected error - log at warning level
            logging.warning(f"MC unexpected error for {ip}: {type(e).__name__}: {e}")
            return MCStatus.offline()

    @staticmethod
    async def wait_for_ready(
        ip: str,
        timeout: int = MC_READY_TIMEOUT_SEC,
        progress_callback: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> MCStatus | None:
        """
        Wait for a Minecraft server to become ready using exponential backoff.

        Args:
            ip: Server IP or hostname
            timeout: Maximum time to wait in seconds
            progress_callback: Optional async callback(elapsed_seconds, total_timeout)

        Returns:
            MCStatus if server responds, None on timeout
        """
        elapsed = 0.0
        delay = float(MC_POLL_INITIAL_DELAY_SEC)
        attempt = 0

        logging.info(f"Waiting for Minecraft server at {ip} (timeout: {timeout}s)")

        while elapsed < timeout:
            attempt += 1
            status = await MinecraftService.check_status(ip)

            if status.online:
                logging.info(
                    f"Minecraft server at {ip} responded after {elapsed:.0f}s (attempt {attempt})"
                )
                return status

            # Report progress via async callback (pass elapsed as int for display)
            if progress_callback:
                try:
                    await progress_callback(int(elapsed), timeout)
                except Exception as e:
                    logging.warning(f"Progress callback error: {type(e).__name__}: {e}")

            # Wait with exponential backoff
            await asyncio.sleep(delay)
            elapsed += delay

            # Increase delay for next iteration (capped at max)
            delay = min(delay * MC_POLL_BACKOFF_FACTOR, float(MC_POLL_MAX_DELAY_SEC))

            logging.debug(
                f"MC poll attempt {attempt} for {ip}, elapsed {elapsed:.0f}s, next delay {delay:.1f}s"
            )

        logging.warning(
            f"Minecraft server at {ip} did not respond after {timeout}s ({attempt} attempts)"
        )
        return None

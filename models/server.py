# models/server.py
"""Server state tracking for multi-server monitoring."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import ServerCfg


@dataclass
class ServerState:
    """Tracks runtime state for a single server."""
    server: ServerCfg
    consecutive_empty: int = 0
    last_check: datetime | None = None
    last_player_count: int = 0
    last_command_channel_id: int | None = None  # Store ID instead of object for persistence
    last_started: datetime | None = None
    maintenance: bool = False

    def record_check(self, player_count: int) -> None:
        """Record a monitoring check."""
        self.last_check = datetime.now()
        self.last_player_count = player_count

    def record_start(self) -> None:
        """Record that the server was started."""
        self.last_started = datetime.now()
        self.reset_empty()

    def in_grace_period(self, grace_minutes: int) -> bool:
        """Check if server is within grace period after start."""
        if self.last_started is None:
            return False
        elapsed = datetime.now() - self.last_started
        return elapsed.total_seconds() < grace_minutes * 60

    def increment_empty(self) -> int:
        """Increment empty counter and return new value. Capped to prevent unbounded growth."""
        if self.consecutive_empty < 999:
            self.consecutive_empty += 1
        return self.consecutive_empty

    def reset_empty(self) -> None:
        """Reset the empty counter."""
        self.consecutive_empty = 0

    def set_command_channel(self, channel_id: int) -> None:
        """Remember the channel where a command was issued."""
        self.last_command_channel_id = channel_id

    def toggle_maintenance(self) -> bool:
        """Toggle maintenance mode and return new state."""
        self.maintenance = not self.maintenance
        return self.maintenance


@dataclass
class ServerMonitor:
    """Monitors multiple servers for auto-stop conditions."""
    states: dict[str, ServerState] = field(default_factory=dict)

    @classmethod
    def from_config(cls, servers: dict[str, ServerCfg]) -> "ServerMonitor":
        """Create a monitor from server configuration."""
        states = {name: ServerState(server=cfg) for name, cfg in servers.items()}
        return cls(states=states)

    def get_state(self, server_name: str) -> ServerState | None:
        """Get state for a specific server."""
        return self.states.get(server_name)

    def all_states(self) -> list[ServerState]:
        """Get all server states."""
        return list(self.states.values())

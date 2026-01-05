# config.py
"""Configuration loading and validation."""
from __future__ import annotations

import json
import logging
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from constants import CONFIG_PATH
from errors import ConfigError

# Validation patterns
INSTANCE_ID_PATTERN = re.compile(r"^i-[a-f0-9]{8,17}$")
AWS_REGION_PATTERN = re.compile(r"^[a-z]{2}-[a-z]+-\d$")


def _validate_server(name: str, instance_id: str, region: str, ip: str) -> list[str]:
    """Validate server configuration, returning list of errors."""
    errors = []

    if not name or not name.strip():
        errors.append("Server name cannot be empty")

    if not INSTANCE_ID_PATTERN.match(instance_id):
        errors.append(f"Invalid instance ID '{instance_id}' (expected format: i-xxxxxxxxxx)")

    if not AWS_REGION_PATTERN.match(region):
        errors.append(f"Invalid AWS region '{region}' (expected format: us-east-1)")

    if not ip or not ip.strip():
        errors.append("Server IP cannot be empty")

    return errors


def _check_file_permissions(path: Path) -> None:
    """Warn if config file has overly permissive permissions."""
    try:
        file_stat = path.stat()
        mode = file_stat.st_mode

        # Check if file is world-readable (others have read permission)
        if mode & stat.S_IROTH:
            logging.warning(
                f"Config file '{path}' is world-readable. "
                "This may expose sensitive credentials. "
                "Consider running: chmod 600 config.json"
            )
    except OSError:
        # Can't check permissions (e.g., on Windows), skip the check
        pass


@dataclass(frozen=True, slots=True)
class ServerCfg:
    """Configuration specific to a single Minecraft server instance."""

    name: str
    instance_id: str
    region: str
    ip: str


@dataclass(frozen=True, slots=True)
class BotCfg:
    """Overall bot configuration including credentials and server definitions."""

    token: str
    aws_key: str
    aws_secret: str
    servers: dict[str, ServerCfg]
    single_server_mode: bool = False
    proxy_mode: bool = False

    @staticmethod
    def load(path: Path = CONFIG_PATH) -> BotCfg:
        """Loads configuration from a JSON file, creating a default if none exists."""
        if not path.exists():
            # Create a default configuration file on the first run
            path.write_text(
                json.dumps(
                    {
                        "DISCORD_TOKEN": "xxxxxxxxxxxxxxxx...",
                        "AWS_ACCESS_KEY": "xxxxxxxxxxxx",
                        "AWS_SECRET": "xxxxxxxxxxxxxxxxxxxxxxxx",
                        "SINGLE_SERVER_MODE": False,
                        "PROXY_MODE": False,
                        "servers": {
                            "default": {
                                "INSTANCE_ID": "i-xxxxxxxxxxxx",
                                "AWS_REGION": "us-east-2",
                                "SERVER_IP": "0.0.0.0",
                            }
                        },
                    },
                    indent=4,
                )
            )
            logging.info(f"Default configuration file created at '{path}'. Edit it then restart.")
            sys.exit(0)

        # Check file permissions before loading
        _check_file_permissions(path)

        try:
            raw = json.loads(path.read_text())

            # Validate credentials
            token = raw.get("DISCORD_TOKEN", "")
            aws_key = raw.get("AWS_ACCESS_KEY", "")
            aws_secret = raw.get("AWS_SECRET", "")

            if not token or len(token) < 50:
                raise ConfigError("DISCORD_TOKEN appears invalid (too short or missing)")
            if not aws_key or len(aws_key) < 16:
                raise ConfigError("AWS_ACCESS_KEY appears invalid (too short or missing)")
            if not aws_secret or len(aws_secret) < 20:
                raise ConfigError("AWS_SECRET appears invalid (too short or missing)")

            # Parse and validate servers
            if "servers" not in raw or not raw["servers"]:
                raise ConfigError("No servers defined in configuration")

            servers = {}
            all_errors = []

            for name, s in raw["servers"].items():
                instance_id = s.get("INSTANCE_ID", "")
                region = s.get("AWS_REGION", "")
                ip = s.get("SERVER_IP", "")

                errors = _validate_server(name, instance_id, region, ip)
                if errors:
                    all_errors.extend([f"Server '{name}': {e}" for e in errors])
                else:
                    servers[name] = ServerCfg(name, instance_id, region, ip)

            if all_errors:
                for err in all_errors:
                    logging.error(f"Config validation: {err}")
                raise ConfigError(f"{len(all_errors)} configuration error(s) found")

            # Optional settings
            single_server_mode = raw.get("SINGLE_SERVER_MODE", False)
            proxy_mode = raw.get("PROXY_MODE", False)

            # Validate mode compatibility
            if proxy_mode and not single_server_mode:
                raise ConfigError(
                    "PROXY_MODE requires SINGLE_SERVER_MODE to be enabled. "
                    "The proxy can only route to one server at a time."
                )

            return BotCfg(token, aws_key, aws_secret, servers, single_server_mode, proxy_mode)

        except ConfigError:
            raise
        except json.JSONDecodeError as e:
            logging.error(f"Invalid JSON in '{path}': {e}")
            raise ConfigError(f"Invalid JSON: {e}") from e
        except KeyError as e:
            logging.error(f"Missing required key in '{path}': {e}")
            raise ConfigError(f"Missing required key: {e}") from e
        except FileNotFoundError as e:
            logging.error(f"Configuration file not found at '{path}'.")
            raise ConfigError(f"Config file not found: {path}") from e

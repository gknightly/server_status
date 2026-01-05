# main.py
"""Entry point for the Minecraft server Discord bot."""
import logging
import sys
from pathlib import Path

from bot import MinecraftServerBot
from config import BotCfg
from constants import VERBOSE
from errors import ConfigError
from services.nftables import DEFAULT_SOCKET_PATH


def setup_logging() -> None:
    """Configures application logging based on verbosity."""
    log_level = logging.DEBUG if VERBOSE else logging.INFO
    log_format = "%(asctime)s - %(levelname)s - %(message)s"
    logging.basicConfig(level=log_level, format=log_format)
    # Silence overly verbose libraries if not in verbose mode
    if not VERBOSE:
        logging.getLogger("discord").setLevel(logging.WARNING)
        logging.getLogger("websockets").setLevel(logging.WARNING)
        logging.getLogger("boto3").setLevel(logging.WARNING)
        logging.getLogger("botocore").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)


def check_nftables_service() -> bool:
    """Check if the nftables-proxy socket is available.

    Returns True if the socket exists, False otherwise.
    """
    return Path(DEFAULT_SOCKET_PATH).exists()


if __name__ == "__main__":
    setup_logging()

    logging.info("Application starting...")
    if VERBOSE:
        logging.debug("Verbose mode enabled.")

    # Load configuration
    try:
        config = BotCfg.load()
        logging.info(f"Configuration loaded. {len(config.servers)} server(s) defined.")
    except ConfigError as e:
        logging.error(f"Configuration error: {e}")
        sys.exit(1)
    except Exception as e:
        logging.exception("Unexpected error loading configuration.")
        sys.exit(1)

    # Check nftables service if proxy mode is enabled
    if config.proxy_mode and not check_nftables_service():
        logging.error(
            f"Proxy mode is enabled but the nftables-proxy socket was not found at {DEFAULT_SOCKET_PATH}. "
            "Please ensure the service is running and the socket is mounted into the container."
        )
        sys.exit(1)

    # Create and run the bot instance
    bot = MinecraftServerBot(config)
    bot.run()

    logging.info("Application shutting down.")

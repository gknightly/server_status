# constants.py
import sys
from pathlib import Path

# Path to the configuration file
CONFIG_PATH = Path("config.json")

# Command line flag for verbose logging
VERBOSE = "-v" in sys.argv

# ----- Timeouts -----
EC2_START_TIMEOUT_SEC = 600       # Max wait for instance_running (boto3 waiter default)
EC2_HEALTH_TIMEOUT_SEC = 300      # Max wait for instance_status_ok
MC_READY_TIMEOUT_SEC = 180        # Max wait for Minecraft to respond after EC2 ready
MC_STATUS_TIMEOUT_SEC = 5         # Timeout for single MC status check

# ----- Polling -----
MC_POLL_INITIAL_DELAY_SEC = 2     # Initial delay between MC status polls
MC_POLL_MAX_DELAY_SEC = 15        # Max delay between polls (exponential backoff cap)
MC_POLL_BACKOFF_FACTOR = 1.5      # Multiplier for exponential backoff

# ----- Auto-stop -----
MONITOR_INTERVAL_MIN = 5          # How often to check for empty servers
CONSECUTIVE_EMPTY_LIMIT = 3       # Empty checks before auto-stop
AUTO_STOP_DELAY_MIN = MONITOR_INTERVAL_MIN * CONSECUTIVE_EMPTY_LIMIT  # Total: 15 min
GRACE_PERIOD_MIN = 15             # Skip empty checks after server start
STOP_TIMEOUT_SEC = 120            # Force stop if graceful stop takes too long

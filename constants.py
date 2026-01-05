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
MC_POLL_FAST_INTERVAL_SEC = 3     # Interval during fast polling phase
MC_POLL_FAST_DURATION_SEC = 60    # How long to fast-poll before slowing down
MC_POLL_SLOW_INTERVAL_SEC = 10    # Interval during slow polling phase

# ----- Auto-stop -----
MONITOR_INTERVAL_MIN = 5          # How often to check for empty servers
CONSECUTIVE_EMPTY_LIMIT = 3       # Empty checks before auto-stop
AUTO_STOP_DELAY_MIN = MONITOR_INTERVAL_MIN * CONSECUTIVE_EMPTY_LIMIT  # Total: 15 min
GRACE_PERIOD_MIN = 15             # Skip empty checks after server start
STOP_TIMEOUT_SEC = 120            # Force stop if graceful stop takes too long

# constants.py
from pathlib import Path

# Default shared folder path – change this to match your system
DEFAULT_SHARED_FOLDER = Path(r"G:\Archnemix\Dataset\Shared")
DEFAULT_WSB_FOLDER = Path(r"G:\Archnemix\Dataset")  # Where to save sandbox.wsb

# Timeouts (in seconds)
SERVER_START_TIMEOUT = 180# Max time to wait for sandbox to be ready
TASK_TIMEOUT = 120          # Max time for a single code execution
POLL_INTERVAL = 0.5         # How often to check for files

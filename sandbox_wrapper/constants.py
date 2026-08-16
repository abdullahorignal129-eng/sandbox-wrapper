from pathlib import Path

DEFAULT_SHARED_FOLDER = Path(r"G:\Archnemix\Dataset\Shared")
DEFAULT_WSB_FOLDER = Path(r"G:\Archnemix\Dataset")

SERVER_START_TIMEOUT = 180  # seconds
TASK_TIMEOUT = 300          # bumped up: venv creation + pip install + run can take longer than 120s
POLL_INTERVAL = 0.5

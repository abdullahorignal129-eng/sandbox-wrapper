from pathlib import Path

DEFAULT_SHARED_FOLDER = Path(r"G:\Archnemix\Dataset\Shared")
DEFAULT_WSB_FOLDER = Path(r"G:\Archnemix\Dataset")

SERVER_START_TIMEOUT = 180  # seconds
TASK_TIMEOUT = 120
POLL_INTERVAL = 0.5

# --- Venv pool configuration -------------------------------------------
# Number of pre-warmed venvs kept per Python version. This is the cap
# that acquire() will grow the pool up to on demand.
VENV_POOL_SIZE = 20

# Versions to eagerly pre-create a full pool for the moment the sandbox
# boots, so the first run_code() call for these versions is already fast.
# Any version not listed here still works — its pool is just built lazily
# on first use instead of at startup.
PREWARM_VERSIONS = ["3.12"]

# Version used when a task doesn't specify one.
DEFAULT_PYTHON_VERSION = "3.12"

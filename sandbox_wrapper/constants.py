from pathlib import Path

DEFAULT_SHARED_FOLDER = Path(r"G:\Archnemix\Dataset\Shared")
DEFAULT_WSB_FOLDER = Path(r"G:\Archnemix\Dataset")

SERVER_START_TIMEOUT = 180  # seconds
TASK_TIMEOUT = 120
POLL_INTERVAL = 0.5

# --- Venv pool configuration -------------------------------------------
# Number of pre-warmed venvs kept per Python version by default. This is
# also the maximum number of "run" tasks for that version that can
# execute concurrently -- each task holds one venv for its duration.
# Use SandboxManager.create_venvs()/remove_venvs() to change the ceiling
# for a given version at runtime; concurrency scales with it directly.
VENV_POOL_SIZE = 20

# Versions to eagerly pre-create a full pool for the moment the sandbox
# boots, so the first run_code() call for these versions is already fast
# and already has full concurrency available.
PREWARM_VERSIONS = ["3.12"]

# Version used when a task doesn't specify one.
DEFAULT_PYTHON_VERSION = "3.12"

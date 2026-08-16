from pathlib import Path

DEFAULT_SHARED_FOLDER = Path(r"G:\Archnemix\Dataset\Shared")
DEFAULT_WSB_FOLDER = Path(r"G:\Archnemix\Dataset")

# Map version strings to the host Python installation paths
PYTHON_SOURCE_PATHS = {
    "3.11": Path(r"F:\Apps\Dev\Python\311"),
    "3.12": Path(r"F:\Apps\Dev\Python\312"),
    "3.13": Path(r"F:\Apps\Dev\Python\313"),
    "3.14": Path(r"F:\Apps\Dev\Python\314"),
}

SERVER_START_TIMEOUT = 180   # seconds
TASK_TIMEOUT = 120
POLL_INTERVAL = 0.5

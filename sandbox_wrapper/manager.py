# manager.py - Main SandboxManager class
import json
import subprocess
import time
from pathlib import Path
from typing import Optional, Dict, Any
import sys

from .constants import (
    DEFAULT_SHARED_FOLDER,
    DEFAULT_WSB_FOLDER,
    SERVER_START_TIMEOUT,
    TASK_TIMEOUT,
    POLL_INTERVAL,
)
from .server_script import SERVER_PY
from .wsb_template import WSB_TEMPLATE


class SandboxManager:
    """
    Manages a single Windows Sandbox instance to run Python code
    with multiple versions in isolation.
    """

    def __init__(
        self,
        shared_folder: Optional[Path] = None,
        wsb_folder: Optional[Path] = None,
    ):
        self.shared_folder = Path(shared_folder or DEFAULT_SHARED_FOLDER)
        self.wsb_folder = Path(wsb_folder or DEFAULT_WSB_FOLDER)
        self.wsb_file = self.wsb_folder / "sandbox.wsb"
        self.task_file = self.shared_folder / "task.json"
        self.result_file = self.shared_folder / "result.json"
        self.ready_file = self.shared_folder / "ready.txt"
        self.server_script_file = self.shared_folder / "server.py"

        self._sandbox_process = None
        self._ensure_folders()
        self._prepare_files()

    def _ensure_folders(self):
        """Create the shared and WSB folders if they don't exist."""
        self.shared_folder.mkdir(parents=True, exist_ok=True)
        self.wsb_folder.mkdir(parents=True, exist_ok=True)

    def _prepare_files(self):
        """Write server.py and generate the .wsb file."""
        # Write server.py
        self.server_script_file.write_text(SERVER_PY, encoding='utf-8')

        # Generate .wsb
        wsb_content = WSB_TEMPLATE.format(
            host_folder=str(self.shared_folder),
            sandbox_folder=str(self.shared_folder),
        )
        self.wsb_file.write_text(wsb_content, encoding='utf-8')

    def _is_sandbox_ready(self) -> bool:
        """Check if the sandbox server has started."""
        return self.ready_file.exists()

    def launch(self, timeout: int = SERVER_START_TIMEOUT) -> None:
        """
        Launch the Windows Sandbox and wait for the server to be ready.
        """
        if self._sandbox_process is not None:
            # Check if it's still running
            if self._sandbox_process.poll() is None:
                print("Sandbox already running.")
                return
            else:
                print("Sandbox process died; relaunching.")

        print("Launching Windows Sandbox...")
        self._sandbox_process = subprocess.Popen(
            ["WindowsSandbox.exe", str(self.wsb_file)],
            shell=True,
        )

        # Wait for the ready file
        print(f"Waiting for sandbox to be ready (timeout: {timeout}s)...")
        start = time.time()
        while not self._is_sandbox_ready():
            if time.time() - start > timeout:
                raise TimeoutError("Sandbox did not become ready in time.")
            time.sleep(POLL_INTERVAL)
        print("Sandbox is ready.")

    def run_code(self, code: str, version: str = "3.12") -> Dict[str, Any]:
        """
        Execute Python code inside the sandbox and return the result.
        """
        if not self._is_sandbox_ready():
            raise RuntimeError("Sandbox is not ready. Call .launch() first.")

        # Clean up any stale files
        if self.task_file.exists():
            self.task_file.unlink()
        if self.result_file.exists():
            self.result_file.unlink()

        # Write the task
        task = {"code": code, "version": version}
        self.task_file.write_text(json.dumps(task), encoding='utf-8')

        # Wait for the result file
        start = time.time()
        while not self.result_file.exists():
            if time.time() - start > TASK_TIMEOUT:
                raise TimeoutError(f"Task did not complete within {TASK_TIMEOUT}s.")
            time.sleep(POLL_INTERVAL)

        # Read and return result
        result = json.loads(self.result_file.read_text(encoding='utf-8'))
        self.result_file.unlink()  # Clean up
        return result

    def close(self):
        """Terminate the sandbox process (if still running)."""
        if self._sandbox_process and self._sandbox_process.poll() is None:
            self._sandbox_process.terminate()
            self._sandbox_process.wait(timeout=10)
        # Optionally clean up ready file
        if self.ready_file.exists():
            self.ready_file.unlink()

    def __enter__(self):
        self.launch()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

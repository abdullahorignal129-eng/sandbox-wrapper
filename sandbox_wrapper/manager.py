# manager.py
import json
import subprocess
import time
import shutil
import sys
from pathlib import Path
from typing import Optional, Dict, Any

from .constants import (
    DEFAULT_SHARED_FOLDER,
    DEFAULT_WSB_FOLDER,
    SERVER_START_TIMEOUT,
    TASK_TIMEOUT,
    POLL_INTERVAL,
    VENV_POOL_SIZE,
    PREWARM_VERSIONS,
    DEFAULT_PYTHON_VERSION,
)
from .server_script import SERVER_PY
from .wsb_template import WSB_TEMPLATE


class SandboxManager:
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
        self.shared_folder.mkdir(parents=True, exist_ok=True)
        self.wsb_folder.mkdir(parents=True, exist_ok=True)

    def _prepare_files(self):
        # Inject pool config from constants.py into the server script.
        # Plain string.replace, not str.format -- SERVER_PY has dict
        # literals ({...}) that str.format would choke on.
        server_code = (
            SERVER_PY
            .replace("__VENV_POOL_SIZE__", str(VENV_POOL_SIZE))
            .replace("__PREWARM_VERSIONS__", json.dumps(PREWARM_VERSIONS))
            .replace("__DEFAULT_VERSION__", DEFAULT_PYTHON_VERSION)
        )
        self.server_script_file.write_text(server_code, encoding='utf-8')

        wsb_content = WSB_TEMPLATE.format(host_folder=str(self.shared_folder))
        self.wsb_file.write_text(wsb_content, encoding='utf-8')

    def _is_sandbox_ready(self) -> bool:
        return self.ready_file.exists()

    def launch(self, timeout: int = SERVER_START_TIMEOUT) -> None:
        if self._sandbox_process is not None:
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

        print(f"Waiting for sandbox to be ready (timeout: {timeout}s)...")
        start = time.time()
        while not self._is_sandbox_ready():
            if time.time() - start > timeout:
                log_file = self.shared_folder / "server.log"
                if log_file.exists():
                    print(f"Server log:\n{log_file.read_text(encoding='utf-8')}")
                raise TimeoutError("Sandbox did not become ready in time.")
            time.sleep(POLL_INTERVAL)
        print("Sandbox is ready.")

    def _send_task(self, task: Dict[str, Any], timeout: int = TASK_TIMEOUT) -> Dict[str, Any]:
        """Write a task to task.json and block until result.json shows up.
        Used for both code execution and venv-pool management commands --
        they're just different "action" values on the same channel.
        """
        if not self._is_sandbox_ready():
            raise RuntimeError("Sandbox is not ready. Call .launch() first.")

        if self.task_file.exists():
            self.task_file.unlink()
        if self.result_file.exists():
            self.result_file.unlink()

        self.task_file.write_text(json.dumps(task), encoding='utf-8')

        start = time.time()
        while not self.result_file.exists():
            if time.time() - start > timeout:
                raise TimeoutError(f"Task did not complete within {timeout}s.")
            time.sleep(POLL_INTERVAL)

        result = json.loads(self.result_file.read_text(encoding='utf-8'))
        self.result_file.unlink()
        return result

    def run_code(self, code: str, version: str = DEFAULT_PYTHON_VERSION) -> Dict[str, Any]:
        return self._send_task({"action": "run", "code": code, "version": version})

    def create_venvs(self, version: str = DEFAULT_PYTHON_VERSION, count: int = 1) -> Dict[str, Any]:
        """Add `count` more warm venvs to the pool for `version`, on top
        of whatever's already there. Ignores VENV_POOL_SIZE -- this is
        the explicit override for when you want more than the default.
        """
        return self._send_task({"action": "create_venv", "version": version, "count": count})

    def remove_venvs(self, version: str = DEFAULT_PYTHON_VERSION, count: int = 1) -> Dict[str, Any]:
        """Remove up to `count` idle venvs from the pool for `version`.
        Busy venvs are left alone."""
        return self._send_task({"action": "remove_venv", "version": version, "count": count})

    def pool_status(self) -> Dict[str, Any]:
        """Return {version: {total, busy, idle}} for every pool the
        server has built so far."""
        return self._send_task({"action": "status"})

    def close(self):
        if self._sandbox_process and self._sandbox_process.poll() is None:
            self._sandbox_process.terminate()
            self._sandbox_process.wait(timeout=10)
        if self.ready_file.exists():
            self.ready_file.unlink()

    def __enter__(self):
        self.launch()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

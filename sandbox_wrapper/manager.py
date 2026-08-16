# manager.py
import json
import subprocess
import time
import shutil
import sys
import uuid
from pathlib import Path
from typing import Optional, Dict, Any, List

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
        self.tasks_dir = self.shared_folder / "tasks"
        self.results_dir = self.shared_folder / "results"
        self.ready_file = self.shared_folder / "ready.txt"
        self.server_script_file = self.shared_folder / "server.py"

        self._sandbox_process = None

        self._ensure_folders()
        self._prepare_files()

    def _ensure_folders(self):
        self.shared_folder.mkdir(parents=True, exist_ok=True)
        self.wsb_folder.mkdir(parents=True, exist_ok=True)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)

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

    # --- low-level task protocol --------------------------------------
    # Every task gets its own id and its own result file, so many tasks
    # can be in flight in the sandbox at once. The sandbox-side
    # concurrency for "run" tasks is capped by the venv pool size for
    # the requested version -- see server_script.py's VenvPool.

    def submit(self, task: Dict[str, Any]) -> str:
        """Write a task file and return its id immediately, without
        waiting for a result. Pair with await_result()."""
        if not self._is_sandbox_ready():
            raise RuntimeError("Sandbox is not ready. Call .launch() first.")
        task_id = uuid.uuid4().hex
        (self.tasks_dir / f"{task_id}.json").write_text(json.dumps(task), encoding='utf-8')
        return task_id

    def await_result(self, task_id: str, timeout: int = TASK_TIMEOUT) -> Dict[str, Any]:
        result_file = self.results_dir / f"{task_id}.json"
        start = time.time()
        while not result_file.exists():
            if time.time() - start > timeout:
                raise TimeoutError(f"Task {task_id} did not complete within {timeout}s.")
            time.sleep(POLL_INTERVAL)
        result = json.loads(result_file.read_text(encoding='utf-8'))
        result_file.unlink()
        return result

    def _send_task(self, task: Dict[str, Any], timeout: int = TASK_TIMEOUT) -> Dict[str, Any]:
        return self.await_result(self.submit(task), timeout=timeout)

    # --- code execution --------------------------------------------------

    def run_code(self, code: str, version: str = DEFAULT_PYTHON_VERSION,
                 timeout: int = TASK_TIMEOUT) -> Dict[str, Any]:
        """Run one snippet and block for its result."""
        return self._send_task({"action": "run", "code": code, "version": version}, timeout=timeout)

    def run_code_async(self, code: str, version: str = DEFAULT_PYTHON_VERSION) -> str:
        """Submit one snippet and return its task id right away."""
        return self.submit({"action": "run", "code": code, "version": version})

    def run_many(self, tasks: List[Dict[str, str]], timeout: int = TASK_TIMEOUT) -> List[Dict[str, Any]]:
        """Run several snippets concurrently. Each item is
        {"code": ..., "version": ... (optional)}.

        Actual concurrency is capped by how many warm venvs exist for
        the requested version(s) -- e.g. 10 tasks against a pool of 4
        venvs run 4 at a time, the rest queue inside the sandbox.
        Call create_venvs() first if you want more of these in flight
        together. Results are returned in the same order as `tasks`.
        """
        ids = [
            self.submit({
                "action": "run",
                "code": t["code"],
                "version": t.get("version", DEFAULT_PYTHON_VERSION),
            })
            for t in tasks
        ]
        return [self.await_result(task_id, timeout=timeout) for task_id in ids]

    # --- venv pool control -----------------------------------------------

    def create_venvs(self, version: str = DEFAULT_PYTHON_VERSION, count: int = 1) -> Dict[str, Any]:
        """Add `count` more warm venvs to the pool for `version`, raising
        how many "run" tasks for that version can execute at once."""
        return self._send_task({"action": "create_venv", "version": version, "count": count})

    def remove_venvs(self, version: str = DEFAULT_PYTHON_VERSION, count: int = 1) -> Dict[str, Any]:
        """Lower the concurrency ceiling for `version` by up to `count`.
        Idle venvs are removed immediately; if fewer than `count` are
        idle, the remaining busy ones are torn down as soon as their
        current task finishes instead of being recycled."""
        return self._send_task({"action": "remove_venv", "version": version, "count": count})

    def pool_status(self) -> Dict[str, Any]:
        """Return {version: {total, busy, idle}} for every pool the
        server has built so far -- `total` is that version's current
        concurrency ceiling."""
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

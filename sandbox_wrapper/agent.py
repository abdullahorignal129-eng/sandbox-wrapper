#!/usr/bin/env python3
"""GitHub Actions worker agent with concurrent job execution using venv pools."""

import argparse
import logging
import os
import queue
import random
import shutil
import subprocess
import sys
import tempfile
import time
import threading
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Optional, List

import httpx

# The old WindowsSandboxRunner is no longer used.
# We keep the import optional just in case some future code needs it.
try:
    from windows_runner import WindowsSandboxRunner
except ImportError:
    WindowsSandboxRunner = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("worker_agent")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    worker_id: int
    coordinator_url: str
    secret: str
    environment: Dict[str, object]

    poll_interval: float = 10.0
    stop_polling_after: float = 17700.0          # 4h55m
    max_retries: int = 5
    base_backoff: float = 1.0
    max_backoff: float = 30.0

    run_url: str = ""

    # Concurrency settings
    venv_pool_size: int = 20                    # venvs per Python version
    max_concurrent_per_version: Dict[str, int] = field(
        default_factory=lambda: {
            "3.11.9": 5,
            "3.12.10": 10,
            "3.13.15": 5,
            "3.14.7": 5,
        }
    )
    total_max_concurrent: int = 20              # global cap

    python_versions: List[str] = field(default_factory=lambda: [
        "3.11.9", "3.12.10", "3.13.15", "3.14.7"
    ])

    now_fn: Callable[[], float] = time.monotonic
    sleep_fn: Callable[[float], None] = time.sleep


# ---------------------------------------------------------------------------
# Python interpreter discovery & venv management
# ---------------------------------------------------------------------------

def find_python_executable(version: str) -> Optional[Path]:
    """Locate the Python interpreter for a given version in the GitHub tool cache."""
    tool_cache = os.environ.get("RUNNER_TOOL_CACHE", r"C:\hostedtoolcache\windows")
    cache_dir = Path(tool_cache) / "Python"
    if not cache_dir.exists():
        return None
    for child in cache_dir.iterdir():
        if child.name.startswith(version):
            python_exe = child / "x64" / "python.exe"
            if python_exe.exists():
                return python_exe
    return None


class VenvManager:
    """Manages a pool of ready venvs for each Python version (background creation)."""

    def __init__(self, python_versions: List[str], pool_size: int, venv_base_dir: Path):
        self.python_versions = python_versions
        self.pool_size = pool_size
        self.venv_base_dir = venv_base_dir
        self.venv_base_dir.mkdir(exist_ok=True)
        self.venvs: Dict[str, queue.Queue] = {}
        self.lock = threading.Lock()
        self._start_background_creation()

    def _start_background_creation(self):
        """Spawn a daemon thread to create all venvs in the background."""
        for version in self.python_versions:
            self.venvs[version] = queue.Queue()
        t = threading.Thread(target=self._create_all_venvs, daemon=True)
        t.start()

    def _create_all_venvs(self):
        for version in self.python_versions:
            for i in range(self.pool_size):
                venv_dir = self.venv_base_dir / f"{version}-venv-{i}"
                try:
                    self._create_venv(version, venv_dir)
                    with self.lock:
                        self.venvs[version].put(venv_dir)
                except Exception as e:
                    logger.error(f"Failed to create venv {version} #{i}: {e}")
            logger.info(f"Finished creating {self.pool_size} venvs for {version}")

    def _create_venv(self, version: str, venv_dir: Path) -> None:
        python_exe = find_python_executable(version)
        if not python_exe:
            raise RuntimeError(f"Python {version} not found in tool cache")
        subprocess.run(
            [str(python_exe), "-m", "venv", str(venv_dir)],
            check=True,
            capture_output=True,
        )

    def get_venv(self, version: str) -> Optional[Path]:
        with self.lock:
            if version not in self.venvs:
                self.venvs[version] = queue.Queue()
            try:
                return self.venvs[version].get_nowait()
            except queue.Empty:
                # On-demand creation if pool exhausted (up to double)
                if self.venvs[version].qsize() < self.pool_size * 2:
                    venv_dir = self.venv_base_dir / f"{version}-venv-ondemand-{int(time.time() * 1000)}"
                    try:
                        self._create_venv(version, venv_dir)
                        return venv_dir
                    except Exception as e:
                        logger.error(f"On-demand venv creation failed: {e}")
                return None

    def release_venv(self, version: str, venv_dir: Path) -> None:
        with self.lock:
            self.venvs[version].put(venv_dir)


# ---------------------------------------------------------------------------
# Job executor
# ---------------------------------------------------------------------------

class JobExecutor:
    """Runs jobs using direct interpreter (no deps) or venv (deps), respecting limits."""

    def __init__(self, config: AgentConfig, venv_manager: VenvManager):
        self.config = config
        self.venv_manager = venv_manager
        self.semaphores: Dict[str, threading.Semaphore] = {
            version: threading.Semaphore(limit)
            for version, limit in config.max_concurrent_per_version.items()
        }
        self.global_semaphore = threading.Semaphore(config.total_max_concurrent)

    def execute(self, job: dict) -> dict:
        version = job["python_version"]
        with self.global_semaphore:
            with self.semaphores[version]:
                if job.get("dependencies"):
                    # Use venv
                    venv_dir = self.venv_manager.get_venv(version)
                    if venv_dir is None:
                        return self._infra_failure(job, "No venv available")
                    try:
                        return self._run_with_venv(job, venv_dir)
                    finally:
                        self.venv_manager.release_venv(version, venv_dir)
                else:
                    # Use direct interpreter
                    python_exe = find_python_executable(version)
                    if not python_exe:
                        return self._infra_failure(job, f"Python {version} not found")
                    return self._run_with_interpreter(job, python_exe, install_deps=False)

    def _run_with_venv(self, job: dict, venv_dir: Path) -> dict:
        python_exe = venv_dir / "Scripts" / "python.exe"
        if not python_exe.exists():
            return self._infra_failure(job, "Venv python not found")
        return self._run_with_interpreter(job, python_exe, install_deps=True)

    def _run_with_interpreter(self, job: dict, python_exe: Path, install_deps: bool) -> dict:
        work_dir = Path(tempfile.mkdtemp(prefix="job_"))
        try:
            # Write job files
            for filename, content in job.get("files", {}).items():
                file_path = work_dir / filename
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(content, encoding="utf-8")

            # Install dependencies only if requested and dependencies exist
            if install_deps and job.get("dependencies"):
                subprocess.run(
                    [str(python_exe), "-m", "pip", "install", "--quiet"] + job["dependencies"],
                    check=True,
                    capture_output=True,
                    timeout=120,
                )

            # Run the code
            cmd = [str(python_exe), job.get("entry_point", "main.py")]
            result = subprocess.run(
                cmd,
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=job.get("timeout", 15.0),
                input=job.get("stdin") or None,
            )

            category = "success" if result.returncode == 0 else "checker_error"
            return {
                "job_id": job["job_id"],
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.returncode,
                "timed_out": False,
                "category": category,
                "failure_reason": None,
                "network_activity_detected": None,
            }

        except subprocess.TimeoutExpired:
            return self._timeout_failure(job)
        except subprocess.CalledProcessError as e:
            return self._infra_failure(job, f"Dependency install failed: {e.stderr}")
        except Exception as e:
            return self._infra_failure(job, f"Execution error: {e}")
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _infra_failure(self, job: dict, reason: str) -> dict:
        return {
            "job_id": job["job_id"],
            "stdout": "",
            "stderr": reason,
            "exit_code": 1,
            "timed_out": False,
            "category": "infra_failure",
            "failure_reason": reason,
            "network_activity_detected": None,
        }

    def _timeout_failure(self, job: dict) -> dict:
        return {
            "job_id": job["job_id"],
            "stdout": "",
            "stderr": "Timeout",
            "exit_code": -1,
            "timed_out": True,
            "category": "timeout",
            "failure_reason": f"Job timed out after {job.get('timeout', 15.0)}s",
            "network_activity_detected": None,
        }


# ---------------------------------------------------------------------------
# Coordinator client (unchanged)
# ---------------------------------------------------------------------------

class CoordinatorClient:
    def __init__(self, base_url: str, secret: str):
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=20.0)
        self.client.headers.update({"Authorization": f"Bearer {secret}"})

    def register(self, worker_id: int, url: str, environment: dict) -> dict:
        r = self.client.post("/worker/register", json={"worker_id": worker_id, "url": url, "environment": environment})
        r.raise_for_status()
        return r.json()

    def poll(self, worker_id: int) -> Optional[dict]:
        r = self.client.post("/worker/poll", json={"worker_id": worker_id})
        r.raise_for_status()
        return r.json().get("job")

    def report_result(self, worker_id: int, result: dict) -> dict:
        r = self.client.post("/worker/result", params={"worker_id": worker_id}, json=result)
        r.raise_for_status()
        return r.json()

    def report_done(self, worker_id: int) -> dict:
        r = self.client.post("/worker/done", json={"worker_id": worker_id})
        r.raise_for_status()
        return r.json()

    def close(self):
        self.client.close()


# ---------------------------------------------------------------------------
# Polling agent with concurrent execution
# ---------------------------------------------------------------------------

class PollingAgent:
    def __init__(self, config: AgentConfig, client: CoordinatorClient, executor: JobExecutor):
        self.config = config
        self.client = client
        self.executor = executor
        self.start_time = 0.0
        self.job_queue = queue.Queue()
        self.running_jobs = 0
        self.lock = threading.Lock()

    def elapsed(self) -> float:
        return self.config.now_fn() - self.start_time

    def should_stop_polling(self) -> bool:
        return self.elapsed() >= self.config.stop_polling_after

    def _call_with_retry(self, func: Callable, description: str):
        attempt = 0
        while True:
            try:
                return func()
            except Exception as e:
                attempt += 1
                if attempt > self.config.max_retries:
                    logger.error(f"{description} failed after {self.config.max_retries} attempts: {e}")
                    return None
                delay = min(self.config.base_backoff * (2 ** (attempt - 1)) + random.uniform(0, 0.5), self.config.max_backoff)
                logger.warning(f"{description} attempt {attempt} failed ({e}); retrying in {delay:.1f}s")
                self.config.sleep_fn(delay)

    def _report_result_thread(self, worker_id: int, result: dict):
        reported = self._call_with_retry(
            lambda: self.client.report_result(worker_id, result),
            "report_result",
        )
        if reported is None:
            logger.error(f"Failed to report result for {result.get('job_id')}; coordinator will re-queue after claim timeout")
        else:
            logger.info(f"Reported result for {result.get('job_id')}")
        with self.lock:
            self.running_jobs -= 1

    def _process_job(self, worker_id: int, job: dict):
        with self.lock:
            self.running_jobs += 1
        result = self.executor.execute(job)
        logger.info(f"Job {job.get('job_id')} finished with category {result.get('category')}")
        self._report_result_thread(worker_id, result)

    def run(self):
        self.start_time = self.config.now_fn()

        # Start worker threads (one per total_max_concurrent)
        worker_threads = [
            threading.Thread(target=self._worker_loop, args=(self.config.worker_id,))
            for _ in range(self.config.total_max_concurrent)
        ]
        for t in worker_threads:
            t.daemon = True
            t.start()

        registered = False
        while not self.should_stop_polling():
            if not registered:
                res = self._call_with_retry(
                    lambda: self.client.register(
                        self.config.worker_id,
                        self.config.run_url,
                        self.config.environment,
                    ),
                    "register",
                )
                if res is None:
                    self.config.sleep_fn(self.config.poll_interval)
                    continue
                registered = True
                logger.info(f"Registered worker {self.config.worker_id}")

            job = self._call_with_retry(
                lambda: self.client.poll(self.config.worker_id), "poll"
            )
            if job:
                logger.info(f"Queued job {job.get('job_id')}")
                self.job_queue.put(job)
            else:
                self.config.sleep_fn(self.config.poll_interval)

        logger.info("Stop polling threshold reached; waiting for running jobs to finish...")
        timeout = 300  # up to 5 minutes
        end_time = self.config.now_fn() + timeout
        while (not self.job_queue.empty() or self.running_jobs > 0) and self.config.now_fn() < end_time:
            self.config.sleep_fn(1)

        logger.info("Calling report_done and shutting down")
        if registered:
            self._call_with_retry(
                lambda: self.client.report_done(self.config.worker_id),
                "report_done",
            )
        logger.info("Shutdown complete")

    def _worker_loop(self, worker_id: int):
        while True:
            try:
                job = self.job_queue.get(timeout=1)
            except queue.Empty:
                continue
            self._process_job(worker_id, job)
            self.job_queue.task_done()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="Worker pool agent (concurrent)")
    parser.add_argument("--worker-id", type=int, required=True)
    parser.add_argument("--coordinator-url", required=True)
    parser.add_argument("--run-url", default="")
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--stop-polling-after", type=float, default=17700.0)
    parser.add_argument("--venv-pool-size", type=int, default=20)
    return parser.parse_args()


def main():
    args = parse_args()
    secret = os.environ.get("GITHUB_SHARED_SECRET")
    if not secret:
        logger.error("GITHUB_SHARED_SECRET not set")
        return 1

    python_versions = [
        v.strip()
        for v in os.environ.get(
            "WINDOWS_PYTHON_VERSIONS",
            "3.11.9,3.12.10,3.13.15,3.14.7",
        ).split(",")
        if v.strip()
    ]

    # Verify all versions are actually available
    missing = [v for v in python_versions if not find_python_executable(v)]
    if missing:
        logger.error(f"Missing Python versions in tool cache: {missing}")
        return 1

    environment = {
        "platform": "windows",
        "os_version": os.environ.get("WINDOWS_OS_VERSION", "windows-2025"),
        "python_versions": python_versions,
        "notes": f"Runner: {os.environ.get('RUNNER_LABEL', 'windows-2025')}; Run: {os.environ.get('GITHUB_RUN_ID', 'local')}",
    }

    run_url = args.run_url
    if not run_url and "GITHUB_REPOSITORY" in os.environ and "GITHUB_RUN_ID" in os.environ:
        run_url = f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    if not run_url:
        run_url = "https://local.example.com"

    config = AgentConfig(
        worker_id=args.worker_id,
        coordinator_url=args.coordinator_url,
        secret=secret,
        environment=environment,
        poll_interval=args.poll_interval,
        stop_polling_after=args.stop_polling_after,
        run_url=run_url,
        venv_pool_size=args.venv_pool_size,
        python_versions=python_versions,
    )

    venv_manager = VenvManager(python_versions, config.venv_pool_size, Path("venvs"))
    executor = JobExecutor(config, venv_manager)
    client = CoordinatorClient(args.coordinator_url, secret)
    agent = PollingAgent(config, client, executor)

    try:
        agent.run()
    finally:
        client.close()

    return 0

if __name__ == "__main__":
    raise SystemExit(main())

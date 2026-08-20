#!/usr/bin/env python3
"""Windows worker agent for dataset_verification (with shutdown support)."""

import argparse
import base64
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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Optional, List

import httpx
import psutil

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("worker_agent")

BASE_PATH = "/dataset_verification"
MAX_OUTPUT_BYTES = int(os.environ.get("MAX_OUTPUT_BYTES", 1024 * 1024))
MAX_FILESYSTEM_STATE_BYTES = int(os.environ.get("MAX_FILESYSTEM_STATE_BYTES", 5 * 1024 * 1024))  # 5MB default


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

    venv_pool_size: int = 20
    max_concurrent_per_version: Dict[str, int] = field(default_factory=lambda: {
        "3.11.9": 5,
        "3.12.10": 10,
        "3.13.15": 5,
        "3.14.7": 5,
    })
    total_max_concurrent: int = 20
    python_versions: List[str] = field(default_factory=lambda: [
        "3.11.9", "3.12.10", "3.13.15", "3.14.7"
    ])

    # V2: Batch processing settings
    batch_size: int = 50
    batch_report_threshold: int = 10  # Report results after this many complete

    now_fn: Callable[[], float] = time.monotonic
    sleep_fn: Callable[[float], None] = time.sleep


def find_python_executable(version: str) -> Optional[Path]:
    tool_cache = os.environ.get("RUNNER_TOOL_CACHE", r"C:\hostedtoolcache\windows")
    cache_dir = Path(tool_cache) / "Python"
    if not cache_dir.exists():
        return None
    for child in cache_dir.iterdir():
        if child.name.startswith(version):
            exe = child / "x64" / "python.exe"
            if exe.exists():
                return exe
    return None


class VenvManager:
    def __init__(self, python_versions, pool_size, venv_base_dir):
        self.python_versions = python_versions
        self.pool_size = pool_size
        self.venv_base_dir = venv_base_dir.resolve()
        self.venv_base_dir.mkdir(exist_ok=True)
        self.venvs = {v: queue.Queue() for v in python_versions}
        self.lock = threading.Lock()
        self._start_background_creation()

    def _start_background_creation(self):
        t = threading.Thread(target=self._create_all_venvs, daemon=True)
        t.start()

    def _create_all_venvs(self):
        max_workers = min(4, os.cpu_count() or 1)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for version in self.python_versions:
                for i in range(self.pool_size):
                    venv_dir = self.venv_base_dir / f"{version}-venv-{i}"
                    futures.append(executor.submit(self._create_and_store, version, venv_dir))
            for f in futures:
                f.result()

    def _create_and_store(self, version, venv_dir):
        try:
            self._create_venv(version, venv_dir)
            with self.lock:
                self.venvs[version].put(venv_dir)
        except Exception as e:
            logger.error(f"Failed to create venv {version} {venv_dir.name}: {e}")

    def _create_venv(self, version, venv_dir):
        python_exe = find_python_executable(version)
        if not python_exe:
            raise RuntimeError(f"Python {version} not found")
        subprocess.run([str(python_exe), "-m", "venv", str(venv_dir)],
                       check=True, capture_output=True)

    def create_and_test_initial_venvs(self) -> bool:
        for version in self.python_versions:
            venv_dir = self.venv_base_dir / f"{version}-test-venv"
            try:
                self._create_venv(version, venv_dir)
                if not self._test_venv(version, venv_dir):
                    return False
                if not self._test_venv_pip_install(version, venv_dir):
                    logger.error(f"Initial pip install test failed for {version}")
                    return False
                with self.lock:
                    self.venvs[version].put(venv_dir)
            except Exception as e:
                logger.error(f"Initial venv test failed for {version}: {e}")
                return False
        return True

    def _test_venv(self, version, venv_dir):
        python_exe = venv_dir / "Scripts" / "python.exe"
        if not python_exe.exists():
            return False
        try:
            result = subprocess.run([str(python_exe), "-c", "import sys; print(sys.version)"],
                                    capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                return False
            logger.info(f"Venv test OK for {version}")
            return True
        except Exception:
            return False

    def _test_venv_pip_install(self, version, venv_dir):
        python_exe = venv_dir / "Scripts" / "python.exe"
        if not python_exe.exists():
            return False
        test_pkg = "six"
        try:
            r = subprocess.run([str(python_exe), "-m", "pip", "install", "--quiet", test_pkg],
                               capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                return False
            import_r = subprocess.run([str(python_exe), "-c", f"import {test_pkg}"],
                                      capture_output=True, text=True, timeout=30)
            return import_r.returncode == 0
        except Exception:
            return False

    def get_venv(self, version):
        with self.lock:
            if version not in self.venvs:
                self.venvs[version] = queue.Queue()
            try:
                return self.venvs[version].get_nowait()
            except queue.Empty:
                if self.venvs[version].qsize() < self.pool_size * 2:
                    venv_dir = self.venv_base_dir / f"{version}-venv-ondemand-{int(time.time()*1000)}"
                    try:
                        self._create_venv(version, venv_dir)
                        return venv_dir
                    except Exception as e:
                        logger.error(f"On-demand venv creation failed: {e}")
                return None

    def release_venv(self, version, venv_dir):
        with self.lock:
            self.venvs[version].put(venv_dir)


class JobExecutor:
    def __init__(self, config, venv_manager):
        self.config = config
        self.venv_manager = venv_manager
        self.semaphores = {v: threading.Semaphore(limit) for v, limit in config.max_concurrent_per_version.items()}
        self.global_semaphore = threading.Semaphore(config.total_max_concurrent)

    def _truncate(self, text):
        if len(text.encode('utf-8')) > MAX_OUTPUT_BYTES:
            return text[:MAX_OUTPUT_BYTES // 2] + "\n... [truncated]"
        return text

    # ==================== V2: Filesystem Snapshotting ====================

    def _snapshot_workspace(self, work_dir: Path) -> Dict[str, str]:
        """
        Recursively scan work_dir, base64-encode each file's content,
        and return a dict mapping relative paths to base64-encoded strings.
        Skips files larger than MAX_FILESYSTEM_STATE_BYTES.
        Uses absolute paths internally to avoid resolution errors.
        """
        filesystem_state = {}
        work_dir_abs = work_dir.resolve()

        if not work_dir_abs.exists():
            return filesystem_state

        # Use os.walk for reliable recursive traversal
        for root, dirs, files in os.walk(work_dir_abs):
            root_path = Path(root)
            for filename in files:
                file_path = root_path / filename
                try:
                    # Get size before reading
                    file_size = file_path.stat().st_size
                    if file_size > MAX_FILESYSTEM_STATE_BYTES:
                        logger.debug(f"Skipping {file_path} ({file_size} bytes > {MAX_FILESYSTEM_STATE_BYTES} limit)")
                        continue

                    # Read file content as bytes
                    with open(file_path, 'rb') as f:
                        content_bytes = f.read()

                    # Base64 encode and decode to string
                    encoded = base64.b64encode(content_bytes).decode('utf-8')[reference:0]

                    # Store relative path (relative to work_dir_abs)
                    rel_path = str(file_path.relative_to(work_dir_abs))
                    filesystem_state[rel_path] = encoded

                except Exception as e:
                    logger.warning(f"Failed to snapshot {file_path}: {e}")

        return filesystem_state

    def _build_result_with_snapshot(self, job: dict, base_result: dict, work_dir: Path) -> dict:
        """Add filesystem_state to the result and clean up."""
        try:
            filesystem_state = self._snapshot_workspace(work_dir)
            base_result["filesystem_state"] = filesystem_state
        except Exception as e:
            logger.warning(f"Failed to snapshot workspace for {job.get('job_id')}: {e}")
            base_result["filesystem_state"] = {}

        return base_result

    def execute(self, job):
        version = job["python_version"]
        with self.global_semaphore:
            with self.semaphores[version]:
                if job.get("dependencies"):
                    venv_dir = self.venv_manager.get_venv(version)
                    if venv_dir is None:
                        return self._infra_failure(job, "No venv available")
                    try:
                        result = self._run_with_venv(job, venv_dir)
                    finally:
                        self.venv_manager.release_venv(version, venv_dir)
                else:
                    python_exe = find_python_executable(version)
                    if not python_exe:
                        return self._infra_failure(job, f"Python {version} not found")
                    result = self._run_with_interpreter(job, python_exe, install_deps=False)
                result["stdout"] = self._truncate(result["stdout"])
                result["stderr"] = self._truncate(result["stderr"])
                return result

    def _run_with_venv(self, job, venv_dir):
        python_exe = venv_dir / "Scripts" / "python.exe"
        if not python_exe.exists():
            return self._infra_failure(job, "Venv python not found")
        return self._run_with_interpreter(job, python_exe, install_deps=True)

    def _run_with_interpreter(self, job, python_exe, install_deps):
        # Create temporary workspace and get its absolute path[reference:1]
        work_dir = Path(tempfile.mkdtemp(prefix="job_")).resolve()[reference:2]
        try:
            # Write job files to workspace
            for filename, content in job.get("files", {}).items():
                fp = work_dir / filename
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(content, encoding="utf-8")

            # Install dependencies if needed
            if install_deps and job.get("dependencies"):
                pip_result = subprocess.run(
                    [str(python_exe), "-m", "pip", "install", "--quiet"] + job["dependencies"],
                    check=False, capture_output=True, timeout=120,
                )
                if pip_result.returncode != 0:
                    return self._dependency_install_failure(
                        job, f"Dependency install failed: {pip_result.stderr.decode(errors='replace')}"
                    )

            # Execute the job
            cmd = [str(python_exe), job.get("entry_point", "main.py")]
            result = subprocess.run(cmd, cwd=work_dir, capture_output=True, text=True,
                                    timeout=job.get("timeout", 15.0),
                                    input=job.get("stdin") or None)

            category = "success" if result.returncode == 0 else "checker_error"
            base_result = {
                "job_id": job["job_id"], "stdout": result.stdout, "stderr": result.stderr,
                "exit_code": result.returncode, "timed_out": False, "category": category,
                "failure_reason": None, "network_activity_detected": None,
            }

            # V2: Snapshot the workspace and add filesystem_state
            return self._build_result_with_snapshot(job, base_result, work_dir)

        except subprocess.TimeoutExpired:
            # Snapshot workspace before returning timeout failure
            base_result = self._timeout_failure(job)
            try:
                # Try to snapshot what we can before cleanup
                fs_state = self._snapshot_workspace(work_dir)
                base_result["filesystem_state"] = fs_state
            except Exception:
                base_result["filesystem_state"] = {}
            return base_result

        except Exception as e:
            return self._infra_failure(job, f"Execution error: {e}")

        finally:
            # Clean up workspace[reference:3]
            shutil.rmtree(work_dir, ignore_errors=True)

    def _infra_failure(self, job, reason):
        return {
            "job_id": job["job_id"], "stdout": "", "stderr": reason, "exit_code": 1,
            "timed_out": False, "category": "infra_failure", "failure_reason": reason,
            "network_activity_detected": None, "filesystem_state": {},
        }

    def _dependency_install_failure(self, job, reason):
        return {
            "job_id": job["job_id"], "stdout": "", "stderr": reason, "exit_code": 1,
            "timed_out": False, "category": "dependency_install_failed", "failure_reason": reason,
            "network_activity_detected": None, "filesystem_state": {},
        }

    def _timeout_failure(self, job):
        return {
            "job_id": job["job_id"], "stdout": "", "stderr": "Timeout", "exit_code": -1,
            "timed_out": True, "category": "timeout",
            "failure_reason": f"Job timed out after {job.get('timeout', 15.0)}s",
            "network_activity_detected": None, "filesystem_state": {},
        }


class CoordinatorClient:
    def __init__(self, base_url, secret):
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=20.0)[reference:4]
        self.client.headers.update({"Authorization": f"Bearer {secret}"})

    def register(self, worker_id, url, environment):
        r = self.client.post(f"{BASE_PATH}/worker/register",
                             json={"worker_id": worker_id, "url": url, "environment": environment})
        r.raise_for_status()
        return r.json()

    def poll(self, worker_id, url):
        r = self.client.post(f"{BASE_PATH}/worker/poll",
                             json={"worker_id": worker_id, "url": url})
        r.raise_for_status()
        data = r.json()
        if data.get("shutdown"):
            return {"shutdown": True}
        return data.get("job")

    # ==================== V2: Batch Endpoints ====================

    def poll_batch(self, worker_id: int, url: str, batch_size: int = 50) -> List[dict]:
        """
        V2: Poll up to batch_size jobs from the coordinator.
        Returns a list of job dicts, or [{"shutdown": True}] if shutdown signal received.
        """
        try:
            r = self.client.post(
                f"{BASE_PATH}/worker/jobs/batch",
                json={"worker_id": worker_id, "url": url, "batch_size": batch_size}
            )
            r.raise_for_status()
            data = r.json()
            # Check for shutdown signal
            if data.get("shutdown"):
                return [{"shutdown": True}]
            return data.get("jobs", [])
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.warning("Worker not registered (404) during batch poll")
                return []
            raise

    def report_results_batch(self, worker_id: int, url: str, results: List[dict]) -> dict:
        """
        V2: Report multiple results to the coordinator in a single call.
        """
        r = self.client.post(
            f"{BASE_PATH}/worker/results/batch",
            json={"worker_id": worker_id, "url": url, "results": results}
        )
        r.raise_for_status()
        return r.json()

    def report_result(self, worker_id, url, result):
        r = self.client.post(f"{BASE_PATH}/worker/result",
                             params={"worker_id": worker_id, "url": url}, json=result)
        r.raise_for_status()
        return r.json()

    def report_done(self, worker_id, url):
        r = self.client.post(f"{BASE_PATH}/worker/done",
                             json={"worker_id": worker_id, "url": url})
        r.raise_for_status()
        return r.json()

    def close(self):
        self.client.close()


class PollingAgent:
    def __init__(self, config, client, executor):
        self.config = config
        self.client = client
        self.executor = executor
        self.start_time = 0.0
        self.job_queue = queue.Queue()
        self.running_jobs = 0
        self.lock = threading.Lock()
        # V2: Batch result accumulator
        self.pending_results: List[dict] = []
        self.pending_results_lock = threading.Lock()
        self.last_batch_report_time = 0.0

    def elapsed(self):
        return self.config.now_fn() - self.start_time

    def should_stop_polling(self):
        return self.elapsed() >= self.config.stop_polling_after

    def _call_with_retry(self, func, description):
        attempt = 0
        while True:
            try:
                return func()
            except Exception as e:
                attempt += 1
                if attempt > self.config.max_retries:
                    logger.error(f"{description} failed after {self.config.max_retries} attempts: {e}")
                    return None
                delay = min(self.config.base_backoff * (2 ** (attempt-1)) + random.uniform(0, 0.5), self.config.max_backoff)
                logger.warning(f"{description} attempt {attempt} failed ({e}); retrying in {delay:.1f}s")
                self.config.sleep_fn(delay)

    # ==================== V2: Batch Result Reporting ====================

    def _add_pending_result(self, result: dict):
        """Add a result to the pending batch, report if threshold reached."""
        with self.pending_results_lock:
            self.pending_results.append(result)
            if len(self.pending_results) >= self.config.batch_report_threshold:
                self._flush_pending_results_locked()

    def _flush_pending_results_locked(self):
        """Flush pending results to coordinator (must hold pending_results_lock)."""
        if not self.pending_results:
            return

        results_to_send = self.pending_results[:]
        self.pending_results = []

        # Release lock before network call
        def do_report():
            reported = self._call_with_retry(
                lambda: self.client.report_results_batch(
                    self.config.worker_id,
                    self.config.run_url,
                    results_to_send
                ),
                "report_results_batch"
            )
            if reported is None:
                logger.error(f"Failed to report batch of {len(results_to_send)} results")
                # Re-queue failed results? For now, log and continue.
                # In production, you might want to retry individually.
            else:
                logger.info(f"Reported batch of {len(results_to_send)} results")

        # Run reporting in a separate thread to not block the main loop
        t = threading.Thread(target=do_report, daemon=True)
        t.start()

    def _flush_pending_results(self):
        """Public method to flush pending results."""
        with self.pending_results_lock:
            self._flush_pending_results_locked()

    def _report_result_thread(self, worker_id, result):
        """Legacy single-result reporting (kept for compatibility)."""
        reported = self._call_with_retry(
            lambda: self.client.report_result(worker_id, self.config.run_url, result),
            "report_result",
        )
        if reported is None:
            logger.error(f"Failed to report result for {result.get('job_id')}")
        else:
            logger.info(f"Reported result for {result.get('job_id')}")
        with self.lock:
            self.running_jobs -= 1

    def _process_job(self, worker_id, job):
        with self.lock:
            self.running_jobs += 1
        result = self.executor.execute(job)
        logger.info(f"Job {job.get('job_id')} finished with category {result.get('category')}")
        # V2: Add to batch instead of reporting immediately
        self._add_pending_result(result)
        with self.lock:
            self.running_jobs -= 1

    def run(self):
        self.start_time = self.config.now_fn()
        worker_threads = [
            threading.Thread(target=self._worker_loop, args=(self.config.worker_id,))
            for _ in range(self.config.total_max_concurrent)
        ]
        for t in worker_threads:
            t.daemon = True
            t.start()

        registered = False
        should_shutdown = False
        while not self.should_stop_polling() and not should_shutdown:
            if not registered:
                res = self._call_with_retry(
                    lambda: self.client.register(self.config.worker_id, self.config.run_url, self.config.environment),
                    "register",
                )
                if res is None:
                    self.config.sleep_fn(self.config.poll_interval)
                    continue
                registered = True
                logger.info(f"Registered worker {self.config.worker_id}")

            # V2: Use batch poll instead of single poll
            response = self._call_with_retry(
                lambda: self.client.poll_batch(self.config.worker_id, self.config.run_url, self.config.batch_size),
                "poll_batch",
            )

            # Handle shutdown signal
            if isinstance(response, list) and len(response) == 1 and response[0].get("shutdown"):
                logger.info("Shutdown signal received from coordinator. Exiting polling loop.")
                should_shutdown = True
                break
            elif isinstance(response, list) and response:
                logger.info(f"Queued {len(response)} jobs from batch")
                for job in response:
                    self.job_queue.put(job)
            else:
                self.config.sleep_fn(self.config.poll_interval)

        logger.info("Stop polling threshold reached or shutdown requested; waiting for running jobs to finish...")
        timeout = 300
        end_time = self.config.now_fn() + timeout
        while (not self.job_queue.empty() or self.running_jobs > 0) and self.config.now_fn() < end_time:
            self.config.sleep_fn(1)

        # Flush any remaining pending results before shutdown
        logger.info("Flushing remaining pending results...")
        self._flush_pending_results()
        # Give the flush thread a moment to complete
        time.sleep(2)

        logger.info("Calling report_done and shutting down")
        if registered:
            self._call_with_retry(
                lambda: self.client.report_done(self.config.worker_id, self.config.run_url),
                "report_done",
            )
        logger.info("Shutdown complete")

    def _worker_loop(self, worker_id):
        while True:
            try:
                job = self.job_queue.get(timeout=1)
            except queue.Empty:
                continue
            self._process_job(worker_id, job)
            self.job_queue.task_done()


def parse_args():
    parser = argparse.ArgumentParser(description="Worker pool agent (Windows)")
    parser.add_argument("--worker-id", type=int, required=True)
    parser.add_argument("--coordinator-url", required=True)
    parser.add_argument("--run-url", default="")
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--stop-polling-after", type=float, default=17700.0)
    parser.add_argument("--venv-pool-size", type=int, default=20)
    # V2: Batch size argument
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--batch-report-threshold", type=int, default=10)
    return parser.parse_args()


def main():
    args = parse_args()
    secret = os.environ.get("GITHUB_SHARED_SECRET")
    if not secret:
        logger.error("GITHUB_SHARED_SECRET not set")
        return 1

    logger.info(f"CPU count: {os.cpu_count()}")
    logger.info(f"RAM total: {psutil.virtual_memory().total / (1024**3):.2f} GB")

    python_versions = [v.strip() for v in os.environ.get("WINDOWS_PYTHON_VERSIONS", "3.11.9,3.12.10,3.13.15,3.14.7").split(",") if v.strip()]
    missing = [v for v in python_versions if not find_python_executable(v)]
    if missing:
        logger.error(f"Missing Python versions: {missing}")
        return 1

    environment = {
        "platform": "windows",
        "os_version": os.environ.get("WINDOWS_OS_VERSION", "windows-2025"),
        "python_versions": python_versions,
        "max_concurrent_jobs": 20,
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
        batch_size=args.batch_size,
        batch_report_threshold=args.batch_report_threshold,
    )

    venv_manager = VenvManager(python_versions, config.venv_pool_size, Path("venvs").resolve())
    if not venv_manager.create_and_test_initial_venvs():
        logger.error("Initial venv test failed. Exiting.")
        return 1

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

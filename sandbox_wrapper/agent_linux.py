#!/usr/bin/env python3
"""Linux worker agent - DEBUG BUILD.

Same behavior as the original agent_linux.py, but with exhaustive logging
added at every point that touches a venv:

  - At startup: for each python version, create the initial venv, print its
    ABSOLUTE path, confirm existence, list bin/ contents, then run a REAL
    `pip install` (not just `import sys`) against it and print full
    stdout/stderr, so the actual failing code path is exercised at boot.

  - Every time a job is assigned a venv (pool hit, pool-empty on-demand
    creation, OR plain get_nowait() success - which was the previously
    unlogged path): print absolute path, os.path.exists(), bin/ listing,
    and whether bin/python passes os.access(X_OK) - RIGHT BEFORE use.

  - Immediately before subprocess.run() for both `pip install` and the
    actual job execution: print the exact resolved python_exe path and
    whether it exists at that exact instant (to catch anything that
    disappears between the check and the exec).

  - After pip install: full stdout/stderr regardless of success/failure.

Nothing about job semantics, retry logic, or the coordinator protocol is
changed. Only logging is added, plus the two safety checks from the prior
fix (still present) so we don't lose that fix while debugging.
"""

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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Optional, List

import httpx
import psutil

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("worker_agent_linux")

BASE_PATH = "/dataset_verification"
MAX_OUTPUT_BYTES = int(os.environ.get("MAX_OUTPUT_BYTES", 1024 * 1024))  # default 1 MB


def _debug_dump_path(label: str, p: Path):
    """Print exhaustive detail about a filesystem path: absolute form,
    existence, type, and (if a venv dir) its bin/ listing with per-file
    executability."""
    abs_p = p.resolve()
    logger.info(f"[PATHCHECK] {label}: given={p}  absolute={abs_p}")
    logger.info(f"[PATHCHECK] {label}: exists={p.exists()}  "
                f"is_dir={p.is_dir() if p.exists() else 'n/a'}  "
                f"is_symlink={p.is_symlink()}")
    bin_dir = p / "bin" if p.is_dir() or not p.exists() else p
    if p.name == "bin" or (p.is_dir() and (p / "bin").exists()):
        bin_dir = p if p.name == "bin" else (p / "bin")
        if bin_dir.exists():
            logger.info(f"[PATHCHECK] {label}: bin dir = {bin_dir.resolve()}")
            try:
                for item in sorted(bin_dir.iterdir()):
                    st = item.lstat()
                    logger.info(
                        f"[PATHCHECK] {label}:   {item.name}  "
                        f"abs={item.resolve() if item.exists() else '(broken symlink)'}  "
                        f"exists={item.exists()}  "
                        f"is_symlink={item.is_symlink()}  "
                        f"x_ok={os.access(item, os.X_OK) if item.exists() else False}  "
                        f"mode={oct(st.st_mode)}"
                    )
            except FileNotFoundError as e:
                logger.info(f"[PATHCHECK] {label}: bin dir listing failed: {e}")
        else:
            logger.info(f"[PATHCHECK] {label}: bin dir does not exist at {bin_dir.resolve()}")


def _debug_check_python_exe(label: str, python_exe: Optional[Path]):
    if python_exe is None:
        logger.info(f"[PATHCHECK] {label}: python_exe is None")
        return
    abs_exe = python_exe.resolve()
    logger.info(
        f"[PATHCHECK] {label}: python_exe given={python_exe}  absolute={abs_exe}  "
        f"exists={python_exe.exists()}  x_ok={os.access(python_exe, os.X_OK) if python_exe.exists() else False}"
    )


# ---------------------------------------------------------------------------
# Python discovery & venv
# ---------------------------------------------------------------------------

def find_python_executable(version: str) -> Optional[Path]:
    tool_cache = os.environ.get("RUNNER_TOOL_CACHE", "/opt/hostedtoolcache")
    cache_dir = Path(tool_cache) / "Python"
    if not cache_dir.exists():
        return None
    for child in cache_dir.iterdir():
        if child.name.startswith(version):
            exe = child / "x64" / "bin" / "python3"
            if exe.exists():
                return exe
            exe = child / "bin" / "python3"
            if exe.exists():
                return exe
    return None


class VenvManager:
    def __init__(self, python_versions, pool_size, venv_base_dir):
        self.python_versions = python_versions
        self.pool_size = pool_size
        self.venv_base_dir = venv_base_dir
        self.venv_base_dir.mkdir(exist_ok=True)
        logger.info(f"[VENVMANAGER] base dir absolute path: {self.venv_base_dir.resolve()}")
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
            python_exe = self._find_venv_python(venv_dir)
            _debug_check_python_exe(f"create_and_store/{venv_dir.name}", python_exe)
            if python_exe is None or not os.access(python_exe, os.X_OK):
                raise RuntimeError(f"venv created but python executable not usable at {venv_dir}")
            with self.lock:
                self.venvs[version].put(venv_dir)
            logger.info(f"[POOL] queued {venv_dir.name} for version {version} "
                        f"(absolute={venv_dir.resolve()})")
        except Exception as e:
            logger.error(f"Failed to create venv {version} {venv_dir.name}: {e}")

    def _create_venv(self, version, venv_dir):
        python_exe = find_python_executable(version)
        if not python_exe:
            raise RuntimeError(f"Python {version} not found")
        logger.info(f"[CREATE] creating venv {venv_dir.resolve()} using base interpreter {python_exe.resolve()}")
        result = subprocess.run([str(python_exe), "-m", "venv", str(venv_dir)],
                       check=True, capture_output=True)
        logger.info(f"[CREATE] venv module exited rc={result.returncode} for {venv_dir.resolve()}")
        if result.stdout:
            logger.info(f"[CREATE] venv stdout: {result.stdout.decode(errors='replace')}")
        if result.stderr:
            logger.info(f"[CREATE] venv stderr: {result.stderr.decode(errors='replace')}")
        _debug_dump_path(f"post-create/{venv_dir.name}", venv_dir)

    def _find_venv_python(self, venv_dir: Path) -> Optional[Path]:
        bin_dir = venv_dir / "bin"
        if not bin_dir.exists():
            return None
        for name in ["python", "python3"]:
            candidate = bin_dir / name
            if candidate.exists():
                return candidate
        # scan for any python*
        for f in bin_dir.iterdir():
            if f.name.startswith("python"):
                return f
        return None

    def create_and_test_initial_venvs(self) -> bool:
        for version in self.python_versions:
            venv_dir = self.venv_base_dir / f"{version}-test-venv"
            try:
                self._create_venv(version, venv_dir)
                # Log path contents for debugging
                self._log_directory_contents(self.venv_base_dir)
                self._log_directory_contents(venv_dir / "bin")
                if not self._test_venv(version, venv_dir):
                    return False

                # NEW: also exercise the REAL pip-install path at startup,
                # against a harmless, fast, reliable package - this proves
                # or disproves whether install-time behavior itself is fine
                # right after creation, before any job ever touches it.
                if not self._test_venv_pip_install(version, venv_dir):
                    logger.error(f"[STARTUP-PIP-TEST] pip install smoke test failed for {version}; "
                                 f"continuing anyway so we can observe job behavior too")

                with self.lock:
                    self.venvs[version].put(venv_dir)
                logger.info(f"[POOL] queued initial test-venv {venv_dir.name} for version {version} "
                            f"(absolute={venv_dir.resolve()})")
            except Exception as e:
                logger.error(f"Initial venv test failed for {version}: {e}")
                return False
        return True

    def _test_venv(self, version, venv_dir) -> bool:
        python_exe = self._find_venv_python(venv_dir)
        _debug_check_python_exe(f"test_venv/{venv_dir.name}", python_exe)
        if python_exe is None:
            logger.error(f"Venv python not found in {venv_dir / 'bin'}")
            self._log_directory_contents(venv_dir / "bin")
            return False
        try:
            result = subprocess.run([str(python_exe), "-c", "import sys; print(sys.version)"],
                                    capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                logger.error(f"Venv test failed for {version}:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
                return False
            logger.info(f"Venv test OK for {version}: {result.stdout.strip()}")
            return True
        except Exception as e:
            logger.error(f"Venv test exception for {version}: {e}")
            return False

    def _test_venv_pip_install(self, version, venv_dir) -> bool:
        """Run a REAL pip install (not just `import sys`) against the
        just-created venv, right now, at startup - to see if the
        install-time code path itself works before any job ever uses it."""
        python_exe = self._find_venv_python(venv_dir)
        _debug_check_python_exe(f"pip_install_test/{venv_dir.name}", python_exe)
        if python_exe is None:
            logger.error(f"[STARTUP-PIP-TEST] no python exe found for {venv_dir}")
            return False
        test_pkg = "six"
        logger.info(f"[STARTUP-PIP-TEST] running: {python_exe.resolve()} -m pip install --quiet {test_pkg}")
        try:
            result = subprocess.run(
                [str(python_exe), "-m", "pip", "install", "--quiet", test_pkg],
                capture_output=True, text=True, timeout=120,
            )
            logger.info(f"[STARTUP-PIP-TEST] rc={result.returncode}")
            logger.info(f"[STARTUP-PIP-TEST] stdout:\n{result.stdout}")
            logger.info(f"[STARTUP-PIP-TEST] stderr:\n{result.stderr}")
            _debug_dump_path(f"post-pip-install/{venv_dir.name}", venv_dir)
            if result.returncode != 0:
                return False
            # confirm the interpreter is STILL there and executable after install
            python_exe_after = self._find_venv_python(venv_dir)
            _debug_check_python_exe(f"pip_install_test-AFTER/{venv_dir.name}", python_exe_after)
            if python_exe_after is None or not os.access(python_exe_after, os.X_OK):
                logger.error(f"[STARTUP-PIP-TEST] python exe vanished or became non-executable "
                             f"AFTER pip install completed for {venv_dir}")
                return False
            # actually run the import to prove it works end to end
            run_result = subprocess.run(
                [str(python_exe_after), "-c", f"import {test_pkg}; print('{test_pkg} import OK')"],
                capture_output=True, text=True, timeout=30,
            )
            logger.info(f"[STARTUP-PIP-TEST] post-install import test rc={run_result.returncode} "
                        f"stdout={run_result.stdout.strip()} stderr={run_result.stderr.strip()}")
            return run_result.returncode == 0
        except Exception as e:
            logger.error(f"[STARTUP-PIP-TEST] exception during pip install test: {e}")
            _debug_dump_path(f"post-pip-install-EXCEPTION/{venv_dir.name}", venv_dir)
            return False

    def _log_directory_contents(self, path: Path):
        if path.exists():
            logger.info(f"Contents of {path} (absolute={path.resolve()}):")
            for item in sorted(path.iterdir()):
                logger.info(f"  {item.name}")
        else:
            logger.warning(f"Directory does not exist: {path} (absolute={path.resolve()})")

    def get_venv(self, version):
        with self.lock:
            if version not in self.venvs:
                self.venvs[version] = queue.Queue()
            try:
                venv_dir = self.venvs[version].get_nowait()
                logger.info(f"[GET_VENV] pool-hit for {version}: got {venv_dir.name} "
                            f"(absolute={venv_dir.resolve()})")
                _debug_dump_path(f"get_venv-pool-hit/{venv_dir.name}", venv_dir)
                python_exe = self._find_venv_python(venv_dir)
                _debug_check_python_exe(f"get_venv-pool-hit/{venv_dir.name}", python_exe)
                if python_exe is None or not os.access(python_exe, os.X_OK):
                    logger.error(f"[GET_VENV] pool-hit venv {venv_dir.name} is BROKEN at the moment "
                                 f"of assignment (python_exe={python_exe}). This is the smoking gun: "
                                 f"it passed validation earlier but is not usable right now.")
                return venv_dir
            except queue.Empty:
                if self.venvs[version].qsize() < self.pool_size * 2:
                    venv_dir = self.venv_base_dir / f"{version}-venv-ondemand-{int(time.time()*1000)}"
                    logger.info(f"[GET_VENV] pool-empty for {version}, creating on-demand: "
                                f"{venv_dir.resolve()}")
                    try:
                        self._create_venv(version, venv_dir)
                        python_exe = self._find_venv_python(venv_dir)
                        _debug_check_python_exe(f"get_venv-ondemand/{venv_dir.name}", python_exe)
                        if python_exe is None or not os.access(python_exe, os.X_OK):
                            raise RuntimeError(f"on-demand venv created but python executable not usable at {venv_dir}")
                        return venv_dir
                    except Exception as e:
                        logger.error(f"On-demand venv creation failed: {e}")
                return None

    def release_venv(self, version, venv_dir):
        with self.lock:
            logger.info(f"[RELEASE_VENV] releasing {venv_dir.name} back to pool for {version} "
                        f"(absolute={venv_dir.resolve()}, still_exists={venv_dir.exists()})")
            self.venvs[version].put(venv_dir)


# ---------------------------------------------------------------------------
# Job executor with output truncation
# ---------------------------------------------------------------------------

class JobExecutor:
    def __init__(self, config, venv_manager):
        self.config = config
        self.venv_manager = venv_manager
        self.semaphores = {v: threading.Semaphore(limit) for v, limit in config.max_concurrent_per_version.items()}
        self.global_semaphore = threading.Semaphore(config.total_max_concurrent)

    def _truncate(self, text: str) -> str:
        if len(text.encode('utf-8')) > MAX_OUTPUT_BYTES:
            return text[:MAX_OUTPUT_BYTES // 2] + "\n... [truncated]"
        return text

    def execute(self, job):
        version = job["python_version"]
        job_id = job.get("job_id")
        logger.info(f"[EXECUTE] job {job_id} starting, version={version}, "
                    f"dependencies={job.get('dependencies')}")
        with self.global_semaphore:
            with self.semaphores[version]:
                if job.get("dependencies"):
                    venv_dir = self.venv_manager.get_venv(version)
                    if venv_dir is None:
                        logger.error(f"[EXECUTE] job {job_id}: no venv available for {version}")
                        return self._infra_failure(job, "No venv available")
                    logger.info(f"[EXECUTE] job {job_id}: assigned venv {venv_dir.name} "
                                f"(absolute={venv_dir.resolve()}) exists={venv_dir.exists()}")
                    try:
                        result = self._run_with_venv(job, venv_dir)
                    finally:
                        self.venv_manager.release_venv(version, venv_dir)
                else:
                    python_exe = find_python_executable(version)
                    if not python_exe:
                        return self._infra_failure(job, f"Python {version} not found")
                    logger.info(f"[EXECUTE] job {job_id}: using base interpreter "
                                f"{python_exe.resolve()}")
                    result = self._run_with_interpreter(job, python_exe, install_deps=False)
                result["stdout"] = self._truncate(result["stdout"])
                result["stderr"] = self._truncate(result["stderr"])
                logger.info(f"[EXECUTE] job {job_id}: finished category={result['category']}")
                return result

    def _run_with_venv(self, job, venv_dir):
        job_id = job.get("job_id")
        _debug_dump_path(f"run_with_venv/job_{job_id}/{venv_dir.name}", venv_dir)
        python_exe = self.venv_manager._find_venv_python(venv_dir)  # reuse robust finder
        _debug_check_python_exe(f"run_with_venv/job_{job_id}", python_exe)
        if python_exe is None:
            logger.error(f"[RUN_WITH_VENV] job {job_id}: venv python not found at moment of use "
                         f"for {venv_dir.resolve()}")
            return self._infra_failure(job, "Venv python not found")
        return self._run_with_interpreter(job, python_exe, install_deps=True)

    def _run_with_interpreter(self, job, python_exe, install_deps):
        job_id = job.get("job_id")
        work_dir = Path(tempfile.mkdtemp(prefix="job_"))
        logger.info(f"[RUN] job {job_id}: work_dir={work_dir.resolve()}, "
                    f"python_exe={python_exe} absolute={python_exe.resolve() if Path(python_exe).exists() else '(missing)'}, "
                    f"exists_right_now={Path(python_exe).exists()}")
        try:
            for filename, content in job.get("files", {}).items():
                fp = work_dir / filename
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(content, encoding="utf-8")
            if install_deps and job.get("dependencies"):
                logger.info(f"[RUN] job {job_id}: pre-pip-install check, python_exe exists="
                            f"{Path(python_exe).exists()} x_ok={os.access(python_exe, os.X_OK)}")
                logger.info(f"[RUN] job {job_id}: running pip install: "
                            f"{python_exe} -m pip install --quiet {job['dependencies']}")
                pip_result = subprocess.run(
                    [str(python_exe), "-m", "pip", "install", "--quiet"] + job["dependencies"],
                    check=False, capture_output=True, timeout=120,
                )
                logger.info(f"[RUN] job {job_id}: pip install rc={pip_result.returncode}")
                logger.info(f"[RUN] job {job_id}: pip stdout: {pip_result.stdout.decode(errors='replace')}")
                logger.info(f"[RUN] job {job_id}: pip stderr: {pip_result.stderr.decode(errors='replace')}")
                logger.info(f"[RUN] job {job_id}: post-pip-install check, python_exe exists="
                            f"{Path(python_exe).exists()} x_ok={os.access(python_exe, os.X_OK) if Path(python_exe).exists() else False}")
                if pip_result.returncode != 0:
                    return self._infra_failure(
                        job, f"Dependency install failed: {pip_result.stderr}"
                    )
            logger.info(f"[RUN] job {job_id}: pre-exec check, python_exe exists="
                        f"{Path(python_exe).exists()} x_ok={os.access(python_exe, os.X_OK) if Path(python_exe).exists() else False}")
            cmd = [str(python_exe), job.get("entry_point", "main.py")]
            logger.info(f"[RUN] job {job_id}: executing cmd={cmd} cwd={work_dir.resolve()}")
            result = subprocess.run(cmd, cwd=work_dir, capture_output=True, text=True,
                                    timeout=job.get("timeout", 15.0),
                                    input=job.get("stdin") or None)
            category = "success" if result.returncode == 0 else "checker_error"
            return {"job_id": job["job_id"], "stdout": result.stdout, "stderr": result.stderr,
                    "exit_code": result.returncode, "timed_out": False, "category": category,
                    "failure_reason": None, "network_activity_detected": None}
        except subprocess.TimeoutExpired:
            logger.error(f"[RUN] job {job_id}: TimeoutExpired")
            return self._timeout_failure(job)
        except subprocess.CalledProcessError as e:
            logger.error(f"[RUN] job {job_id}: CalledProcessError: {e.stderr}")
            return self._infra_failure(job, f"Dependency install failed: {e.stderr}")
        except Exception as e:
            logger.error(f"[RUN] job {job_id}: Exception during execution: {type(e).__name__}: {e}")
            logger.error(f"[RUN] job {job_id}: python_exe at exception time: {python_exe}  "
                         f"exists={Path(python_exe).exists()}")
            return self._infra_failure(job, f"Execution error: {e}")
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _infra_failure(self, job, reason):
        return {"job_id": job["job_id"], "stdout": "", "stderr": reason, "exit_code": 1,
                "timed_out": False, "category": "infra_failure", "failure_reason": reason,
                "network_activity_detected": None}

    def _timeout_failure(self, job):
        return {"job_id": job["job_id"], "stdout": "", "stderr": "Timeout", "exit_code": -1,
                "timed_out": True, "category": "timeout",
                "failure_reason": f"Job timed out after {job.get('timeout', 15.0)}s",
                "network_activity_detected": None}


# ---------------------------------------------------------------------------
# Coordinator client (same as Windows)
# ---------------------------------------------------------------------------

class CoordinatorClient:
    def __init__(self, base_url, secret):
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=20.0)
        self.client.headers.update({"Authorization": f"Bearer {secret}"})

    def register(self, worker_id, url, environment):
        r = self.client.post(f"{BASE_PATH}/worker/register", json={"worker_id": worker_id, "url": url, "environment": environment})
        r.raise_for_status()
        return r.json()

    def poll(self, worker_id):
        r = self.client.post(f"{BASE_PATH}/worker/poll", json={"worker_id": worker_id})
        r.raise_for_status()
        return r.json().get("job")

    def report_result(self, worker_id, url, result):
        r = self.client.post(f"{BASE_PATH}/worker/result", params={"worker_id": worker_id, "url": url}, json=result)
        r.raise_for_status()
        return r.json()

    def report_done(self, worker_id, url):
        r = self.client.post(f"{BASE_PATH}/worker/done", json={"worker_id": worker_id, "url": url})
        r.raise_for_status()
        return r.json()

    def close(self):
        self.client.close()


# ---------------------------------------------------------------------------
# Polling agent (same as Windows)
# ---------------------------------------------------------------------------

class PollingAgent:
    def __init__(self, config, client, executor):
        self.config = config
        self.client = client
        self.executor = executor
        self.start_time = 0.0
        self.job_queue = queue.Queue()
        self.running_jobs = 0
        self.lock = threading.Lock()

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

    def _report_result_thread(self, worker_id, result):
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
        self._report_result_thread(worker_id, result)

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
        while not self.should_stop_polling():
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

            job = self._call_with_retry(
                lambda: self.client.poll(self.config.worker_id), "poll"
            )
            if job:
                logger.info(f"Queued job {job.get('job_id')}")
                self.job_queue.put(job)
            else:
                self.config.sleep_fn(self.config.poll_interval)

        logger.info("Stop polling threshold reached; waiting for running jobs to finish...")
        timeout = 300
        end_time = self.config.now_fn() + timeout
        while (not self.job_queue.empty() or self.running_jobs > 0) and self.config.now_fn() < end_time:
            self.config.sleep_fn(1)

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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="Worker pool agent (Linux, DEBUG)")
    parser.add_argument("--worker-id", type=int, required=True)
    parser.add_argument("--coordinator-url", required=True)
    parser.add_argument("--run-url", default="")
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--stop-polling-after", type=float, default=17700.0)
    parser.add_argument("--venv-pool-size", type=int, default=20)
    return parser.parse_args()

@dataclass
class AgentConfig:
    worker_id: int
    coordinator_url: str
    secret: str
    environment: Dict[str, object]

    poll_interval: float = 10.0
    stop_polling_after: float = 17700.0
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

    now_fn: Callable[[], float] = time.monotonic
    sleep_fn: Callable[[float], None] = time.sleep


def main():
    args = parse_args()
    secret = os.environ.get("GITHUB_SHARED_SECRET")
    if not secret:
        logger.error("GITHUB_SHARED_SECRET not set")
        return 1

    logger.info(f"CPU count: {os.cpu_count()}")
    logger.info(f"RAM total: {psutil.virtual_memory().total / (1024**3):.2f} GB")
    logger.info(f"[STARTUP] cwd={os.getcwd()}  absolute cwd={Path.cwd().resolve()}")

    python_versions = [
        v.strip() for v in os.environ.get("PYTHON_VERSIONS", "3.11.9,3.12.10,3.13.15,3.14.7").split(",") if v.strip()
    ]
    missing = [v for v in python_versions if not find_python_executable(v)]
    if missing:
        logger.error(f"Missing Python versions: {missing}")
        return 1

    environment = {
        "platform": "linux",
        "os_version": os.environ.get("OS_VERSION", "ubuntu-22.04"),
        "python_versions": python_versions,
        "max_concurrent_jobs": 20,
        "notes": f"Runner: {os.environ.get('RUNNER_LABEL', 'ubuntu-22.04')}; Run: {os.environ.get('GITHUB_RUN_ID', 'local')}",
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

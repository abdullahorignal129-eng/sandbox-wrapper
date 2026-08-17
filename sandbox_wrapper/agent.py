#!/usr/bin/env python3
"""GitHub Actions worker agent for the HF Space worker-pool coordinator."""

import argparse
import json
import logging
import os
import random
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Callable, Dict, Optional

import httpx

# Import the real Windows Sandbox runner. The exact path may need adjustment.
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
# Mock runner for local dry-run / tests
# ---------------------------------------------------------------------------

class MockWindowsSandboxRunner:
    """Fake runner that returns successful canned results."""

    def start(self) -> None:
        logger.info("Mock runner: start()")

    def run_job(self, job: dict) -> dict:
        logger.info(f"Mock runner: running job {job.get('job_id')}")
        return {
            "job_id": job.get("job_id", "unknown"),
            "stdout": "mock output\n",
            "stderr": "",
            "exit_code": 0,
            "timed_out": False,
            "category": "success",
            "failure_reason": None,
            "network_activity_detected": None,
        }

    def stop(self) -> None:
        logger.info("Mock runner: stop()")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()


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

    now_fn: Callable[[], float] = time.monotonic
    sleep_fn: Callable[[float], None] = time.sleep


# ---------------------------------------------------------------------------
# Coordinator client
# ---------------------------------------------------------------------------

class CoordinatorClient:
    """HTTP client for the worker-facing endpoints of the coordinator."""

    def __init__(self, base_url: str, secret: str):
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=20.0,
        )
        self.client.headers.update({"Authorization": f"Bearer {secret}"})

    def register(self, worker_id: int, url: str, environment: dict) -> dict:
        r = self.client.post(
            "/worker/register",
            json={"worker_id": worker_id, "url": url, "environment": environment},
        )
        r.raise_for_status()
        return r.json()

    def poll(self, worker_id: int) -> Optional[dict]:
        r = self.client.post("/worker/poll", json={"worker_id": worker_id})
        r.raise_for_status()
        data = r.json()
        return data.get("job")

    def report_result(self, worker_id: int, result: dict) -> dict:
        r = self.client.post(
            "/worker/result", params={"worker_id": worker_id}, json=result
        )
        r.raise_for_status()
        return r.json()

    def report_done(self, worker_id: int) -> dict:
        r = self.client.post("/worker/done", json={"worker_id": worker_id})
        r.raise_for_status()
        return r.json()

    def close(self):
        self.client.close()


# ---------------------------------------------------------------------------
# Polling agent (no self-trigger)
# ---------------------------------------------------------------------------

class PollingAgent:
    def __init__(
        self,
        config: AgentConfig,
        client: CoordinatorClient,
        runner,
    ):
        self.config = config
        self.client = client
        self.runner = runner
        self.start_time: float = 0.0

    def elapsed(self) -> float:
        return self.config.now_fn() - self.start_time

    def should_stop_polling(self) -> bool:
        return self.elapsed() >= self.config.stop_polling_after

    def _call_with_retry(self, func: Callable, description: str):
        """Call func with exponential backoff on exceptions."""
        attempt = 0
        while True:
            try:
                return func()
            except Exception as e:
                attempt += 1
                if attempt > self.config.max_retries:
                    logger.error(
                        f"{description} failed after {self.config.max_retries} attempts: {e}"
                    )
                    return None
                delay = min(
                    self.config.base_backoff * (2 ** (attempt - 1))
                    + random.uniform(0, 0.5),
                    self.config.max_backoff,
                )
                logger.warning(
                    f"{description} attempt {attempt} failed ({e}); retrying in {delay:.1f}s"
                )
                self.config.sleep_fn(delay)

    def _run_job_safely(self, job: dict) -> dict:
        """Run a job, converting unhandled exceptions into an infra_failure result."""
        try:
            return self.runner.run_job(job)
        except Exception as e:
            logger.exception("Unhandled exception in runner")
            return {
                "job_id": job.get("job_id", "unknown"),
                "stdout": "",
                "stderr": str(e),
                "exit_code": 1,
                "timed_out": False,
                "category": "infra_failure",
                "failure_reason": f"Unhandled runner exception: {e}",
                "network_activity_detected": None,
            }

    def run(self) -> None:
        self.start_time = self.config.now_fn()

        with suppress(Exception):
            self.runner.start()

        registered = False
        while not self.should_stop_polling():
            if not registered:
                logger.info("Registering worker...")
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
                logger.info(f"Registered worker {self.config.worker_id}: {res}")

            job = self._call_with_retry(
                lambda: self.client.poll(self.config.worker_id), "poll"
            )
            if job:
                job_id = job.get("job_id")
                logger.info(f"Claimed job {job_id}")
                result = self._run_job_safely(job)
                logger.info(f"Job {job_id} result category={result.get('category')}")

                reported = self._call_with_retry(
                    lambda: self.client.report_result(self.config.worker_id, result),
                    "report_result",
                )
                if reported is None:
                    logger.error(
                        f"Failed to report result for {job_id}; coordinator will re-queue after claim timeout"
                    )
                else:
                    logger.info(f"Reported result for {job_id}")
            else:
                self.config.sleep_fn(self.config.poll_interval)

        logger.info("Stop polling threshold reached; starting graceful shutdown")
        if registered:
            self._call_with_retry(
                lambda: self.client.report_done(self.config.worker_id),
                "report_done",
            )
        with suppress(Exception):
            self.runner.stop()
        logger.info("Shutdown complete")


# ---------------------------------------------------------------------------
# Command-line entry point
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="Worker pool agent")
    parser.add_argument("--worker-id", type=int, required=True)
    parser.add_argument("--coordinator-url", required=True)
    parser.add_argument("--mock-runner", action="store_true",
                        help="Use MockWindowsSandboxRunner instead of the real WindowsSandboxRunner")
    parser.add_argument("--run-url", default="")
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--stop-polling-after", type=float, default=17700.0)
    return parser.parse_args()


def main():
    args = parse_args()

    secret = os.environ.get("GITHUB_SHARED_SECRET")
    if not secret:
        logger.error("GITHUB_SHARED_SECRET environment variable not set")
        return 1

    python_versions = [
        v.strip()
        for v in os.environ.get(
            "WINDOWS_PYTHON_VERSIONS",
            "3.11.9,3.12.10,3.13.15,3.14.7",
        ).split(",")
        if v.strip()
    ]

    environment = {
        "platform": "windows",
        "os_version": os.environ.get("WINDOWS_OS_VERSION", "windows-2025"),
        "python_versions": python_versions,
        "notes": (
            f"Runner: {os.environ.get('RUNNER_LABEL', 'windows-2025')}; "
            f"Run: {os.environ.get('GITHUB_RUN_ID', 'local')}"
        ),
    }

    run_url = args.run_url
    if not run_url and "GITHUB_REPOSITORY" in os.environ and "GITHUB_RUN_ID" in os.environ:
        run_url = f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    if not run_url:
        run_url = "https://local.example.com"

    # Choose runner
    if args.mock_runner or WindowsSandboxRunner is None:
        if WindowsSandboxRunner is None and not args.mock_runner:
            logger.warning(
                "windows_runner.WindowsSandboxRunner not found; falling back to MockWindowsSandboxRunner. "
                "Use --mock-runner to suppress this warning."
            )
        runner = MockWindowsSandboxRunner()
    else:
        runner = WindowsSandboxRunner()

    config = AgentConfig(
        worker_id=args.worker_id,
        coordinator_url=args.coordinator_url,
        secret=secret,
        environment=environment,
        poll_interval=args.poll_interval,
        stop_polling_after=args.stop_polling_after,
        run_url=run_url,
    )

    client = CoordinatorClient(args.coordinator_url, secret)
    agent = PollingAgent(config, client, runner)

    try:
        agent.run()
    finally:
        client.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# server_script.py
#
# Exports SERVER_PY, the source that gets written into the sandbox and
# run there. manager.py substitutes the __PLACEHOLDER__ tokens below
# with values from constants.py using plain string.replace (NOT
# str.format -- the dict literals here use {} and would break .format()).
#
# Protocol: instead of a single task.json/result.json pair (one task in
# flight at a time), the host writes one file per task into tasks/, and
# this server writes one file per result into results/, both named
# <task_id>.json. Each incoming "run" task is handled on its own thread,
# which blocks on the venv pool until a slot for the requested version
# is free. That's what ties concurrency directly to pool size: N warm
# venvs for a version means N "run" tasks for that version can be
# in-flight at once; the (N+1)th just waits.

SERVER_PY = '''
import os
import json
import subprocess
import time
import sys
import shutil
import threading
import queue
from pathlib import Path

SHARED_FOLDER = Path("C:/Shared")
TASKS_DIR = SHARED_FOLDER / "tasks"
RESULTS_DIR = SHARED_FOLDER / "results"
READY_FILE = SHARED_FOLDER / "ready.txt"

TEMPLATE_ROOT = SHARED_FOLDER / "_venv_templates"
POOL_ROOT = SHARED_FOLDER / "_venv_pool"

# Map version strings to sandbox Python paths. These interpreters now
# live inside the shared folder itself (e.g. host F:\Abdullah\Codes\
# Dataset\Shared\312 -> C:/Shared/312 in the sandbox), not at a fixed
# drive-root path, so they're built relative to SHARED_FOLDER instead
# of hardcoded.
PYTHON_PATHS = {
    "3.11": str(SHARED_FOLDER / "Python311" / "python.exe"),
    "3.12": str(SHARED_FOLDER / "Python312" / "python.exe"),
    "3.13": str(SHARED_FOLDER / "Python313" / "python.exe"),
    "3.14": str(SHARED_FOLDER / "Python314" / "python.exe"),
}

# Injected from constants.py by manager.py
VENV_POOL_SIZE = __VENV_POOL_SIZE__
PREWARM_VERSIONS = __PREWARM_VERSIONS__
DEFAULT_VERSION = "__DEFAULT_VERSION__"


def _robocopy_mirror(src, dst):
    """Mirror src -> dst. Deletes files in dst not present in src,
    copies new/changed files. Used to clone a pool slot from its
    template, and to wipe a slot back to pristine after a task finishes.
    robocopy's "success" exit codes are 0-7, not just 0, so we don't
    check=True here.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "robocopy", str(src), str(dst), "/MIR",
            "/NFL", "/NDL", "/NJH", "/NJS", "/NC", "/NS", "/NP",
        ],
        capture_output=True,
    )


class VenvSlot:
    def __init__(self, path, version, index):
        self.path = path
        self.version = version
        self.index = index
        self.busy = False          # currently running a task
        self.dirty = False         # needs cleaning (queued for wipe)
        self.marked_for_removal = False


class VenvPool:
    """Manages a reusable pool of venvs per Python version, and gates
    concurrency on it: acquire() blocks until a slot is free, so at most
    len(pool) "run" tasks for a version execute at the same time.

    Cleaning (the slow robocopy) is done asynchronously in a background
    thread so the pool lock is never held for a long time.
    """

    def __init__(self, python_paths, pool_size):
        self.python_paths = python_paths
        self.pool_size = pool_size
        self.pools = {}          # version -> list[VenvSlot]
        self._building = set()
        self.cond = threading.Condition()
        self.clean_queue = queue.Queue()
        self.clean_thread = threading.Thread(target=self._cleaner, daemon=True)
        self.clean_thread.start()
        TEMPLATE_ROOT.mkdir(parents=True, exist_ok=True)
        POOL_ROOT.mkdir(parents=True, exist_ok=True)

    def _cleaner(self):
        """Background thread: takes dirty slots, wipes them, then marks idle."""
        while True:
            slot = self.clean_queue.get()
            try:
                template_dir = self._template_dir(slot.version)
                _robocopy_mirror(template_dir, slot.path)
            except Exception as e:
                # If wipe fails, delete the slot to avoid pollution.
                print(f"Clean failed for {slot.path}: {e}")
                with self.cond:
                    if slot in self.pools.get(slot.version, []):
                        self.pools[slot.version].remove(slot)
                    shutil.rmtree(slot.path, ignore_errors=True)
            finally:
                with self.cond:
                    slot.dirty = False
                    slot.busy = False
                    self.cond.notify_all()

    def _template_dir(self, version):
        return TEMPLATE_ROOT / f"template_{version}"

    def _ensure_template(self, version):
        """Build the one-time template venv for a version, if it isn't
        already there. Slow (runs ensurepip) but only happens once."""
        template_dir = self._template_dir(version)
        marker = template_dir / "Scripts" / "python.exe"
        if marker.exists():
            return template_dir
        python_exe = self.python_paths.get(version)
        if not python_exe:
            raise RuntimeError(f"Unknown Python version: {version}")
        if not Path(python_exe).exists():
            raise RuntimeError(f"Python {version} not found at {python_exe}")
        if template_dir.exists():
            shutil.rmtree(template_dir, ignore_errors=True)
        subprocess.run(
            [python_exe, "-m", "venv", str(template_dir)],
            check=True, capture_output=True,
        )
        return template_dir

    def _new_slot(self, version, template_dir, index):
        slot_dir = POOL_ROOT / version / f"venv_{index}"
        _robocopy_mirror(template_dir, slot_dir)
        return VenvSlot(slot_dir, version, index)

    def ensure_pool(self, version, size=None):
        """Make sure `version` has at least `size` (default
        VENV_POOL_SIZE) slots. Safe to call from multiple threads for
        the same brand-new version -- only one of them actually builds.
        """
        size = self.pool_size if size is None else size

        with self.cond:
            while version in self._building:
                self.cond.wait()
            slots = self.pools.get(version, [])
            if len(slots) >= size:
                return len(slots)
            self._building.add(version)

        try:
            template_dir = self._ensure_template(version)  # slow, outside lock
            with self.cond:
                start = len(self.pools.get(version, []))
                need = max(0, size - start)
            new_slots = [self._new_slot(version, template_dir, start + i) for i in range(need)]
            with self.cond:
                self.pools.setdefault(version, []).extend(new_slots)
                return len(self.pools[version])
        finally:
            with self.cond:
                self._building.discard(version)
                self.cond.notify_all()

    def grow(self, version, by):
        """Explicitly add `by` slots on top of whatever's there now --
        raises the concurrency ceiling for this version immediately."""
        current = len(self.pools.get(version, []))
        total = self.ensure_pool(version, size=current + by)
        with self.cond:
            self.cond.notify_all()
        return total

    def shrink(self, version, by):
        """Lower the concurrency ceiling for `version` by up to `by`.
        Idle slots are deleted right away. If there aren't enough idle
        slots, busy ones are flagged and torn down as soon as they
        finish instead of being reset back into the pool.
        """
        with self.cond:
            slots = self.pools.get(version, [])
            removed = 0
            i = len(slots) - 1
            while i >= 0 and removed < by:
                slot = slots[i]
                # Can remove if not busy (including dirty or idle)
                if not slot.busy:
                    # If it's dirty, it's in the queue – we can remove it now.
                    shutil.rmtree(slot.path, ignore_errors=True)
                    slots.pop(i)
                    removed += 1
                i -= 1
            still_needed = by - removed
            if still_needed > 0:
                flagged = 0
                for slot in slots:
                    if flagged >= still_needed:
                        break
                    if slot.busy and not slot.marked_for_removal:
                        slot.marked_for_removal = True
                        flagged += 1
            return removed

    def acquire(self, version, timeout=None):
        """Block until a venv slot for `version` is free, then claim it.
        This is the concurrency gate: with a pool of N slots, at most N
        callers hold a slot at once; the rest wait here.
        """
        if version not in self.pools:
            self.ensure_pool(version)

        start = time.time()
        with self.cond:
            while True:
                for slot in self.pools.get(version, []):
                    if not slot.busy and not slot.dirty and not slot.marked_for_removal:
                        slot.busy = True
                        return slot
                if timeout is not None and time.time() - start > timeout:
                    raise TimeoutError(f"Timed out waiting for a free venv for {version}")
                self.cond.wait(timeout=1.0)

    def release(self, slot):
        """Called after task finishes. Mark slot as dirty and enqueue for cleaning."""
        with self.cond:
            slot.busy = False
            # If marked for removal, delete it instead of cleaning.
            if slot.marked_for_removal:
                self.pools[slot.version].remove(slot)
                shutil.rmtree(slot.path, ignore_errors=True)
                self.cond.notify_all()
                return
            slot.dirty = True
        # Enqueue for background cleaning (lock is released here)
        self.clean_queue.put(slot)
        # Do NOT notify waiters here; they'll be notified when cleaner finishes.

    def status(self):
        with self.cond:
            return {
                version: {
                    "total": len(slots),
                    "busy": sum(1 for s in slots if s.busy),
                    "idle": sum(1 for s in slots if not s.busy and not s.dirty),
                    "dirty": sum(1 for s in slots if s.dirty),
                }
                for version, slots in self.pools.items()
            }


def execute_python_code(pool, code, version, acquire_timeout):
    if version not in PYTHON_PATHS:
        return {"error": f"Unknown Python version: {version}"}
    try:
        slot = pool.acquire(version, timeout=acquire_timeout)
    except Exception as e:
        return {"error": f"Could not acquire venv: {e}"}
    try:
        venv_python = slot.path / "Scripts" / "python.exe"
        result = subprocess.run(
            [str(venv_python), "-c", code],
            capture_output=True, text=True, timeout=120,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"error": "Execution timed out after 120 seconds"}
    except Exception as e:
        return {"error": str(e)}
    finally:
        pool.release(slot)


def handle_task(pool, task):
    action = task.get("action", "run")

    if action == "run":
        code = task.get("code", "")
        version = task.get("version", DEFAULT_VERSION)
        acquire_timeout = task.get("acquire_timeout", 120)
        return execute_python_code(pool, code, version, acquire_timeout)

    if action == "create_venv":
        version = task.get("version", DEFAULT_VERSION)
        count = int(task.get("count", 1))
        try:
            total = pool.grow(version, count)
            return {"ok": True, "version": version, "total_slots": total}
        except Exception as e:
            return {"error": str(e)}

    if action == "remove_venv":
        version = task.get("version", DEFAULT_VERSION)
        count = int(task.get("count", 1))
        removed = pool.shrink(version, count)
        return {"ok": True, "version": version, "removed_now": removed}

    if action == "status":
        return {"ok": True, "pools": pool.status()}

    return {"error": f"Unknown action: {action}"}


def process_task(pool, task_id, task):
    try:
        result = handle_task(pool, task)
    except Exception as e:
        result = {"error": f"Server error: {e}"}
    (RESULTS_DIR / f"{task_id}.json").write_text(json.dumps(result), encoding="utf-8")


def main():
    SHARED_FOLDER.mkdir(parents=True, exist_ok=True)
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    pool = VenvPool(PYTHON_PATHS, VENV_POOL_SIZE)

    for version in PREWARM_VERSIONS:
        try:
            print(f"Pre-warming venv pool for Python {version} ({VENV_POOL_SIZE} slots)...")
            pool.ensure_pool(version)
        except Exception as e:
            print(f"Failed to pre-warm pool for {version}: {e}")

    READY_FILE.write_text("ready")
    print(f"Server ready. Watching {TASKS_DIR} (concurrency = venv pool size per version)...")

    while True:
        for task_path in sorted(TASKS_DIR.glob("*.json")):
            task_id = task_path.stem
            try:
                task = json.loads(task_path.read_text(encoding="utf-8"))
            except Exception as e:
                task_path.unlink(missing_ok=True)
                (RESULTS_DIR / f"{task_id}.json").write_text(
                    json.dumps({"error": f"Bad task file: {e}"}), encoding="utf-8"
                )
                continue
            task_path.unlink(missing_ok=True)
            t = threading.Thread(target=process_task, args=(pool, task_id, task), daemon=True)
            t.start()
        time.sleep(0.2)


if __name__ == "__main__":
    main()
'''

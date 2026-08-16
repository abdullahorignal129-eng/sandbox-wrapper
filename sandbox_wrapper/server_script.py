# server_script.py
#
# This module still just exports SERVER_PY, a string of the Python source
# that gets written into the sandbox and run there. manager.py substitutes
# the __PLACEHOLDER__ tokens below with values from constants.py before
# writing the file (plain string.replace, NOT str.format — the dict
# literals below use {} and would break .format()).

SERVER_PY = '''
import os
import json
import subprocess
import time
import sys
import shutil
from pathlib import Path

SHARED_FOLDER = Path("C:/Shared")
TASK_FILE = SHARED_FOLDER / "task.json"
RESULT_FILE = SHARED_FOLDER / "result.json"
READY_FILE = SHARED_FOLDER / "ready.txt"

TEMPLATE_ROOT = SHARED_FOLDER / "_venv_templates"
POOL_ROOT = SHARED_FOLDER / "_venv_pool"

# Map version strings to sandbox Python paths
PYTHON_PATHS = {
    "3.11": "C:/Python311/python.exe",
    "3.12": "C:/Python312/python.exe",
    "3.13": "C:/Python313/python.exe",
    "3.14": "C:/Python314/python.exe",
}

# Injected from constants.py by manager.py
VENV_POOL_SIZE = __VENV_POOL_SIZE__
PREWARM_VERSIONS = __PREWARM_VERSIONS__
DEFAULT_VERSION = "__DEFAULT_VERSION__"


def _robocopy_mirror(src, dst):
    """Mirror src -> dst. Fast incremental sync: deletes files in dst
    that aren't in src, copies new/changed files. Used both to clone a
    pool slot from its template and to wipe a slot back to pristine
    after a task finishes.

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
        self.busy = False


class VenvPool:
    """Manages a reusable pool of venvs per Python version.

    Instead of `python -m venv` + `shutil.rmtree` on every single task
    (slow: venv creation runs ensurepip every time), each version gets
    one pristine "template" venv built once. Pool slots are cheap
    robocopy clones of that template. When a task finishes, the slot is
    wiped back to pristine by robocopy-mirroring the template over it
    again -- this removes anything pip installed and reverts any files
    the task's code touched, without paying the ensurepip cost.
    """

    def __init__(self, python_paths, pool_size):
        self.python_paths = python_paths
        self.pool_size = pool_size
        self.pools = {}  # version -> list[VenvSlot]
        TEMPLATE_ROOT.mkdir(parents=True, exist_ok=True)
        POOL_ROOT.mkdir(parents=True, exist_ok=True)

    def _template_dir(self, version):
        return TEMPLATE_ROOT / f"template_{version}"

    def _ensure_template(self, version):
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

    def grow(self, version, by):
        """Add `by` more slots to this version's pool, ignoring the
        VENV_POOL_SIZE cap -- this is the explicit "make more venvs on
        the go" path.
        """
        template_dir = self._ensure_template(version)
        slots = self.pools.setdefault(version, [])
        start = len(slots)
        for i in range(by):
            slots.append(self._new_slot(version, template_dir, start + i))
        return len(slots)

    def shrink(self, version, by):
        """Remove up to `by` idle slots from the end of the pool.
        Busy slots are never removed.
        """
        slots = self.pools.get(version, [])
        removed = 0
        i = len(slots) - 1
        while i >= 0 and removed < by:
            if not slots[i].busy:
                shutil.rmtree(slots[i].path, ignore_errors=True)
                slots.pop(i)
                removed += 1
            i -= 1
        return removed

    def ensure_pool(self, version, size=None):
        size = self.pool_size if size is None else size
        slots = self.pools.setdefault(version, [])
        if len(slots) < size:
            self.grow(version, size - len(slots))
        return len(self.pools[version])

    def acquire(self, version):
        template_dir = self._ensure_template(version)
        slots = self.pools.setdefault(version, [])
        for slot in slots:
            if not slot.busy:
                slot.busy = True
                return slot
        if len(slots) < self.pool_size:
            slot = self._new_slot(version, template_dir, len(slots))
            slots.append(slot)
            slot.busy = True
            return slot
        # Pool is at cap and every slot shows busy. In the normal
        # single-task-at-a-time flow this only happens if a previous
        # task crashed without releasing its slot. Recover by reusing
        # the first slot rather than hanging forever.
        slot = slots[0]
        _robocopy_mirror(template_dir, slot.path)
        slot.busy = True
        return slot

    def release(self, slot):
        template_dir = self._template_dir(slot.version)
        _robocopy_mirror(template_dir, slot.path)
        slot.busy = False

    def status(self):
        return {
            version: {
                "total": len(slots),
                "busy": sum(1 for s in slots if s.busy),
                "idle": sum(1 for s in slots if not s.busy),
            }
            for version, slots in self.pools.items()
        }


def execute_python_code(pool, code, version):
    """Run code using a venv checked out from the pool, then wipe and
    return that venv to the pool instead of deleting it."""
    if version not in PYTHON_PATHS:
        return {"error": f"Unknown Python version: {version}"}
    try:
        slot = pool.acquire(version)
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
        return execute_python_code(pool, code, version)

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
        return {"ok": True, "version": version, "removed": removed}

    if action == "status":
        return {"ok": True, "pools": pool.status()}

    return {"error": f"Unknown action: {action}"}


def main():
    SHARED_FOLDER.mkdir(parents=True, exist_ok=True)
    pool = VenvPool(PYTHON_PATHS, VENV_POOL_SIZE)

    for version in PREWARM_VERSIONS:
        try:
            print(f"Pre-warming venv pool for Python {version} ({VENV_POOL_SIZE} slots)...")
            pool.ensure_pool(version)
        except Exception as e:
            print(f"Failed to pre-warm pool for {version}: {e}")

    READY_FILE.write_text("ready")
    print(f"Server ready. Watching {TASK_FILE}...")

    while True:
        if TASK_FILE.exists():
            try:
                with open(TASK_FILE, "r") as f:
                    task = json.load(f)
                result = handle_task(pool, task)
                with open(RESULT_FILE, "w") as f:
                    json.dump(result, f)
                TASK_FILE.unlink()
            except Exception as e:
                with open(RESULT_FILE, "w") as f:
                    json.dump({"error": f"Server error: {str(e)}"}, f)
                if TASK_FILE.exists():
                    TASK_FILE.unlink()
        time.sleep(0.5)


if __name__ == "__main__":
    main()
'''

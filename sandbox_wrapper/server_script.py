SERVER_PY = r'''
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
ENVS_FOLDER = SHARED_FOLDER / "envs"

# Manually-copied, writable Python installs live directly under C:\Shared.
# You are responsible for copying these in yourself, e.g.:
#   G:\Archnemix\Dataset\Shared\Python311\
#   G:\Archnemix\Dataset\Shared\Python312\
#   G:\Archnemix\Dataset\Shared\Python313\
#   G:\Archnemix\Dataset\Shared\Python314\
# These are never touched or written to by the server itself -- only venvs
# built FROM them are created/deleted.
PYTHON_PATHS = {
    "3.11": "C:/Shared/Python311/python.exe",
    "3.12": "C:/Shared/Python312/python.exe",
    "3.13": "C:/Shared/Python313/python.exe",
    "3.14": "C:/Shared/Python314/python.exe",
}


def pip_install(venv_python, packages, timeout=180):
    """Install a list of package specs into the given venv. Raises on failure."""
    print(f"[pip_install] installing: {packages}", flush=True)
    result = subprocess.run(
        [str(venv_python), "-m", "pip", "install", *packages],
        capture_output=True, text=True, timeout=timeout,
    )
    print(f"[pip_install] returncode={result.returncode} stdout={result.stdout[-2000:]!r} stderr={result.stderr[-2000:]!r}", flush=True)
    if result.returncode != 0:
        raise RuntimeError(f"pip install failed: {result.stderr or result.stdout}")


def get_or_create_persistent_env(python_exe, version, env_id, packages, venv_timeout=120, pip_timeout=180):
    """
    Reuse C:\\Shared\\envs\\<env_id> if it already exists; otherwise create it
    (with pip) and install packages. Never deleted automatically -- persists
    across sandbox sessions since it lives under the writable Shared mount.
    """
    ENVS_FOLDER.mkdir(parents=True, exist_ok=True)
    env_dir = ENVS_FOLDER / env_id
    venv_python = env_dir / "Scripts" / "python.exe"

    if env_dir.exists() and venv_python.exists():
        print(f"[get_or_create_persistent_env] reusing existing env_id={env_id!r} at {env_dir}", flush=True)
        return venv_python

    print(f"[get_or_create_persistent_env] creating new env_id={env_id!r} at {env_dir} (version={version})", flush=True)
    subprocess.run(
        [python_exe, "-m", "venv", str(env_dir)],
        check=True, capture_output=True, text=True, timeout=venv_timeout,
    )
    if packages:
        pip_install(venv_python, packages, timeout=pip_timeout)
    return venv_python


def create_throwaway_env(python_exe, version, packages, venv_timeout=120, pip_timeout=180):
    """
    Create a fresh, uniquely-named venv for a single task. Caller is
    responsible for deleting it afterward (see execute_python_code's finally
    block).
    """
    venv_dir = SHARED_FOLDER / f"venv_{version}_{int(time.time())}_{os.getpid()}"
    print(f"[create_throwaway_env] creating at {venv_dir}", flush=True)
    subprocess.run(
        [python_exe, "-m", "venv", str(venv_dir)],
        check=True, capture_output=True, text=True, timeout=venv_timeout,
    )
    venv_python = venv_dir / "Scripts" / "python.exe"
    if packages:
        pip_install(venv_python, packages, timeout=pip_timeout)
    return venv_dir, venv_python


def run_code_in_env(venv_python, code, timeout=60):
    print(f"[run_code_in_env] running via {venv_python}", flush=True)
    result = subprocess.run(
        [str(venv_python), "-c", code],
        capture_output=True, text=True, timeout=timeout,
    )
    print(f"[run_code_in_env] complete. returncode={result.returncode}", flush=True)
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
    }


def execute_python_code(code, version, packages=None, env_id=None):
    """
    Execute Python code using the specified interpreter version.

    - env_id given: reuse/create a persistent, never-auto-deleted venv under
      C:\\Shared\\envs\\<env_id>. Good for repeated calls with the same
      dependency set.
    - env_id omitted: create a throwaway venv, install packages if any, run
      the code, then delete the venv -- clean workspace per task.
    """
    packages = packages or []
    print(f"[execute_python_code] version={version} env_id={env_id} packages={packages}", flush=True)

    python_exe = PYTHON_PATHS.get(version)
    if not python_exe:
        return {"error": f"Unknown Python version: {version}"}
    if not Path(python_exe).exists():
        print(f"[execute_python_code] python_exe not found: {python_exe}", flush=True)
        return {"error": f"Python {version} not found at {python_exe}. Did you copy it into C:/Shared?"}

    throwaway_dir = None
    try:
        if env_id:
            venv_python = get_or_create_persistent_env(python_exe, version, env_id, packages)
        else:
            throwaway_dir, venv_python = create_throwaway_env(python_exe, version, packages)

        return run_code_in_env(venv_python, code)

    except subprocess.CalledProcessError as e:
        print(f"[execute_python_code] FAILED: returncode={e.returncode} stdout={e.stdout!r} stderr={e.stderr!r}", flush=True)
        return {"error": f"setup failed: {e.stderr or e.stdout or str(e)}"}
    except subprocess.TimeoutExpired as e:
        print(f"[execute_python_code] TIMEOUT during: {e.cmd}", flush=True)
        return {"error": f"Execution timed out: {e.cmd}"}
    except Exception as e:
        print(f"[execute_python_code] EXCEPTION: {type(e).__name__}: {e}", flush=True)
        return {"error": str(e)}
    finally:
        # Only throwaway envs get cleaned up. Persistent env_id envs are
        # intentionally left alone.
        if throwaway_dir and throwaway_dir.exists():
            print(f"[execute_python_code] cleaning up throwaway env {throwaway_dir}", flush=True)
            shutil.rmtree(throwaway_dir, ignore_errors=True)


def main():
    SHARED_FOLDER.mkdir(parents=True, exist_ok=True)
    READY_FILE.write_text("ready")
    print(f"Server ready. Watching {TASK_FILE}...", flush=True)

    while True:
        if TASK_FILE.exists():
            print(f"[main] task detected: {TASK_FILE}", flush=True)
            try:
                with open(TASK_FILE, 'r') as f:
                    task = json.load(f)
                code = task.get('code', '')
                version = task.get('version', '3.12')
                packages = task.get('packages') or []
                env_id = task.get('env_id')
                print(f"[main] dispatching task version={version} env_id={env_id} packages={packages} code={code!r}", flush=True)

                result = execute_python_code(code, version, packages=packages, env_id=env_id)
                print(f"[main] result={result}", flush=True)

                with open(RESULT_FILE, 'w') as f:
                    json.dump(result, f)
                TASK_FILE.unlink()
                print(f"[main] result written, task file removed", flush=True)
            except Exception as e:
                print(f"[main] EXCEPTION handling task: {type(e).__name__}: {e}", flush=True)
                with open(RESULT_FILE, 'w') as f:
                    json.dump({"error": f"Server error: {str(e)}"}, f)
                if TASK_FILE.exists():
                    TASK_FILE.unlink()
        time.sleep(0.5)


if __name__ == "__main__":
    main()
'''

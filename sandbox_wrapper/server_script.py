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

# Map version strings to sandbox Python paths
PYTHON_PATHS = {
    "3.11": "C:/Python311/python.exe",
    "3.12": "C:/Python312/python.exe",
    "3.13": "C:/Python313/python.exe",
    "3.14": "C:/Python314/python.exe",
}

def execute_python_code(code, version):
    """Execute Python code using the specified version."""
    print(f"[execute_python_code] version={version}", flush=True)

    python_exe = PYTHON_PATHS.get(version)
    if not python_exe:
        return {"error": f"Unknown Python version: {version}"}

    if not Path(python_exe).exists():
        print(f"[execute_python_code] python_exe not found: {python_exe}", flush=True)
        return {"error": f"Python {version} not found at {python_exe}"}

    # Create temporary venv using this version
    venv_dir = SHARED_FOLDER / f"venv_{version}_{int(time.time())}"
    try:
        print(f"[execute_python_code] creating venv at {venv_dir} ...", flush=True)
        venv_result = subprocess.run(
            [python_exe, "-m", "venv", str(venv_dir)],
            check=True, capture_output=True, text=True, timeout=90,
        )
        print(f"[execute_python_code] venv created. stdout={venv_result.stdout!r} stderr={venv_result.stderr!r}", flush=True)

        venv_python = venv_dir / "Scripts" / "python.exe"
        print(f"[execute_python_code] venv_python={venv_python} exists={venv_python.exists()}", flush=True)

        print(f"[execute_python_code] running code in venv...", flush=True)
        result = subprocess.run(
            [str(venv_python), "-c", code],
            capture_output=True, text=True, timeout=60,
        )
        print(f"[execute_python_code] run complete. returncode={result.returncode}", flush=True)

        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except subprocess.CalledProcessError as e:
        print(f"[execute_python_code] venv creation FAILED: returncode={e.returncode} stdout={e.stdout!r} stderr={e.stderr!r}", flush=True)
        return {"error": f"venv creation failed: {e.stderr or e.stdout or str(e)}"}
    except subprocess.TimeoutExpired as e:
        print(f"[execute_python_code] TIMEOUT during: {e.cmd}", flush=True)
        return {"error": f"Execution timed out: {e.cmd}"}
    except Exception as e:
        print(f"[execute_python_code] EXCEPTION: {type(e).__name__}: {e}", flush=True)
        return {"error": str(e)}
    finally:
        if venv_dir.exists():
            print(f"[execute_python_code] cleaning up {venv_dir}", flush=True)
            shutil.rmtree(venv_dir, ignore_errors=True)


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
                print(f"[main] dispatching task version={version} code={code!r}", flush=True)

                result = execute_python_code(code, version)
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

SERVER_PY = '''
import os
import json
import subprocess
import time
import sys
import shutil
from pathlib import Path

SHARED_FOLDER = Path("C:/Users/WDAGUtilityAccount/Desktop/Shared")
TASK_FILE = SHARED_FOLDER / "task.json"
RESULT_FILE = SHARED_FOLDER / "result.json"
READY_FILE = SHARED_FOLDER / "ready.txt"
PYTHON_VERSIONS_DIR = SHARED_FOLDER / "Python_versions"

def execute_python_code(code, version):
    """Execute Python code using the specified version."""
    # Path to the correct python.exe
    python_exe = PYTHON_VERSIONS_DIR / version / "python.exe"
    if not python_exe.exists():
        return {"error": f"Python version {version} not found at {python_exe}"}

    # Create temporary venv using this version
    venv_dir = SHARED_FOLDER / f"venv_{version}_{int(time.time())}"
    try:
        subprocess.run([str(python_exe), "-m", "venv", str(venv_dir)],
                       check=True, capture_output=True)
        venv_python = venv_dir / "Scripts" / "python.exe"
        result = subprocess.run([str(venv_python), "-c", code],
                                capture_output=True, text=True, timeout=120)
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode
        }
    except subprocess.TimeoutExpired:
        return {"error": "Execution timed out after 120 seconds"}
    except Exception as e:
        return {"error": str(e)}
    finally:
        if venv_dir.exists():
            shutil.rmtree(venv_dir, ignore_errors=True)

def main():
    SHARED_FOLDER.mkdir(parents=True, exist_ok=True)
    READY_FILE.write_text("ready")
    print(f"Server ready. Watching {TASK_FILE}...")
    while True:
        if TASK_FILE.exists():
            try:
                with open(TASK_FILE, 'r') as f:
                    task = json.load(f)
                code = task.get('code', '')
                version = task.get('version', '3.12')
                result = execute_python_code(code, version)
                with open(RESULT_FILE, 'w') as f:
                    json.dump(result, f)
                TASK_FILE.unlink()
            except Exception as e:
                with open(RESULT_FILE, 'w') as f:
                    json.dump({"error": f"Server error: {str(e)}"}, f)
                if TASK_FILE.exists():
                    TASK_FILE.unlink()
        time.sleep(0.5)

if __name__ == "__main__":
    main()
'''

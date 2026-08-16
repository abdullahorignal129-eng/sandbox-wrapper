# server_script.py - The Python code that runs INSIDE the sandbox
SERVER_PY = '''\
import os
import json
import subprocess
import time
import sys
import shutil
from pathlib import Path

# The shared folder path inside the sandbox (now on the Desktop)
SHARED_FOLDER = Path(r"C:\Users\WDAGUtilityAccount\Desktop\Shared")
TASK_FILE = SHARED_FOLDER / "task.json"
RESULT_FILE = SHARED_FOLDER / "result.json"
READY_FILE = SHARED_FOLDER / "ready.txt"

def execute_python_code(code, version):
    """Execute Python code using the specified version and return the result."""
    # Create a temporary venv for isolation
    venv_dir = SHARED_FOLDER / f"venv_{version}_{int(time.time())}"
    try:
        # Create the virtual environment using the system Python launcher
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], 
                       check=True, capture_output=True)
        python_exe = venv_dir / "Scripts" / "python.exe"
        
        # Execute the code
        result = subprocess.run([str(python_exe), "-c", code], 
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
        # Clean up the venv
        if venv_dir.exists():
            shutil.rmtree(venv_dir, ignore_errors=True)

def main():
    # Ensure the Shared folder exists on the desktop
    SHARED_FOLDER.mkdir(parents=True, exist_ok=True)
    
    # Write ready signal
    READY_FILE.write_text("ready")
    print(f"Server ready. Watching {TASK_FILE}...")
    
    while True:
        if TASK_FILE.exists():
            try:
                with open(TASK_FILE, 'r') as f:
                    task = json.load(f)
                code = task.get('code', '')
                version = task.get('version', '3.12')
                
                # Execute
                result = execute_python_code(code, version)
                
                # Write result
                with open(RESULT_FILE, 'w') as f:
                    json.dump(result, f)
                
                # Remove task file
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

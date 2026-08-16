# Sandbox Wrapper

A lightweight Python package to run isolated Python code with multiple versions using Windows Sandbox.

## Features
- Runs Python code in a clean Windows Sandbox VM.
- Supports Python 3.11, 3.12, 3.13, 3.14 (via the Python launcher).
- No network ports exposed; uses file-based communication.
- Each task gets its own temporary virtual environment.
- Works on Windows 10 and 11.

## Installation
```bash
pip install git+https://github.com/abdullahorignal129-eng/sandbox-wrapper.git
```

## Usage

```python
from sandbox_wrapper import SandboxManager

# Create a manager (customize shared folder if needed)
manager = SandboxManager()

# Launch the sandbox (do this once)
manager.launch()

# Run some code
result = manager.run_code('print("Hello from Python 3.12!")', version='3.12')
print(result['stdout'])

# Run with a different version
result = manager.run_code('import sys; print(sys.version)', version='3.13')
print(result['stdout'])

# Clean up when done
manager.close()
```

Or use as a context manager:

```python
with SandboxManager() as manager:
    result = manager.run_code('print("inside sandbox")')
    print(result)
```

##Configuration

By default, the shared folder is G:\Archnemix\Dataset\Shared. To change it:

```python
manager = SandboxManager(
    shared_folder=Path(r"C:\MyShared"),
    wsb_folder=Path(r"C:\MyWSB")
)
```

## Requirements

Windows 10/11 with Windows Sandbox enabled.
Python 3.8+ on the host.
Python launcher (py.exe) installed (comes with Python).


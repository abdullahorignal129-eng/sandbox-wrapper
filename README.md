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

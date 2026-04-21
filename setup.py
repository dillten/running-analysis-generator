#!/usr/bin/env python3
"""Create a virtual environment and install dependencies."""

import subprocess
import sys
from pathlib import Path

VENV_DIR = Path(__file__).parent / ".venv"
DEPS = ["jinja2", "markupsafe"]


def main():
    if not VENV_DIR.exists():
        print("Creating virtual environment...")
        subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True)

    pip = VENV_DIR / ("Scripts/pip" if sys.platform == "win32" else "bin/pip")
    print("Installing dependencies...")
    subprocess.run([str(pip), "install", "--quiet", "--upgrade", "pip"], check=True)
    subprocess.run([str(pip), "install", "--quiet"] + DEPS, check=True)

    print("\nSetup complete. Run the site generator with:")
    print("  python run.py")


if __name__ == "__main__":
    main()

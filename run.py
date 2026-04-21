#!/usr/bin/env python3
"""Build the site and serve it locally."""

import argparse
import http.server
import os
import shutil
import subprocess
import sys
from pathlib import Path

VENV_DIR = Path(__file__).parent / ".venv"
DIST_DIR = Path(__file__).parent / "dist"
PORT = 5500


def venv_python():
    if sys.platform == "win32":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python3"


def in_venv():
    return Path(sys.executable).resolve().is_relative_to(VENV_DIR.resolve())


def ensure_setup():
    python = venv_python()
    if not python.exists():
        print("Virtual environment not found. Running setup first...")
        subprocess.run([sys.executable, "setup.py"], check=True)


def main():
    parser = argparse.ArgumentParser(description="Build and serve the Garmin analysis site.")
    parser.add_argument("--clean", action="store_true", help="Delete the dist/ directory and exit.")
    args, _ = parser.parse_known_args()

    if args.clean:
        if DIST_DIR.exists():
            shutil.rmtree(DIST_DIR)
            print(f"Deleted {DIST_DIR}")
        else:
            print(f"{DIST_DIR} does not exist, nothing to delete.")
        return

    ensure_setup()

    # Re-launch inside the venv if not already there
    if not in_venv():
        result = subprocess.run([str(venv_python()), __file__] + sys.argv[1:])
        sys.exit(result.returncode)

    print("Building site...")
    import generate
    generate.build_site()

    print(f"\nDone. Serving at http://localhost:{PORT}")
    print("Press Ctrl+C to stop.\n")
    os.chdir(DIST_DIR)
    handler = http.server.SimpleHTTPRequestHandler
    handler.log_message = lambda *_: None  # silence request logs
    with http.server.HTTPServer(("", PORT), handler) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
        os._exit(0)

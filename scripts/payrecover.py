"""Run PayRecover AI with one command: python scripts/payrecover.py

The first run sets everything up (a Python virtual environment with every package, the browser
the website checks use, and the built dashboard), then later runs start straight away. It starts:

  the backend and dashboard   http://127.0.0.1:8000
  the Main store              http://127.0.0.1:8100
  the Vulnerable store        http://127.0.0.1:8200
  the Risky store             http://127.0.0.1:8300

Press Ctrl+C to stop them all. PayPal and Gemini keys are optional: without them the dashboard,
the sample scenarios and the sample dispute still work; add keys to backend/.env and
main-store/.env (see README) for live PayPal Sandbox checks.

  python scripts/payrecover.py            set up if needed, then start
  python scripts/payrecover.py setup      only set up (or update after a git pull)
  python scripts/payrecover.py start      only start
  python scripts/payrecover.py --port 9000  backend on 9000, stores on 9100, 9200 and 9300
"""

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
VENV = BACKEND / ".venv"
MIN_PYTHON = (3, 12)
STORES = (  # name, folder, port offset from the backend, extra environment
    ("Main store", "main-store", 100, {}),
    ("Vulnerable store", "vuln-store", 200, {}),
    ("Risky store", "vuln-store", 300, {"VULN_STORE_PROFILE": "risky"}),
)
ENV_FILES = ("backend/.env.example", "main-store/.env.example")


def say(text: str) -> None:
    print(f"[payrecover] {text}", flush=True)


def stop(text: str) -> None:
    say(text)
    sys.exit(1)


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run(command: list, cwd: Path = ROOT) -> None:
    say("$ " + " ".join(str(part) for part in command))
    if subprocess.run([str(part) for part in command], cwd=cwd).returncode != 0:
        stop("That step failed; see the output above. Fix it, then run this script again.")


def npm() -> str:
    found = shutil.which("npm")
    if not found:
        stop("Node.js is not installed (npm was not found). Install Node.js 22 or newer from https://nodejs.org, "
             "then run this script again.")
    return found


def is_set_up() -> bool:
    return venv_python().is_file() and (FRONTEND / "dist" / "index.html").is_file()


def setup() -> None:
    if sys.version_info < MIN_PYTHON:
        stop(f"PayRecover needs Python {'.'.join(map(str, MIN_PYTHON))} or newer; this is "
             f"{sys.version.split()[0]}. Install a newer Python from https://www.python.org and run it with that.")
    node = npm()
    if not venv_python().is_file():
        say("Creating the Python environment in backend/.venv")
        venv.create(VENV, with_pip=True)
    python = venv_python()
    run([python, "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
    run([python, "-m", "pip", "install", "--quiet", "-r", BACKEND / "requirements.txt"])
    # PayPal's Agent Toolkit without its OpenAI, LangChain and CrewAI adapters, which PayRecover does not use.
    run([python, "-m", "pip", "install", "--quiet", "--no-deps", "-r", BACKEND / "requirements-toolkit.txt"])
    for store in ("main-store", "vuln-store"):
        run([python, "-m", "pip", "install", "--quiet", "-r", ROOT / store / "requirements.txt"])
    say("Installing the browser the website checks use (Chromium, once)")
    run([python, "-m", "playwright", "install", "chromium"])
    say("Building the dashboard")
    run([node, "ci", "--no-audit", "--no-fund"], FRONTEND)
    run([node, "run", "build"], FRONTEND)
    for example in ENV_FILES:
        target = ROOT / example.removesuffix(".example")
        if not target.exists():
            shutil.copyfile(ROOT / example, target)
            say(f"Created {target.relative_to(ROOT)} (empty keys: add yours for live PayPal checks)")
    say("Set up.")


def wait_until_up(url: str, seconds: int = 40) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        return probe.connect_ex(("127.0.0.1", port)) == 0


def start(port: int) -> None:
    if not is_set_up():
        stop("PayRecover is not set up yet. Run: python scripts/payrecover.py setup")
    busy = [listen for listen in (port, port + 100, port + 200, port + 300) if port_in_use(listen)]
    if busy:
        stop(f"Port {', '.join(map(str, busy))} is already in use (is PayRecover already running?). "
             f"Stop it, or pick other ports: python scripts/payrecover.py --port 9000")
    python = str(venv_python())
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "PYTHONUNBUFFERED": "1",
           # The backend checks these stores; a store is chosen by name, never by a URL from a request.
           "TESTPILOT_STORE_URL": f"http://127.0.0.1:{port + 100}",
           "TESTPILOT_VULN_STORE_URL": f"http://127.0.0.1:{port + 200}",
           "TESTPILOT_RISKY_STORE_URL": f"http://127.0.0.1:{port + 300}",
           "TESTPILOT_FRONTEND_DIST": str(FRONTEND / "dist")}
    processes = []

    def launch(name: str, folder: Path, app: str, listen: int, extra: dict) -> None:
        command = [python, "-m", "uvicorn", app, "--app-dir", str(folder), "--host", "127.0.0.1", "--port", str(listen)]
        processes.append((name, subprocess.Popen(command, cwd=folder, env={**env, **extra})))

    for name, folder, offset, extra in STORES:
        launch(name, ROOT / folder, "store_app:app", port + offset, extra)
    launch("Backend and dashboard", BACKEND, "app.main:app", port, {})

    def shutdown(*_args) -> None:
        say("Stopping...")
        for _, process in processes:
            if process.poll() is None:
                process.terminate()
        for _, process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)
    if not wait_until_up(f"{base}/api/health"):
        say("The backend did not start; see its output above.")
        shutdown()
    print(f"""
  PayRecover AI is running. Open the dashboard:  {base}

  Main store        http://127.0.0.1:{port + 100}
  Vulnerable store  http://127.0.0.1:{port + 200}
  Risky store       http://127.0.0.1:{port + 300}
  API docs          {base}/docs

  No PayPal keys? Open Check payments > One order and press a sample button,
  or Disputes > Show a sample dispute. Press Ctrl+C here to stop everything.
""", flush=True)
    while True:
        for name, process in processes:
            if process.poll() is not None:
                say(f"{name} stopped (exit code {process.returncode}); stopping the rest.")
                shutdown()
        time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up and run PayRecover AI locally.")
    parser.add_argument("command", nargs="?", choices=("setup", "start"), help="default: set up if needed, then start")
    parser.add_argument("--port", type=int, default=8000, help="backend port; the stores use this +100, +200 and +300")
    args = parser.parse_args()
    if args.command == "setup":
        setup()
        return
    if args.command is None and not is_set_up():
        say("First run: setting up (a few minutes, once).")
        setup()
    start(args.port)


if __name__ == "__main__":
    main()

"""Supervisor for APIPlatform: keeps main.py running, restarts on crash.

Installed by install-daemon.ps1 as a logon scheduled task (pythonw, no window).
Manual restart of the service: run restart.bat (kills main.py; this daemon
relaunches it within seconds). Single instance guarded by data/daemon.pid.
"""
import ctypes
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PIDFILE = ROOT / "data" / "daemon.pid"
LOG = ROOT / "data" / "daemon.log"
PORT = int(os.environ.get("APIPLATFORM_PORT", "8800"))


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def port_alive():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/healthz", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def pid_running(pid):
    if pid <= 0:
        return False
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return False


def main():
    PIDFILE.parent.mkdir(exist_ok=True)
    if PIDFILE.exists():
        try:
            old = int(PIDFILE.read_text().strip())
            if pid_running(old):
                log(f"daemon pid={old} already running, exit")
                return
        except Exception:
            pass
    PIDFILE.write_text(str(os.getpid()))
    log("daemon started")
    try:
        while True:
            if port_alive():
                # someone runs main.py manually (e.g. run.bat); watch, don't duplicate
                time.sleep(5)
                continue
            log("launching main.py")
            with open(ROOT / "data" / "server.log", "ab") as out, \
                 open(ROOT / "data" / "server.err.log", "ab") as err:
                p = subprocess.Popen([sys.executable, "-B", "main.py"],
                                     cwd=str(ROOT), stdout=out, stderr=err)
            while p.poll() is None:
                time.sleep(2)
            log(f"main.py exited code={p.returncode}, restart in 3s")
            time.sleep(3)
    finally:
        PIDFILE.unlink(missing_ok=True)
        log("daemon stopped")


if __name__ == "__main__":
    main()

"""
LazyMirror — Main launcher.
Run:  python lazymirror.py
"""

import sys
import os
import subprocess
import threading
import time
import webbrowser
import signal
import shutil
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT      = Path(__file__).parent.resolve()
SRC       = ROOT / "src"
CACHE_DIR = ROOT / "offline_cache"
CERTS_DIR = ROOT / "certs"
LOG_DIR   = ROOT / "logs"

for d in (CACHE_DIR, CERTS_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)

os.environ["LAZYMIRROR_CACHE"] = str(CACHE_DIR)

# ── Find mitmdump executable ──────────────────────────────────────────────────
def find_mitmdump() -> str:
    """Locate mitmdump — checks PATH and common pip install locations."""
    # 1. Already on PATH
    found = shutil.which("mitmdump")
    if found:
        return found

    # 2. Alongside the current Python executable
    py_dir = Path(sys.executable).parent
    for name in ("mitmdump.exe", "mitmdump"):
        candidate = py_dir / name
        if candidate.exists():
            return str(candidate)
        candidate = py_dir / "Scripts" / name
        if candidate.exists():
            return str(candidate)

    return None


# ── Stale-instance cleanup ────────────────────────────────────────────────────
# If a previous LazyMirror run was left behind (e.g. console closed without
# Ctrl+C), its dashboard/proxy processes still hold our ports.  The functions
# below detect those orphans via netstat and kill them before we start.

def _port_pids(port: int) -> list:
    """Return list of PIDs currently LISTENING on the given port (netstat)."""
    try:
        out = subprocess.check_output(
            ["netstat", "-ano"], text=True, stderr=subprocess.DEVNULL, timeout=5
        )
        pids = []
        for line in out.splitlines():
            parts = line.split()
            # netstat line: Proto  LocalAddr  ForeignAddr  State  PID
            if len(parts) >= 5 and parts[3] == "LISTENING":
                if parts[1].endswith(f":{port}"):
                    try:
                        pids.append(int(parts[4]))
                    except ValueError:
                        pass
        return list(set(pids))
    except Exception:
        return []


def _cmdline_of(pid: int) -> str:
    """Return the command-line string for a PID (empty string on error)."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip().lower()
    except Exception:
        return ""


def release_ports():
    """Kill any orphaned LazyMirror processes that are holding our ports.

    Safety: only terminates processes whose command line contains a known
    LazyMirror script name.  Unrelated programs on the same port numbers
    (e.g. a local web server on 8080) are left untouched.
    """
    # The three ports LazyMirror owns: proxy, dashboard, cache-browser
    own_pid   = os.getpid()
    lm_ports  = [8080, 7779, 7780]
    lm_keywords = ("dashboard.py", "lazymirror.py", "proxy_addon.py", "mitmdump")
    killed    = set()

    for port in lm_ports:
        for pid in _port_pids(port):
            if pid == own_pid or pid in killed:
                continue
            cmdline = _cmdline_of(pid)
            if any(kw in cmdline for kw in lm_keywords):
                try:
                    subprocess.call(
                        ["taskkill", "/F", "/PID", str(pid)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                    print(f"  Stopped stale LazyMirror process (PID {pid}) on port {port}")
                    killed.add(pid)
                except Exception:
                    pass

    if killed:
        # Brief pause so the OS fully releases the port sockets
        time.sleep(0.8)


# ── Processes ─────────────────────────────────────────────────────────────────
proxy_proc     = None
dashboard_proc = None


def start_proxy(mitmdump: str):
    global proxy_proc
    addon_script = str(SRC / "proxy_addon.py")
    log_path = open(LOG_DIR / "proxy.log", "a", encoding="utf-8")

    cmd = [
        mitmdump,
        "--listen-host", "127.0.0.1",
        "--listen-port", "8080",
        "--set", f"confdir={CERTS_DIR}",
        "--set", "ssl_insecure=true",
        "--set", "connection_strategy=lazy",
        "-s", addon_script,
        "-q",   # quiet — no urwid TUI
    ]

    print(f"  CMD: {' '.join(cmd)}")
    proxy_proc = subprocess.Popen(
        cmd,
        stdout=log_path,
        stderr=log_path,
        env={**os.environ, "LAZYMIRROR_CACHE": str(CACHE_DIR)},
    )
    return proxy_proc


def start_dashboard():
    global dashboard_proc
    log_path = open(LOG_DIR / "dashboard.log", "a", encoding="utf-8")
    dashboard_proc = subprocess.Popen(
        [sys.executable, str(SRC / "dashboard.py")],
        stdout=log_path,
        stderr=log_path,
        env={**os.environ, "LAZYMIRROR_CACHE": str(CACHE_DIR)},
    )
    return dashboard_proc


def stop_all():
    for proc in (proxy_proc, dashboard_proc):
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


# ── Tray icon (optional) ──────────────────────────────────────────────────────
def run_tray():
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        return  # No tray — fine

    img  = Image.new("RGB", (64, 64), "#00e5a0")
    draw = ImageDraw.Draw(img)
    draw.rectangle([8, 8, 56, 56], fill="#0d0f14")
    draw.text((14, 20), "LM", fill="#00e5a0")

    def open_dash(icon, item):
        webbrowser.open("http://127.0.0.1:7779")

    def quit_app(icon, item):
        stop_all()
        icon.stop()

    icon = pystray.Icon(
        "LazyMirror", img, "LazyMirror — caching proxy active",
        pystray.Menu(
            pystray.MenuItem("Open Dashboard", open_dash, default=True),
            pystray.MenuItem("Quit", quit_app),
        )
    )
    icon.run()


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    # Clean up any orphaned processes from a previous run before grabbing ports
    release_ports()

    print()
    print("=" * 58)
    print("  LazyMirror — On-demand offline web archiver")
    print("=" * 58)

    mitmdump = find_mitmdump()
    if not mitmdump:
        print()
        print("  [ERROR] mitmdump not found!")
        print("  Run SETUP.bat first to install dependencies,")
        print("  then restart this launcher.")
        input("  Press Enter to exit…")
        sys.exit(1)

    print(f"  mitmdump : {mitmdump}")
    print(f"  Cache    : {CACHE_DIR}")
    print(f"  Certs    : {CERTS_DIR}")
    print(f"  Proxy    : 127.0.0.1:8080")
    print(f"  Dashboard: http://127.0.0.1:7779")
    print(f"  Browser  : http://127.0.0.1:7780")
    print("=" * 58)
    print()
    print("  Starting proxy…")

    start_proxy(mitmdump)
    time.sleep(2)

    if proxy_proc.poll() is not None:
        # Read log for error
        log_file = LOG_DIR / "proxy.log"
        err = log_file.read_text(encoding="utf-8", errors="replace")[-1000:] if log_file.exists() else "(no log)"
        print()
        print("  [ERROR] Proxy failed to start! Last log output:")
        print(err)
        input("  Press Enter to exit…")
        sys.exit(1)

    print("  [OK] Proxy running on 127.0.0.1:8080")
    print()
    print("  Starting dashboard...")
    start_dashboard()
    time.sleep(1)
    print("  [OK] Dashboard at http://127.0.0.1:7779")
    print("  [OK] Cache browser at http://127.0.0.1:7780")
    print()
    print("  Next steps:")
    print("  1. If not done: run install_cert.bat to trust HTTPS")
    print("  2. If not done: run configure_proxy.bat → [1]")
    print("  3. Browse any site in Chrome/Edge — it gets cached!")
    print()
    print("  Press Ctrl+C to stop LazyMirror.")
    print()

    threading.Timer(2, lambda: webbrowser.open("http://127.0.0.1:7779")).start()
    threading.Thread(target=run_tray, daemon=True).start()

    def handle_exit(sig, frame):
        print("\n  Shutting down…")
        stop_all()
        sys.exit(0)

    signal.signal(signal.SIGINT,  handle_exit)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_exit)

    # Keep alive + watchdog
    while True:
        time.sleep(2)
        if proxy_proc and proxy_proc.poll() is not None:
            print("  [!] Proxy crashed — restarting…")
            start_proxy(mitmdump)
        if dashboard_proc and dashboard_proc.poll() is not None:
            print("  [!] Dashboard crashed — restarting…")
            start_dashboard()


if __name__ == "__main__":
    main()

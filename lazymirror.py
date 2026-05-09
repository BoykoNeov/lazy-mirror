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

    print("  ✓ Proxy running on 127.0.0.1:8080")
    print()
    print("  Starting dashboard…")
    start_dashboard()
    time.sleep(1)
    print("  ✓ Dashboard at http://127.0.0.1:7779")
    print("  ✓ Cache browser at http://127.0.0.1:7780")
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

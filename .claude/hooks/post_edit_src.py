"""
PostToolUse hook for Edit/Write on src/*.py files.

Non-blocking. On every edit to a Python source file under src/:
  1. Copy the file to the working folder (M:\\temp\\lazy mirro claude 3\\src\\)
  2. Run pytest and print a one-line summary

Failures do NOT block — the commit-time hook is the gate for that. This is just
fast feedback so a broken edit doesn't go unnoticed.

Reads Claude Code hook JSON from stdin. Schema (relevant fields):
  {"tool_name": "Edit"|"Write", "tool_input": {"file_path": "..."}, ...}
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR   = REPO_ROOT / "src"
TEMP_DIR  = Path(r"M:\temp\lazy mirro claude 3\src")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        # Bad/missing payload — nothing useful we can do, exit silently.
        return 0

    tool_name = payload.get("tool_name", "")
    if tool_name not in ("Edit", "Write", "MultiEdit"):
        return 0

    file_path = payload.get("tool_input", {}).get("file_path", "")
    if not file_path:
        return 0

    p = Path(file_path).resolve()
    # Only fire for files under src/ ending in .py.
    try:
        p.relative_to(SRC_DIR)
    except ValueError:
        return 0
    if p.suffix != ".py":
        return 0

    # 1. Mirror the edited file to the working folder (preserves the user's
    #    "copy to temp before commit" habit without manual steps).
    try:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, TEMP_DIR / p.name)
        print(f"[hook] synced {p.name} -> {TEMP_DIR}", file=sys.stderr)
    except Exception as e:
        print(f"[hook] sync failed: {e}", file=sys.stderr)

    # 2. Run the test suite. Terse output to keep the log small. Non-blocking:
    #    exit 0 regardless of pytest result. The commit-time hook is the gate.
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--no-header",
             "--tb=line", "--maxfail=3"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode == 0:
            # Print only the summary line (last non-empty line of stdout).
            lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
            summary = lines[-1] if lines else "tests OK"
            print(f"[hook] pytest: {summary}", file=sys.stderr)
        else:
            # Surface failures so they're visible even though we don't block.
            print("[hook] pytest FAILED — fix before committing:", file=sys.stderr)
            print(proc.stdout[-2000:], file=sys.stderr)
            if proc.stderr.strip():
                print(proc.stderr[-1000:], file=sys.stderr)
    except subprocess.TimeoutExpired:
        print("[hook] pytest timed out (>60s)", file=sys.stderr)
    except Exception as e:
        print(f"[hook] pytest run error: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())

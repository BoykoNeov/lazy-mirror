"""
PreToolUse hook for Bash(git commit ...) — blocks the commit if pytest is red.

Reads Claude Code hook JSON from stdin. Schema:
  {"tool_name": "Bash", "tool_input": {"command": "..."}, ...}

Behaviour:
  - If the command isn't `git commit`, exit 0 (allow, no action).
  - If it IS `git commit`, run the full pytest suite.
  - On pass: exit 0 (allow the commit).
  - On fail: print failures to stderr and exit 2 (block the tool call).
"""
import json
import re
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

# Matches `git commit` (with or without args, with or without leading whitespace)
# but NOT things like `git commit-tree` or commits inside a larger pipeline.
COMMIT_RE = re.compile(r"\bgit\s+commit(\s|$)")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    if payload.get("tool_name") != "Bash":
        return 0

    command = payload.get("tool_input", {}).get("command", "")
    if not COMMIT_RE.search(command):
        return 0

    # Run the suite. Use the same Python that's running this hook.
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--no-header",
             "--tb=short", "--maxfail=5"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        print("[pre-commit] pytest timed out (>120s) — commit blocked", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"[pre-commit] pytest could not run: {e} — commit blocked", file=sys.stderr)
        return 2

    if proc.returncode == 0:
        # Tests green — let the commit through.
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        summary = lines[-1] if lines else "tests OK"
        print(f"[pre-commit] pytest passed: {summary}", file=sys.stderr)
        return 0

    # Red. Block the commit and surface the failure details.
    print("[pre-commit] pytest FAILED — commit blocked. Fix tests then retry.", file=sys.stderr)
    print(proc.stdout[-3000:], file=sys.stderr)
    if proc.stderr.strip():
        print(proc.stderr[-1000:], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

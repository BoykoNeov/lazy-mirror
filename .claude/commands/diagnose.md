---
description: Run DIAGNOSE.bat and show recent log tails for troubleshooting
---

Run the project's diagnostic script and surface recent logs to investigate environment / runtime issues.

1. Execute `DIAGNOSE.bat` via Bash and capture output
2. Tail the last 30 lines of `logs/proxy.log` and `logs/dashboard.log` (if they exist)
3. Show the contents of `offline_cache/_state.json` for current settings

Report concisely: environment info, recent errors (anything containing `error`, `traceback`, or `exception`), current state.

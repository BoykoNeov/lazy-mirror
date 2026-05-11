---
description: Dump current LazyMirror state — _state.json, _queue.json, _meta.json stats, plus the tail of proxy.log and dashboard.log
---

Read the current snapshot of LazyMirror's runtime files and recent logs to help diagnose what's going on. Show:

1. Contents of `offline_cache/_state.json` (all settings)
2. Number of items in `offline_cache/_queue.json` and a few sample URLs
3. `stats` block from `offline_cache/_meta.json` (total, bytes)
4. Last 20 lines of `logs/proxy.log` and `logs/dashboard.log`

Use parallel Read tool calls where possible. If a file does not exist, note that and continue.

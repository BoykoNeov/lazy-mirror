# LazyMirror — Claude Code Handoff

## What This Is
A Windows 10/11 local proxy-based web archiver. It intercepts browser traffic via
mitmproxy, caches every page and resource the user visits, and provides a dashboard
to manage and browse the offline copy. Nothing is scraped automatically — only pages
the user actually navigates to are cached.

---

## Architecture

### Two Processes, One Shared State Directory

```
lazymirror.py (launcher)
  ├── subprocess: mitmdump -s src/proxy_addon.py  (port 8080)
  └── subprocess: python src/dashboard.py         (Flask, port 7779 + 7780)
```

**Inter-process communication is entirely via files in `offline_cache/`:**

| File | Purpose |
|------|---------|
| `_state.json` | All settings — read/written by both processes |
| `_meta.json` | Index of every cached URL → relative path + metadata |
| `_failed.json` | Failed fetch log |
| `_queue.json` | Live queue snapshot (written by proxy worker) |
| `_refetch/<hash>` | Signal: dashboard asks proxy to re-fetch a URL |
| `_queue_remove/<hash>` | Signal: dashboard asks proxy to remove URL from queue |
| `_queue_clear` | Signal: dashboard asks proxy to clear entire queue |

### Ports
- `8080` — mitmdump HTTPS-intercepting proxy
- `7779` — Flask management dashboard
- `7780` — Flask cache browser (serves cached files over HTTP)

---

## File Structure

```
lazy-mirror/
├── lazymirror.py          # Launcher: finds mitmdump, starts both subprocesses
├── src/
│   ├── proxy_addon.py     # mitmproxy addon — all caching/harvesting logic
│   └── dashboard.py       # Flask dashboard + cache browser (port 7779/7780)
├── offline_cache/         # Everything cached goes here
├── certs/                 # mitmproxy CA certificate
├── logs/                  # proxy.log, dashboard.log
├── SETUP.bat              # pip install + cert generation
├── install_cert.bat       # certutil install into Windows trust store
├── configure_proxy.bat    # Set/unset Windows system proxy registry keys
├── START.bat              # Add Python Scripts to PATH, run lazymirror.py
└── DIAGNOSE.bat           # Prints environment info for troubleshooting
```

---

## Key Design Decisions (important to preserve)

### 1. url_to_path decodes percent-encoding
```python
raw = unquote(p.path.lstrip("/") or "index")
```
Files are stored with **real characters** (`Images Landkarten/file.jpg`, not
`Images%20Landkarten/file.jpg`). This is critical for:
- Windows `file://` URL handling (browser decodes %20 before filesystem lookup)
- Exported HTML relative links working without rewriting
- Human-readable cache folder names

**Do not revert this.** If you change it, update `_build_url_map` in dashboard.py
and all the browser/export rewriters accordingly.

### 2. Strict asset/page URL separation in harvester
`harvest_html()` returns a `HarvestResult` with separate buckets:
- `images`, `css`, `js`, `fonts`, `media`, `other` — render assets (embedded resources)
- `pages` — `<a href>` to other HTML pages
- `linked_files` — `<a href>` to downloadable files (images, PDFs, etc.)

**`href` must never be in the generic embed-attribute regex** (`_EMBED_RE`).
The old bug (caused 8000+ queue items on a single page) was `href` in that list.

### 3. crawl_depth = page hops, gated by fetch_linked_html
`crawl_depth` controls recursion depth for page links.
But page links are **only followed if `fetch_linked_html` is True**.
With `fetch_linked_html=False` (default), visiting a page at any depth
only fetches its render assets — never follows links to other pages.

### 4. Single worker thread for fetch delays
All background fetches go through one `InspectableQueue` and one worker thread.
The delay (`fetch_delay_ms`) is applied between items in that thread.
This makes delays real and consistent. Do not parallelize without redesigning delay.

### 5. Pause/resume via worker's own poll loop
The worker calls `_sync_pause_from_state()` on every loop iteration.
It does NOT rely on browser requests to sync the pause flag.
This was a bug (pause couldn't be resumed without browser traffic) that was fixed.

### 6. _patch_state uses a lock and re-reads inside it
```python
def _patch_state(**kw):
    with _state_lock:
        s = load_state()   # re-read inside lock
        s.update(kw)
        STATE_FILE.write_text(...)
```
The proxy worker calls this many times per second (after every fetch).
Without the lock + re-read, the worker clobbers dashboard writes.

### 7. sanitize_url applied to all harvested URLs
Non-ASCII characters in URLs (e.g. `°` → `%C2%B0`) are percent-encoded
in `norm()` (harvester) and `_do_fetch()` (worker). This fixes URLs like:
`KKH 36°27'19 Yashbanden.jpg` which would crash `urllib.request` with
`'ascii' codec can't encode character`.

### 8. Dashboard pending-settings guard
`_pendingSettings` in the JS prevents the 3-second polling loop from
overwriting checkboxes the user just changed. Settings are removed from
`_pendingSettings` only when the server confirms the value matches what was sent.
**Do not remove this guard** — without it, toggles (especially fetch_linked_images,
fetch_linked_html) reset themselves within 3 seconds of being turned on.

---

## _state.json — All Settings

```json
{
  "offline_mode":          false,   // serve from cache, don't hit network
  "fetch_delay_ms":        0,       // ms between background fetches (with ±20% jitter)
  "queue_paused":          false,   // freeze worker without stopping proxy
  "blocked_domains":       [],      // never cache/fetch these domains
  "queue_depth":           0,       // WRITTEN BY WORKER — current queue size
  "crawl_depth":           0,       // page-link hop depth (0=just assets)
  "cross_host_crawl":      false,   // follow page links to other domains
  "fetch_images":          true,    // cache img/background-image/picture
  "fetch_css_js":          true,    // cache stylesheets and scripts
  "fetch_fonts":           true,    // cache @font-face fonts
  "fetch_media":           true,    // cache video/audio
  "fetch_linked_files":    false,   // fetch <a href> downloadable files
  "fetch_linked_images":   false,   // fetch <a href> image files only
  "fetch_linked_html":     false    // follow <a href> page links (requires crawl_depth>0 for recursion)
}
```

---

## proxy_addon.py — Key Functions

### `harvest_html(html_bytes, base_url, ct_header) → HarvestResult`
Parses HTML and categorises all URLs into strict buckets.
`ct_header` is the full Content-Type header value for charset detection.

### `_detect_charset(raw, ct_header) → str`
Detects encoding from: Content-Type header → `<meta charset>` → BOM → `latin-1`.
Critical for German/French/other non-UTF-8 sites.

### `filter_assets(result, state) → set`
Applies per-category toggles to produce the final set of URLs to fetch.

### `_process_page(body, url, page_hops, origin_host, ct_header)`
Handles one HTML page: fetches its assets, optionally follows page links.
`page_hops` is the remaining recursion depth. Only follows links if
`state["fetch_linked_html"]` is True.

### `_do_fetch(url, referer, force, depth, origin_host)`
Downloads one URL, caches it, recurses into HTML/CSS if needed.
Always calls `sanitize_url(url)` first.

### `InspectableQueue`
Thread-safe FIFO with `snapshot()`, `remove_url()`, `remove_urls()`, `clear()`.
Queue items: `(url, referer, force, depth, origin_host)` tuples.

---

## dashboard.py — Key Functions

### `_build_url_map(entries) → dict`
Maps both encoded (`https://.../Images%20Landkarten/file.jpg`) and decoded
(`https://.../Images Landkarten/file.jpg`) URL forms → same cache-relative path.
Also maps http:// variants. Essential for export rewriting to work.

### `_rewrite_html(raw, page_url, url_map, dest_root, page_dest, sidecar) → bytes`
Rewrites HTML for static export:
- Absolute URLs (both encoded+decoded forms) → `os.path.relpath`-based relative paths
- Root-relative `/path` URLs → same
- Charset: decoded from original, output as UTF-8

### `_browser_rewrite_html(raw, page_url, host, sidecar) → bytes`
Rewrites HTML for cache browser serving:
- `https://host/path` → `/host/path` (for any host)
- `/path` → `/current-host/path`

### Cache browser (`browser_app` on port 7780)
`GET /<host>/<path>` — serves cached files with on-the-fly URL rewriting.
`GET /` — lists all cached domains.
Shows "Not in cache" page with related links when URL is not cached.

---

## Known Issues / Future Work

1. **Export for very large sites** — the text replacement loop in `_rewrite_html`
   is O(n×m) where n=HTML length and m=url_map size. For sites with thousands of
   cached URLs this can be slow. Consider regex-based single-pass replacement.

2. **`<base href>` tag** — not handled. Pages using `<base href>` will have
   incorrect relative URL resolution after export.

3. **JavaScript-injected resources** — dynamically loaded images/scripts
   (via `fetch()`, `document.createElement`, etc.) are not captured. Only
   static HTML resources are harvested.

4. **Existing cache migration** — if upgrading from a version where url_to_path
   used percent-encoded filenames, existing cache files won't be found by the
   new decoded url_to_path. A migration script would be needed.

5. **Firefox proxy** — doesn't use Windows system proxy. User must configure
   manually: Settings → Network Settings → Manual Proxy → 127.0.0.1:8080.

6. **Certificate pinning** — apps/sites with certificate pinning (some banking,
   native apps) won't work through any MITM proxy. Browser-based sites are fine.

7. **The `_state.json` write race** — dashboard and proxy are separate processes.
   `_patch_state` uses a threading lock but Python threading locks don't protect
   against cross-process writes. On heavily loaded systems there could still be
   very rare clobbers. A proper fix would use file locking (e.g. `fcntl.flock`
   on Linux or `msvcrt.locking` on Windows).

---

## Running Tests Manually

```bash
# Syntax check both source files
python3 -c "import ast; ast.parse(open('src/proxy_addon.py').read()); print('OK')"
python3 -c "import ast; ast.parse(open('src/dashboard.py').read()); print('OK')"

# Test url_to_path decoding
python3 -c "
import sys; sys.path.insert(0,'src')
import os; os.environ['LAZYMIRROR_CACHE']='/tmp/test_cache'
# Can't import directly due to mitmproxy dependency, test inline:
from urllib.parse import urlparse, unquote
import re, hashlib
from pathlib import Path
def url_to_path(url, cache=Path('/tmp/tc')):
    p = urlparse(url)
    raw = unquote(p.path.lstrip('/') or 'index')
    safe = re.sub(r'[<>:\"|?*\\\\]', '_', raw)
    if '.' not in Path(safe).name or safe.endswith('/'):
        safe = safe.rstrip('/') + '/index.html'
    return cache / p.netloc / safe
print(url_to_path('https://example.com/Images%20Landkarten/file.jpg').name)
# Expected: file.jpg  (parent folder: 'Images Landkarten' with space)
print(url_to_path('https://example.com/Images%20Landkarten/file.jpg').parent.name)
# Expected: Images Landkarten
"
```

---

## Dependencies

```
mitmproxy>=10.0    # proxy engine
flask>=3.0         # dashboard and cache browser
pystray>=0.19      # optional: Windows system tray icon
Pillow>=10.0       # optional: tray icon image
```

Install: `pip install mitmproxy flask pystray Pillow`

Python 3.10+ required. Tested on Windows 10/11.

---

## Common Development Tasks

### Add a new setting
1. Add to `load_state()` defaults in `proxy_addon.py`
2. Add to `load_state()` defaults in `dashboard.py`
3. Add handling in `api_settings()` route in `dashboard.py`
4. Add UI control in dashboard HTML
5. Add to `idMap` in `applySettings()` JS function
6. Add to `_buildSettingsPayload()` JS function
7. Add to `defaultTrue` or `defaultFalse` list in `applySettings()`

### Add a new asset category
1. Define extension set (like `IMAGE_EXTS`) in `proxy_addon.py`
2. Add category bucket to `HarvestResult` class
3. Populate bucket in `harvest_html()`
4. Add toggle to `filter_assets()`
5. Add setting following steps above

### Debugging queue issues
Check `offline_cache/_queue.json` for current queue state.
Check `offline_cache/_proxy.log` for detailed fetch/harvest logs.
Run `DIAGNOSE.bat` for environment info.

---

## Tests

A pytest suite lives in `tests/`. The whole suite is fast (~1 second on a typical
machine) and the pre-commit hook enforces green tests before every commit.

```
python -m pytest -q
```

Test files and what they cover:
- `tests/test_proxy_addon.py` — harvester, sanitize_url, url_to_path, queue, state I/O, write_cache
- `tests/test_dashboard.py` — API routes (cache, settings, delete, refetch, queue), URL map, export rewriter
- `tests/test_startup_recovery.py` — `_meta.json` auto-heal, `_queue.json` restore, corrupt-file fallback
- `tests/test_state_concurrency.py` — concurrent `_patch_state` writers under threads
- `tests/test_browser_rewrite.py` — `_browser_rewrite_html` (cache browser, port 7780)
- `tests/test_export_roundtrip.py` — end-to-end /api/export with on-disk verification

**Rule: tests must be green before commit.** The pre-commit hook will block a commit
when pytest is red.

---

## Workflow

For anything bigger than a one-line fix, prefer the explore-plan-code-commit loop:
1. Read relevant code first (Read, Grep, Glob)
2. Sketch the approach in chat
3. Code with small, focused edits
4. Run `/test` if you haven't already
5. Commit

For non-trivial features (new asset category, export rewriter rework, etc.) drop
a short three-file plan in `docs/dev/<task-name>/` first — see `docs/dev/README.md`.

---

## Slash Commands

Project-defined commands live in `.claude/commands/`. Available:

| Command | What it does |
|---------|--------------|
| `/state` | Dump `_state.json`, `_queue.json` summary, `_meta.json` stats, log tails |
| `/test` | Run the pytest suite, surface pass/fail |
| `/syntax-check` | Quick AST syntax check on both src files |
| `/sync-temp` | Copy `src/*.py` to `M:\temp\lazy mirro claude 3\src\` |
| `/diagnose` | Run DIAGNOSE.bat + recent log tails |

---

## Hooks (`.claude/settings.json`)

Two project-shared hooks are configured:

1. **PostToolUse (Edit / Write / MultiEdit)** → `.claude/hooks/post_edit_src.py`
   - On every edit to a file under `src/*.py`:
     - copies it to `M:\temp\lazy mirro claude 3\src\`
     - runs pytest, reports a one-line summary (non-blocking — informational only)

2. **PreToolUse (Bash)** → `.claude/hooks/pre_commit_test.py`
   - When the Bash command matches `git commit ...`:
     - runs pytest
     - on red, prints failures and blocks the commit
     - on green, allows the commit

`.claude/settings.local.json` is per-user and gitignored; `.claude/settings.json`
and `.claude/commands/` are committed and shared.

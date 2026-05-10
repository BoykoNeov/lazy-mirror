"""LazyMirror Dashboard v8

Runs two Flask apps in one process:
  port 7779  — management dashboard (this `app`)
  port 7780  — cache browser (`browser_app` on its own thread)

Talks to the proxy via files in `offline_cache/` (see CLAUDE.md). API endpoints
that affect the running queue write a signal file the proxy picks up on its
next request; routes that only touch on-disk state (delete, blocklist,
settings) update the JSON files directly.
"""

import json, os, re, hashlib, shutil, threading, time as _time_mod, mimetypes, posixpath
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse, urljoin
from flask import Flask, jsonify, request, send_file, abort, Response, redirect

CACHE_DIR   = Path(os.environ.get("LAZYMIRROR_CACHE",
                   Path(__file__).parent.parent / "offline_cache"))
META_FILE   = CACHE_DIR / "_meta.json"
STATE_FILE  = CACHE_DIR / "_state.json"
FAILED_FILE = CACHE_DIR / "_failed.json"
QUEUE_FILE       = CACHE_DIR / "_queue.json"
QUEUE_DEPTH_FILE = CACHE_DIR / "_queue_depth.json"  # written by proxy only — avoids _state.json write race

app = Flask(__name__)

# Cache for _meta.json — avoids a disk read on every 3-second poll cycle.
# TTL matches the poll interval so each cycle reads from cache rather than disk.
# Invalidated immediately by api_delete() so deletions are reflected in the next poll.
_meta_cache: dict = {}
_meta_cache_ts: float = 0.0
_META_TTL = 3.0

def load_meta():
    global _meta_cache, _meta_cache_ts
    now = _time_mod.monotonic()
    if _meta_cache and (now - _meta_cache_ts) < _META_TTL:
        return dict(_meta_cache)
    if META_FILE.exists():
        try:
            _meta_cache = json.loads(META_FILE.read_text("utf-8"))
            _meta_cache_ts = now
            return dict(_meta_cache)
        except Exception: pass
    _meta_cache = {"cached_urls": {}, "stats": {"total": 0, "bytes": 0}}
    _meta_cache_ts = now
    return dict(_meta_cache)

def load_state():
    defaults = {
        "offline_mode": False, "fetch_delay_ms": 0,
        "queue_paused": False, "blocked_domains": [],
        "queue_depth": 0, "crawl_depth": 0, "cross_host_crawl": False,
        "fetch_images": True, "fetch_css_js": True,
        "fetch_fonts": True, "fetch_media": True, "fetch_linked_files": False, "fetch_linked_images": False, "fetch_linked_html": False,
    }
    if STATE_FILE.exists():
        try:
            saved = json.loads(STATE_FILE.read_text("utf-8"))
            defaults.update(saved)
        except Exception: pass
    return defaults

def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2), "utf-8")

def load_failed():
    if FAILED_FILE.exists():
        try: return json.loads(FAILED_FILE.read_text("utf-8"))
        except Exception: pass
    return {}

def load_queue():
    # _queue.json is a snapshot written by the proxy worker (throttled ~2 s).
    # It can lag the in-memory queue slightly — used for display only, not for decisions.
    if QUEUE_FILE.exists():
        try: return json.loads(QUEUE_FILE.read_text("utf-8"))
        except Exception: pass
    return []

def load_queue_depth() -> int:
    """Read the live queue count from the proxy's dedicated depth file.

    The proxy writes _queue_depth.json after every fetch instead of patching
    _state.json, so the dashboard and proxy are never writing the same file
    concurrently.  Falls back to counting _queue.json items if the file is
    missing (first run) or transiently unreadable.
    """
    try:
        return json.loads(QUEUE_DEPTH_FILE.read_text("utf-8")).get("queue_depth", 0)
    except Exception:
        return len(load_queue())

def url_to_path(url):
    from urllib.parse import unquote
    p    = urlparse(url)
    raw  = unquote(p.path.lstrip("/") or "index")
    safe = re.sub(r'[<>:"|?*\\]', "_", raw)
    if "." not in Path(safe).name or safe.endswith("/"):
        safe = safe.rstrip("/") + "/index.html"
    if p.query:
        qh = hashlib.md5(p.query.encode()).hexdigest()[:8]
        base, ext = os.path.splitext(safe)
        safe = f"{base}__q{qh}{ext}"
    result = CACHE_DIR / p.netloc / safe
    if len(str(result)) > 240:
        h = hashlib.md5(safe.encode()).hexdigest()[:16]
        ext = Path(safe).suffix or ""
        safe = f"_long/{h}{ext}"
        result = CACHE_DIR / p.netloc / safe
    return result

# ─────────────────────────────────────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>LazyMirror</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap');
:root{
  --bg:#0d0f14;--surf:#161920;--bdr:#252830;
  --acc:#00e5a0;--acc2:#38bdf8;--warn:#f97316;--danger:#ef4444;
  --txt:#e2e8f0;--mut:#6b7280;--purple:#a78bfa;
  --img-c:#34d399;--css-c:#818cf8;--js-c:#fbbf24;
  --font-c:#f472b6;--media-c:#60a5fa;--file-c:#fb923c;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:'IBM Plex Sans',sans-serif;
     min-height:100vh;display:flex;flex-direction:column;font-size:13px}

/* ── HEADER ── */
header{display:flex;align-items:flex-start;justify-content:space-between;
       padding:10px 18px;border-bottom:1px solid var(--bdr);background:var(--surf);
       position:sticky;top:0;z-index:50;gap:10px;flex-wrap:wrap}
.logo{display:flex;align-items:center;gap:9px;flex-shrink:0;padding-top:4px}
.logo-box{width:30px;height:30px;background:var(--acc);border-radius:6px;
          display:flex;align-items:center;justify-content:center;
          font-family:'IBM Plex Mono',monospace;font-weight:700;font-size:11px;color:#000}
.logo-name{font-size:15px;font-weight:600}
.logo-tag{font-size:10px;color:var(--mut);font-family:'IBM Plex Mono',monospace}

/* controls grid */
.hcontrols{display:flex;flex-wrap:wrap;gap:6px;align-items:flex-start;flex:1}
.ctrl-group{display:flex;flex-direction:column;gap:4px;
            background:var(--bg);border:1px solid var(--bdr);border-radius:6px;padding:6px 10px}
.ctrl-group-label{font-size:9px;color:var(--mut);text-transform:uppercase;letter-spacing:.8px;margin-bottom:2px}
.ctrl-row{display:flex;align-items:center;gap:7px;flex-wrap:wrap}

/* asset category chips */
.cat-chips{display:flex;flex-wrap:wrap;gap:4px}
.cat-chip{display:flex;align-items:center;gap:4px;
          border:1px solid var(--bdr);border-radius:4px;padding:3px 7px;
          cursor:pointer;transition:.15s;font-size:10px;font-family:'IBM Plex Mono',monospace;
          user-select:none}
.cat-chip:hover{border-color:currentColor;opacity:.9}
.cat-chip.off{opacity:.35;border-color:var(--bdr)!important}
.cat-chip input{display:none}
.cat-chip.img{color:var(--img-c)}
.cat-chip.css{color:var(--css-c)}
.cat-chip.js {color:var(--js-c)}
.cat-chip.fnt{color:var(--font-c)}
.cat-chip.med{color:var(--media-c)}
.cat-chip.lnk{color:var(--file-c)}

/* depth control */
.depth-row{display:flex;align-items:center;gap:5px}
.depth-btn{width:20px;height:20px;background:var(--surf);border:1px solid var(--bdr);
           border-radius:4px;color:var(--txt);font-size:14px;cursor:pointer;
           display:flex;align-items:center;justify-content:center;transition:.15s;line-height:1}
.depth-btn:hover{border-color:var(--acc);color:var(--acc)}
.depth-val{font-family:'IBM Plex Mono',monospace;font-size:14px;font-weight:600;
           color:var(--acc);min-width:16px;text-align:center}

/* toggle switch */
.sw{position:relative;display:inline-block;width:30px;height:16px;flex-shrink:0}
.sw input{opacity:0;width:0;height:0}
.sw-t{position:absolute;inset:0;background:var(--bdr);border-radius:16px;cursor:pointer;transition:.2s}
.sw input:checked+.sw-t{background:var(--acc)}
.sw-t:before{content:'';position:absolute;width:10px;height:10px;left:3px;bottom:3px;
             background:#fff;border-radius:50%;transition:.2s}
.sw input:checked+.sw-t:before{transform:translateX(14px)}

.sw-lbl{font-size:10px;color:var(--mut);white-space:nowrap}
.btn{background:none;border:1px solid var(--bdr);border-radius:4px;
     padding:4px 9px;font-family:'IBM Plex Mono',monospace;font-size:10px;
     cursor:pointer;color:var(--txt);transition:.15s;white-space:nowrap}
.btn:hover{border-color:var(--acc);color:var(--acc)}
.btn.active{border-color:var(--purple);color:var(--purple)}
.btn.warn:hover{border-color:var(--warn);color:var(--warn)}
.btn.danger:hover{border-color:var(--danger);color:var(--danger)}
.num{background:var(--surf);border:1px solid var(--bdr);border-radius:4px;
     padding:3px 6px;color:var(--txt);font-family:'IBM Plex Mono',monospace;
     font-size:11px;width:68px;outline:none;transition:.15s;text-align:right}
.num:focus{border-color:var(--acc)}
.unit{font-size:10px;color:var(--mut);font-family:'IBM Plex Mono',monospace}
.dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.dot.on{background:var(--acc);box-shadow:0 0 5px var(--acc)}
.dot.off{background:var(--warn);box-shadow:0 0 5px var(--warn)}
.dot.paused{background:var(--purple);box-shadow:0 0 5px var(--purple)}
.vd{width:1px;height:14px;background:var(--bdr);flex-shrink:0}

/* ── SUMMARY BAR ── */
.summary-bar{padding:7px 18px;border-bottom:1px solid var(--bdr);
             background:#0f1117;font-size:11px;line-height:1.6;
             font-family:'IBM Plex Mono',monospace;color:var(--mut)}
.summary-bar .hi{color:var(--acc)}
.summary-bar .warn{color:var(--warn)}
.summary-tag{display:inline-block;border-radius:3px;padding:1px 5px;margin:0 2px;
             font-size:9px;font-weight:700;text-transform:uppercase}

/* ── QUEUE BAR ── */
.qbar{display:flex;align-items:center;gap:8px;padding:6px 18px;
      border-bottom:1px solid var(--bdr);background:var(--surf);
      font-family:'IBM Plex Mono',monospace;font-size:11px}
.qprog{flex:1;height:2px;background:var(--bdr);border-radius:2px;overflow:hidden}
.qprog-bar{height:100%;background:var(--acc);transition:width .4s;border-radius:2px}

/* ── STATS ── */
.stats{display:flex;border-bottom:1px solid var(--bdr);background:var(--surf)}
.stat{flex:1;padding:9px 12px;border-right:1px solid var(--bdr);text-align:center}
.stat:last-child{border:none}
.sv{font-size:17px;font-weight:700;font-family:'IBM Plex Mono',monospace;color:var(--acc)}
.sl{font-size:9px;color:var(--mut);margin-top:2px;text-transform:uppercase;letter-spacing:1px}

/* ── LAYOUT ── */
main{flex:1;display:flex;min-height:0}
aside{width:200px;min-width:200px;border-right:1px solid var(--bdr);
      background:var(--surf);display:flex;flex-direction:column;overflow:hidden}
.aside-tabs{display:flex;border-bottom:1px solid var(--bdr);flex-shrink:0}
.atab{flex:1;padding:6px 3px;font-size:10px;text-align:center;cursor:pointer;
      color:var(--mut);border-bottom:2px solid transparent;transition:.15s}
.atab:hover{color:var(--txt)}.atab.active{color:var(--acc);border-bottom-color:var(--acc)}
.aside-panel{flex:1;overflow-y:auto;padding:8px 0;display:none}
.aside-panel.active{display:block}
.sec-lbl{font-size:9px;color:var(--mut);text-transform:uppercase;letter-spacing:1px;padding:0 10px 5px}
.host-li{list-style:none}
.host-li li{padding:6px 10px;cursor:pointer;font-family:'IBM Plex Mono',monospace;font-size:10px;
            border-left:3px solid transparent;display:flex;justify-content:space-between;align-items:center;gap:3px;transition:.12s}
.host-li li:hover{background:var(--bg);border-left-color:var(--acc)}
.host-li li.active{background:var(--bg);border-left-color:var(--acc);color:var(--acc)}
.host-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.host-meta{display:flex;flex-direction:column;align-items:flex-end;gap:1px;flex-shrink:0}
.cnt{background:var(--bdr);border-radius:7px;padding:1px 5px;font-size:9px;color:var(--mut)}
.host-sz{font-size:8px;color:var(--mut)}
.block-input-row{display:flex;gap:5px;padding:6px 10px 0}
.block-input{flex:1;background:var(--bg);border:1px solid var(--bdr);border-radius:4px;
             padding:4px 7px;color:var(--txt);font-family:'IBM Plex Mono',monospace;
             font-size:10px;outline:none;min-width:0}
.block-input:focus{border-color:var(--acc)}
.block-input::placeholder{color:var(--mut)}
.block-list{list-style:none;padding:4px 0}
.block-li{display:flex;justify-content:space-between;align-items:center;
          padding:4px 10px;font-family:'IBM Plex Mono',monospace;font-size:10px;
          border-bottom:1px solid var(--bdr)}
.block-rm{background:none;border:none;color:var(--mut);cursor:pointer;font-size:13px;padding:0 2px}
.block-rm:hover{color:var(--danger)}
.block-empty{padding:12px 10px;font-size:10px;color:var(--mut);text-align:center}

/* ── CONTENT ── */
.content{flex:1;padding:12px 14px;overflow-y:auto;display:flex;flex-direction:column;gap:10px;min-width:0}
.toolbar{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.search{flex:1;min-width:130px;background:var(--surf);border:1px solid var(--bdr);
        border-radius:6px;padding:6px 10px;color:var(--txt);
        font-family:'IBM Plex Mono',monospace;font-size:11px;outline:none;transition:.15s}
.search:focus{border-color:var(--acc)}
.search::placeholder{color:var(--mut)}
.tab-bar{display:flex;border:1px solid var(--bdr);border-radius:5px;overflow:hidden}
.tab{padding:5px 10px;font-size:10px;cursor:pointer;color:var(--mut);
     font-family:'IBM Plex Mono',monospace;background:var(--surf);border:none;transition:.15s}
.tab:hover{color:var(--txt)}.tab.active{background:var(--bg);color:var(--acc)}

/* ── TABLES ── */
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:6px 8px;font-size:9px;text-transform:uppercase;
   letter-spacing:1px;color:var(--mut);border-bottom:1px solid var(--bdr);font-weight:400}
td{padding:7px 8px;border-bottom:1px solid var(--bdr);vertical-align:middle}
tr.sel td{background:#1a1f2e}
tr:hover td{background:#13161e}
.url-td{font-family:'IBM Plex Mono',monospace;font-size:10px;
        max-width:330px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.url-td a{color:var(--acc2);text-decoration:none}
.url-td a:hover{text-decoration:underline}
.badge{display:inline-block;border-radius:3px;padding:1px 5px;font-size:8px;
       font-weight:700;text-transform:uppercase;letter-spacing:.5px}
.b-html{background:#1e3a5f;color:#60a5fa}.b-css{background:#1e2b5f;color:#818cf8}
.b-js{background:#2d2900;color:#fbbf24}.b-img{background:#0f2e1a;color:#4ade80}
.b-oth{background:var(--bdr);color:var(--mut)}
.sz{color:var(--mut);font-family:'IBM Plex Mono',monospace;font-size:10px;white-space:nowrap}
.dt{color:var(--mut);font-size:10px;white-space:nowrap}
.actions{display:flex;gap:3px}
.depth-chip{display:inline-block;background:var(--bdr);border-radius:3px;
            padding:1px 6px;font-family:'IBM Plex Mono',monospace;font-size:9px}
.depth-chip.d0{color:var(--mut)}.depth-chip.d1{color:var(--acc2)}
.depth-chip.d2{color:var(--acc)}.depth-chip.d3p{color:var(--warn)}
.referer-td{font-family:'IBM Plex Mono',monospace;font-size:9px;color:var(--mut);
            max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fail-reason{color:var(--warn);font-family:'IBM Plex Mono',monospace;font-size:10px}

/* checkbox column */
.chk-col{width:28px}
.row-cb{width:13px;height:13px;accent-color:var(--acc);cursor:pointer}
/* Queue selection bar — shown when one or more queue rows are checked */
.queue-sel-bar{display:none;align-items:center;gap:8px;padding:5px 0;
               font-size:11px;color:var(--mut)}
.queue-sel-bar.show{display:flex}

/* Cache selection bar — shown when rows are selected or a host filter is active */
.cache-sel-bar{display:none;align-items:center;gap:8px;padding:5px 0;
               font-size:11px;color:var(--mut)}
.cache-sel-bar.show{display:flex}

/* ── PAGINATION & SORT ── */
.pg-bar{display:flex;align-items:center;gap:5px;padding:5px 0;font-size:11px;flex-wrap:wrap}
/* Prev/Next/First/Last navigation buttons */
.pg-nav-btn{background:none;border:1px solid var(--bdr);color:var(--txt);border-radius:4px;
            padding:1px 8px;cursor:pointer;font-size:11px;line-height:1.6}
.pg-nav-btn:hover:not([disabled]){border-color:var(--acc);color:var(--acc)}
.pg-nav-btn[disabled]{opacity:.3;cursor:default}
/* Page-size selector buttons (100/500) and sort-scope buttons (All / This page) */
.pg-sz-btn,.scope-btn{background:none;border:1px solid var(--bdr);color:var(--mut);
           border-radius:4px;padding:1px 7px;cursor:pointer;font-size:10px;line-height:1.6}
.pg-sz-btn.active{border-color:var(--acc);color:var(--acc)}
.scope-btn.active{border-color:var(--acc2);color:var(--acc2)}
.pg-info{color:var(--mut);font-size:10px;white-space:nowrap}
/* Jump-to-page number input */
.pg-jump{width:40px;background:var(--bg);border:1px solid var(--bdr);color:var(--txt);
         border-radius:4px;padding:1px 4px;font-size:11px;text-align:center;outline:none}
.pg-sep{color:var(--bdr);padding:0 3px;user-select:none}
.pg-lbl{color:var(--mut);font-size:10px}
/* Clickable sort column headers */
.sort-th{cursor:pointer;user-select:none;white-space:nowrap}
.sort-th:hover{color:var(--acc)}
/* Sort direction icon — JS sets ⇅ / ▲ / ▼ */
.sort-icon{color:var(--bdr);font-size:9px;margin-left:2px}

.empty{text-align:center;padding:50px 20px;color:var(--mut)}
.empty-i{font-size:36px;margin-bottom:8px}
.empty-t{font-size:15px;color:var(--txt);margin-bottom:5px}

/* ── TOAST ── */
#toast{position:fixed;bottom:16px;right:16px;background:var(--surf);
       border:1px solid var(--acc);border-radius:7px;padding:9px 13px;
       font-size:11px;display:none;z-index:999;box-shadow:0 8px 24px rgba(0,0,0,.5)}
#toast.show{display:block;animation:fi .15s}
@keyframes fi{from{opacity:0;transform:translateY(6px)}to{opacity:1}}

/* ── EXPORT MODAL ── */
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:100;
          display:none;align-items:center;justify-content:center}
.modal-bg.show{display:flex}
.modal{background:var(--surf);border:1px solid var(--bdr);border-radius:10px;
       padding:22px;max-width:400px;width:90%;display:flex;flex-direction:column;gap:12px}
.modal h2{font-size:14px;font-weight:600}
.modal p{font-size:11px;color:var(--mut);line-height:1.6}
.modal-btns{display:flex;gap:6px;justify-content:flex-end}
.modal-path{background:var(--bg);border:1px solid var(--bdr);border-radius:4px;
            padding:6px 8px;font-family:'IBM Plex Mono',monospace;font-size:10px;
            color:var(--acc);width:100%;outline:none}
.prog-txt{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--mut);
          text-align:center;min-height:15px}
</style>
</head>
<body>

<header>
  <div class="logo">
    <div class="logo-box">LM</div>
    <div><div class="logo-name">LazyMirror</div><div class="logo-tag">on-demand offline cache</div></div>
  </div>
  <div class="hcontrols">

    <!-- Asset categories -->
    <div class="ctrl-group">
      <div class="ctrl-group-label">Fetch assets</div>
      <div class="cat-chips" id="catChips">
        <label class="cat-chip img" title="Images (img, background-image, picture)">
          <input type="checkbox" id="catImages" checked onchange="onCatChange()"/>🖼 Images
        </label>
        <label class="cat-chip css" title="Stylesheets and scripts">
          <input type="checkbox" id="catCssJs" checked onchange="onCatChange()"/>🎨 CSS/JS
        </label>
        <label class="cat-chip fnt" title="Web fonts (@font-face)">
          <input type="checkbox" id="catFonts" checked onchange="onCatChange()"/>Aa Fonts
        </label>
        <label class="cat-chip med" title="Video and audio">
          <input type="checkbox" id="catMedia" checked onchange="onCatChange()"/>▶ Media
        </label>
        <label class="cat-chip lnk" title="Files linked via &lt;a href&gt; (images, PDFs, ZIPs…)">
          <input type="checkbox" id="catLinked" onchange="onCatChange()"/>📎 Linked files
        </label>
        <label class="cat-chip img" style="border-style:dashed" title="Only images linked via &lt;a href&gt; — not PDFs or ZIPs">
          <input type="checkbox" id="catLinkedImages" onchange="onCatChange()"/>🔗 Linked images
        </label>
        <label class="cat-chip" style="color:var(--acc2);border-style:dashed" title="Follow &lt;a href&gt; links to other HTML pages (depth controls how deep)">
          <input type="checkbox" id="catLinkedHtml" onchange="onCatChange()"/>🌐 Linked HTML
        </label>
      </div>
    </div>

    <!-- Crawl depth -->
    <div class="ctrl-group">
      <div class="ctrl-group-label">Page link depth</div>
      <div class="ctrl-row">
        <div class="depth-row">
          <button class="depth-btn" onclick="adjDepth(-1)">−</button>
          <span class="depth-val" id="depthVal">0</span>
          <button class="depth-btn" onclick="adjDepth(+1)">+</button>
        </div>
        <div class="vd"></div>
        <label class="sw" title="Follow page links to other domains">
          <input type="checkbox" id="chkCross" onchange="saveSettings()"/>
          <span class="sw-t"></span>
        </label>
        <span class="sw-lbl">Cross-host</span>
      </div>
    </div>

    <!-- Fetch delay -->
    <div class="ctrl-group">
      <div class="ctrl-group-label">Fetch delay</div>
      <div class="ctrl-row">
        <input class="num" type="number" id="fetchDelay" min="0" max="60000"
               value="0" oninput="scheduleDelaySave()" title="ms between background fetches"/>
        <span class="unit">ms</span>
      </div>
    </div>

    <!-- Mode & actions -->
    <div class="ctrl-group">
      <div class="ctrl-group-label">Mode</div>
      <div class="ctrl-row">
        <span class="dot" id="dot"></span>
        <span class="sw-lbl" id="modeLabel">…</span>
        <button class="btn" onclick="toggleMode()">Switch</button>
        <div class="vd"></div>
        <button class="btn" onclick="openExport()">⬇ Export</button>
      </div>
    </div>

  </div>
</header>

<!-- SUMMARY BAR -->
<div class="summary-bar" id="summaryBar">
  Loading settings…
</div>

<!-- BROWSER BAR -->
<div style="padding:5px 18px;border-bottom:1px solid var(--bdr);background:#0a0d10;
     font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--mut);display:flex;gap:16px;flex-wrap:wrap">
  <span>Proxy: <span style="color:var(--acc)">127.0.0.1:8080</span></span>
  <span>Dashboard: <a href="http://127.0.0.1:7779" target="_blank" style="color:var(--acc2)">127.0.0.1:7779</a></span>
  <span>&#127775; Cache browser: <a href="http://127.0.0.1:7780" target="_blank" style="color:var(--acc2)">127.0.0.1:7780</a>
    <span style="color:var(--mut)"> &#8212; browse your offline mirror here</span></span>
</div>

<!-- QUEUE BAR -->
<div class="qbar">
  <span class="dot" id="qDot" style="background:var(--bdr)"></span>
  <span id="qLabel" style="color:var(--mut)">Queue idle</span>
  <div class="qprog"><div class="qprog-bar" id="qBar" style="width:0%"></div></div>
  <button class="btn" id="qPauseBtn" onclick="togglePause()">Pause</button>
</div>

<!-- STATS -->
<div class="stats">
  <div class="stat"><div class="sv" id="sTotal">—</div><div class="sl">Cached</div></div>
  <div class="stat"><div class="sv" id="sSize">—</div><div class="sl">Size</div></div>
  <div class="stat"><div class="sv" id="sHosts">—</div><div class="sl">Domains</div></div>
  <div class="stat"><div class="sv" id="sPages">—</div><div class="sl">Pages</div></div>
  <div class="stat"><div class="sv" id="sQueue" style="color:var(--acc2)">—</div><div class="sl">Queued</div></div>
  <div class="stat"><div class="sv" id="sFailed" style="color:var(--warn)">—</div><div class="sl">Failed</div></div>
</div>

<main>
  <aside>
    <div class="aside-tabs">
      <div class="atab active" onclick="switchAside('Domains',this)">Domains</div>
      <div class="atab" onclick="switchAside('Blocked',this)">Blocked</div>
    </div>
    <div class="aside-panel active" id="panelDomains">
      <ul class="host-li" id="hostList">
        <li class="active" data-host="" onclick="filterHost('',this)">
          <span class="host-name">All</span>
          <div class="host-meta"><span class="cnt" id="cntAll">0</span></div>
        </li>
      </ul>
    </div>
    <div class="aside-panel" id="panelBlocked">
      <div class="block-input-row">
        <input class="block-input" id="blockInput" placeholder="example.com"
               onkeydown="if(event.key==='Enter')addBlock()"/>
        <button class="btn" onclick="addBlock()">+</button>
      </div>
      <ul class="block-list" id="blockList"></ul>
    </div>
  </aside>

  <div class="content">
    <div class="toolbar">
      <input class="search" id="srch" placeholder="Filter by URL…" oninput="renderActive()"/>
      <div class="tab-bar">
        <button class="tab active" onclick="switchTab('cached',this)">Cached</button>
        <button class="tab" onclick="switchTab('queue',this)">Queue <span id="qTabBadge"></span></button>
        <button class="tab" onclick="switchTab('failed',this)">Failed <span id="failBadge"></span></button>
      </div>
      <span id="tabActions"></span>
    </div>

    <!-- Cached -->
    <div id="tabCached">
      <!-- Selection toolbar: visible when items are selected or a domain filter is active -->
      <div class="cache-sel-bar" id="cacheSelBar">
        <span id="cacheSelCount"></span>
        <!-- Delete selected URLs (uses server-side /api/delete-bulk with URL list) -->
        <button class="btn danger" id="deleteSelBtn" onclick="deleteSelectedCache()" style="display:none">Delete selected</button>
        <!-- Delete every cached file for the active host — bypasses the 500-item UI cap -->
        <button class="btn danger" id="delHostBtn" onclick="deleteAllFromHost()" style="display:none"></button>
        <button class="btn" id="deselectAllBtn" onclick="clearCacheSelection()" style="display:none">Deselect all</button>
      </div>
      <!-- Top bar: page-size selector, sort-scope toggle, entry count -->
      <div class="pg-bar" id="cachePgTop"></div>
      <table>
        <thead><tr>
          <!-- Master checkbox: checked=all visible selected; indeterminate=partial -->
          <th class="chk-col"><input type="checkbox" class="row-cb" id="cacheSelectAll" onchange="toggleCacheSelectAll()"/></th>
          <th class="sort-th" onclick="setSort('cache','type')"><span class="sort-icon" id="si-cache-type">⇅</span> Type</th>
          <th class="sort-th" onclick="setSort('cache','url')"><span class="sort-icon" id="si-cache-url">⇅</span> URL</th>
          <th class="sort-th" onclick="setSort('cache','size')"><span class="sort-icon" id="si-cache-size">⇅</span> Size</th>
          <th class="sort-th" onclick="setSort('cache','cached_at')"><span class="sort-icon" id="si-cache-cached_at">▼</span> Cached</th>
          <th></th>
        </tr></thead>
        <tbody id="tbody"></tbody>
      </table>
      <!-- Bottom bar: prev/next/jump page navigation -->
      <div class="pg-bar" id="cachePgNav"></div>
      <div class="empty" id="emptyCached" style="display:none">
        <div class="empty-i">📭</div><div class="empty-t">Nothing cached yet</div>
        <div>Browse any site through the proxy.</div>
      </div>
    </div>

    <!-- Queue -->
    <div id="tabQueue" style="display:none">
      <div class="queue-sel-bar" id="qSelBar">
        <span id="qSelCount">0 selected</span>
        <button class="btn danger" onclick="deleteSelected()">Delete selected</button>
        <button class="btn" onclick="clearSelection()">Deselect all</button>
      </div>
      <table>
        <thead>
          <tr>
            <th class="chk-col"><input type="checkbox" class="row-cb" id="selectAll" onchange="toggleSelectAll()"/></th>
            <th>Depth</th>
            <th class="sort-th" onclick="setSort('queue','url')"><span class="sort-icon" id="si-queue-url">⇅</span> URL</th>
            <th>Referer / Origin</th><th></th>
          </tr>
        </thead>
        <tbody id="queueTbody"></tbody>
      </table>
      <div class="pg-bar" id="queuePgNav"></div>
      <div class="empty" id="emptyQueue" style="display:none">
        <div class="empty-i">✅</div><div class="empty-t">Queue is empty</div>
      </div>
    </div>

    <!-- Failed -->
    <div id="tabFailed" style="display:none">
      <table>
        <thead><tr>
          <th class="sort-th" onclick="setSort('failed','url')"><span class="sort-icon" id="si-failed-url">⇅</span> URL</th>
          <th>Reason</th>
          <th class="sort-th" onclick="setSort('failed','failed_at')"><span class="sort-icon" id="si-failed-failed_at">⇅</span> Failed at</th>
          <th></th>
        </tr></thead>
        <tbody id="failedTbody"></tbody>
      </table>
      <div class="pg-bar" id="failedPgNav"></div>
      <div class="empty" id="emptyFailed" style="display:none">
        <div class="empty-i">✅</div><div class="empty-t">No failed fetches</div>
      </div>
    </div>
  </div>
</main>

<!-- EXPORT MODAL -->
<div class="modal-bg" id="exportModal">
  <div class="modal">
    <h2>Export offline cache</h2>
    <p>Copies all cached files to a self-contained folder with an index.html listing all pages.</p>
    <div>
      <div style="font-size:10px;color:var(--mut);margin-bottom:5px">Destination folder</div>
      <input class="modal-path" id="exportPath" placeholder="e.g. C:\Users\You\Desktop\my-mirror"/>
    </div>
    <div class="prog-txt" id="exportProgress"></div>
    <div class="modal-btns">
      <button class="btn" onclick="closeExport()">Cancel</button>
      <button class="btn" onclick="doExport()" id="exportBtn">Export</button>
    </div>
  </div>
</div>
<!-- CONFIRM MODAL — reused by all destructive actions -->
<div class="modal-bg" id="confirmModal">
  <div class="modal">
    <h2 id="confirmTitle">Confirm</h2>
    <p id="confirmMsg" style="white-space:pre-wrap;line-height:1.7"></p>
    <div class="modal-btns">
      <button class="btn" onclick="confirmCancel()">Cancel</button>
      <!-- Label and color set dynamically by showConfirm() -->
      <button class="btn" id="confirmOkBtn" onclick="confirmOk()">Delete</button>
    </div>
  </div>
</div>
<div id="toast"></div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
let entries=[], failedEntries=[], queueEntries=[], activeHost='', currentTab='cached';
let maxQSeen=0, currentDepth=0;
let _lastQueueData=[], _delaySaveTimer=null;
let _lastNonEmptyQueueTime = 0;  // timestamp of last poll that returned queue items
const selectedQueueUrls = new Set();
const selectedCacheUrls = new Set(); // tracks which cached URLs have their checkbox checked
let hostsData = {};                  // per-hostname {count, bytes} from last /api/cache poll
let _confirmCallback = null;         // callback stored by showConfirm(), invoked by confirmOk()

// ── Pagination / sort state ────────────────────────────────────────────────
// Each tab has its own sort and pagination state, independent of the others.
// sortScope 'all'  → server re-fetch with new sort, resets to page 1.
// sortScope 'page' → client-side sort of the already-loaded page, no network request.
const cacheState  = {page:1, pageSize:500, sortBy:'cached_at', sortDir:'desc', sortScope:'all'};
const queueState  = {page:1, pageSize:500, sortBy:'', sortDir:'asc'};
const failedState = {page:1, pageSize:500, sortBy:'url', sortDir:'asc'};
let cacheTotalFiltered = 0;  // total server-side entries matching the active host/search filter

// ── Polling ────────────────────────────────────────────────────────────────
async function load(){
  // Build cache URL with current sort, pagination, and host-filter parameters.
  // The server sorts and paginates the full dataset so each page is consistent.
  const _cOff = (cacheState.page - 1) * cacheState.pageSize;
  const _cUrl = `/api/cache?sort_by=${cacheState.sortBy}&sort_dir=${cacheState.sortDir}`
              + `&limit=${cacheState.pageSize}&offset=${_cOff}`
              + `&host=${encodeURIComponent(activeHost)}`;

  const [cache, state, failed, qData] = await Promise.all([
    fetch(_cUrl).then(r=>r.json()),
    fetch('/api/mode').then(r=>r.json()),
    fetch('/api/failed').then(r=>r.json()),
    fetch('/api/queue').then(r=>r.json()),
  ]);

  entries            = cache.entries||[];
  cacheTotalFiltered = cache.total_filtered || 0;  // total entries matching current filter
  failedEntries      = failed.entries||[];

  // Queue debounce: the proxy worker writes _queue.json between every fetch, so the
  // file can be transiently empty mid-write even while items are still being processed.
  // Strategy: always accept non-empty data immediately; only clear the display after
  // the server has returned empty for at least 5 continuous seconds.
  const newQ = qData.items||[];
  if(newQ.length > 0){
    _lastNonEmptyQueueTime = Date.now();
    queueEntries   = newQ;
    _lastQueueData = newQ;
  } else if(Date.now() - _lastNonEmptyQueueTime > 5000){
    // Genuinely empty for 5+ seconds — clear the display.
    queueEntries   = [];
    _lastQueueData = [];
  }
  // else: transient empty within 5s of last real data — keep showing previous snapshot.

  // Stats
  document.getElementById('sTotal').textContent = cache.stats.total.toLocaleString();
  document.getElementById('sSize').textContent  = fmtB(cache.stats.bytes);
  // Keep hostsData in module scope so deleteAllFromHost() can read total counts
  hostsData = cache.hosts || {};
  document.getElementById('sHosts').textContent = Object.keys(hostsData).length;
  // Use server-computed page count (covers all cached content, not just the current page).
  document.getElementById('sPages').textContent = (cache.stats.pages||0).toLocaleString();
  document.getElementById('sQueue').textContent = queueEntries.length||'—';
  document.getElementById('sFailed').textContent= failedEntries.length||'—';
  document.getElementById('qTabBadge').textContent  = queueEntries.length?`(${queueEntries.length})`:'';
  document.getElementById('failBadge').textContent  = failedEntries.length?`(${failedEntries.length})`:'';

  // Sidebar
  document.getElementById('cntAll').textContent = cache.stats.total.toLocaleString();
  const ul=document.getElementById('hostList');
  [...ul.querySelectorAll('[data-host]:not([data-host=""])')].forEach(el=>el.remove());
  Object.entries(hostsData).sort().forEach(([h, data])=>{
    const li=document.createElement('li'); li.dataset.host=h;
    li.onclick=function(){filterHost(h,this)};
    if(activeHost===h) li.classList.add('active');
    li.innerHTML=`<span class="host-name" title="${h}">${h}</span>
      <div class="host-meta"><span class="cnt">${data.count}</span><span class="host-sz">${fmtB(data.bytes||0)}</span></div>`;
    ul.appendChild(li);
  });

  applySettings(state);
  updateQueueBar(state);
  renderBlockList(state.blocked_domains||[]);
  renderActive();
  // Keep tabActions in sync with live data on every poll cycle.
  // switchTab() only runs when the user clicks a tab, so after a background action
  // (like clearFailed) changes the data, the action buttons must be refreshed here.
  syncTabActions();
}
// Refresh the tabActions toolbar to match the current tab and live entry counts.
function syncTabActions(){
  const ta = document.getElementById('tabActions');
  if(currentTab === 'failed'){
    ta.innerHTML = failedEntries.length
      ? `<button class="btn warn" onclick="retryAll()">↻ Retry all</button>
         <button class="btn danger" onclick="clearFailed()">Clear</button>`
      : '';
  }
}

// ── Summary bar ────────────────────────────────────────────────────────────
function updateSummary(){
  const depth      = currentDepth;
  const images     = document.getElementById('catImages').checked;
  const cssjs      = document.getElementById('catCssJs').checked;
  const fonts      = document.getElementById('catFonts').checked;
  const media      = document.getElementById('catMedia').checked;
  const linked     = document.getElementById('catLinked').checked;
  const linkedImgs = document.getElementById('catLinkedImages').checked;
  const linkedHtml = document.getElementById('catLinkedHtml').checked;
  const cross      = document.getElementById('chkCross').checked;
  const delay      = parseInt(document.getElementById('fetchDelay').value)||0;

  const assetParts=[];
  if(images)     assetParts.push(`<span class="summary-tag" style="background:#0f2e1a;color:var(--img-c)">images</span>`);
  if(cssjs)      assetParts.push(`<span class="summary-tag" style="background:#1e2b5f;color:var(--css-c)">CSS/JS</span>`);
  if(fonts)      assetParts.push(`<span class="summary-tag" style="background:#2d1a2e;color:var(--font-c)">fonts</span>`);
  if(media)      assetParts.push(`<span class="summary-tag" style="background:#1a2b3d;color:var(--media-c)">media</span>`);
  if(linked)     assetParts.push(`<span class="summary-tag" style="background:#2d1f0a;color:var(--file-c)">linked files</span>`);
  if(linkedImgs) assetParts.push(`<span class="summary-tag" style="background:#0f2e1a;color:var(--img-c);border:1px dashed">linked images</span>`);

  const extras=[];
  if(linkedHtml && depth===0) extras.push(`<span class="summary-tag" style="background:#1a2535;color:var(--acc2)">linked HTML pages (depth 0 — no page recursion)</span>`);
  if(linkedHtml && depth>0)   extras.push(`<span class="summary-tag" style="background:#1a2535;color:var(--acc2)">linked HTML ${depth} level${depth>1?'s':''} deep${cross?' (all domains)':' (same host)'}</span>`);

  let msg='';
  const nothingToFetch = assetParts.length===0 && extras.length===0;
  if(nothingToFetch){
    msg='<span class="warn">⚠ Nothing extra will be fetched — enable at least one asset type or linked HTML.</span>';
  } else {
    const assetStr = assetParts.length ? assetParts.join('') : '';
    const extraStr = extras.length ? extras.join('') : '';

    let parts=[];
    if(assetStr) parts.push(`fetch ${assetStr}`);
    if(extraStr) parts.push(`follow ${extraStr}`);

    msg = `When you visit a page: cache the HTML and ${parts.join(', also ')}.`;
    if(delay>0) msg+=` <span style="color:var(--mut)"> · ${delay}ms delay between fetches</span>`;
  }
  document.getElementById('summaryBar').innerHTML=msg;
}

// ── Queue bar ──────────────────────────────────────────────────────────────
function updateQueueBar(state){
  const depth  = queueEntries.length;
  const paused = !!state.queue_paused;
  const qDot=document.getElementById('qDot'), qLbl=document.getElementById('qLabel');
  const qBar=document.getElementById('qBar'), qBtn=document.getElementById('qPauseBtn');
  if(depth>maxQSeen) maxQSeen=depth;
  const pct = maxQSeen>0 ? Math.round((1-depth/maxQSeen)*100) : 100;
  if(paused){
    qDot.className='dot paused'; qLbl.textContent=`PAUSED — ${depth} queued`; qLbl.style.color='var(--purple)';
    qBtn.textContent='Resume'; qBtn.classList.add('active');
  } else if(depth>0){
    qDot.className='dot on'; qLbl.textContent=`Fetching — ${depth} queued`; qLbl.style.color='var(--acc)';
    qBtn.textContent='Pause'; qBtn.classList.remove('active');
  } else {
    qDot.style.background='var(--bdr)'; qDot.style.boxShadow='none';
    qLbl.textContent='Queue idle'; qLbl.style.color='var(--mut)';
    qBtn.textContent='Pause'; qBtn.classList.remove('active'); maxQSeen=0;
  }
  qBar.style.width=depth>0?pct+'%':'0%';
}

async function togglePause(){
  const d=await fetch('/api/toggle-pause',{method:'POST'}).then(r=>r.json());
  updateQueueBar(d); toast(d.queue_paused?'⏸ Queue paused':'▶ Queue resumed');
}

// ── Settings ───────────────────────────────────────────────────────────────
// ── Settings — pending-state guard ────────────────────────────────────────
// Tracks settings that have been changed locally but may not yet be confirmed
// by the server. applySettings skips fields that are pending.
let _pendingSettings = {};
let _saveInFlight = false;

function applySettings(d){
  setModeUI(d.offline_mode);

  // For each setting, only apply server value if no pending local change
  const defaultTrue  = ['fetch_images','fetch_css_js','fetch_fonts','fetch_media'];
  const defaultFalse = ['fetch_linked_files','fetch_linked_images','fetch_linked_html','cross_host_crawl'];
  const idMap = {
    'fetch_images':        'catImages',
    'fetch_css_js':        'catCssJs',
    'fetch_fonts':         'catFonts',
    'fetch_media':         'catMedia',
    'fetch_linked_files':  'catLinked',
    'fetch_linked_images': 'catLinkedImages',
    'fetch_linked_html':   'catLinkedHtml',
    'cross_host_crawl':    'chkCross',
  };
  for(const [key, id] of Object.entries(idMap)){
    if(key in _pendingSettings) continue;  // user changed this, don't overwrite
    const isDefault = defaultTrue.includes(key);
    document.getElementById(id).checked = isDefault ? (d[key] !== false) : !!d[key];
  }

  // Only update delay if not currently focused and not pending
  const delayEl = document.getElementById('fetchDelay');
  if(document.activeElement !== delayEl && !('fetch_delay_ms' in _pendingSettings))
    delayEl.value = d.fetch_delay_ms ?? 0;

  if(!('crawl_depth' in _pendingSettings)){
    const cd = d.crawl_depth ?? 0;
    if(cd !== currentDepth){
      currentDepth = cd;
      document.getElementById('depthVal').textContent = cd;
    }
  }

  // Update chip visual state
  ['catImages','catCssJs','catFonts','catMedia','catLinked','catLinkedImages','catLinkedHtml'].forEach(id=>{
    const cb = document.getElementById(id);
    cb.closest('.cat-chip').classList.toggle('off', !cb.checked);
  });
  updateSummary();
}

function onCatChange(){
  ['catImages','catCssJs','catFonts','catMedia','catLinked','catLinkedImages','catLinkedHtml'].forEach(id=>{
    const cb = document.getElementById(id);
    cb.closest('.cat-chip').classList.toggle('off', !cb.checked);
  });
  // Don't fire-and-forget — mark pending immediately before async save
  const payload = _buildSettingsPayload();
  Object.assign(_pendingSettings, payload);
  saveSettings();
}

function _buildSettingsPayload(){
  return {
    fetch_images:        document.getElementById('catImages').checked,
    fetch_css_js:        document.getElementById('catCssJs').checked,
    fetch_fonts:         document.getElementById('catFonts').checked,
    fetch_media:         document.getElementById('catMedia').checked,
    fetch_linked_files:  document.getElementById('catLinked').checked,
    fetch_linked_images: document.getElementById('catLinkedImages').checked,
    fetch_linked_html:   document.getElementById('catLinkedHtml').checked,
    cross_host_crawl:    document.getElementById('chkCross').checked,
    crawl_depth:         currentDepth,
    fetch_delay_ms:      Math.max(0, parseInt(document.getElementById('fetchDelay').value)||0),
  };
}

async function saveSettings(){
  const payload = _buildSettingsPayload();
  // Mark everything as pending BEFORE the request
  Object.assign(_pendingSettings, payload);

  try {
    const confirmed = await fetch('/api/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    }).then(r => r.json());

    // Only clear pending for keys where server confirmed the value we sent
    for(const key of Object.keys(payload)){
      if(key in _pendingSettings && confirmed[key] === payload[key]){
        delete _pendingSettings[key];
      }
    }
    // Special: boolean coercion for false values
    for(const key of Object.keys(payload)){
      if(key in _pendingSettings){
        const sent = payload[key];
        const got  = confirmed[key];
        if(!!sent === !!got) delete _pendingSettings[key];
      }
    }
  } catch(e) {
    console.warn('Settings save failed:', e);
    // Keep _pendingSettings so poll doesn't overwrite
  }
  updateSummary();
}

function adjDepth(delta){
  currentDepth = Math.max(0, Math.min(10, currentDepth + delta));
  document.getElementById('depthVal').textContent = currentDepth;
  _pendingSettings['crawl_depth'] = currentDepth;
  updateSummary();
  saveSettings();
}

function scheduleDelaySave(){
  if(_delaySaveTimer) clearTimeout(_delaySaveTimer);
  const v = Math.max(0, parseInt(document.getElementById('fetchDelay').value)||0);
  _pendingSettings['fetch_delay_ms'] = v;
  _delaySaveTimer = setTimeout(()=>{ saveSettings(); _delaySaveTimer=null; }, 700);
  updateSummary();
}

// ── Mode ───────────────────────────────────────────────────────────────────
async function toggleMode(){
  const d=await fetch('/api/toggle-mode',{method:'POST'}).then(r=>r.json());
  setModeUI(d.offline_mode);
  toast(d.offline_mode?'🔒 Offline — from cache':'🌐 Online — caching as you browse');
}
function setModeUI(offline){
  const dot=document.getElementById('dot'), lbl=document.getElementById('modeLabel');
  if(offline){dot.className='dot off'; lbl.textContent='OFFLINE';}
  else{dot.className='dot on'; lbl.textContent='ONLINE';}
}

// ── Tabs ───────────────────────────────────────────────────────────────────
function switchTab(name,el){
  currentTab=name;
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  el.classList.add('active');
  ['cached','queue','failed'].forEach(n=>
    document.getElementById('tab'+n.charAt(0).toUpperCase()+n.slice(1)).style.display=n===name?'':'none');
  syncTabActions(); // update action buttons for newly active tab
  renderActive();
}

function renderActive(){
  if(currentTab==='cached') render();
  else if(currentTab==='queue') renderQueue();
  else renderFailed();
}

function switchAside(name,el){
  document.querySelectorAll('.atab').forEach(t=>t.classList.remove('active'));
  el.classList.add('active');
  document.querySelectorAll('.aside-panel').forEach(p=>p.classList.remove('active'));
  document.getElementById('panel'+name).classList.add('active');
}

function filterHost(h,el){
  activeHost=h;
  document.querySelectorAll('.host-li li').forEach(i=>i.classList.remove('active'));
  el.classList.add('active');
  // Reset to page 1 when switching host filter — new server request needed.
  cacheState.page = 1;
  load();
}

// ── Pagination & sort helpers ──────────────────────────────────────────────

// Extract a sortable scalar from an entry for a given column key.
function sortVal(e, key){
  if(key==='url')       return (e.url||'').toLowerCase();
  if(key==='size')      return e.size||0;
  if(key==='type')      return (e.content_type||'').toLowerCase();
  if(key==='cached_at') return e.cached_at||'';
  if(key==='failed_at') return e.failed_at||'';
  return '';
}

// Return a new sorted copy of arr by key/dir without mutating the original.
function sortArr(arr, key, dir){
  if(!key) return arr;
  const d = dir==='asc' ? 1 : -1;
  return [...arr].sort((a,b)=>{
    const va=sortVal(a,key), vb=sortVal(b,key);
    return va<vb ? -d : va>vb ? d : 0;
  });
}

// Update the ⇅/▲/▼ icons in a tab's column headers to reflect the active sort.
function updateSortIcons(tab, activeSortBy, dir){
  const cols = {cache:['type','url','size','cached_at'], queue:['url'], failed:['url','failed_at']}[tab]||[];
  cols.forEach(col=>{
    const el = document.getElementById(`si-${tab}-${col}`);
    if(!el) return;
    el.textContent = col===activeSortBy ? (dir==='asc'?'▲':'▼') : '⇅';
  });
}

// Handle a sort-column header click: toggle direction if same column, else switch to new column.
function setSort(tab, col){
  const state = tab==='cache' ? cacheState : tab==='queue' ? queueState : failedState;
  if(state.sortBy===col){
    state.sortDir = state.sortDir==='asc' ? 'desc' : 'asc';
  } else {
    state.sortBy  = col;
    state.sortDir = 'desc'; // always start descending on a newly selected column
  }
  state.page = 1; // changing sort resets to the first page

  // Cache tab with scope='all': re-fetch from server so sort applies to all pages.
  // Scope='page' or queue/failed: client-side only, no network request.
  if(tab==='cache' && cacheState.sortScope==='all') load();
  else renderActive();
}

// Change per-page size for a tab and reset to page 1.
function setPageSize(tab, size){
  const state = tab==='cache' ? cacheState : tab==='queue' ? queueState : failedState;
  state.pageSize = size;
  state.page     = 1;
  if(tab==='cache') load(); else renderActive();
}

// Toggle cache sort scope: 'all' triggers a server re-fetch; 'page' sorts the current page locally.
function setSortScope(scope){
  cacheState.sortScope = scope;
  if(scope==='all'){ cacheState.page=1; load(); }
  else render();
}

// Navigate to a specific page number; re-fetches for cache (server-side), re-renders for others.
function goPage(tab, page){
  const state = tab==='cache' ? cacheState : tab==='queue' ? queueState : failedState;
  state.page = page;
  if(tab==='cache' && cacheState.sortScope==='all') load();
  else renderActive();
}

// Render a pagination bar (entry count, page-size buttons, scope toggle, prev/next/jump nav).
// containerId: id of the .pg-bar element; state: the tab's state object; total: total entries; tab: 'cache'|'queue'|'failed'.
function renderPgBar(containerId, state, total, tab){
  const el = document.getElementById(containerId);
  if(!el) return;
  const totalPages = Math.max(1, Math.ceil(total / state.pageSize));
  const p    = state.page;
  const from = total===0 ? 0 : (p-1)*state.pageSize+1;
  const to   = Math.min(p*state.pageSize, total);

  // Page-size selector
  const szBtns = [100,500].map(sz=>
    `<button class="pg-sz-btn${state.pageSize===sz?' active':''}" onclick="setPageSize('${tab}',${sz})">${sz}/page</button>`
  ).join('');

  // Sort-scope toggle — only shown on the cache tab
  const scopePart = tab==='cache' ? `
    <span class="pg-sep">|</span>
    <span class="pg-lbl">Sort:</span>
    <button class="scope-btn${cacheState.sortScope==='all'?' active':''}" onclick="setSortScope('all')" title="Sort all entries (server-side re-fetch)">All entries</button>
    <button class="scope-btn${cacheState.sortScope==='page'?' active':''}" onclick="setSortScope('page')" title="Sort only this page locally">This page</button>
  ` : '';

  el.innerHTML = `
    <span class="pg-info">${from.toLocaleString()}–${to.toLocaleString()} of ${total.toLocaleString()}</span>
    <span class="pg-sep">|</span>${szBtns}${scopePart}
    <span style="flex:1"></span>
    <button class="pg-nav-btn" onclick="goPage('${tab}',1)" ${p<=1?'disabled':''}>«</button>
    <button class="pg-nav-btn" onclick="goPage('${tab}',${p-1})" ${p<=1?'disabled':''}>‹</button>
    <span class="pg-info">Page</span>
    <input class="pg-jump" type="number" value="${p}" min="1" max="${totalPages}"
      onchange="goPage('${tab}',Math.max(1,Math.min(${totalPages},+this.value||1)))"
      onkeydown="if(event.key==='Enter')goPage('${tab}',Math.max(1,Math.min(${totalPages},+this.value||1)))"/>
    <span class="pg-info">of ${totalPages}</span>
    <button class="pg-nav-btn" onclick="goPage('${tab}',${p+1})" ${p>=totalPages?'disabled':''}>›</button>
    <button class="pg-nav-btn" onclick="goPage('${tab}',${totalPages})" ${p>=totalPages?'disabled':''}>»</button>`;
}

// ── Cache selection ────────────────────────────────────────────────────────
// Toggle one row's selected state; url is the decoded URL string.
function toggleCacheRowSel(url, cb){
  if(cb.checked) selectedCacheUrls.add(url);
  else           selectedCacheUrls.delete(url);
  cb.closest('tr').classList.toggle('sel', cb.checked);
  updateCacheSelBar();
}

// Master checkbox handler — selects/deselects all currently visible rows.
// Rows hidden by the host-filter or search-filter are not affected.
function toggleCacheSelectAll(){
  const all = document.getElementById('cacheSelectAll').checked;
  const q   = document.getElementById('srch').value.toLowerCase();
  let es    = entries;
  if(activeHost) es = es.filter(e=>{try{return new URL(e.url).hostname===activeHost}catch{return false}});
  if(q) es = es.filter(e=>e.url.toLowerCase().includes(q));
  es.forEach(e=>{ if(all) selectedCacheUrls.add(e.url); else selectedCacheUrls.delete(e.url); });
  render(); // re-renders rows with updated checked state
}

// Clear all selected cache URLs and refresh the table.
function clearCacheSelection(){
  selectedCacheUrls.clear();
  render();
}

// Sync the selection bar visibility and button labels to current selection state.
function updateCacheSelBar(){
  const bar       = document.getElementById('cacheSelBar');
  const hasSelected = selectedCacheUrls.size > 0;
  const hasHost     = !!activeHost;
  // Show bar when items are checked OR when a domain filter is active (for host-delete button)
  bar.classList.toggle('show', hasSelected || hasHost);
  const countEl = document.getElementById('cacheSelCount');
  countEl.textContent = hasSelected ? `${selectedCacheUrls.size} selected` : '';
  document.getElementById('deleteSelBtn').style.display  = hasSelected ? '' : 'none';
  document.getElementById('deselectAllBtn').style.display = hasSelected ? '' : 'none';
  // Host-delete button: shows the total count from hostsData (not capped at 500)
  const delHostBtn = document.getElementById('delHostBtn');
  if(hasHost){
    const total = (hostsData[activeHost]||{}).count || '?';
    delHostBtn.textContent = `Delete all from ${activeHost} (${total} files)`;
    delHostBtn.style.display = '';
  } else {
    delHostBtn.style.display = 'none';
  }
}

// Bulk-delete selected cache URLs via /api/delete-bulk with a URL list.
function deleteSelectedCache(){
  if(!selectedCacheUrls.size) return;
  const urls = [...selectedCacheUrls];
  showConfirm(
    'Delete from cache',
    `Permanently delete ${urls.length} cached item${urls.length>1?'s':''}?\nThis cannot be undone.`,
    `Delete ${urls.length} item${urls.length>1?'s':''}`,
    async()=>{
      await fetch('/api/delete-bulk',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({urls})});
      urls.forEach(u=>selectedCacheUrls.delete(u));
      toast(`Deleted ${urls.length} item${urls.length>1?'s':''} from cache`);
      load();
    }
  );
}

// Delete ALL cached files for the active host — server-side so the 500-item UI cap doesn't apply.
function deleteAllFromHost(){
  if(!activeHost) return;
  const total = (hostsData[activeHost]||{}).count || '?';
  showConfirm(
    `Delete ${activeHost}`,
    `Permanently delete all ${total} cached files for ${activeHost}?\nThis cannot be undone.`,
    `Delete all from ${activeHost}`,
    async()=>{
      await fetch('/api/delete-bulk',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({host:activeHost})});
      selectedCacheUrls.clear();
      toast(`Deleted all cached files for ${activeHost}`);
      // Return to "All" domain view since the host is now empty
      const allLi = document.querySelector('.host-li li[data-host=""]');
      if(allLi) filterHost('', allLi);
      else load();
    }
  );
}

// ── Cached table ───────────────────────────────────────────────────────────
function render(){
  const q = document.getElementById('srch').value.toLowerCase();
  // `entries` contains the current server page already sorted and host-filtered.
  // Apply client-side text search on top of that.
  let es = q ? entries.filter(e=>e.url.toLowerCase().includes(q)) : entries;

  // If sort scope is 'page', re-sort client-side (no server round-trip).
  // Scope 'all' means the server already sorted; we display as-is.
  if(cacheState.sortScope==='page' && cacheState.sortBy){
    es = sortArr(es, cacheState.sortBy, cacheState.sortDir);
  }

  // Top bar: entry count, page-size buttons, sort-scope toggle.
  // For scope='all' total is the server's cacheTotalFiltered; for 'page' total is the current page size.
  const topTotal = cacheState.sortScope==='all' ? cacheTotalFiltered : es.length;
  renderPgBar('cachePgTop', cacheState, topTotal, 'cache');

  const tb = document.getElementById('tbody');
  tb.innerHTML = '';
  document.getElementById('emptyCached').style.display = es.length ? 'none' : 'block';

  es.forEach(e=>{
    const enc     = encodeURIComponent(e.url);
    const checked = selectedCacheUrls.has(e.url);
    const tr      = document.createElement('tr');
    if(checked) tr.classList.add('sel');
    // Use enc (percent-encoded) in inline handlers to prevent quote injection from URLs.
    tr.innerHTML=`
      <td class="chk-col"><input type="checkbox" class="row-cb" ${checked?'checked':''}
        onchange="toggleCacheRowSel(decodeURIComponent('${enc}'),this)"/></td>
      <td>${badge(e.content_type)}</td>
      <td class="url-td"><a href="/view?url=${enc}" target="_blank" title="${e.url}">${e.url}</a></td>
      <td class="sz">${fmtB(e.size)}</td><td class="dt">${fmtD(e.cached_at)}</td>
      <td><div class="actions">
        <button class="btn" onclick="refetch('${enc}')" title="Re-fetch">↻</button>
        <button class="btn" onclick="del('${enc}')" style="color:var(--mut)" title="Remove">✕</button>
      </div></td>`;
    tb.appendChild(tr);
  });

  // Sync master checkbox: fully checked when all visible rows are selected, indeterminate when partial.
  const selAll = document.getElementById('cacheSelectAll');
  if(selAll && es.length>0){
    const allSel = es.every(e=>selectedCacheUrls.has(e.url));
    const anySel = es.some(e=>selectedCacheUrls.has(e.url));
    selAll.checked       = allSel;
    selAll.indeterminate = !allSel && anySel;
  } else if(selAll){
    selAll.checked = selAll.indeterminate = false;
  }

  // Bottom nav bar (prev/next/jump) — uses cacheTotalFiltered for page count.
  renderPgBar('cachePgNav', cacheState, cacheTotalFiltered, 'cache');
  updateCacheSelBar();
  updateSortIcons('cache', cacheState.sortBy, cacheState.sortDir);
}

// ── Queue table ────────────────────────────────────────────────────────────
function renderQueue(){
  const q = document.getElementById('srch').value.toLowerCase();
  let es = queueEntries;
  if(q) es = es.filter(e=>e.url.toLowerCase().includes(q)||(e.referer||'').toLowerCase().includes(q));

  // Client-side sort (queue data is small enough to sort entirely in JS).
  if(queueState.sortBy) es = sortArr(es, queueState.sortBy, queueState.sortDir);

  const total = es.length;
  const start = (queueState.page - 1) * queueState.pageSize;
  const pageEs = es.slice(start, start + queueState.pageSize);

  const tb = document.getElementById('queueTbody');
  tb.innerHTML = '';
  document.getElementById('emptyQueue').style.display = total ? 'none' : 'block';

  pageEs.forEach(e=>{
    const d   = e.depth??0;
    const cls = d===0?'d0':d===1?'d1':d===2?'d2':'d3p';
    const enc = encodeURIComponent(e.url);
    const checked = selectedQueueUrls.has(e.url);
    const tr  = document.createElement('tr');
    if(checked) tr.classList.add('sel');
    // Use enc (percent-encoded) in all inline handlers to prevent quote injection.
    tr.innerHTML=`
      <td class="chk-col"><input type="checkbox" class="row-cb" ${checked?'checked':''}
        onchange="toggleRowSel(decodeURIComponent('${enc}'),this)"/></td>
      <td><span class="depth-chip ${cls}">${d}</span></td>
      <td class="url-td" title="${e.url}">${e.url}</td>
      <td class="referer-td" title="${e.referer||''}">${e.origin_host||e.referer||'—'}</td>
      <td><button class="btn" onclick="removeOneFromQueue('${enc}')">✕</button></td>`;
    tb.appendChild(tr);
  });

  renderPgBar('queuePgNav', queueState, total, 'queue');
  updateQSelBar();
  updateSortIcons('queue', queueState.sortBy, queueState.sortDir);
}

function toggleRowSel(url, cb){
  // url is always the decoded string (inline handler uses decodeURIComponent)
  if(cb.checked) selectedQueueUrls.add(url);
  else           selectedQueueUrls.delete(url);
  cb.closest('tr').classList.toggle('sel', cb.checked);
  updateQSelBar();
}

function toggleSelectAll(){
  const all = document.getElementById('selectAll').checked;
  queueEntries.forEach(e=>{
    if(all) selectedQueueUrls.add(e.url); else selectedQueueUrls.delete(e.url);
  });
  renderQueue();
}

function clearSelection(){
  selectedQueueUrls.clear();
  document.getElementById('selectAll').checked=false;
  renderQueue();
}

// ── Confirm modal ──────────────────────────────────────────────────────────
// Single shared dialog used by every destructive action so the style is consistent.
// title    — modal heading
// msg      — body text (supports \n line breaks via white-space:pre-wrap)
// okLabel  — text for the confirm button (e.g. "Delete 5 items")
// onOk     — async function to run when the user confirms
function showConfirm(title, msg, okLabel, onOk){
  _confirmCallback = onOk;
  document.getElementById('confirmTitle').textContent = title;
  document.getElementById('confirmMsg').textContent   = msg;
  const btn = document.getElementById('confirmOkBtn');
  btn.textContent = okLabel || 'Delete';
  // Style the confirm button red to signal the action is destructive
  btn.style.borderColor = 'var(--danger)';
  btn.style.color       = 'var(--danger)';
  document.getElementById('confirmModal').classList.add('show');
}
function confirmOk(){
  document.getElementById('confirmModal').classList.remove('show');
  if(_confirmCallback) _confirmCallback();
  _confirmCallback = null;
}
function confirmCancel(){
  document.getElementById('confirmModal').classList.remove('show');
  _confirmCallback = null;
}

function updateQSelBar(){
  const bar=document.getElementById('qSelBar');
  bar.classList.toggle('show', selectedQueueUrls.size>0);
  document.getElementById('qSelCount').textContent=`${selectedQueueUrls.size} selected`;
}

function deleteSelected(){
  if(!selectedQueueUrls.size) return;
  const urls=[...selectedQueueUrls];
  showConfirm(
    'Remove from queue',
    `Remove ${urls.length} item${urls.length>1?'s':''} from the fetch queue?`,
    `Remove ${urls.length}`,
    async()=>{
      await fetch('/api/queue-remove',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({urls})});
      urls.forEach(u=>selectedQueueUrls.delete(u));
      toast(`Removed ${urls.length} item${urls.length>1?'s':''} from queue`);
      load();
    }
  );
}

function removeOneFromQueue(enc){
  const url=decodeURIComponent(enc);
  showConfirm(
    'Remove from queue',
    `Remove from fetch queue?\n${url}`,
    'Remove',
    async()=>{
      await fetch('/api/queue-remove',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({urls:[url]})});
      toast('Removed from queue'); load();
    }
  );
}

async function clearQueue(){
  await fetch('/api/queue-clear',{method:'POST'});
  selectedQueueUrls.clear(); toast('Queue cleared'); load();
}

// ── Failed table ───────────────────────────────────────────────────────────
function renderFailed(){
  const q = document.getElementById('srch').value.toLowerCase();
  let es = failedEntries.filter(e=>!q||e.url.toLowerCase().includes(q));

  // Client-side sort.
  if(failedState.sortBy) es = sortArr(es, failedState.sortBy, failedState.sortDir);

  const total  = es.length;
  const start  = (failedState.page - 1) * failedState.pageSize;
  const pageEs = es.slice(start, start + failedState.pageSize);

  const tb = document.getElementById('failedTbody');
  tb.innerHTML = '';
  document.getElementById('emptyFailed').style.display = total ? 'none' : 'block';

  pageEs.forEach(e=>{
    const enc = encodeURIComponent(e.url);
    const tr  = document.createElement('tr');
    tr.innerHTML=`
      <td class="url-td" title="${e.url}">${e.url}</td>
      <td class="fail-reason">${e.reason||'?'}</td>
      <td class="dt">${fmtD(e.failed_at)}</td>
      <td><div class="actions">
        <button class="btn" onclick="retryOne('${enc}')">↻</button>
        <button class="btn" onclick="dismissFailed('${enc}')" style="color:var(--mut)">✕</button>
      </div></td>`;
    tb.appendChild(tr);
  });

  renderPgBar('failedPgNav', failedState, total, 'failed');
  updateSortIcons('failed', failedState.sortBy, failedState.sortDir);
}

// ── Actions ────────────────────────────────────────────────────────────────
function del(enc){
  const url=decodeURIComponent(enc);
  showConfirm(
    'Remove from cache',
    `Remove this file from the offline cache?\n${url}`,
    'Remove',
    async()=>{
      await fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({url})});
      selectedCacheUrls.delete(url); // keep selection set consistent
      toast('Removed from cache'); load();
    }
  );
}
async function refetch(enc){
  await fetch('/api/refetch',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({url:decodeURIComponent(enc)})});
  toast('↻ Re-fetch queued');
}
async function retryOne(enc){
  await fetch('/api/retry',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({url:decodeURIComponent(enc)})});
  toast('↻ Retry queued'); load();
}
async function retryAll(){
  await fetch('/api/retry-all',{method:'POST'});
  toast('↻ Retrying all…'); load();
}
function clearFailed(){
  showConfirm(
    'Clear failed list',
    'Remove all failed fetch records?\nThis only clears the log — it does not delete any cached files.',
    'Clear all',
    async()=>{
      await fetch('/api/clear-failed',{method:'POST'});
      // Optimistic update: clear local state immediately so the table empties
      // right now without waiting for the load() network round-trip to finish.
      failedEntries = [];
      syncTabActions();
      renderActive();
      toast('Failed list cleared');
      load(); // background refresh to confirm server state
    }
  );
}
function dismissFailed(enc){
  const url=decodeURIComponent(enc);
  showConfirm(
    'Dismiss failed fetch',
    `Remove this failed fetch record?\n${url}`,
    'Dismiss',
    async()=>{
      await fetch('/api/clear-failed',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({url})});
      // Optimistic update: remove just this entry from local state immediately
      failedEntries = failedEntries.filter(e => e.url !== url);
      syncTabActions();
      renderActive();
      load();
    }
  );
}

// ── Blocklist ──────────────────────────────────────────────────────────────
function renderBlockList(blocked){
  const ul=document.getElementById('blockList');
  ul.innerHTML='';
  if(!blocked.length){ul.innerHTML='<li class="block-empty">No blocked domains</li>';return;}
  blocked.forEach(d=>{
    const li=document.createElement('li'); li.className='block-li';
    li.innerHTML=`<span>${d}</span><button class="block-rm" onclick="removeBlock('${d}')">×</button>`;
    ul.appendChild(li);
  });
}
async function addBlock(){
  let domain=document.getElementById('blockInput').value.trim().toLowerCase()
    .replace(/^https?:\/\//,'').split('/')[0];
  if(!domain)return;
  const state=await fetch('/api/settings',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({add_blocked_domain:domain})}).then(r=>r.json());
  document.getElementById('blockInput').value='';
  renderBlockList(state.blocked_domains||[]); toast(`Blocked: ${domain}`);
}
async function removeBlock(domain){
  const state=await fetch('/api/settings',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({remove_blocked_domain:domain})}).then(r=>r.json());
  renderBlockList(state.blocked_domains||[]); toast(`Unblocked: ${domain}`);
}

// ── Export ─────────────────────────────────────────────────────────────────
function openExport(){document.getElementById('exportModal').classList.add('show');document.getElementById('exportProgress').textContent='';}
function closeExport(){document.getElementById('exportModal').classList.remove('show');}
async function doExport(){
  const dest=document.getElementById('exportPath').value.trim();
  if(!dest){toast('Enter a path first');return;}
  document.getElementById('exportBtn').disabled=true;
  document.getElementById('exportProgress').textContent='Exporting…';
  try{
    const r=await fetch('/api/export',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({dest})}).then(r=>r.json());
    document.getElementById('exportProgress').textContent=
      r.ok?`✓ Exported ${r.count} files to ${r.dest}`:'Error: '+(r.error||'unknown');
  }catch(e){document.getElementById('exportProgress').textContent='Error: '+e;}
  document.getElementById('exportBtn').disabled=false;
}

// ── Helpers ────────────────────────────────────────────────────────────────
function badge(ct){
  if(!ct)return'<span class="badge b-oth">?</span>';
  if(ct.includes('html'))return'<span class="badge b-html">HTML</span>';
  if(ct.includes('css'))return'<span class="badge b-css">CSS</span>';
  if(ct.includes('javascript'))return'<span class="badge b-js">JS</span>';
  if(ct.includes('image'))return'<span class="badge b-img">IMG</span>';
  return`<span class="badge b-oth">${ct.split('/').pop().slice(0,5).toUpperCase()}</span>`;
}
function fmtB(b){
  if(b>=1e9)return(b/1e9).toFixed(1)+' GB';
  if(b>=1e6)return(b/1e6).toFixed(1)+' MB';
  if(b>=1e3)return(b/1e3).toFixed(1)+' KB';
  return(b||0)+' B';
}
function fmtD(d){
  if(!d)return'—';
  try{const dt=new Date(d);return dt.toLocaleDateString()+' '+dt.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});}catch{return d}
}
function toast(msg){
  const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),3000);
}

// ── Init ───────────────────────────────────────────────────────────────────
load();
setInterval(load, 3000);
</script>
</body>
</html>"""

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index(): return DASHBOARD_HTML

@app.route("/api/cache")
def api_cache():
    meta  = load_meta()
    raw   = meta.get("cached_urls", {})
    stats = meta.get("stats", {"total": 0, "bytes": 0})

    # Compute per-host counts/bytes over ALL entries before filtering so the
    # sidebar always shows correct domain totals regardless of the active filter.
    hc, hs = {}, {}
    for url, info in raw.items():
        try:
            h = urlparse(url).hostname or ""
            if h:
                hc[h] = hc.get(h, 0) + 1
                hs[h] = hs.get(h, 0) + info.get("size", 0)
        except Exception: pass
    hosts = {h: {"count": hc[h], "bytes": hs[h]} for h in hc}

    q        = request.args.get("q",        "").strip().lower()
    host     = request.args.get("host",     "").strip()
    sort_by  = request.args.get("sort_by",  "cached_at")
    sort_dir = request.args.get("sort_dir", "desc")
    try:    limit  = min(max(int(request.args.get("limit",  500)), 1), 1000)
    except: limit  = 500
    try:    offset = max(int(request.args.get("offset", 0)), 0)
    except: offset = 0

    entries = [{"url": url, **info} for url, info in raw.items()]
    if q:    entries = [e for e in entries if q in e["url"].lower()]
    if host: entries = [e for e in entries if urlparse(e["url"]).hostname == host]

    # Sort the full filtered list server-side so pagination is consistent across pages.
    rev = (sort_dir == "desc")
    if sort_by == "url":
        entries.sort(key=lambda e: e["url"].lower(), reverse=rev)
    elif sort_by == "size":
        entries.sort(key=lambda e: e.get("size", 0), reverse=rev)
    elif sort_by == "type":
        entries.sort(key=lambda e: e.get("content_type", ""), reverse=rev)
    else:  # cached_at (default)
        entries.sort(key=lambda e: e.get("cached_at", ""), reverse=rev)

    # Add total HTML-page count across all cached content for the dashboard stats widget.
    stats["pages"] = sum(1 for info in raw.values() if "html" in info.get("content_type", ""))

    total_filtered = len(entries)
    entries = entries[offset : offset + limit]
    return jsonify({"entries": entries, "total_filtered": total_filtered, "stats": stats, "hosts": hosts})

@app.route("/api/mode")
def api_mode():
    s = load_state()
    # Overlay the live queue depth from the proxy's dedicated file.
    # The proxy writes _queue_depth.json; it never writes _state.json.
    # This prevents the write race that caused pause/delay/toggles to revert.
    s["queue_depth"] = load_queue_depth()
    return jsonify(s)

@app.route("/api/failed")
def api_failed(): return jsonify({"entries": list(load_failed().values())})

@app.route("/api/queue")
def api_queue():
    items = load_queue()
    return jsonify({"items": items, "count": len(items)})

@app.route("/api/toggle-mode", methods=["POST"])
def api_toggle_mode():
    state = load_state(); state["offline_mode"] = not state.get("offline_mode", False)
    save_state(state); return jsonify(state)

@app.route("/api/toggle-pause", methods=["POST"])
def api_toggle_pause():
    state = load_state(); state["queue_paused"] = not state.get("queue_paused", False)
    save_state(state); return jsonify(state)

@app.route("/api/settings", methods=["POST"])
def api_settings():
    """Patch state.json with whatever keys the client sent.

    Boolean toggles, the integer fields (fetch_delay_ms, crawl_depth) and the
    blocklist mutators (add_blocked_domain, remove_blocked_domain) are all
    processed in one round-trip; missing keys are left untouched. Returns the
    full state so the dashboard's pending-settings guard can confirm each
    field individually.
    """
    data  = request.get_json() or {}
    state = load_state()
    for k in ("fetch_images","fetch_css_js","fetch_fonts","fetch_media",
              "fetch_linked_files","fetch_linked_images","fetch_linked_html",
              "cross_host_crawl"):
        if k in data: state[k] = bool(data[k])
    if "fetch_delay_ms" in data:
        state["fetch_delay_ms"] = max(0, int(data.get("fetch_delay_ms", 0)))
    if "crawl_depth" in data:
        state["crawl_depth"] = max(0, min(10, int(data.get("crawl_depth", 0))))
    if "add_blocked_domain" in data:
        bl = state.get("blocked_domains", [])
        d  = data["add_blocked_domain"].strip().lower()
        if d and d not in bl: bl.append(d)
        state["blocked_domains"] = bl
    if "remove_blocked_domain" in data:
        d = data["remove_blocked_domain"].strip().lower()
        state["blocked_domains"] = [x for x in state.get("blocked_domains", []) if x != d]
    save_state(state); return jsonify(state)

@app.route("/api/delete", methods=["POST"])
def api_delete():
    global _meta_cache_ts
    url = (request.get_json() or {}).get("url", "")
    if not url: return abort(400)
    meta = load_meta()
    if url in meta.get("cached_urls", {}):
        info = meta["cached_urls"].pop(url)
        try:
            cp = CACHE_DIR / info["path"]
            cp.unlink(missing_ok=True)
            cp.with_suffix(cp.suffix + ".meta.json").unlink(missing_ok=True)
        except Exception: pass
        meta["stats"]["total"] = len(meta["cached_urls"])
        meta["stats"]["bytes"] = sum(v.get("size",0) for v in meta["cached_urls"].values())
        META_FILE.write_text(json.dumps(meta, indent=2), "utf-8")
        _meta_cache_ts = 0.0
    return jsonify({"ok": True})

@app.route("/api/delete-bulk", methods=["POST"])
def api_delete_bulk():
    """Delete multiple cached items in one request.

    Accepts either:
      {"urls": ["https://..."]}    — delete a specific list of URLs
      {"host": "example.com"}     — delete every URL for that hostname
                                    (server-side, not capped by the 500-item UI limit)

    Returns the count of entries actually removed from _meta.json.
    """
    global _meta_cache_ts
    data  = request.get_json() or {}
    meta  = load_meta()
    cached = meta.get("cached_urls", {})
    deleted = 0

    if "host" in data:
        # Server-side host filter — finds all URLs regardless of the 500-item UI cap
        host   = data["host"].strip().lower()
        to_del = [url for url in list(cached.keys())
                  if urlparse(url).hostname == host]
        for url in to_del:
            info = cached.pop(url)
            try:
                cp = CACHE_DIR / info["path"]
                cp.unlink(missing_ok=True)
                cp.with_suffix(cp.suffix + ".meta.json").unlink(missing_ok=True)
            except Exception:
                pass
            deleted += 1
    else:
        # Client-provided URL list (selected checkboxes)
        urls = data.get("urls", [])
        if not urls:
            return abort(400)
        for url in urls:
            if url in cached:
                info = cached.pop(url)
                try:
                    cp = CACHE_DIR / info["path"]
                    cp.unlink(missing_ok=True)
                    cp.with_suffix(cp.suffix + ".meta.json").unlink(missing_ok=True)
                except Exception:
                    pass
                deleted += 1

    # Recompute stats and persist
    meta["stats"]["total"] = len(cached)
    meta["stats"]["bytes"] = sum(v.get("size", 0) for v in cached.values())
    META_FILE.write_text(json.dumps(meta, indent=2), "utf-8")
    _meta_cache_ts = 0.0  # Invalidate in-memory cache so next poll sees updated state
    return jsonify({"ok": True, "deleted": deleted})

@app.route("/api/queue-clear", methods=["POST"])
def api_queue_clear():
    # Signal file triggers the proxy's in-memory queue.clear() on its next request hook.
    # Writing [] to QUEUE_FILE is an optimistic update so the UI shows 0 immediately.
    (CACHE_DIR / "_queue_clear").write_text("1", "utf-8")
    QUEUE_FILE.write_text("[]", "utf-8")
    return jsonify({"ok": True})

@app.route("/api/queue-remove", methods=["POST"])
def api_queue_remove():
    """Drop URLs from the proxy's in-memory queue.

    The proxy holds the queue in a thread, not on disk, so we drop a signal
    file in `_queue_remove/` for it to consume on the next request. The
    queue.json snapshot is also pruned optimistically so the UI updates
    immediately instead of waiting for the proxy's next snapshot write.
    """
    data = request.get_json() or {}
    urls = data.get("urls", [])
    if isinstance(urls, str): urls = [urls]
    if not urls: return abort(400)
    rm_dir = CACHE_DIR / "_queue_remove"; rm_dir.mkdir(exist_ok=True)
    key = hashlib.md5(json.dumps(sorted(urls)).encode()).hexdigest()
    (rm_dir / key).write_text(json.dumps(urls), "utf-8")
    items = load_queue()
    url_set = set(urls)
    items = [i for i in items if i.get("url") not in url_set]
    QUEUE_FILE.write_text(json.dumps(items), "utf-8")
    return jsonify({"ok": True, "removed": len(urls)})

@app.route("/api/refetch", methods=["POST"])
def api_refetch():
    # The proxy queue lives in its own process — we can't call it directly.
    # Drop a signal file in _refetch/ that the proxy reads during its next request hook
    # and re-queues with force=True (bypassing the is_cached() guard).
    url = (request.get_json() or {}).get("url", "")
    if not url: return abort(400)
    rf_dir = CACHE_DIR / "_refetch"; rf_dir.mkdir(exist_ok=True)
    (rf_dir / hashlib.md5(url.encode()).hexdigest()).write_text(url, "utf-8")
    return jsonify({"ok": True})

@app.route("/api/retry", methods=["POST"])
def api_retry():
    url = (request.get_json() or {}).get("url", "")
    if not url: return abort(400)
    f = load_failed(); f.pop(url, None)
    FAILED_FILE.write_text(json.dumps(f, indent=2), "utf-8")
    rf_dir = CACHE_DIR / "_refetch"; rf_dir.mkdir(exist_ok=True)
    (rf_dir / hashlib.md5(url.encode()).hexdigest()).write_text(url, "utf-8")
    return jsonify({"ok": True})

@app.route("/api/retry-all", methods=["POST"])
def api_retry_all():
    f = load_failed()
    rf_dir = CACHE_DIR / "_refetch"; rf_dir.mkdir(exist_ok=True)
    for url in list(f.keys()):
        (rf_dir / hashlib.md5(url.encode()).hexdigest()).write_text(url, "utf-8")
    FAILED_FILE.write_text("{}", "utf-8")
    return jsonify({"ok": True, "count": len(f)})

@app.route("/api/clear-failed", methods=["POST"])
def api_clear_failed():
    data = request.get_json() or {}
    if "url" in data:
        f = load_failed(); f.pop(data["url"], None)
        FAILED_FILE.write_text(json.dumps(f, indent=2), "utf-8")
    else:
        FAILED_FILE.write_text("{}", "utf-8")
    return jsonify({"ok": True})


# ── Charset-aware decode (mirrors proxy_addon logic) ─────────────────────────
_META_CHARSET_RE2 = re.compile(r'<meta[^>]+charset\s*=\s*["\']?\s*([\w-]+)', re.I)
_META_CT_RE2      = re.compile(r'<meta[^>]+content\s*=\s*["\'][^"\']*charset=([\w-]+)', re.I)

def _detect_charset_dash(raw: bytes, ct_header: str = "") -> str:
    # Intentional duplicate of proxy_addon._detect_charset.  The dashboard is a
    # separate process and cannot import proxy_addon (mitmproxy is not importable
    # stand-alone), so the charset detection logic must live here too.
    if "charset=" in ct_header.lower():
        m = re.search(r'charset=([\w-]+)', ct_header, re.I)
        if m: return m.group(1).strip()
    peek = raw[:2048].decode("ascii", errors="replace")
    m = _META_CHARSET_RE2.search(peek)
    if m: return m.group(1).strip()
    m = _META_CT_RE2.search(peek)
    if m: return m.group(1).strip()
    if raw.startswith(b'\xef\xbb\xbf'): return 'utf-8-sig'
    return "latin-1"

def _smart_decode(raw: bytes, sidecar: Path) -> str:
    """Decode bytes using charset from sidecar or detection."""
    ct_full = ""
    if sidecar.exists():
        try:
            sc = json.loads(sidecar.read_text("utf-8"))
            ct_full = sc.get("content_type_full", sc.get("content_type", ""))
        except Exception: pass
    charset = _detect_charset_dash(raw, ct_full)
    try:
        return raw.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return raw.decode("latin-1", errors="replace")


# ── URL rewriter ──────────────────────────────────────────────────────────────

def _build_url_map(entries: dict) -> dict:
    """
    Build a lookup map for the export rewriter.
    Since url_to_path now decodes %XX → real chars, the 'path' values in
    _meta.json already use decoded filenames (e.g. 'Images Landkarten/file.jpg').

    We map BOTH the original percent-encoded URL AND its decoded form to the
    same cache path, so the rewriter can find a match regardless of whether
    the HTML contains 'Images%20Landkarten/file.jpg' or 'Images Landkarten/file.jpg'.
    """
    from urllib.parse import unquote
    url_map = {}
    for url, info in entries.items():
        path = info.get("path", "")
        if not path:
            continue
        url_map[url] = path                  # encoded form:  https://.../Images%20Landkarten/file.jpg
        decoded = unquote(url)
        if decoded != url:
            url_map[decoded] = path          # decoded form:  https://.../Images Landkarten/file.jpg
        # Also map the http:// variant — some pages link to the same resource
        # with a mixed scheme (e.g. stylesheet via http:// on an https:// page).
        if url.startswith("https://"):
            http = "http://" + url[8:]
            url_map[http] = path
            url_map[unquote(http)] = path
    return url_map


import os as _os

def _rel_path(target_cache_path: str, dest_root: Path, page_dest: Path) -> str:
    """
    Compute a correct relative path from page_dest's directory to the target file.
    Uses os.path.relpath so it's always right regardless of nesting depth.
    Returns forward-slash path for HTML/CSS.
    """
    target_abs = dest_root / target_cache_path.replace("\\", "/")
    # PurePosixPath.relative_to() would raise if target is outside page's tree;
    # os.path.relpath handles cross-subtree cases correctly (e.g. ../images/foo.png).
    rel = _os.path.relpath(str(target_abs), str(page_dest.parent))
    return rel.replace("\\", "/")


def _rewrite_html(raw: bytes, page_url: str, url_map: dict,
                  dest_root: Path, page_dest: Path, sidecar: Path,
                  url_pattern=None) -> bytes:
    """
    Rewrite HTML for export so all URLs point to local files.

    Handles:
    1. Absolute https://host/path URLs  → relative path (if in url_map)
    2. Absolute http://host/path URLs   → relative path
    3. Root-relative /path URLs         → relative path
    4. Relative paths that still work   → left unchanged (already correct)

    Because url_to_path now stores files with decoded names (spaces not %20),
    and because we build url_map with both encoded+decoded variants as keys,
    all forms resolve correctly.

    Charset is decoded from original encoding and output as UTF-8.
    """
    from urllib.parse import unquote
    parsed    = urlparse(page_url)
    host_https = f"{parsed.scheme}://{parsed.hostname}"
    host_http  = f"http://{parsed.hostname}"

    text = _smart_decode(raw, sidecar)

    # Single-pass replacement of all absolute URLs using pre-compiled pattern
    if url_pattern:
        text = url_pattern.sub(
            lambda m: _rel_path(url_map[m.group(0)], dest_root, page_dest), text)
    else:
        for url in sorted(url_map, key=len, reverse=True):
            if url in text:
                text = text.replace(url, _rel_path(url_map[url], dest_root, page_dest))

    # Root-relative /path in attributes
    def _fix_root_attr(m):
        attr  = m.group(1)
        rpath = m.group(2)
        quote = m.group(3)
        for prefix in (host_https, host_http):
            for variant in (prefix + rpath, unquote(prefix + rpath)):
                if variant in url_map:
                    return attr + _rel_path(url_map[variant], dest_root, page_dest) + quote
        return m.group(0)

    text = re.sub(
        r'((?:src|href|action|data-src|poster|data-lazy|data-original|data-bg|data-background)'
        r'\s*=\s*["\'])(/[^"\'> ]+)(["\'])',
        _fix_root_attr, text, flags=re.IGNORECASE)

    # Root-relative /path in url()
    def _fix_root_url(m):
        rpath = m.group(1)
        for prefix in (host_https, host_http):
            for variant in (prefix + rpath, unquote(prefix + rpath)):
                if variant in url_map:
                    return 'url("' + _rel_path(url_map[variant], dest_root, page_dest) + '")'
        return m.group(0)

    text = re.sub(r'url\(["\']?(/[^"\')\s]+)["\']?\)', _fix_root_url, text, flags=re.IGNORECASE)

    # Bare relative src/href paths (e.g. "images/foo.gif", not starting with / or scheme)
    # Needed when the stored path is deeper than the URL implies — e.g. the root URL
    # https://host/ is stored at host/index/index.html, so relative refs like images/foo.gif
    # resolve incorrectly after export unless we fix them here.
    def _fix_bare_attr(m):
        attr  = m.group(1)
        rpath = m.group(2)
        quote = m.group(3)
        if not rpath or rpath[0] in ('.', '/', '#'):
            return m.group(0)
        if re.match(r'^[a-zA-Z][a-zA-Z0-9+\-.]*:', rpath):  # scheme (http:, data:, mailto:…)
            return m.group(0)
        abs_url = urljoin(page_url, rpath)
        for variant in (abs_url, unquote(abs_url)):
            if variant in url_map:
                return attr + _rel_path(url_map[variant], dest_root, page_dest) + quote
        return m.group(0)

    text = re.sub(
        r'((?:src|href|action|data-src|poster|data-lazy|data-original|data-bg|data-background)'
        r'\s*=\s*["\'])([^"\'>\s]+)(["\'])',
        _fix_bare_attr, text, flags=re.IGNORECASE)

    # Bare relative url() — same logic as above for inline styles / style blocks
    def _fix_bare_url(m):
        rpath = m.group(1)
        if not rpath or rpath[0] in ('.', '/', '#'):
            return m.group(0)
        if re.match(r'^[a-zA-Z][a-zA-Z0-9+\-.]*:', rpath):
            return m.group(0)
        abs_url = urljoin(page_url, rpath)
        for variant in (abs_url, unquote(abs_url)):
            if variant in url_map:
                return 'url("' + _rel_path(url_map[variant], dest_root, page_dest) + '")'
        return m.group(0)

    text = re.sub(r'url\(["\']?([^"\'()\s]+)["\']?\)', _fix_bare_url, text, flags=re.IGNORECASE)

    # Update charset declaration to utf-8
    text = re.sub(r'charset\s*=\s*[\w-]+', 'charset=utf-8', text, flags=re.IGNORECASE)
    return text.encode("utf-8")


def _rewrite_css(raw: bytes, page_url: str, url_map: dict,
                 dest_root: Path, page_dest: Path, sidecar: Path,
                 url_pattern=None) -> bytes:
    """Rewrite CSS for export — same logic as HTML but for url() and @import."""
    from urllib.parse import unquote
    parsed     = urlparse(page_url)
    host_https = f"{parsed.scheme}://{parsed.hostname}"
    host_http  = f"http://{parsed.hostname}"

    text = _smart_decode(raw, sidecar)

    if url_pattern:
        text = url_pattern.sub(
            lambda m: _rel_path(url_map[m.group(0)], dest_root, page_dest), text)
    else:
        for url in sorted(url_map, key=len, reverse=True):
            if url in text:
                text = text.replace(url, _rel_path(url_map[url], dest_root, page_dest))

    def _fix_root(m):
        rpath = m.group(1)
        for prefix in (host_https, host_http):
            for variant in (prefix + rpath, unquote(prefix + rpath)):
                if variant in url_map:
                    return 'url("' + _rel_path(url_map[variant], dest_root, page_dest) + '")'
        return m.group(0)

    text = re.sub(r'url\(["\']?(/[^"\')\s]+)["\']?\)', _fix_root, text, flags=re.IGNORECASE)
    return text.encode("utf-8")



@app.route("/api/export", methods=["POST"])
def api_export():
    data = request.get_json() or {}
    dest = data.get("dest", "").strip()
    if not dest:
        return jsonify({"ok": False, "error": "No destination path"})
    dest_path = Path(dest)
    try:
        dest_path.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

    meta    = load_meta()
    entries = meta.get("cached_urls", {})
    url_map = _build_url_map(entries)
    # Compile all cached URLs into a single alternation regex (longest first to avoid
    # partial matches) — makes absolute-URL replacement O(n) per file instead of O(n×m),
    # which is critical when exporting caches with thousands of unique URLs.
    url_pattern = re.compile(
        '|'.join(re.escape(u) for u in sorted(url_map, key=len, reverse=True))
    ) if url_map else None
    count   = 0

    for url, info in entries.items():
        src = CACHE_DIR / info.get("path", "")
        if not src.exists():
            continue
        out = dest_path / Path(info.get("path", ""))
        out.parent.mkdir(parents=True, exist_ok=True)
        sc  = src.with_suffix(src.suffix + ".meta.json")
        ct  = info.get("content_type", "")
        try:
            raw = src.read_bytes()
            if "html" in ct or "xhtml" in ct:
                raw = _rewrite_html(raw, url, url_map, dest_path, out, sc, url_pattern)
            elif "css" in ct:
                raw = _rewrite_css(raw, url, url_map, dest_path, out, sc, url_pattern)
            out.write_bytes(raw)
        except Exception:
            shutil.copy2(src, out)
        count += 1

    pages = [(u, i) for u, i in entries.items() if "html" in i.get("content_type", "")]
    idx = [
        "<!DOCTYPE html><html><head><meta charset=UTF-8>",
        "<style>body{font-family:sans-serif;padding:24px;max-width:900px;margin:auto}"
        "h1{margin-bottom:16px}li{margin:4px 0}a{color:#38bdf8}</style>",
        "<title>LazyMirror Export</title></head><body>",
        f"<h1>LazyMirror \u2014 {count} cached files</h1>",
        "<p>Click any page below to browse the offline archive.</p><ul>",
    ]
    for url, info in sorted(pages, key=lambda x: x[0]):
        idx.append(f'<li><a href="{info.get("path","").replace(chr(92), "/")}">{url}</a></li>')
    idx += ["</ul></body></html>"]
    (dest_path / "index.html").write_text("\n".join(idx), "utf-8")
    return jsonify({"ok": True, "count": count, "dest": str(dest_path)})


# ── Cache browser ─────────────────────────────────────────────────────────────
# http://127.0.0.1:7780/<hostname>/path
# Example: http://127.0.0.1:7780/www.himalaya-info.org/index.htm
#
# A separate Flask app on port 7780 so it can use a different URL scheme from
# the management dashboard.  All absolute URLs in served HTML/CSS are rewritten
# to /<hostname>/path so the browser resolves them back through this same app,
# regardless of the original domain.
browser_app = Flask("cache_browser")

def _ct_for(path: Path, sidecar: Path) -> str:
    if sidecar.exists():
        try:
            sc = json.loads(sidecar.read_text("utf-8"))
            return sc.get("content_type", "application/octet-stream")
        except Exception: pass
    ct, _ = mimetypes.guess_type(str(path))
    return ct or "application/octet-stream"


@browser_app.route("/")
def _browser_root():
    meta  = load_meta()
    hosts = sorted({urlparse(u).hostname or "" for u in meta.get("cached_urls", {})
                    if urlparse(u).hostname})
    html  = ["<!DOCTYPE html><html><head><meta charset=UTF-8>",
             "<style>body{font-family:sans-serif;padding:24px;max-width:800px;margin:auto}"
             "h1{margin-bottom:12px}li{margin:5px 0}a{color:#38bdf8;text-decoration:none}"
             "a:hover{text-decoration:underline}.mut{color:#6b7280;font-size:12px}</style>",
             "<title>LazyMirror Cache Browser</title></head><body>",
             "<h1>\U0001fa9e LazyMirror \u2014 Cached Sites</h1>",
             "<p class='mut'>Click a domain to browse its cached pages.</p><ul>"]
    for h in hosts:
        html.append(f'<li><a href="/{h}/">{h}</a></li>')
    html += ["</ul></body></html>"]
    return "\n".join(html), 200, {"Content-Type": "text/html; charset=utf-8"}


def _try_cache(host: str, subpath: str, query: str = ""):
    """Return (cache_path, sidecar, ct, trial_url) or None.

    query — raw query string (no leading '?'). When present, URLs cached
    under the `?q=…` form are matched via their stable __q<hash> filename.
    """
    from urllib.parse import unquote
    # Build candidate sub-paths to try.  Directories are stored as <path>/index.html
    # by url_to_path(), so a browser request for /foo/ must check /foo/index.html too.
    candidates = []
    if not subpath or subpath.endswith("/"):
        base = subpath.rstrip("/")
        if base:
            candidates += [base + "/index.html", base + "/index.htm"]
        else:
            candidates += ["index.html", "index.htm"]
    candidates.append(subpath)

    qsuffix = ("?" + query) if query else ""
    for scheme in ("https", "http"):
        for sp in candidates:
            sp_path = sp if sp else ""
            # Try both the encoded URL (as stored in _meta.json) and decoded
            for path_form in (sp_path, unquote(sp_path)):
                trial_url = f"{scheme}://{host}/{path_form}{qsuffix}"
                cp = url_to_path(trial_url)
                if cp.exists():
                    sc = cp.with_suffix(cp.suffix + ".meta.json")
                    ct = _ct_for(cp, sc)
                    return cp, sc, ct, trial_url
            # Also try without the query — files cached without one
            if qsuffix:
                trial_url = f"{scheme}://{host}/{sp_path}"
                cp = url_to_path(trial_url)
                if cp.exists():
                    sc = cp.with_suffix(cp.suffix + ".meta.json")
                    ct = _ct_for(cp, sc)
                    return cp, sc, ct, trial_url
    return None


def _browser_rewrite_html(raw: bytes, page_url: str, host: str, sidecar: Path) -> bytes:
    """
    Rewrite HTML for the cache browser:
    - absolute http(s)://any-host/path  ->  /any-host/path
    - root-relative /path               ->  /current-host/path
    - charset: decode with original charset, serve as utf-8 with updated meta
    """
    text = _smart_decode(raw, sidecar)

    # Absolute URLs in tag attributes -> /host/path
    def _abs_to_local(m):
        attr  = m.group(1)
        h     = m.group(2)
        path  = m.group(3) or "/"
        quote = m.group(4)
        return attr + "/" + h + path + quote

    text = re.sub(
        r'((?:src|href|action|data-src|data-lazy|data-original|poster|data-bg|data-background)'
        r'\s*=\s*["\'])https?://([^/"\'>\s]+)(/[^"\'>\s]*)?(["\'])',
        _abs_to_local, text, flags=re.IGNORECASE)

    # Root-relative /path -> /current-host/path
    def _root_to_local(m):
        attr  = m.group(1)
        rpath = m.group(2)
        quote = m.group(3)
        return attr + "/" + host + rpath + quote

    text = re.sub(
        r'((?:src|href|action|data-src|data-lazy|data-original|poster|data-bg|data-background)'
        r'\s*=\s*["\'])(/[^"\'>\s]+)(["\'])',
        _root_to_local, text, flags=re.IGNORECASE)

    # Absolute url() in style attributes / style blocks
    text = re.sub(
        r'url\(["\']?https?://([^/"\')\s]+)(/[^"\')\s]*)?["\']?\)',
        lambda m: 'url("/' + m.group(1) + (m.group(2) or "/") + '")',
        text, flags=re.IGNORECASE)

    # Root-relative url()
    text = re.sub(
        r'url\(["\']?(/[^"\')\s]+)["\']?\)',
        lambda m: 'url("/' + host + m.group(1) + '")',
        text, flags=re.IGNORECASE)

    # Update/add charset meta so browser knows it's utf-8
    text = re.sub(r'charset\s*=\s*[\w-]+', 'charset=utf-8', text, flags=re.IGNORECASE)

    return text.encode("utf-8")


def _browser_rewrite_css(raw: bytes, page_url: str, host: str, sidecar: Path) -> bytes:
    """Rewrite CSS url() and @import for cache browser."""
    text = _smart_decode(raw, sidecar)

    text = re.sub(
        r'url\(["\']?https?://([^/"\')\s]+)(/[^"\')\s]*)?["\']?\)',
        lambda m: 'url("/' + m.group(1) + (m.group(2) or "/") + '")',
        text, flags=re.IGNORECASE)

    text = re.sub(
        r'url\(["\']?(/[^"\')\s]+)["\']?\)',
        lambda m: 'url("/' + host + m.group(1) + '")',
        text, flags=re.IGNORECASE)

    text = re.sub(
        r'@import\s+["\']https?://([^/"\')\s]+)(/[^"\')\s]*)?["\']',
        lambda m: '@import "/' + m.group(1) + (m.group(2) or "/") + '"',
        text, flags=re.IGNORECASE)

    text = re.sub(
        r'@import\s+["\']?(/[^"\')\s]+)["\']?',
        lambda m: '@import "/' + host + m.group(1) + '"',
        text, flags=re.IGNORECASE)

    return text.encode("utf-8")


@browser_app.route("/<path:urlpath>")
def _browser_serve(urlpath):
    parts   = urlpath.strip("/").split("/", 1)
    host    = parts[0]
    subpath = parts[1] if len(parts) > 1 else ""
    query   = request.query_string.decode("utf-8", errors="replace")

    hit = _try_cache(host, subpath, query)
    if hit:
        cache_path, sc, ct, trial_url = hit
        raw = cache_path.read_bytes()
        if "html" in ct or "xhtml" in ct:
            raw = _browser_rewrite_html(raw, trial_url, host, sc)
            resp = Response(raw, mimetype="text/html; charset=utf-8")
        elif "css" in ct:
            raw = _browser_rewrite_css(raw, trial_url, host, sc)
            resp = Response(raw, mimetype="text/css; charset=utf-8")
        else:
            resp = Response(raw, mimetype=ct)
        resp.headers["X-LazyMirror"] = "cache-hit"
        return resp

    return _not_cached_page(host, subpath), 200, {"Content-Type": "text/html; charset=utf-8"}


def _not_cached_page(host: str, subpath: str) -> str:
    url   = f"https://{host}/{subpath}"
    meta  = load_meta()
    related = [u for u in meta.get("cached_urls", {})
               if urlparse(u).hostname == host][:30]
    links = "".join(
        f'<li><a href="/{urlparse(u).hostname}{urlparse(u).path}">{u}</a></li>'
        for u in sorted(related))
    return (
        f'<!DOCTYPE html><html><head><meta charset=UTF-8>'
        f'<style>body{{font-family:sans-serif;padding:32px;max-width:700px;margin:auto}}'
        f'.box{{background:#161920;border:1px solid #252830;border-radius:8px;padding:20px;margin:16px 0}}'
        f'h1{{color:#f97316}}a{{color:#38bdf8}}li{{margin:3px 0}}.mut{{color:#6b7280;font-size:12px}}</style>'
        f'<title>Not cached \u2014 LazyMirror</title></head><body>'
        f'<h1>Not in cache</h1>'
        f'<p>The URL <code>{url}</code> has not been cached yet.</p>'
        f'<p>Browse to <a href="{url}" target="_blank">{url}</a> with the proxy active to cache it.</p>'
        f'<div class="box"><b>Other cached pages on {host}:</b>'
        f'{"<ul>" + links + "</ul>" if related else "<p class=mut>None yet.</p>"}</div>'
        f'<p><a href="/{host}/">\u2190 {host} index</a> &nbsp;|&nbsp; <a href="/">\u2190 All sites</a></p>'
        f'</body></html>'
    )


def _run_browser_server():
    import logging as _log
    _log.getLogger("werkzeug").setLevel(_log.ERROR)
    browser_app.run(host="127.0.0.1", port=7780, debug=False, use_reloader=False)


threading.Thread(target=_run_browser_server, daemon=True).start()



@app.route("/view")
def view_cached():
    url = request.args.get("url", "")
    if not url: return abort(400)
    path = url_to_path(url)
    if not path.exists(): return abort(404, "Not in cache")
    sidecar = path.with_suffix(path.suffix + ".meta.json")
    ct = "application/octet-stream"
    if sidecar.exists():
        try: ct = json.loads(sidecar.read_text("utf-8")).get("content_type", ct)
        except Exception: pass
    return send_file(path, mimetype=ct)


def _refetch_watcher():
    """Background thread that consumes `_refetch/<hash>` signal files.

    Each signal file holds one URL the user asked to re-fetch. We import
    proxy_addon as a library and call refetch_url directly — that runs the
    fetch inside the dashboard process, which is fine because direct fetches
    bypass the proxy by design (we want the live network response, not a
    cache hit). The proxy process and dashboard process each have their own
    copy of the proxy_addon worker; they share state only through cache
    files, which is the intended IPC boundary.
    """
    rfd = CACHE_DIR / "_refetch"; rfd.mkdir(exist_ok=True)
    while True:
        _time_mod.sleep(2)
        try:
            import sys; sys.path.insert(0, str(Path(__file__).parent))
            from proxy_addon import refetch_url
            for f in list(rfd.iterdir()):
                try:
                    url = f.read_text("utf-8").strip()
                    if url: refetch_url(url)
                    f.unlink()
                except Exception: pass
        except Exception: pass

threading.Thread(target=_refetch_watcher, daemon=True).start()

if __name__ == "__main__":
    print(f"[Dashboard] Cache: {CACHE_DIR}")
    print("[Dashboard] Listening on http://127.0.0.1:7779")
    app.run(host="127.0.0.1", port=7779, debug=False, use_reloader=False)

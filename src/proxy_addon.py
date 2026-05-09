"""
LazyMirror proxy addon v8
─────────────────────────
Key fix in v8
─────────────
  The harvester now correctly separates "render assets" (things the
  browser needs to display the page: CSS, JS, images, fonts, media)
  from "page links" (<a href> to other HTML pages) and "linked files"
  (<a href> to downloadable non-HTML assets).

  Previously href was in the generic attribute list, causing every
  hyperlink on a page to be treated as an asset to fetch. Fixed.

Crawl depth semantics (unchanged from v7)
──────────────────────────────────────────
  depth=0  Fetch render-assets of the visited page only.
           Never follow any <a href> page links.
  depth=1  Also follow same-host <a href> page links once.
           Fetch render-assets of each linked page. Stop there.
  depth=N  Follow same-host page links N hops deep from the visited page.

Asset category controls (new in v8)
────────────────────────────────────
  Each category can be independently enabled in settings:
    fetch_images   — <img>, CSS background-image, <picture>
    fetch_css_js   — stylesheets and scripts
    fetch_fonts    — web fonts (@font-face)
    fetch_media    — <video>, <audio>, posters
    fetch_linked_files — <a href> targets that are downloadable files
                         (images, PDFs, archives, etc.)

  fetch_assets master switch: when False, all categories are skipped.
  cross_host_crawl: when True, page links to other domains are followed.
"""

import os, re, json, hashlib, logging, threading, time, queue as _queue_mod
import urllib.request, urllib.error, ssl, random
from pathlib import Path
from urllib.parse import urlparse, urljoin, urlunparse
from datetime import datetime, timezone

# ── Paths ─────────────────────────────────────────────────────────────────────
CACHE_DIR   = Path(os.environ.get("LAZYMIRROR_CACHE",
                   Path(__file__).parent.parent / "offline_cache"))
META_FILE   = CACHE_DIR / "_meta.json"
STATE_FILE  = CACHE_DIR / "_state.json"
FAILED_FILE = CACHE_DIR / "_failed.json"
QUEUE_FILE  = CACHE_DIR / "_queue.json"
LOG_FILE    = CACHE_DIR / "_proxy.log"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(filename=str(LOG_FILE), level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("lazymirror")

# ── Extension / MIME sets ─────────────────────────────────────────────────────
IMAGE_EXTS  = {".png",".jpg",".jpeg",".gif",".webp",".avif",".jxl",
               ".svg",".ico",".bmp",".tiff",".tif",".cur",".heif",".heic"}
FONT_EXTS   = {".woff",".woff2",".ttf",".otf",".eot"}
MEDIA_EXTS  = {".mp4",".webm",".ogv",".mp3",".ogg",".wav",".aac",".flac"}
SCRIPT_EXTS = {".js",".mjs",".cjs"}
STYLE_EXTS  = {".css"}
PAGE_EXTS   = {"",".html",".htm",".php",".asp",".aspx",".jsp",".cfm",".shtml"}
LINKED_FILE_EXTS = (
    IMAGE_EXTS | FONT_EXTS | MEDIA_EXTS |
    {".pdf",".zip",".gz",".tar",".7z",".rar",
     ".doc",".docx",".xls",".xlsx",".ppt",".pptx",
     ".json",".xml",".csv",".webmanifest",".txt"}
)
ALL_ASSET_EXTS = IMAGE_EXTS | FONT_EXTS | MEDIA_EXTS | SCRIPT_EXTS | STYLE_EXTS

IMAGE_MIMES = {"image/png","image/jpeg","image/gif","image/webp","image/avif",
               "image/svg+xml","image/x-icon","image/ico","image/vnd.microsoft.icon",
               "image/bmp","image/tiff","image/heif","image/jxl"}
FONT_MIMES  = {"font/woff","font/woff2","font/ttf","font/otf","font/eot",
               "application/font-woff","application/font-woff2",
               "application/vnd.ms-fontobject","application/x-font-ttf"}
MEDIA_MIMES = {"audio/mpeg","audio/ogg","audio/wav","audio/webm","audio/aac",
               "video/mp4","video/webm","video/ogg"}
CSS_MIMES   = {"text/css"}
JS_MIMES    = {"application/javascript","text/javascript","application/ecmascript"}
HTML_MIMES  = {"text/html","application/xhtml+xml","application/xml","text/xml"}
MISC_MIMES  = {"application/json","application/manifest+json",
               "application/x-web-app-manifest+json","application/pdf"}
ALL_CACHE_MIMES = (IMAGE_MIMES | FONT_MIMES | MEDIA_MIMES |
                   CSS_MIMES | JS_MIMES | HTML_MIMES | MISC_MIMES)

# ── SSL ───────────────────────────────────────────────────────────────────────
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode    = ssl.CERT_NONE

# ── Locks ─────────────────────────────────────────────────────────────────────
_inflight:     set = set()
_inflight_lock      = threading.Lock()
_meta_lock          = threading.Lock()
_failed_lock        = threading.Lock()
_state_lock         = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════════════
#  State
# ═══════════════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    defaults = {
        # Proxy mode
        "offline_mode":      False,
        "fetch_delay_ms":    0,
        "queue_paused":      False,
        "blocked_domains":   [],
        "queue_depth":       0,        # live size (written by worker)
        # Crawl
        "crawl_depth":       0,        # page-link hops (0=no page following)
        "cross_host_crawl":  False,    # follow off-domain page links
        # Asset categories (all default on)
        "fetch_images":      True,
        "fetch_css_js":      True,
        "fetch_fonts":       True,
        "fetch_media":       True,
        "fetch_linked_files":False,    # <a href> any downloadable file
        "fetch_linked_images":False,   # <a href> only image files (subset of above)
        "fetch_linked_html":  False,   # follow <a href> page links (depth controls how deep)
    }
    if STATE_FILE.exists():
        try:
            saved = json.loads(STATE_FILE.read_text("utf-8"))
            defaults.update(saved)
        except Exception:
            pass
    return defaults

def save_state(s: dict):
    with _state_lock:
        STATE_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False), "utf-8")

def _patch_state(**kw):
    """Update only specific keys in state — never overwrites unrelated keys."""
    with _state_lock:
        # Re-read inside lock to get freshest version, then merge
        s = load_state()
        s.update(kw)
        # Write directly (bypass save_state to avoid double-lock)
        STATE_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False), "utf-8")

# ── Meta ──────────────────────────────────────────────────────────────────────
def load_meta() -> dict:
    if META_FILE.exists():
        try: return json.loads(META_FILE.read_text("utf-8"))
        except Exception: pass
    return {"cached_urls": {}, "stats": {"total": 0, "bytes": 0}}

def save_meta(m: dict):
    META_FILE.write_text(json.dumps(m, indent=2, ensure_ascii=False), "utf-8")

def load_failed() -> dict:
    if FAILED_FILE.exists():
        try: return json.loads(FAILED_FILE.read_text("utf-8"))
        except Exception: pass
    return {}

def save_failed(f: dict):
    FAILED_FILE.write_text(json.dumps(f, indent=2, ensure_ascii=False), "utf-8")

def record_failure(url: str, reason: str):
    with _failed_lock:
        f = load_failed()
        f[url] = {"url": url, "reason": str(reason),
                  "failed_at": datetime.now(timezone.utc).isoformat()}
        save_failed(f)

def clear_failure(url: str):
    with _failed_lock:
        f = load_failed(); f.pop(url, None); save_failed(f)

# ── Filesystem ────────────────────────────────────────────────────────────────
def url_to_path(url: str) -> Path:
    """
    Convert a URL to a filesystem path under CACHE_DIR.

    Key design decision: percent-encoding is DECODED in the path.
    So 'Images%20Landkarten' becomes 'Images Landkarten' on disk.
    This ensures:
      1. file:// URLs work on Windows (browser decodes %20 before filesystem lookup)
      2. Relative hrefs like 'images/Images Landkarten/foo.jpg' resolve correctly
         in exported HTML without any rewriting needed
      3. Human-readable folder/file names in the cache

    Windows-illegal characters (<>:"|?*\) are replaced with _.
    The query string (if any) is folded into the filename as a short hash.
    """
    from urllib.parse import unquote
    p    = urlparse(url)
    # Decode %XX sequences → real characters, then strip illegal Windows chars
    raw  = unquote(p.path.lstrip("/") or "index")
    safe = re.sub(r'[<>:"|?*\\]', "_", raw)
    if "." not in Path(safe).name or safe.endswith("/"):
        safe = safe.rstrip("/") + "/index.html"
    if p.query:
        qh = hashlib.md5(p.query.encode()).hexdigest()[:8]
        base, ext = os.path.splitext(safe)
        safe = f"{base}__q{qh}{ext}"
    return CACHE_DIR / p.netloc / safe

def sidecar_path(p: Path) -> Path:
    return p.with_suffix(p.suffix + ".meta.json")

def is_cached(url: str) -> bool:
    return url_to_path(url).exists()

def should_cache_ct(ct: str) -> bool:
    ct = ct.split(";")[0].strip().lower()
    return (ct in ALL_CACHE_MIMES or
            ct.split("/")[0] in {"image","font","audio","video"})

def is_blocked(url: str) -> bool:
    try:
        host    = urlparse(url).hostname or ""
        blocked = load_state().get("blocked_domains", [])
        return any(host == b or host.endswith("." + b) for b in blocked)
    except Exception:
        return False

def is_same_host(url: str, origin: str) -> bool:
    try: return urlparse(url).hostname == urlparse(origin).hostname
    except Exception: return False

def is_page_url(url: str) -> bool:
    return Path(urlparse(url).path).suffix.lower() in PAGE_EXTS

def ext_of(url: str) -> str:
    return Path(urlparse(url).path).suffix.lower()

# ── Cache write ───────────────────────────────────────────────────────────────
def write_cache(url: str, status: int, ct_full: str, body: bytes):
    """
    ct_full = full Content-Type header value, e.g. 'text/html; charset=windows-1252'
    The body is stored as raw bytes exactly as received.
    """
    ct_mime = ct_full.split(";")[0].strip().lower()
    path = url_to_path(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    sidecar_path(path).write_text(json.dumps({
        "url": url, "content_type": ct_mime, "content_type_full": ct_full,
        "status": status, "size": len(body),
        "cached_at": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False), "utf-8")
    with _meta_lock:
        meta = load_meta()
        meta["cached_urls"][url] = {
            "path": str(path.relative_to(CACHE_DIR)),
            "content_type": ct_mime, "size": len(body),
            "cached_at": datetime.now(timezone.utc).isoformat(),
        }
        meta["stats"]["total"] = len(meta["cached_urls"])
        meta["stats"]["bytes"] = sum(v.get("size",0) for v in meta["cached_urls"].values())
        save_meta(meta)
    clear_failure(url)
    log.info("CACHED [%d] %s  (%s  %d B)", status, url, ct_mime, len(body))

# ── URL helpers ───────────────────────────────────────────────────────────────
_SKIP_STARTS = ("data:","blob:","javascript:","#","mailto:","tel:",
                "about:","chrome:","chrome-extension:")

# Safe characters that must NOT be re-encoded in URL path/query
_PATH_SAFE  = "/!$&'()*+,;=:@~"
_QUERY_SAFE = "=&+!$'()*,;:@/~"

def sanitize_url(url: str) -> str:
    """
    Ensure all non-ASCII characters in a URL are percent-encoded.
    Already-encoded sequences (e.g. %20) are preserved.
    This fixes URLs like .../KKH%2036°27'19...jpg where ° is raw unicode.
    """
    try:
        from urllib.parse import unquote, quote
        p = urlparse(url)
        fixed_path  = quote(unquote(p.path),  safe=_PATH_SAFE)
        fixed_query = quote(unquote(p.query), safe=_QUERY_SAFE) if p.query else ""
        fixed_frag  = quote(unquote(p.fragment), safe=_PATH_SAFE) if p.fragment else ""
        return urlunparse(p._replace(path=fixed_path, query=fixed_query, fragment=fixed_frag))
    except Exception:
        return url

def norm(raw: str, base: str):
    raw = raw.strip()
    if not raw: return None
    for s in _SKIP_STARTS:
        if raw.startswith(s): return None
    try:
        abs_url = urljoin(base, raw)
        p = urlparse(abs_url)
        if p.scheme not in ("http","https"): return None
        clean = urlunparse(p._replace(fragment=""))
        return sanitize_url(clean)
    except Exception:
        return None

def add_url(urls: set, raw: str, base: str):
    u = norm(raw, base)
    if u: urls.add(u)


# ── Charset-aware HTML decode ─────────────────────────────────────────────────
_META_CHARSET_RE = re.compile(
    r'<meta[^>]+charset\s*=\s*["\']?\s*([\w-]+)', re.I)
_META_CT_RE = re.compile(
    r'<meta[^>]+content\s*=\s*["\'][^"\']*charset=([\w-]+)', re.I)

def _detect_charset(raw: bytes, ct_header: str = "") -> str:
    """
    Detect the charset of an HTML document, in priority order:
    1. charset= in Content-Type header  (e.g. 'text/html; charset=windows-1252')
    2. <meta charset="...">
    3. <meta http-equiv Content-Type charset=...>
    4. BOM detection
    5. Fallback: latin-1 (which never fails and covers most western European pages)
    """
    # 1. From header
    if "charset=" in ct_header.lower():
        m = re.search(r'charset=([\w-]+)', ct_header, re.I)
        if m:
            return m.group(1).strip()

    # Peek at first 2 KB for meta tags (ASCII-safe)
    peek = raw[:2048]
    try:
        peek_str = peek.decode("ascii", errors="replace")
    except Exception:
        peek_str = ""

    # 2. <meta charset="...">
    m = _META_CHARSET_RE.search(peek_str)
    if m:
        return m.group(1).strip()

    # 3. <meta http-equiv content-type>
    m = _META_CT_RE.search(peek_str)
    if m:
        return m.group(1).strip()

    # 4. BOM
    if raw.startswith(b'\xff\xfe'): return 'utf-16-le'
    if raw.startswith(b'\xfe\xff'): return 'utf-16-be'
    if raw.startswith(b'\xef\xbb\xbf'): return 'utf-8-sig'

    # 5. Fallback: latin-1 (lossless for all byte values)
    return "latin-1"


def _decode_html(raw: bytes, ct_header: str = "") -> str:
    charset = _detect_charset(raw, ct_header)
    try:
        return raw.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return raw.decode("latin-1", errors="replace")


# ═══════════════════════════════════════════════════════════════════════════════
#  Harvesters  — strictly categorised, no cross-contamination
# ═══════════════════════════════════════════════════════════════════════════════

_CSS_URL    = re.compile(r"""url\(\s*["']?([^"')>\s]+)["']?\s*\)""", re.I)
_CSS_IMPORT = re.compile(r"""@import\s+(?:url\(\s*)?["']([^"']+)["']""", re.I)

def harvest_css(css_bytes: bytes, base_url: str) -> set:
    try: text = css_bytes.decode("utf-8", errors="replace")
    except Exception: return set()
    urls: set = set()
    for m in _CSS_URL.finditer(text):    add_url(urls, m.group(1), base_url)
    for m in _CSS_IMPORT.finditer(text): add_url(urls, m.group(1), base_url)
    return urls

# Regexes — NOTE: href is NOT in _EMBED_ATTRS; it's only in <link> and <a>
_SRCSET_PART  = re.compile(r'([^\s,][^\s,]*[^\s,]|[^\s,])(?:\s+[\d.]+[wx])?')

# Attributes that embed sub-resources (not hyperlinks)
_EMBED_ATTRS = (
    "src","data-src","data-href","data-lazy","data-original",
    "data-url","data-background","data-bg","data-img","data-image",
    "data-poster","data-lazyload","data-lazy-src","data-echo","poster",
)
_EMBED_RE = re.compile(
    r"\b(" + "|".join(re.escape(a) for a in sorted(_EMBED_ATTRS, key=len, reverse=True)) +
    r")\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", re.I)

_SRCSET_RE    = re.compile(r'\b(?:srcset|imagesrcset|data-srcset)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')', re.I)
_STYLE_ATTR   = re.compile(r'\bstyle\s*=\s*(?:"([^"]*)"|\'([^\']*)\')', re.I)
_STYLE_BLOCK  = re.compile(r'<style\b[^>]*>(.*?)</style>', re.I | re.DOTALL)
_SCRIPT_SRC   = re.compile(r"<script\b[^>]+?\bsrc\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", re.I)

_LINK_TAG     = re.compile(r'<link\b([^>]+?)/?>', re.I | re.DOTALL)
_LINK_REL_OK  = {"stylesheet","preload","prefetch","modulepreload",
                 "icon","shortcut icon","apple-touch-icon",
                 "apple-touch-icon-precomposed","manifest","alternate stylesheet"}

_META_TAG     = re.compile(r'<meta\b([^>]+?)/?>', re.I | re.DOTALL)
_META_IMG_P   = re.compile(r'(?:og:|twitter:)(?:image(?::(?:src|url))?|url)', re.I)

_MEDIA_TAG    = re.compile(r'<(?:video|audio|track|embed)\b([^>]+?)/?>', re.I | re.DOTALL)
_SOURCE_TAG   = re.compile(r'<source\b([^>]+?)/?>', re.I | re.DOTALL)
_OBJECT_TAG   = re.compile(r'<object\b([^>]+?)/?>', re.I | re.DOTALL)
_INPUT_IMG    = re.compile(r"<input\b[^>]+?type\s*=\s*[\"']image[\"'][^>]+?src\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", re.I)
_ANCHOR_TAG   = re.compile(r'<a\b[^>]+?\bhref\s*=\s*(?:"([^"]*)"|\'([^\']*)\')', re.I)
_IFRAME_TAG   = re.compile(r'<iframe\b[^>]+?\bsrc\s*=\s*(?:"([^"]*)"|\'([^\']*)\')', re.I)

def _ga(attrs: str, name: str):
    m = re.search(rf'\b{re.escape(name)}\s*=\s*(?:"([^"]*)"|\'([^\']*)\')', attrs, re.I)
    return (m.group(1) or m.group(2)) if m else None

def _pss(val: str, base: str, urls: set):
    for m in _SRCSET_PART.finditer(val):
        add_url(urls, m.group(1).strip(","), base)


class HarvestResult:
    """Strictly categorised harvest output."""
    __slots__ = ("images","css","js","fonts","media","pages","linked_files","other")
    def __init__(self):
        self.images:       set = set()  # img src, bg-images, <picture>
        self.css:          set = set()  # stylesheets
        self.js:           set = set()  # scripts
        self.fonts:        set = set()  # web fonts
        self.media:        set = set()  # video/audio
        self.pages:        set = set()  # <a href> → same/cross-host HTML pages
        self.linked_files: set = set()  # <a href> → downloadable files
        self.other:        set = set()  # manifest, favicon, misc

    def render_assets(self) -> set:
        """Everything needed to *display* the page (no page links, no linked files)."""
        return self.images | self.css | self.js | self.fonts | self.media | self.other


def _cat_url(result: HarvestResult, url: str):
    """Categorise a URL by extension into the appropriate bucket."""
    e = ext_of(url)
    if e in IMAGE_EXTS:  result.images.add(url)
    elif e in FONT_EXTS: result.fonts.add(url)
    elif e in MEDIA_EXTS:result.media.add(url)
    elif e in SCRIPT_EXTS: result.js.add(url)
    elif e in STYLE_EXTS:  result.css.add(url)
    else:                  result.other.add(url)


def harvest_html(html_bytes: bytes, base_url: str, ct_header: str = "") -> HarvestResult:
    result = HarvestResult()
    try:
        text = _decode_html(html_bytes, ct_header)
    except Exception:
        return result

    # ── 1. Generic embed attributes (src, data-src, poster, etc.) ────────────
    for m in _EMBED_RE.finditer(text):
        u = norm(m.group(2) or m.group(3) or "", base_url)
        if u: _cat_url(result, u)

    # ── 2. srcset / data-srcset ───────────────────────────────────────────────
    for m in _SRCSET_RE.finditer(text):
        tmp: set = set()
        _pss(m.group(1) or m.group(2) or "", base_url, tmp)
        for u in tmp: result.images.add(u)

    # ── 3. <script src> ───────────────────────────────────────────────────────
    for m in _SCRIPT_SRC.finditer(text):
        u = norm(m.group(1) or m.group(2), base_url)
        if u: result.js.add(u)

    # ── 4. <link> — stylesheets, preload, icon, manifest ─────────────────────
    for m in _LINK_TAG.finditer(text):
        attrs = m.group(1)
        rel   = (_ga(attrs,"rel") or "").lower()
        if rel and not any(r in rel for r in _LINK_REL_OK):
            continue
        href = _ga(attrs, "href")
        if not href: continue
        u = norm(href, base_url)
        if not u: continue
        e = ext_of(u)
        if e in STYLE_EXTS:           result.css.add(u)
        elif e in IMAGE_EXTS:         result.images.add(u)
        elif e in FONT_EXTS:          result.fonts.add(u)
        elif "icon" in rel:           result.other.add(u)   # favicon
        elif "manifest" in rel:       result.other.add(u)
        else:                         result.other.add(u)

        isrc = _ga(attrs, "imagesrcset")
        if isrc:
            tmp: set = set()
            _pss(isrc, base_url, tmp)
            result.images |= tmp

    # ── 5. Inline <style> blocks ──────────────────────────────────────────────
    for m in _STYLE_BLOCK.finditer(text):
        for u in harvest_css(m.group(1).encode(), base_url):
            _cat_url(result, u)

    # ── 6. style="..." attributes ─────────────────────────────────────────────
    for m in _STYLE_ATTR.finditer(text):
        for u in harvest_css((m.group(1) or m.group(2) or "").encode(), base_url):
            _cat_url(result, u)

    # ── 7. Open Graph / Twitter card images ───────────────────────────────────
    for m in _META_TAG.finditer(text):
        attrs = m.group(1)
        prop  = _ga(attrs,"property") or _ga(attrs,"name") or ""
        if _META_IMG_P.search(prop):
            c = _ga(attrs,"content")
            if c:
                u = norm(c, base_url)
                if u: result.images.add(u)

    # ── 8. <video>, <audio>, <track>, <embed> ─────────────────────────────────
    for m in _MEDIA_TAG.finditer(text):
        attrs = m.group(1)
        src = _ga(attrs,"src")
        if src:
            u = norm(src, base_url)
            if u: result.media.add(u)
        poster = _ga(attrs,"poster")
        if poster:
            u = norm(poster, base_url)
            if u: result.images.add(u)  # poster is an image

    # ── 9. <source srcset/src> (inside <picture>, <video>, <audio>) ──────────
    for m in _SOURCE_TAG.finditer(text):
        attrs = m.group(1)
        src = _ga(attrs,"src")
        if src:
            u = norm(src, base_url)
            if u: _cat_url(result, u)
        ss = _ga(attrs,"srcset")
        if ss:
            tmp: set = set()
            _pss(ss, base_url, tmp)
            result.images |= tmp

    # ── 10. <object data> ─────────────────────────────────────────────────────
    for m in _OBJECT_TAG.finditer(text):
        v = _ga(m.group(1),"data")
        if v:
            u = norm(v, base_url)
            if u: result.other.add(u)

    # ── 11. <input type="image"> ──────────────────────────────────────────────
    for m in _INPUT_IMG.finditer(text):
        u = norm(m.group(1) or m.group(2), base_url)
        if u: result.images.add(u)

    # ── 12. <iframe src> ──────────────────────────────────────────────────────
    for m in _IFRAME_TAG.finditer(text):
        u = norm(m.group(1) or m.group(2), base_url)
        if u: result.other.add(u)

    # ── 13. Implicit /favicon.ico ─────────────────────────────────────────────
    p = urlparse(base_url)
    result.other.add(urlunparse(p._replace(path="/favicon.ico", query="", fragment="")))

    # ── 14. <a href> — STRICTLY separated from render assets ─────────────────
    #   Only add to pages or linked_files; NEVER to render asset sets.
    for m in _ANCHOR_TAG.finditer(text):
        raw = m.group(1) or m.group(2) or ""
        u   = norm(raw, base_url)
        if u is None: continue
        e = ext_of(u)
        if e in LINKED_FILE_EXTS:
            result.linked_files.add(u)          # downloadable file
        elif is_page_url(u):
            result.pages.add(u)                 # HTML page link
        # anything else (anchors to unknown types) — ignore

    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  Asset filter — apply per-category settings
# ═══════════════════════════════════════════════════════════════════════════════

def filter_assets(result: HarvestResult, state: dict) -> set:
    """
    Build the final set of asset URLs to fetch, filtered by category toggles.
    Never includes page links — those are handled separately.
    """
    urls: set = set()
    if state.get("fetch_images",  True):  urls |= result.images
    if state.get("fetch_css_js",  True):  urls |= result.css | result.js
    if state.get("fetch_fonts",   True):  urls |= result.fonts
    if state.get("fetch_media",   True):  urls |= result.media
    urls |= result.other   # favicons, manifests — always (tiny)

    # Linked files via <a href>
    if state.get("fetch_linked_files", False):
        urls |= result.linked_files
    elif state.get("fetch_linked_images", False):
        # Only linked files that are image formats
        urls |= {u for u in result.linked_files if ext_of(u) in IMAGE_EXTS}

    return urls


# ═══════════════════════════════════════════════════════════════════════════════
#  Inspectable queue
# ═══════════════════════════════════════════════════════════════════════════════

class InspectableQueue:
    def __init__(self):
        self._items: list = []
        self._cv = threading.Condition(threading.Lock())

    def put(self, item):
        with self._cv:
            self._items.append(item)
            self._cv.notify()

    def get(self, timeout=1.0):
        with self._cv:
            dl = time.monotonic() + timeout
            while not self._items:
                rem = dl - time.monotonic()
                if rem <= 0: raise _queue_mod.Empty()
                self._cv.wait(timeout=rem)
            return self._items.pop(0)

    def snapshot(self) -> list:
        with self._cv: return list(self._items)

    def qsize(self) -> int:
        with self._cv: return len(self._items)

    def remove_url(self, url: str) -> int:
        with self._cv:
            before = len(self._items)
            self._items = [i for i in self._items if i[0] != url]
            return before - len(self._items)

    def remove_urls(self, urls: set) -> int:
        with self._cv:
            before = len(self._items)
            self._items = [i for i in self._items if i[0] not in urls]
            return before - len(self._items)

    def clear(self) -> int:
        with self._cv:
            n = len(self._items); self._items.clear(); return n


_fetch_queue    = InspectableQueue()
_worker_started = False
_worker_lock    = threading.Lock()
_pause_event    = threading.Event()
_pause_event.set()

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0.0.0 Safari/537.36")


def _write_queue_file():
    try:
        items = _fetch_queue.snapshot()
        QUEUE_FILE.write_text(json.dumps(
            [{"url":u,"referer":r,"force":f,"depth":d,"origin_host":oh}
             for u,r,f,d,oh in items],
            ensure_ascii=False), "utf-8")
    except Exception:
        pass


def set_paused(paused: bool):
    if paused: _pause_event.clear()
    else:      _pause_event.set()
    _patch_state(queue_paused=paused)


def _ensure_worker():
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=_worker_loop, daemon=True).start()
            _worker_started = True


def _worker_loop():
    while True:
        # Sync pause state from file directly — don't rely on browser requests
        _sync_pause_from_state()

        if not _pause_event.is_set():
            # Paused — sleep briefly and re-check rather than blocking forever
            time.sleep(0.5)
            continue

        try:
            item = _fetch_queue.get(timeout=1)
        except _queue_mod.Empty:
            _patch_state(queue_depth=_fetch_queue.qsize())
            _write_queue_file()
            continue

        if item is None:
            break

        url, referer, force, depth, origin_host = item
        try:
            _do_fetch(url, referer, force, depth, origin_host)
        except Exception as e:
            log.debug("Worker error %s: %s", url, e)
        finally:
            _patch_state(queue_depth=_fetch_queue.qsize())
            _write_queue_file()

        delay_ms = load_state().get("fetch_delay_ms", 0) or 0
        if delay_ms > 0:
            jitter = delay_ms * 0.2 * (random.random() * 2 - 1)
            time.sleep(max(0, (delay_ms + jitter) / 1000.0))


def _sync_pause_from_state():
    """Read pause flag from state file and sync to the threading event."""
    try:
        paused = load_state().get("queue_paused", False)
        if paused and _pause_event.is_set():
            _pause_event.clear()
        elif not paused and not _pause_event.is_set():
            _pause_event.set()
    except Exception:
        pass


def _do_fetch(url: str, referer: str, force: bool, depth: int, origin_host: str):
    """Fetch URL, cache it, recurse into it if it's HTML or CSS."""
    # Ensure URL is properly encoded (catches non-ASCII chars like ° in filenames)
    url = sanitize_url(url)
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent":      _UA,
            "Referer":         referer,
            "Accept":          "image/avif,image/webp,image/apng,image/*,text/css,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",
        })
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=25) as resp:
            body      = resp.read()
            ct_full   = resp.headers.get("Content-Type","application/octet-stream")
            ct        = ct_full.split(";")[0].strip().lower()
            if not body: return

            write_cache(url, resp.status, ct_full, body)

            if "html" in ct or "xhtml" in ct:
                _process_page(body, url, depth, origin_host, ct_full)
            elif "css" in ct:
                # CSS: harvest fonts/images referenced inside it
                for u in harvest_css(body, url):
                    _schedule_one(u, referer=url, depth=0,
                                  origin_host=origin_host, force=force)

    except urllib.error.HTTPError as e:
        record_failure(url, f"HTTP {e.code}")
        log.debug("HTTP %d  %s", e.code, url)
    except Exception as e:
        record_failure(url, str(e))
        log.debug("Fetch error %s: %s", url, e)
    finally:
        with _inflight_lock:
            _inflight.discard(url)


def _process_page(body: bytes, page_url: str, page_hops: int, origin_host: str,
                  ct_header: str = ""):
    """
    Harvest a page's assets and optionally follow its page links.
    page_hops = remaining levels of page-link following allowed.
    ct_header = full Content-Type header for charset detection.
    fetch_linked_html must be True for page links to be followed at all.
    """
    state  = load_state()
    result = harvest_html(body, page_url, ct_header)
    assets = filter_assets(result, state)

    # Schedule render assets (never counted as page hops)
    for u in assets:
        _schedule_one(u, referer=page_url, depth=0,
                      origin_host=origin_host, force=False)
    if assets:
        log.info("Harvested %d assets from %s", len(assets), page_url)

    # Follow page links only if:
    #  1. fetch_linked_html is explicitly enabled, AND
    #  2. page hops remain
    if not state.get("fetch_linked_html", False):
        return
    if page_hops <= 0:
        return

    if state.get("cross_host_crawl"):
        target_pages = result.pages
    else:
        target_pages = {p for p in result.pages
                        if urlparse(p).hostname == origin_host}

    for u in target_pages:
        _schedule_one(u, referer=page_url, depth=page_hops - 1,
                      origin_host=origin_host, force=False)
    if target_pages:
        log.info("Queued %d page links from %s (hops_left=%d)",
                 len(target_pages), page_url, page_hops)


def _schedule_one(url: str, referer: str, depth: int,
                  origin_host: str, force: bool = False):
    if is_blocked(url): return
    with _inflight_lock:
        if not force and (url in _inflight or is_cached(url)):
            return
        _inflight.add(url)
    _ensure_worker()
    _fetch_queue.put((url, referer, force, depth, origin_host))


def _schedule_batch(urls: set, referer: str, depth: int,
                    origin_host: str, force: bool = False):
    to_add = []
    with _inflight_lock:
        for url in urls:
            if is_blocked(url): continue
            if not force and (url in _inflight or is_cached(url)): continue
            _inflight.add(url)
            to_add.append(url)
    _ensure_worker()
    for url in to_add:
        _fetch_queue.put((url, referer, force, depth, origin_host))
    if to_add:
        _patch_state(queue_depth=_fetch_queue.qsize())
        _write_queue_file()
    return len(to_add)


# ── Public API (called from dashboard) ───────────────────────────────────────

def refetch_url(url: str):
    with _inflight_lock:
        if url in _inflight: return
        _inflight.add(url)
    _ensure_worker()
    hops = load_state().get("crawl_depth", 0)
    host = urlparse(url).hostname or ""
    _fetch_queue.put((url, url, True, hops, host))
    _patch_state(queue_depth=_fetch_queue.qsize())
    _write_queue_file()

def remove_from_queue(url: str):
    _fetch_queue.remove_url(url)
    with _inflight_lock: _inflight.discard(url)
    _patch_state(queue_depth=_fetch_queue.qsize())
    _write_queue_file()

def remove_urls_from_queue(urls: set):
    removed = _fetch_queue.remove_urls(urls)
    with _inflight_lock:
        for u in urls: _inflight.discard(u)
    _patch_state(queue_depth=_fetch_queue.qsize())
    _write_queue_file()
    return removed

def clear_queue():
    n = _fetch_queue.clear()
    with _inflight_lock: _inflight.clear()
    _patch_state(queue_depth=0)
    _write_queue_file()
    return n

def get_queue_snapshot() -> list:
    return [{"url":u,"referer":r,"force":f,"depth":d,"origin_host":oh}
            for u,r,f,d,oh in _fetch_queue.snapshot()]


# ═══════════════════════════════════════════════════════════════════════════════
#  mitmproxy addon
# ═══════════════════════════════════════════════════════════════════════════════

class LazyMirrorAddon:

    def request(self, flow):
        """mitmproxy request hook — runs once per outgoing browser request.

        Three responsibilities, in order:
          1. Mirror the dashboard's `queue_paused` flag onto the worker's
             threading.Event (the worker also self-syncs, but doing it here
             makes pause/resume feel instant under traffic).
          2. Drain dashboard→proxy signal directories (`_queue_remove`,
             `_queue_clear`). We piggyback on request flow because the proxy
             has no other tick.
          3. In offline_mode, short-circuit cached URLs by setting
             `flow.response` directly so mitmproxy never goes to network.
        """
        state = load_state()

        if state.get("queue_paused") and _pause_event.is_set():
            _pause_event.clear()
        elif not state.get("queue_paused") and not _pause_event.is_set():
            _pause_event.set()

        rm_dir = CACHE_DIR / "_queue_remove"
        if rm_dir.exists():
            for f in list(rm_dir.iterdir()):
                try:
                    content = f.read_text("utf-8").strip()
                    # May be JSON list or single URL
                    try:
                        urls = set(json.loads(content))
                        remove_urls_from_queue(urls)
                    except Exception:
                        remove_from_queue(content)
                    f.unlink()
                except Exception:
                    pass

        # Process queue-clear signal
        clr = CACHE_DIR / "_queue_clear"
        if clr.exists():
            try: clear_queue(); clr.unlink()
            except Exception: pass

        if not state.get("offline_mode"): return

        url = flow.request.pretty_url
        if not is_cached(url): return

        path = url_to_path(url)
        body = path.read_bytes()
        ct, status = "application/octet-stream", 200
        sc = sidecar_path(path)
        if sc.exists():
            try:
                md = json.loads(sc.read_text("utf-8"))
                ct = md.get("content_type", ct); status = md.get("status", 200)
            except Exception: pass
        from mitmproxy import http
        flow.response = http.Response.make(
            status, body,
            {"content-type": ct, "x-lazymirror": "cache-hit", "cache-control": "no-store"})
        log.info("CACHE HIT %s", url)

    def response(self, flow):
        """mitmproxy response hook — fires after the upstream server replies.

        Two responsibilities:
          1. Cache the body if the Content-Type is one we recognise.
          2. If it's HTML or CSS, harvest sub-resources and queue them for
             background fetching (subject to the per-category toggles in
             state.json).

        Skips flows we just served from cache (the "x-lazymirror: cache-hit"
        marker we set in request()), so we don't loop on our own responses.
        """
        if flow.response is None: return
        if flow.response.headers.get("x-lazymirror") == "cache-hit": return

        url = flow.request.pretty_url
        if is_blocked(url): return

        body    = flow.response.content
        if not body: return

        ct_full = flow.response.headers.get("content-type","application/octet-stream")
        ct      = ct_full.split(";")[0].strip().lower()

        try:
            if should_cache_ct(ct):
                write_cache(url, flow.response.status_code, ct_full, body)
        except Exception as e:
            log.warning("Cache write failed %s: %s", url, e)

        try:
            state       = load_state()
            crawl_depth = state.get("crawl_depth", 0)
            origin_host = urlparse(url).hostname or ""

            if "html" in ct or "xhtml" in ct:
                _process_page(body, url, crawl_depth, origin_host, ct_full)
            elif "css" in ct:
                if state.get("fetch_css_js", True):
                    for u in harvest_css(body, url):
                        _schedule_one(u, referer=url, depth=0,
                                      origin_host=origin_host)
                    _patch_state(queue_depth=_fetch_queue.qsize())
                    _write_queue_file()
        except Exception as e:
            log.warning("Harvest error %s: %s", url, e)


addons = [LazyMirrorAddon()]

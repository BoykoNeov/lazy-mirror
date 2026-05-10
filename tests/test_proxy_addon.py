"""
Tests for src/proxy_addon.py — pure functions and I/O helpers.

Each test gets a fresh temp directory via the _tmp_cache fixture so that
file-based state (meta.json, state.json, etc.) never leaks between tests.
"""
import json
import pytest
from pathlib import Path

import proxy_addon as pa


# ── Per-test isolation ────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _tmp_cache(tmp_path, monkeypatch):
    """Redirect all path globals and clear in-process caches before each test."""
    monkeypatch.setattr(pa, "CACHE_DIR",   tmp_path)
    monkeypatch.setattr(pa, "META_FILE",   tmp_path / "_meta.json")
    monkeypatch.setattr(pa, "STATE_FILE",  tmp_path / "_state.json")
    monkeypatch.setattr(pa, "FAILED_FILE", tmp_path / "_failed.json")
    monkeypatch.setattr(pa, "QUEUE_FILE",  tmp_path / "_queue.json")
    monkeypatch.setattr(pa, "_state_cache",    {})
    monkeypatch.setattr(pa, "_state_cache_ts", 0.0)


# ═══════════════════════════════════════════════════════════════════════════════
#  sanitize_url
# ═══════════════════════════════════════════════════════════════════════════════

def test_sanitize_url_ascii_passthrough():
    url = "https://example.com/path/file.html?q=1"
    assert pa.sanitize_url(url) == url

def test_sanitize_url_non_ascii_encoded():
    url = "https://example.com/path/45°N.jpg"  # ° is non-ASCII
    result = pa.sanitize_url(url)
    assert "°" not in result
    assert "45" in result

def test_sanitize_url_preserves_existing_percent():
    url = "https://example.com/Images%20Landkarten/file.jpg"
    result = pa.sanitize_url(url)
    assert "%20" in result

def test_sanitize_url_preserves_fragment():
    url = "https://example.com/page.html#section"
    result = pa.sanitize_url(url)
    assert "#section" in result


# ═══════════════════════════════════════════════════════════════════════════════
#  norm
# ═══════════════════════════════════════════════════════════════════════════════

BASE = "https://example.com/page.html"

def test_norm_empty_returns_none():
    assert pa.norm("", BASE) is None

def test_norm_whitespace_returns_none():
    assert pa.norm("   ", BASE) is None

def test_norm_data_url_skipped():
    assert pa.norm("data:image/png;base64,abc", BASE) is None

def test_norm_javascript_skipped():
    assert pa.norm("javascript:void(0)", BASE) is None

def test_norm_blob_skipped():
    assert pa.norm("blob:https://example.com/x", BASE) is None

def test_norm_hash_only_skipped():
    assert pa.norm("#section", BASE) is None

def test_norm_mailto_skipped():
    assert pa.norm("mailto:user@example.com", BASE) is None

def test_norm_absolute_https_passthrough():
    url = "https://cdn.example.com/style.css"
    assert pa.norm(url, BASE) == url

def test_norm_relative_resolved():
    result = pa.norm("../images/logo.png", "https://example.com/sub/page.html")
    assert result == "https://example.com/images/logo.png"

def test_norm_root_relative_resolved():
    result = pa.norm("/static/app.js", BASE)
    assert result == "https://example.com/static/app.js"

def test_norm_fragment_stripped():
    result = pa.norm("https://example.com/page.html#section", BASE)
    assert result == "https://example.com/page.html"

def test_norm_ftp_scheme_returns_none():
    assert pa.norm("ftp://example.com/file.txt", BASE) is None


# ═══════════════════════════════════════════════════════════════════════════════
#  url_to_path
# ═══════════════════════════════════════════════════════════════════════════════

def test_url_to_path_percent_decoded(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "CACHE_DIR", tmp_path)
    p = pa.url_to_path("https://example.com/Images%20Landkarten/file.jpg")
    assert p.parent.name == "Images Landkarten"
    assert p.name == "file.jpg"

def test_url_to_path_domain_first_component(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "CACHE_DIR", tmp_path)
    p = pa.url_to_path("https://cdn.example.com/style.css")
    assert "cdn.example.com" in str(p)

def test_url_to_path_no_extension_becomes_index_html(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "CACHE_DIR", tmp_path)
    p = pa.url_to_path("https://example.com/section/")
    assert p.name == "index.html"

def test_url_to_path_bare_path_becomes_index_html(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "CACHE_DIR", tmp_path)
    p = pa.url_to_path("https://example.com/")
    assert p.name == "index.html"

def test_url_to_path_query_makes_unique_name(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "CACHE_DIR", tmp_path)
    p1 = pa.url_to_path("https://example.com/page.html?v=1")
    p2 = pa.url_to_path("https://example.com/page.html?v=2")
    assert p1 != p2

def test_url_to_path_same_query_same_name(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "CACHE_DIR", tmp_path)
    p1 = pa.url_to_path("https://example.com/page.html?v=1")
    p2 = pa.url_to_path("https://example.com/page.html?v=1")
    assert p1 == p2

def test_url_to_path_illegal_chars_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "CACHE_DIR", tmp_path)
    p = pa.url_to_path("https://example.com/path<>|file.html")
    assert "<" not in str(p)
    assert ">" not in str(p)
    assert "|" not in str(p)

def test_url_to_path_long_path_hashed(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "CACHE_DIR", tmp_path)
    # Build a URL whose path would exceed 240 chars
    long_segment = "a" * 30
    url = "https://example.com/" + "/".join([long_segment] * 10) + "/file.html"
    p = pa.url_to_path(url)
    assert len(str(p)) <= 240 + len(str(tmp_path))
    assert "_long" in str(p)

def test_url_to_path_long_path_preserves_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "CACHE_DIR", tmp_path)
    long_segment = "a" * 30
    url = "https://example.com/" + "/".join([long_segment] * 10) + "/style.css"
    p = pa.url_to_path(url)
    assert p.suffix == ".css"


# ═══════════════════════════════════════════════════════════════════════════════
#  is_page_url / ext_of / should_cache_ct
# ═══════════════════════════════════════════════════════════════════════════════

def test_is_page_url_html():
    assert pa.is_page_url("https://example.com/index.html") is True

def test_is_page_url_php():
    assert pa.is_page_url("https://example.com/page.php") is True

def test_is_page_url_trailing_slash():
    assert pa.is_page_url("https://example.com/section/") is True

def test_is_page_url_no_ext():
    assert pa.is_page_url("https://example.com/about") is True

def test_is_page_url_jpg():
    assert pa.is_page_url("https://example.com/photo.jpg") is False

def test_is_page_url_pdf():
    assert pa.is_page_url("https://example.com/doc.pdf") is False

def test_is_page_url_js():
    assert pa.is_page_url("https://example.com/app.js") is False

def test_should_cache_ct_html():
    assert pa.should_cache_ct("text/html") is True

def test_should_cache_ct_with_charset():
    assert pa.should_cache_ct("text/html; charset=utf-8") is True

def test_should_cache_ct_png():
    assert pa.should_cache_ct("image/png") is True

def test_should_cache_ct_vendor_image():
    # image/* prefix should always be cached, even uncommon vendor subtypes
    assert pa.should_cache_ct("image/x-weird-format") is True

def test_should_cache_ct_css():
    assert pa.should_cache_ct("text/css") is True

def test_should_cache_ct_javascript():
    assert pa.should_cache_ct("application/javascript") is True

def test_should_cache_ct_font():
    assert pa.should_cache_ct("font/woff2") is True

def test_should_cache_ct_octet_stream_not_cached():
    # binary blobs with no recognised type are not cached
    assert pa.should_cache_ct("application/octet-stream") is False


# ═══════════════════════════════════════════════════════════════════════════════
#  _detect_charset
# ═══════════════════════════════════════════════════════════════════════════════

def test_detect_charset_from_header():
    raw = b"<html></html>"
    assert pa._detect_charset(raw, "text/html; charset=windows-1252") == "windows-1252"

def test_detect_charset_header_wins_over_meta():
    raw = b"<meta charset=ISO-8859-1>"
    assert pa._detect_charset(raw, "text/html; charset=utf-8") == "utf-8"

def test_detect_charset_meta_charset():
    raw = b"<html><head><meta charset=ISO-8859-1></head></html>"
    assert pa._detect_charset(raw, "") == "ISO-8859-1"

def test_detect_charset_meta_content_type():
    raw = b'<meta http-equiv="Content-Type" content="text/html; charset=windows-1251">'
    assert pa._detect_charset(raw, "") == "windows-1251"

def test_detect_charset_bom_utf16le():
    raw = b"\xff\xfe" + b"\x00" * 10
    assert pa._detect_charset(raw, "") == "utf-16-le"

def test_detect_charset_bom_utf16be():
    raw = b"\xfe\xff" + b"\x00" * 10
    assert pa._detect_charset(raw, "") == "utf-16-be"

def test_detect_charset_bom_utf8sig():
    raw = b"\xef\xbb\xbf<html></html>"
    assert pa._detect_charset(raw, "") == "utf-8-sig"

def test_detect_charset_fallback_latin1():
    raw = b"<html></html>"
    assert pa._detect_charset(raw, "") == "latin-1"


# ═══════════════════════════════════════════════════════════════════════════════
#  harvest_css
# ═══════════════════════════════════════════════════════════════════════════════

CSS_BASE = "https://example.com/css/style.css"

def test_harvest_css_absolute_url():
    css = b'body { background: url("https://example.com/bg.png"); }'
    urls = pa.harvest_css(css, CSS_BASE)
    assert "https://example.com/bg.png" in urls

def test_harvest_css_relative_url():
    css = b"body { background: url('../images/bg.png'); }"
    urls = pa.harvest_css(css, CSS_BASE)
    assert "https://example.com/images/bg.png" in urls

def test_harvest_css_import_quoted():
    css = b'@import "https://fonts.googleapis.com/css2?family=Roboto";'
    urls = pa.harvest_css(css, CSS_BASE)
    assert "https://fonts.googleapis.com/css2?family=Roboto" in urls

def test_harvest_css_import_url():
    css = b"@import url('https://example.com/base.css');"
    urls = pa.harvest_css(css, CSS_BASE)
    assert "https://example.com/base.css" in urls

def test_harvest_css_data_url_skipped():
    css = b'body { background: url("data:image/png;base64,abc"); }'
    urls = pa.harvest_css(css, CSS_BASE)
    assert not any(u.startswith("data:") for u in urls)

def test_harvest_css_multiple_urls():
    css = (
        b'body { background: url("https://example.com/a.png"); }'
        b'.cls { background: url("https://example.com/b.png"); }'
    )
    urls = pa.harvest_css(css, CSS_BASE)
    assert "https://example.com/a.png" in urls
    assert "https://example.com/b.png" in urls


# ═══════════════════════════════════════════════════════════════════════════════
#  harvest_html
# ═══════════════════════════════════════════════════════════════════════════════

HTML_BASE = "https://example.com/page.html"

def _harvest(html: str) -> pa.HarvestResult:
    return pa.harvest_html(html.encode("utf-8"), HTML_BASE, "text/html; charset=utf-8")

def test_harvest_html_img_src():
    r = _harvest('<img src="https://example.com/logo.png">')
    assert "https://example.com/logo.png" in r.images

def test_harvest_html_img_relative():
    r = _harvest('<img src="images/logo.png">')
    assert "https://example.com/images/logo.png" in r.images

def test_harvest_html_script_src():
    r = _harvest('<script src="https://example.com/app.js"></script>')
    assert "https://example.com/app.js" in r.js

def test_harvest_html_link_stylesheet():
    r = _harvest('<link rel="stylesheet" href="https://example.com/style.css">')
    assert "https://example.com/style.css" in r.css

def test_harvest_html_link_icon():
    r = _harvest('<link rel="icon" href="https://example.com/favicon.ico">')
    assert "https://example.com/favicon.ico" in r.other

def test_harvest_html_anchor_page_in_pages():
    r = _harvest('<a href="https://example.com/about.html">About</a>')
    assert "https://example.com/about.html" in r.pages

def test_harvest_html_anchor_NOT_in_render_assets():
    """Critical regression test: <a href> to a page must never be in render_assets().
    This was the root cause of the 8000+ queue items bug when href was in _EMBED_RE."""
    r = _harvest('<a href="https://example.com/about.html">About</a>')
    assert "https://example.com/about.html" not in r.render_assets()

def test_harvest_html_anchor_pdf_in_linked_files():
    r = _harvest('<a href="https://example.com/doc.pdf">PDF</a>')
    assert "https://example.com/doc.pdf" in r.linked_files

def test_harvest_html_anchor_image_in_linked_files():
    r = _harvest('<a href="https://example.com/photo.jpg">Photo</a>')
    assert "https://example.com/photo.jpg" in r.linked_files

def test_harvest_html_anchor_pdf_NOT_in_pages():
    r = _harvest('<a href="https://example.com/doc.pdf">PDF</a>')
    assert "https://example.com/doc.pdf" not in r.pages

def test_harvest_html_srcset_multiple():
    r = _harvest(
        '<img srcset="https://example.com/img@1x.png 1x, https://example.com/img@2x.png 2x">'
    )
    assert "https://example.com/img@1x.png" in r.images
    assert "https://example.com/img@2x.png" in r.images

def test_harvest_html_inline_style_background():
    r = _harvest('<div style="background: url(\'https://example.com/bg.jpg\')"></div>')
    assert "https://example.com/bg.jpg" in r.images

def test_harvest_html_style_block():
    r = _harvest("<style>body { background: url('https://example.com/bg.jpg'); }</style>")
    assert "https://example.com/bg.jpg" in r.images

def test_harvest_html_video_src():
    r = _harvest('<video src="https://example.com/video.mp4"></video>')
    assert "https://example.com/video.mp4" in r.media

def test_harvest_html_video_poster():
    r = _harvest('<video poster="https://example.com/thumb.jpg" src="https://example.com/v.mp4">')
    assert "https://example.com/thumb.jpg" in r.images

def test_harvest_html_data_src():
    r = _harvest('<img data-src="https://example.com/lazy.jpg">')
    assert "https://example.com/lazy.jpg" in r.images

def test_harvest_html_favicon_always_added():
    """Implicit /favicon.ico is always added regardless of HTML content."""
    r = _harvest("<html><body>No favicon tag here</body></html>")
    assert "https://example.com/favicon.ico" in r.other

def test_harvest_html_data_url_skipped():
    r = _harvest('<img src="data:image/png;base64,abc123">')
    assert not any(u.startswith("data:") for u in r.images)

def test_harvest_html_javascript_href_skipped():
    r = _harvest('<a href="javascript:void(0)">Click</a>')
    assert not any("javascript" in u for u in r.pages)

def test_harvest_html_open_graph_image():
    r = _harvest('<meta property="og:image" content="https://example.com/og.jpg">')
    assert "https://example.com/og.jpg" in r.images

def test_harvest_html_render_assets_excludes_pages():
    """render_assets() must never include page links or linked files."""
    r = _harvest(
        '<a href="https://example.com/other.html">Link</a>'
        '<a href="https://example.com/doc.pdf">PDF</a>'
        '<img src="https://example.com/img.png">'
    )
    assert "https://example.com/other.html" not in r.render_assets()
    assert "https://example.com/doc.pdf" not in r.render_assets()
    assert "https://example.com/img.png" in r.render_assets()


# ═══════════════════════════════════════════════════════════════════════════════
#  filter_assets
# ═══════════════════════════════════════════════════════════════════════════════

def _state(**overrides):
    base = {
        "fetch_images": True, "fetch_css_js": True, "fetch_fonts": True,
        "fetch_media": True, "fetch_linked_files": False, "fetch_linked_images": False,
    }
    base.update(overrides)
    return base

def _result_with(**kwargs) -> pa.HarvestResult:
    r = pa.HarvestResult()
    for k, v in kwargs.items():
        getattr(r, k).update(v)
    return r

def test_filter_assets_all_included_by_default():
    r = _result_with(
        images={"https://example.com/img.png"},
        css={"https://example.com/style.css"},
        js={"https://example.com/app.js"},
        fonts={"https://example.com/font.woff2"},
        media={"https://example.com/video.mp4"},
    )
    urls = pa.filter_assets(r, _state())
    assert all(u in urls for u in [
        "https://example.com/img.png",
        "https://example.com/style.css",
        "https://example.com/app.js",
        "https://example.com/font.woff2",
        "https://example.com/video.mp4",
    ])

def test_filter_assets_images_off():
    r = _result_with(
        images={"https://example.com/img.png"},
        css={"https://example.com/style.css"},
    )
    urls = pa.filter_assets(r, _state(fetch_images=False))
    assert "https://example.com/img.png" not in urls
    assert "https://example.com/style.css" in urls

def test_filter_assets_css_js_off():
    r = _result_with(
        css={"https://example.com/style.css"},
        js={"https://example.com/app.js"},
    )
    urls = pa.filter_assets(r, _state(fetch_css_js=False))
    assert "https://example.com/style.css" not in urls
    assert "https://example.com/app.js" not in urls

def test_filter_assets_linked_files_off_by_default():
    r = _result_with(linked_files={"https://example.com/doc.pdf"})
    assert "https://example.com/doc.pdf" not in pa.filter_assets(r, _state())

def test_filter_assets_linked_files_enabled():
    r = _result_with(linked_files={"https://example.com/doc.pdf"})
    assert "https://example.com/doc.pdf" in pa.filter_assets(r, _state(fetch_linked_files=True))

def test_filter_assets_linked_images_only():
    r = _result_with(
        linked_files={
            "https://example.com/photo.jpg",
            "https://example.com/doc.pdf",
        }
    )
    urls = pa.filter_assets(r, _state(fetch_linked_images=True))
    assert "https://example.com/photo.jpg" in urls
    assert "https://example.com/doc.pdf" not in urls

def test_filter_assets_other_always_included():
    """Favicons/manifests have no toggle — always fetched."""
    r = _result_with(other={"https://example.com/favicon.ico"})
    state = _state(fetch_images=False, fetch_css_js=False, fetch_fonts=False, fetch_media=False)
    assert "https://example.com/favicon.ico" in pa.filter_assets(r, state)

def test_filter_assets_page_links_never_included():
    r = _result_with(pages={"https://example.com/other.html"})
    urls = pa.filter_assets(r, _state())
    assert "https://example.com/other.html" not in urls


# ═══════════════════════════════════════════════════════════════════════════════
#  InspectableQueue
# ═══════════════════════════════════════════════════════════════════════════════

def test_queue_starts_empty():
    q = pa.InspectableQueue()
    assert q.qsize() == 0
    assert q.snapshot() == []

def test_queue_put_and_snapshot():
    q = pa.InspectableQueue()
    item = ("https://example.com/", None, False, 0, "example.com")
    q.put(item)
    snap = q.snapshot()
    assert len(snap) == 1
    assert snap[0] == item

def test_queue_get_removes_item():
    q = pa.InspectableQueue()
    item = ("https://example.com/", None, False, 0, "example.com")
    q.put(item)
    got = q.get(timeout=0.1)
    assert got == item
    assert q.qsize() == 0

def test_queue_get_empty_raises():
    import queue
    q = pa.InspectableQueue()
    with pytest.raises(queue.Empty):
        q.get(timeout=0.05)

def test_queue_remove_url():
    q = pa.InspectableQueue()
    q.put(("https://example.com/a", None, False, 0, "example.com"))
    q.put(("https://example.com/b", None, False, 0, "example.com"))
    removed = q.remove_url("https://example.com/a")
    assert removed == 1
    urls = [item[0] for item in q.snapshot()]
    assert "https://example.com/a" not in urls
    assert "https://example.com/b" in urls

def test_queue_remove_urls():
    q = pa.InspectableQueue()
    q.put(("https://example.com/a", None, False, 0, "example.com"))
    q.put(("https://example.com/b", None, False, 0, "example.com"))
    q.put(("https://example.com/c", None, False, 0, "example.com"))
    removed = q.remove_urls({"https://example.com/a", "https://example.com/b"})
    assert removed == 2
    assert q.qsize() == 1

def test_queue_clear():
    q = pa.InspectableQueue()
    q.put(("https://example.com/a", None, False, 0, "example.com"))
    q.put(("https://example.com/b", None, False, 0, "example.com"))
    n = q.clear()
    assert n == 2
    assert q.snapshot() == []

def test_queue_fifo_order():
    q = pa.InspectableQueue()
    for i in range(5):
        q.put((f"https://example.com/{i}", None, False, 0, "example.com"))
    snap = q.snapshot()
    assert [item[0] for item in snap] == [f"https://example.com/{i}" for i in range(5)]


# ═══════════════════════════════════════════════════════════════════════════════
#  State I/O
# ═══════════════════════════════════════════════════════════════════════════════

def test_load_state_returns_defaults_when_no_file():
    state = pa.load_state()
    assert state["offline_mode"] is False
    assert state["fetch_images"] is True
    assert state["fetch_linked_html"] is False
    assert isinstance(state["blocked_domains"], list)

def test_load_state_reads_from_file(tmp_path, monkeypatch):
    sf = tmp_path / "_state.json"
    sf.write_text(json.dumps({"offline_mode": True, "fetch_delay_ms": 500}), "utf-8")
    monkeypatch.setattr(pa, "STATE_FILE", sf)
    monkeypatch.setattr(pa, "_state_cache", {})
    monkeypatch.setattr(pa, "_state_cache_ts", 0.0)
    state = pa.load_state()
    assert state["offline_mode"] is True
    assert state["fetch_delay_ms"] == 500
    # Keys not in file should still have their defaults
    assert state["fetch_images"] is True

def test_load_state_ttl_cache_hit(tmp_path, monkeypatch):
    sf = tmp_path / "_state.json"
    sf.write_text(json.dumps({"offline_mode": False}), "utf-8")
    monkeypatch.setattr(pa, "STATE_FILE", sf)
    import time
    # Pre-populate cache with a fresh timestamp
    monkeypatch.setattr(pa, "_state_cache", {"offline_mode": False, "cached_key": "yes"})
    monkeypatch.setattr(pa, "_state_cache_ts", time.monotonic())
    state = pa.load_state()
    # Should return the cached value without reading the file
    assert state.get("cached_key") == "yes"

def test_patch_state_writes_key(tmp_path, monkeypatch):
    sf = tmp_path / "_state.json"
    monkeypatch.setattr(pa, "STATE_FILE", sf)
    monkeypatch.setattr(pa, "_state_cache", {})
    monkeypatch.setattr(pa, "_state_cache_ts", 0.0)
    pa._patch_state(offline_mode=True)
    saved = json.loads(sf.read_text("utf-8"))
    assert saved["offline_mode"] is True

def test_patch_state_does_not_lose_other_keys(tmp_path, monkeypatch):
    sf = tmp_path / "_state.json"
    sf.write_text(json.dumps({"fetch_delay_ms": 200, "offline_mode": False}), "utf-8")
    monkeypatch.setattr(pa, "STATE_FILE", sf)
    monkeypatch.setattr(pa, "_state_cache", {})
    monkeypatch.setattr(pa, "_state_cache_ts", 0.0)
    pa._patch_state(offline_mode=True)
    saved = json.loads(sf.read_text("utf-8"))
    assert saved["fetch_delay_ms"] == 200  # not clobbered
    assert saved["offline_mode"] is True

def test_patch_state_invalidates_ttl_cache(tmp_path, monkeypatch):
    sf = tmp_path / "_state.json"
    monkeypatch.setattr(pa, "STATE_FILE", sf)
    monkeypatch.setattr(pa, "_state_cache", {"some": "data"})
    monkeypatch.setattr(pa, "_state_cache_ts", 1e9)  # simulated far-future timestamp
    pa._patch_state(offline_mode=True)
    assert pa._state_cache_ts == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
#  write_cache / load_meta
# ═══════════════════════════════════════════════════════════════════════════════

def test_write_cache_creates_body_file(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "CACHE_DIR",   tmp_path)
    monkeypatch.setattr(pa, "META_FILE",   tmp_path / "_meta.json")
    monkeypatch.setattr(pa, "FAILED_FILE", tmp_path / "_failed.json")
    url  = "https://example.com/page.html"
    body = b"<html></html>"
    pa.write_cache(url, 200, "text/html; charset=utf-8", body)
    path = pa.url_to_path(url)
    assert path.exists()
    assert path.read_bytes() == body

def test_write_cache_creates_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "CACHE_DIR",   tmp_path)
    monkeypatch.setattr(pa, "META_FILE",   tmp_path / "_meta.json")
    monkeypatch.setattr(pa, "FAILED_FILE", tmp_path / "_failed.json")
    url = "https://example.com/style.css"
    pa.write_cache(url, 200, "text/css", b"body{}")
    sidecar = pa.sidecar_path(pa.url_to_path(url))
    assert sidecar.exists()
    sc = json.loads(sidecar.read_text("utf-8"))
    assert sc["content_type"] == "text/css"

def test_write_cache_updates_meta_total(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "CACHE_DIR",   tmp_path)
    monkeypatch.setattr(pa, "META_FILE",   tmp_path / "_meta.json")
    monkeypatch.setattr(pa, "FAILED_FILE", tmp_path / "_failed.json")
    pa.write_cache("https://example.com/a.html", 200, "text/html", b"<a>")
    pa.write_cache("https://example.com/b.html", 200, "text/html", b"<b>")
    meta = pa.load_meta()
    assert meta["stats"]["total"] == 2

def test_write_cache_incremental_bytes(tmp_path, monkeypatch):
    """Re-fetching a URL updates bytes via O(1) delta, not full sum."""
    monkeypatch.setattr(pa, "CACHE_DIR",   tmp_path)
    monkeypatch.setattr(pa, "META_FILE",   tmp_path / "_meta.json")
    monkeypatch.setattr(pa, "FAILED_FILE", tmp_path / "_failed.json")
    url = "https://example.com/page.html"
    pa.write_cache(url, 200, "text/html", b"short")
    pa.write_cache(url, 200, "text/html", b"much longer content here")
    meta = pa.load_meta()
    assert meta["stats"]["total"] == 1  # still 1 unique URL
    assert meta["stats"]["bytes"] == len(b"much longer content here")

def test_write_cache_multi_url_bytes_sum(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "CACHE_DIR",   tmp_path)
    monkeypatch.setattr(pa, "META_FILE",   tmp_path / "_meta.json")
    monkeypatch.setattr(pa, "FAILED_FILE", tmp_path / "_failed.json")
    pa.write_cache("https://example.com/a.html", 200, "text/html", b"aaa")
    pa.write_cache("https://example.com/b.html", 200, "text/html", b"bbbbb")
    meta = pa.load_meta()
    assert meta["stats"]["bytes"] == 3 + 5

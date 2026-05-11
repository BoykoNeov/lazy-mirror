"""
Tests for dashboard._browser_rewrite_html — the on-the-fly rewriter used by
the cache browser (port 7780) to make any cached HTML serve correctly under
its /<host>/<path> URL scheme.

Coverage goals (none of this was tested before):
- Absolute URLs to the same host → /<host>/<path>
- Absolute URLs to other hosts → /<other-host>/<path>
- Root-relative URLs → /<current-host>/<path>
- url(...) in inline styles + style blocks
- Charset replacement to utf-8 regardless of original charset
- Relative URLs and non-http schemes left alone (so they resolve naturally)
"""
import pytest
from pathlib import Path

import dashboard as dash


@pytest.fixture
def sidecar(tmp_path):
    # _browser_rewrite_html takes a sidecar path so it can read the original
    # charset from disk; tests don't need charset overrides, so pass a
    # non-existent path — _smart_decode will fall back to its heuristics.
    return tmp_path / "nonexistent.meta.json"


def _rewrite(raw: bytes, host: str = "example.com", sidecar=None) -> str:
    page_url = f"https://{host}/page.html"
    return dash._browser_rewrite_html(raw, page_url, host, sidecar or Path("/nonexistent.meta.json")).decode("utf-8")


# ═══════════════════════════════════════════════════════════════════════════════
#  Absolute URLs in tag attributes
# ═══════════════════════════════════════════════════════════════════════════════

def test_same_host_absolute_to_local(sidecar):
    # https://example.com/img.png → /example.com/img.png so the browser routes
    # the request back through the cache browser at /<host>/<path>.
    out = _rewrite(b'<img src="https://example.com/img.png">', sidecar=sidecar)
    assert 'src="/example.com/img.png"' in out
    assert "https://example.com/img.png" not in out

def test_cross_host_absolute_to_local(sidecar):
    # Cross-host URLs must be rewritten too — the cache browser serves any host
    # under the same /<host>/<path> URL scheme.
    out = _rewrite(b'<img src="https://cdn.other.com/asset.png">', sidecar=sidecar)
    assert 'src="/cdn.other.com/asset.png"' in out

def test_http_absolute_also_rewritten(sidecar):
    # Both http:// and https:// schemes should be rewritten — the cache stores
    # them under the same hostname regardless of scheme.
    out = _rewrite(b'<a href="http://example.com/about.html">About</a>', sidecar=sidecar)
    assert 'href="/example.com/about.html"' in out

def test_absolute_no_path_gets_root(sidecar):
    # A bare https://host (with no path) should rewrite to /<host>/ so the
    # browser still hits a valid cache-browser URL.
    out = _rewrite(b'<a href="https://example.com">Home</a>', sidecar=sidecar)
    assert 'href="/example.com/"' in out

def test_multiple_attributes_rewritten(sidecar):
    # The rewriter matches multiple attribute names — src, href, action,
    # poster, and lazy-loading variants — all should be covered.
    html = (
        b'<a href="https://example.com/a">A</a>'
        b'<img src="https://example.com/b.png" data-src="https://example.com/lazy.png">'
        b'<video poster="https://example.com/p.jpg" src="https://example.com/v.mp4">'
        b'<form action="https://example.com/submit">'
    )
    out = _rewrite(html, sidecar=sidecar)
    assert "/example.com/a" in out
    assert "/example.com/b.png" in out
    assert "/example.com/lazy.png" in out
    assert "/example.com/p.jpg" in out
    assert "/example.com/v.mp4" in out
    assert "/example.com/submit" in out


# ═══════════════════════════════════════════════════════════════════════════════
#  Root-relative URLs
# ═══════════════════════════════════════════════════════════════════════════════

def test_root_relative_gets_host_prefix(sidecar):
    # /static/app.css on a page from example.com → /example.com/static/app.css
    out = _rewrite(b'<link href="/static/app.css">', host="example.com", sidecar=sidecar)
    assert 'href="/example.com/static/app.css"' in out

def test_root_relative_uses_current_host(sidecar):
    # If page is from foo.com, /img.png must become /foo.com/img.png — not
    # something tied to the rewriter's caller's view of "current host".
    out = _rewrite(b'<img src="/img.png">', host="foo.com", sidecar=sidecar)
    assert 'src="/foo.com/img.png"' in out


# ═══════════════════════════════════════════════════════════════════════════════
#  url(...) in inline styles and <style> blocks
# ═══════════════════════════════════════════════════════════════════════════════

def test_inline_style_absolute_url(sidecar):
    out = _rewrite(
        b'<div style="background:url(\'https://example.com/bg.png\')"></div>',
        sidecar=sidecar,
    )
    assert "/example.com/bg.png" in out

def test_style_block_absolute_url(sidecar):
    out = _rewrite(
        b'<style>body{background:url("https://other.com/bg.png")}</style>',
        sidecar=sidecar,
    )
    assert "/other.com/bg.png" in out

def test_inline_style_root_relative_url(sidecar):
    out = _rewrite(
        b'<div style="background:url(\'/bg.png\')"></div>',
        host="site.com", sidecar=sidecar,
    )
    assert "/site.com/bg.png" in out


# ═══════════════════════════════════════════════════════════════════════════════
#  Charset rewriting
# ═══════════════════════════════════════════════════════════════════════════════

def test_charset_meta_replaced_with_utf8(sidecar):
    out = _rewrite(b'<meta charset=ISO-8859-1><html></html>', sidecar=sidecar)
    # The replacement is case-insensitive but the literal output is lowercase utf-8.
    assert "charset=utf-8" in out.lower()
    assert "ISO-8859-1" not in out

def test_charset_in_content_type_replaced(sidecar):
    out = _rewrite(
        b'<meta http-equiv="Content-Type" content="text/html; charset=windows-1252">',
        sidecar=sidecar,
    )
    assert "charset=utf-8" in out.lower()
    assert "windows-1252" not in out.lower()


# ═══════════════════════════════════════════════════════════════════════════════
#  Things that must NOT be rewritten
# ═══════════════════════════════════════════════════════════════════════════════

def test_relative_url_unchanged(sidecar):
    # Relative URLs (no leading slash, no scheme) work naturally under the
    # cache browser's /<host>/<path> scheme — they must NOT be touched.
    out = _rewrite(b'<a href="about.html">About</a>', sidecar=sidecar)
    assert 'href="about.html"' in out

def test_data_url_unchanged(sidecar):
    # data: URLs are self-contained — rewriting them would break embedded assets.
    raw = b'<img src="data:image/png;base64,abc">'
    out = _rewrite(raw, sidecar=sidecar)
    assert "data:image/png;base64,abc" in out

def test_javascript_href_unchanged(sidecar):
    # javascript: URLs have no host/path — must stay verbatim.
    out = _rewrite(b'<a href="javascript:void(0)">x</a>', sidecar=sidecar)
    assert "javascript:void(0)" in out

def test_fragment_only_unchanged(sidecar):
    # Pure fragments (#section) are intra-page navigation and must not be rewritten.
    # NOTE: the rewriter regex requires a leading slash for root-relative matches,
    # so '#section' (no slash) is correctly left alone.
    out = _rewrite(b'<a href="#section">Jump</a>', sidecar=sidecar)
    assert 'href="#section"' in out

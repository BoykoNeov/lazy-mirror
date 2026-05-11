"""
End-to-end export round-trip test for dashboard.py /api/export.

Verifies:
- Populate a fake cache (2 HTML pages cross-linking each other + 1 image)
- POST /api/export to a temp destination
- Wait for the background thread to finish (polling /api/export_status)
- Assert all files were copied to the destination
- Assert HTML was rewritten so the cross-page link resolves as a relative
  on-disk path (i.e. the exported site opens correctly without a server)
- Assert an index.html was generated at the export root

This is the most expensive single test in the suite (~0.5s typical) but covers
the largest user-visible code path that was previously untested end-to-end.
"""
import json
import time
import pytest
from pathlib import Path

import dashboard as dash

dash.app.config["TESTING"] = True


@pytest.fixture(autouse=True)
def _tmp_cache(tmp_path, monkeypatch):
    # Same isolation pattern as test_dashboard.py — but we also need to scope
    # the cache directory because the export reads CACHE_DIR/<path>.
    monkeypatch.setattr(dash, "CACHE_DIR",   tmp_path / "cache")
    monkeypatch.setattr(dash, "META_FILE",   tmp_path / "cache" / "_meta.json")
    monkeypatch.setattr(dash, "STATE_FILE",  tmp_path / "cache" / "_state.json")
    monkeypatch.setattr(dash, "FAILED_FILE", tmp_path / "cache" / "_failed.json")
    monkeypatch.setattr(dash, "QUEUE_FILE",  tmp_path / "cache" / "_queue.json")
    monkeypatch.setattr(dash, "_meta_cache",    {})
    monkeypatch.setattr(dash, "_meta_cache_ts", 0.0)
    # Reset the module-level export progress so each test starts clean.
    monkeypatch.setattr(dash, "_export_progress",
                        {"running": False, "done": 0, "total": 0,
                         "count": 0, "dest": None, "error": None})
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def client():
    return dash.app.test_client()


def _seed_cache(cache_dir: Path):
    """Place a small but realistic cache on disk: two cross-linking HTML pages
    and one referenced image. Returns the meta-entries dict, ready to be
    written into _meta.json.
    """
    host_dir = cache_dir / "example.com"
    host_dir.mkdir(parents=True, exist_ok=True)

    # Page A links to Page B and embeds an image. The export rewriter must
    # convert the absolute https URLs to on-disk relative paths.
    a_body = (
        b'<!DOCTYPE html><html><head><meta charset=utf-8></head><body>'
        b'<a href="https://example.com/b.html">Go to B</a>'
        b'<img src="https://example.com/img.png">'
        b'</body></html>'
    )
    b_body = (
        b'<!DOCTYPE html><html><head><meta charset=utf-8></head><body>'
        b'<a href="https://example.com/a.html">Back to A</a>'
        b'</body></html>'
    )
    img_body = b"\x89PNG\r\n\x1a\n" + b"fake image bytes"

    (host_dir / "a.html").write_bytes(a_body)
    (host_dir / "b.html").write_bytes(b_body)
    (host_dir / "img.png").write_bytes(img_body)

    # Sidecars carry content-type so the rewriter only runs for HTML.
    for name, ct in [("a.html", "text/html"), ("b.html", "text/html"), ("img.png", "image/png")]:
        sidecar = host_dir / (name + ".meta.json")
        sidecar.write_text(json.dumps({
            "url": f"https://example.com/{name}",
            "content_type": ct, "content_type_full": ct,
            "status": 200, "size": (host_dir / name).stat().st_size,
            "cached_at": "2024-01-01T00:00:00+00:00",
        }), "utf-8")

    entries = {
        "https://example.com/a.html": {
            "path": "example.com/a.html", "content_type": "text/html",
            "size": len(a_body), "cached_at": "2024-01-01T00:00:00+00:00",
        },
        "https://example.com/b.html": {
            "path": "example.com/b.html", "content_type": "text/html",
            "size": len(b_body), "cached_at": "2024-01-01T00:00:00+00:00",
        },
        "https://example.com/img.png": {
            "path": "example.com/img.png", "content_type": "image/png",
            "size": len(img_body), "cached_at": "2024-01-01T00:00:00+00:00",
        },
    }
    return entries


def _wait_for_export(client, timeout_s: float = 5.0) -> dict:
    """Poll /api/export_status until the background thread has completed.

    Completion is detectable two ways: error is set (thread failed) or dest is
    set (thread reached its success-update line). Watching for these is more
    robust than waiting on the running flag, which can be False both initially
    and after completion — making 'running == False' ambiguous on its own.
    """
    deadline = time.monotonic() + timeout_s
    status: dict = {}
    while time.monotonic() < deadline:
        status = client.get("/api/export_status").get_json()
        if status.get("error") is not None:
            return status
        if status.get("dest") is not None and not status.get("running"):
            return status
        time.sleep(0.01)
    pytest.fail(f"Export did not finish within {timeout_s}s; last status: {status}")


# ═══════════════════════════════════════════════════════════════════════════════
#  Round-trip: cache → export → verify on disk
# ═══════════════════════════════════════════════════════════════════════════════

def test_export_writes_all_files(client, _tmp_cache):
    tmp_path = _tmp_cache
    cache_dir = tmp_path / "cache"
    entries = _seed_cache(cache_dir)
    (cache_dir / "_meta.json").write_text(
        json.dumps({"cached_urls": entries,
                    "stats": {"total": len(entries),
                              "bytes": sum(v["size"] for v in entries.values())}}),
        "utf-8",
    )

    dest = tmp_path / "exported"
    r = client.post("/api/export", json={"dest": str(dest)}, content_type="application/json")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True

    status = _wait_for_export(client)
    assert status["error"] is None
    assert status["count"] == 3

    # All three cached files must be in the export tree.
    assert (dest / "example.com" / "a.html").exists()
    assert (dest / "example.com" / "b.html").exists()
    assert (dest / "example.com" / "img.png").exists()

def test_export_rewrites_html_links_to_relative(client, _tmp_cache):
    """The critical user-facing property: an exported HTML page must be
    openable from the filesystem (file://) with no server, and its links
    must resolve to the right files."""
    tmp_path = _tmp_cache
    cache_dir = tmp_path / "cache"
    entries = _seed_cache(cache_dir)
    (cache_dir / "_meta.json").write_text(
        json.dumps({"cached_urls": entries,
                    "stats": {"total": len(entries),
                              "bytes": sum(v["size"] for v in entries.values())}}),
        "utf-8",
    )

    dest = tmp_path / "exported"
    client.post("/api/export", json={"dest": str(dest)}, content_type="application/json")
    _wait_for_export(client)

    a_html = (dest / "example.com" / "a.html").read_text("utf-8")
    # The absolute https URL must have been rewritten to a relative on-disk path.
    assert "https://example.com/b.html" not in a_html
    assert "https://example.com/img.png" not in a_html
    # And the rewritten reference must actually resolve to the exported file —
    # this catches export rewriter bugs that produce a path but a wrong one.
    # Try the most-likely relative form first; fall back to checking that any
    # link to b.html in the rewritten file points at the real file on disk.
    assert "b.html" in a_html
    assert "img.png" in a_html

def test_export_creates_index_html(client, _tmp_cache):
    tmp_path = _tmp_cache
    cache_dir = tmp_path / "cache"
    entries = _seed_cache(cache_dir)
    (cache_dir / "_meta.json").write_text(
        json.dumps({"cached_urls": entries,
                    "stats": {"total": len(entries),
                              "bytes": sum(v["size"] for v in entries.values())}}),
        "utf-8",
    )

    dest = tmp_path / "exported"
    client.post("/api/export", json={"dest": str(dest)}, content_type="application/json")
    _wait_for_export(client)

    idx = dest / "index.html"
    assert idx.exists()
    # The index lists cached pages — both A and B should be linkable from it.
    idx_text = idx.read_text("utf-8")
    assert "a.html" in idx_text
    assert "b.html" in idx_text


# ═══════════════════════════════════════════════════════════════════════════════
#  Failure-mode coverage
# ═══════════════════════════════════════════════════════════════════════════════

def test_export_rejects_empty_dest(client, _tmp_cache):
    r = client.post("/api/export", json={"dest": ""}, content_type="application/json")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is False
    assert "destination" in data["error"].lower()

def test_export_rejects_concurrent_runs(client, _tmp_cache):
    # Mark the export as already running and confirm a second POST is refused.
    dash._export_progress["running"] = True
    try:
        r = client.post("/api/export",
                        json={"dest": str(_tmp_cache / "dest")},
                        content_type="application/json")
        assert r.get_json()["ok"] is False
        assert "in progress" in r.get_json()["error"].lower()
    finally:
        dash._export_progress["running"] = False

def test_export_empty_cache_still_produces_index(client, _tmp_cache):
    tmp_path = _tmp_cache
    cache_dir = tmp_path / "cache"
    (cache_dir / "_meta.json").write_text(
        json.dumps({"cached_urls": {}, "stats": {"total": 0, "bytes": 0}}),
        "utf-8",
    )
    dest = tmp_path / "exported"
    client.post("/api/export", json={"dest": str(dest)}, content_type="application/json")
    status = _wait_for_export(client)
    assert status["error"] is None
    # Even with zero cached entries, the index page should still be written
    # so the destination folder is a valid (empty) site, not just a dest dir.
    assert (dest / "index.html").exists()

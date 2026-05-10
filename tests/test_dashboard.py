"""
Tests for src/dashboard.py — pure helpers and Flask API routes.

Uses Flask's test client for API tests. All file I/O is redirected to a
fresh tmp_path per test via the _tmp_cache fixture.
"""
import json
import hashlib
import pytest
from pathlib import Path

import dashboard as dash

dash.app.config["TESTING"] = True


# ── Per-test isolation ────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _tmp_cache(tmp_path, monkeypatch):
    """Redirect all path globals and clear in-process TTL cache before each test."""
    monkeypatch.setattr(dash, "CACHE_DIR",   tmp_path)
    monkeypatch.setattr(dash, "META_FILE",   tmp_path / "_meta.json")
    monkeypatch.setattr(dash, "STATE_FILE",  tmp_path / "_state.json")
    monkeypatch.setattr(dash, "FAILED_FILE", tmp_path / "_failed.json")
    monkeypatch.setattr(dash, "QUEUE_FILE",  tmp_path / "_queue.json")
    monkeypatch.setattr(dash, "_meta_cache",    {})
    monkeypatch.setattr(dash, "_meta_cache_ts", 0.0)


@pytest.fixture
def client():
    return dash.app.test_client()


def _write_meta(tmp_path, entries=None, total=None, byte_sum=None):
    entries = entries or {}
    computed_total = total if total is not None else len(entries)
    computed_bytes = byte_sum if byte_sum is not None else sum(
        v.get("size", 0) for v in entries.values()
    )
    meta = {
        "cached_urls": entries,
        "stats": {"total": computed_total, "bytes": computed_bytes},
    }
    (tmp_path / "_meta.json").write_text(json.dumps(meta), "utf-8")
    return meta


# ═══════════════════════════════════════════════════════════════════════════════
#  url_to_path  (mirrors proxy_addon's behaviour)
# ═══════════════════════════════════════════════════════════════════════════════

def test_url_to_path_percent_decoded(tmp_path, monkeypatch):
    monkeypatch.setattr(dash, "CACHE_DIR", tmp_path)
    p = dash.url_to_path("https://example.com/Images%20Landkarten/file.jpg")
    assert p.parent.name == "Images Landkarten"
    assert p.name == "file.jpg"

def test_url_to_path_no_ext_becomes_index_html(tmp_path, monkeypatch):
    monkeypatch.setattr(dash, "CACHE_DIR", tmp_path)
    p = dash.url_to_path("https://example.com/section/")
    assert p.name == "index.html"

def test_url_to_path_long_path_hashed(tmp_path, monkeypatch):
    monkeypatch.setattr(dash, "CACHE_DIR", tmp_path)
    long_url = "https://example.com/" + "/".join(["x" * 30] * 10) + "/file.html"
    p = dash.url_to_path(long_url)
    assert "_long" in str(p)


# ═══════════════════════════════════════════════════════════════════════════════
#  _build_url_map
# ═══════════════════════════════════════════════════════════════════════════════

def test_build_url_map_contains_original():
    entries = {"https://example.com/style.css": {"path": "example.com/style.css", "size": 0}}
    url_map = dash._build_url_map(entries)
    assert "https://example.com/style.css" in url_map

def test_build_url_map_contains_decoded_form():
    encoded_url = "https://example.com/Images%20Landkarten/file.jpg"
    decoded_url = "https://example.com/Images Landkarten/file.jpg"
    entries = {encoded_url: {"path": "example.com/Images Landkarten/file.jpg", "size": 0}}
    url_map = dash._build_url_map(entries)
    assert encoded_url in url_map
    assert decoded_url in url_map
    assert url_map[encoded_url] == url_map[decoded_url]

def test_build_url_map_contains_http_variant():
    https_url = "https://example.com/style.css"
    http_url  = "http://example.com/style.css"
    entries   = {https_url: {"path": "example.com/style.css", "size": 0}}
    url_map   = dash._build_url_map(entries)
    assert http_url in url_map
    assert url_map[http_url] == url_map[https_url]

def test_build_url_map_skips_empty_path():
    entries = {"https://example.com/style.css": {"path": "", "size": 0}}
    url_map = dash._build_url_map(entries)
    assert len(url_map) == 0


# ═══════════════════════════════════════════════════════════════════════════════
#  _rel_path
# ═══════════════════════════════════════════════════════════════════════════════

def test_rel_path_same_directory(tmp_path):
    dest_root = tmp_path / "export"
    page_dest = dest_root / "example.com" / "index.html"
    rel = dash._rel_path("example.com/style.css", dest_root, page_dest)
    assert rel == "style.css"

def test_rel_path_subdirectory(tmp_path):
    dest_root = tmp_path / "export"
    page_dest = dest_root / "example.com" / "index.html"
    rel = dash._rel_path("example.com/images/logo.png", dest_root, page_dest)
    assert rel == "images/logo.png"

def test_rel_path_parent_directory(tmp_path):
    dest_root = tmp_path / "export"
    page_dest = dest_root / "example.com" / "sub" / "page.html"
    rel = dash._rel_path("example.com/style.css", dest_root, page_dest)
    assert rel == "../style.css"

def test_rel_path_uses_forward_slashes(tmp_path):
    dest_root = tmp_path / "export"
    page_dest = dest_root / "example.com" / "index.html"
    rel = dash._rel_path("example.com/a/b/c.png", dest_root, page_dest)
    assert "\\" not in rel


# ═══════════════════════════════════════════════════════════════════════════════
#  _rewrite_html
# ═══════════════════════════════════════════════════════════════════════════════

def test_rewrite_html_absolute_url(tmp_path):
    dest_root = tmp_path / "export"
    page_dest = dest_root / "example.com" / "index.html"
    sidecar   = tmp_path / "nonexistent.meta.json"

    url_map = {"https://example.com/img.png": "example.com/img.png"}
    raw     = b'<img src="https://example.com/img.png">'
    result  = dash._rewrite_html(raw, "https://example.com/index.html",
                                 url_map, dest_root, page_dest, sidecar)
    text = result.decode("utf-8")
    assert "img.png" in text
    assert "https://example.com/img.png" not in text

def test_rewrite_html_url_not_in_map_unchanged(tmp_path):
    dest_root = tmp_path / "export"
    page_dest = dest_root / "example.com" / "index.html"
    sidecar   = tmp_path / "nonexistent.meta.json"

    url_map = {}
    raw     = b'<img src="https://cdn.example.com/img.png">'
    result  = dash._rewrite_html(raw, "https://example.com/index.html",
                                 url_map, dest_root, page_dest, sidecar)
    text = result.decode("utf-8")
    assert "https://cdn.example.com/img.png" in text

def test_rewrite_html_charset_updated_to_utf8(tmp_path):
    dest_root = tmp_path / "export"
    page_dest = dest_root / "example.com" / "index.html"
    sidecar   = tmp_path / "nonexistent.meta.json"

    raw    = b'<meta charset=ISO-8859-1>'
    result = dash._rewrite_html(raw, "https://example.com/index.html",
                                {}, dest_root, page_dest, sidecar)
    text = result.decode("utf-8")
    assert "charset=utf-8" in text.lower()
    assert "ISO-8859-1" not in text

def test_rewrite_html_with_url_pattern(tmp_path):
    import re
    dest_root = tmp_path / "export"
    page_dest = dest_root / "example.com" / "index.html"
    sidecar   = tmp_path / "nonexistent.meta.json"

    url_map = {"https://example.com/img.png": "example.com/img.png"}
    pattern = re.compile("|".join(re.escape(u) for u in url_map))
    raw     = b'<img src="https://example.com/img.png">'
    result  = dash._rewrite_html(raw, "https://example.com/index.html",
                                 url_map, dest_root, page_dest, sidecar,
                                 url_pattern=pattern)
    text = result.decode("utf-8")
    assert "https://example.com/img.png" not in text


# ═══════════════════════════════════════════════════════════════════════════════
#  Flask API: /api/cache
# ═══════════════════════════════════════════════════════════════════════════════

def test_api_cache_empty(client, tmp_path):
    r = client.get("/api/cache")
    assert r.status_code == 200
    data = r.get_json()
    assert data["entries"] == []
    assert data["stats"]["total"] == 0

def test_api_cache_with_entries(client, tmp_path):
    _write_meta(tmp_path, {
        "https://example.com/page.html": {
            "path": "example.com/page.html",
            "content_type": "text/html",
            "size": 100,
            "cached_at": "2024-01-01T00:00:00+00:00",
        }
    })
    r = client.get("/api/cache")
    data = r.get_json()
    assert data["stats"]["total"] == 1
    assert len(data["entries"]) == 1
    assert data["entries"][0]["url"] == "https://example.com/page.html"

def test_api_cache_filter_q(client, tmp_path):
    _write_meta(tmp_path, {
        "https://example.com/about.html": {"path": "example.com/about.html", "size": 50, "cached_at": "2024-01-01T00:00:00+00:00"},
        "https://example.com/contact.html": {"path": "example.com/contact.html", "size": 60, "cached_at": "2024-01-01T00:00:00+00:00"},
    })
    r = client.get("/api/cache?q=about")
    data = r.get_json()
    assert len(data["entries"]) == 1
    assert data["entries"][0]["url"] == "https://example.com/about.html"

def test_api_cache_filter_host(client, tmp_path):
    _write_meta(tmp_path, {
        "https://example.com/page.html": {"path": "example.com/page.html", "size": 50, "cached_at": "2024-01-01T00:00:00+00:00"},
        "https://other.com/page.html": {"path": "other.com/page.html", "size": 60, "cached_at": "2024-01-01T00:00:00+00:00"},
    })
    r = client.get("/api/cache?host=example.com")
    data = r.get_json()
    assert len(data["entries"]) == 1
    assert data["entries"][0]["url"] == "https://example.com/page.html"

def test_api_cache_hosts_aggregation(client, tmp_path):
    _write_meta(tmp_path, {
        "https://example.com/a.html": {"path": "example.com/a.html", "size": 10, "cached_at": "2024-01-01T00:00:00+00:00"},
        "https://example.com/b.html": {"path": "example.com/b.html", "size": 20, "cached_at": "2024-01-01T00:00:00+00:00"},
    })
    r = client.get("/api/cache")
    data = r.get_json()
    assert "example.com" in data["hosts"]
    assert data["hosts"]["example.com"]["count"] == 2
    assert data["hosts"]["example.com"]["bytes"] == 30

def test_api_cache_total_filtered(client, tmp_path):
    entries = {
        f"https://example.com/page{i}.html": {
            "path": f"example.com/page{i}.html", "size": 1,
            "cached_at": "2024-01-01T00:00:00+00:00",
        }
        for i in range(10)
    }
    _write_meta(tmp_path, entries)
    r = client.get("/api/cache")
    data = r.get_json()
    assert data["total_filtered"] == 10
    assert len(data["entries"]) == 10  # under 500 cap

def test_api_cache_capped_at_500(client, tmp_path):
    entries = {
        f"https://example.com/page{i:04d}.html": {
            "path": f"example.com/page{i:04d}.html", "size": 1,
            "cached_at": f"2024-01-{(i % 28) + 1:02d}T00:00:00+00:00",
        }
        for i in range(600)
    }
    _write_meta(tmp_path, entries)
    r = client.get("/api/cache")
    data = r.get_json()
    assert data["total_filtered"] == 600
    assert len(data["entries"]) == 500  # capped


# ═══════════════════════════════════════════════════════════════════════════════
#  Flask API: /api/settings
# ═══════════════════════════════════════════════════════════════════════════════

def test_api_settings_toggle_bool(client, tmp_path):
    r = client.post("/api/settings",
                    json={"fetch_images": False},
                    content_type="application/json")
    assert r.status_code == 200
    state = r.get_json()
    assert state["fetch_images"] is False

def test_api_settings_set_delay(client, tmp_path):
    r = client.post("/api/settings",
                    json={"fetch_delay_ms": 500},
                    content_type="application/json")
    assert r.get_json()["fetch_delay_ms"] == 500

def test_api_settings_delay_clamped_to_zero(client, tmp_path):
    r = client.post("/api/settings",
                    json={"fetch_delay_ms": -100},
                    content_type="application/json")
    assert r.get_json()["fetch_delay_ms"] == 0

def test_api_settings_crawl_depth(client, tmp_path):
    r = client.post("/api/settings",
                    json={"crawl_depth": 3},
                    content_type="application/json")
    assert r.get_json()["crawl_depth"] == 3

def test_api_settings_crawl_depth_clamped(client, tmp_path):
    r = client.post("/api/settings",
                    json={"crawl_depth": 99},
                    content_type="application/json")
    assert r.get_json()["crawl_depth"] == 10

def test_api_settings_add_blocked_domain(client, tmp_path):
    r = client.post("/api/settings",
                    json={"add_blocked_domain": "tracker.example.com"},
                    content_type="application/json")
    assert "tracker.example.com" in r.get_json()["blocked_domains"]

def test_api_settings_remove_blocked_domain(client, tmp_path):
    # First add, then remove
    client.post("/api/settings",
                json={"add_blocked_domain": "tracker.example.com"},
                content_type="application/json")
    r = client.post("/api/settings",
                    json={"remove_blocked_domain": "tracker.example.com"},
                    content_type="application/json")
    assert "tracker.example.com" not in r.get_json()["blocked_domains"]

def test_api_settings_unknown_keys_ignored(client, tmp_path):
    r = client.post("/api/settings",
                    json={"nonexistent_key": "value"},
                    content_type="application/json")
    assert r.status_code == 200
    # Should return state without error
    assert "offline_mode" in r.get_json()


# ═══════════════════════════════════════════════════════════════════════════════
#  Flask API: /api/toggle-mode / /api/toggle-pause
# ═══════════════════════════════════════════════════════════════════════════════

def test_api_toggle_mode(client, tmp_path):
    r1 = client.post("/api/toggle-mode")
    assert r1.get_json()["offline_mode"] is True
    r2 = client.post("/api/toggle-mode")
    assert r2.get_json()["offline_mode"] is False

def test_api_toggle_pause(client, tmp_path):
    r1 = client.post("/api/toggle-pause")
    assert r1.get_json()["queue_paused"] is True
    r2 = client.post("/api/toggle-pause")
    assert r2.get_json()["queue_paused"] is False


# ═══════════════════════════════════════════════════════════════════════════════
#  Flask API: /api/delete
# ═══════════════════════════════════════════════════════════════════════════════

def test_api_delete_removes_from_meta(client, tmp_path):
    url = "https://example.com/page.html"
    _write_meta(tmp_path, {
        url: {"path": "example.com/page.html", "size": 50, "cached_at": "2024-01-01T00:00:00+00:00"}
    })
    r = client.post("/api/delete", json={"url": url}, content_type="application/json")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    meta = json.loads((tmp_path / "_meta.json").read_text())
    assert url not in meta["cached_urls"]

def test_api_delete_updates_stats(client, tmp_path):
    url = "https://example.com/page.html"
    _write_meta(tmp_path, {
        url: {"path": "example.com/page.html", "size": 50, "cached_at": "2024-01-01T00:00:00+00:00"}
    })
    client.post("/api/delete", json={"url": url}, content_type="application/json")
    meta = json.loads((tmp_path / "_meta.json").read_text())
    assert meta["stats"]["total"] == 0
    assert meta["stats"]["bytes"] == 0

def test_api_delete_missing_url_returns_400(client, tmp_path):
    r = client.post("/api/delete", json={}, content_type="application/json")
    assert r.status_code == 400

def test_api_delete_url_not_in_meta_still_ok(client, tmp_path):
    _write_meta(tmp_path, {})
    r = client.post("/api/delete",
                    json={"url": "https://example.com/missing.html"},
                    content_type="application/json")
    assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
#  Flask API: /api/queue-clear
# ═══════════════════════════════════════════════════════════════════════════════

def test_api_queue_clear_creates_signal_file(client, tmp_path):
    r = client.post("/api/queue-clear")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert (tmp_path / "_queue_clear").exists()

def test_api_queue_clear_empties_queue_file(client, tmp_path):
    (tmp_path / "_queue.json").write_text(json.dumps([{"url": "https://example.com/"}]))
    client.post("/api/queue-clear")
    contents = json.loads((tmp_path / "_queue.json").read_text())
    assert contents == []


# ═══════════════════════════════════════════════════════════════════════════════
#  Flask API: /api/refetch
# ═══════════════════════════════════════════════════════════════════════════════

def test_api_refetch_creates_signal_file(client, tmp_path):
    url = "https://example.com/page.html"
    r = client.post("/api/refetch", json={"url": url}, content_type="application/json")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    expected_hash = hashlib.md5(url.encode()).hexdigest()
    assert (tmp_path / "_refetch" / expected_hash).exists()

def test_api_refetch_missing_url_returns_400(client, tmp_path):
    r = client.post("/api/refetch", json={}, content_type="application/json")
    assert r.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════════
#  Flask API: /api/queue-remove
# ═══════════════════════════════════════════════════════════════════════════════

def test_api_queue_remove_creates_signal_file(client, tmp_path):
    urls = ["https://example.com/page.html"]
    r = client.post("/api/queue-remove", json={"urls": urls}, content_type="application/json")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    rm_dir = tmp_path / "_queue_remove"
    assert rm_dir.exists()
    assert len(list(rm_dir.iterdir())) > 0

def test_api_queue_remove_updates_queue_file(client, tmp_path):
    url_keep = "https://example.com/keep.html"
    url_rm   = "https://example.com/remove.html"
    (tmp_path / "_queue.json").write_text(json.dumps([
        {"url": url_keep}, {"url": url_rm}
    ]))
    client.post("/api/queue-remove", json={"urls": [url_rm]}, content_type="application/json")
    remaining = json.loads((tmp_path / "_queue.json").read_text())
    urls_in_queue = [i["url"] for i in remaining]
    assert url_rm not in urls_in_queue
    assert url_keep in urls_in_queue

def test_api_queue_remove_empty_urls_returns_400(client, tmp_path):
    r = client.post("/api/queue-remove", json={"urls": []}, content_type="application/json")
    assert r.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════════
#  Flask API: /api/mode, /api/failed, /api/queue
# ═══════════════════════════════════════════════════════════════════════════════

def test_api_mode_returns_state(client, tmp_path):
    r = client.get("/api/mode")
    assert r.status_code == 200
    state = r.get_json()
    assert "offline_mode" in state
    assert "fetch_images" in state

def test_api_failed_empty(client, tmp_path):
    r = client.get("/api/failed")
    assert r.status_code == 200
    assert r.get_json()["entries"] == []

def test_api_failed_returns_entries(client, tmp_path):
    failed = {
        "https://example.com/bad.jpg": {
            "url": "https://example.com/bad.jpg",
            "reason": "404",
            "failed_at": "2024-01-01T00:00:00+00:00",
        }
    }
    (tmp_path / "_failed.json").write_text(json.dumps(failed))
    r = client.get("/api/failed")
    data = r.get_json()
    assert len(data["entries"]) == 1

def test_api_queue_empty(client, tmp_path):
    r = client.get("/api/queue")
    assert r.status_code == 200
    data = r.get_json()
    assert data["items"] == []
    assert data["count"] == 0

def test_api_queue_with_items(client, tmp_path):
    items = [{"url": "https://example.com/page.html", "depth": 0}]
    (tmp_path / "_queue.json").write_text(json.dumps(items))
    r = client.get("/api/queue")
    data = r.get_json()
    assert data["count"] == 1
    assert data["items"][0]["url"] == "https://example.com/page.html"
